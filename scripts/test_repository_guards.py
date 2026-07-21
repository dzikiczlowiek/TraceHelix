#!/usr/bin/env python3
"""Regression tests for repository-level reproducibility and supply-chain guards."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import time
import tomllib
import unittest
import xml.etree.ElementTree as ET

import yaml


ROOT = Path(__file__).resolve().parents[1]
FINGERPRINT = ROOT / "scripts" / "source_fingerprint.py"
PIN_VERIFIER = ROOT / "scripts" / "verify_container_pins.py"
DOCKERFILE = ROOT / "Dockerfile"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
NGINX_CONF = ROOT / "deploy" / "nginx.conf"
COMPOSE_LIFECYCLE = ROOT / "scripts" / "verify-compose-lifecycle.sh"
VERSION = ROOT / "VERSION"
DIRECTORY_BUILD_PROPS = ROOT / "Directory.Build.props"
WEB_PACKAGE_JSON = ROOT / "web" / "package.json"
TRAINING_PYPROJECT = ROOT / "training" / "pyproject.toml"
SECURITY_MD = ROOT / "SECURITY.md"
CONTRIBUTING_MD = ROOT / "CONTRIBUTING.md"
CHANGELOG_MD = ROOT / "CHANGELOG.md"
CODEOWNERS = ROOT / ".github" / "CODEOWNERS"
PR_TEMPLATE = ROOT / ".github" / "pull_request_template.md"
RELEASE_READINESS = ROOT / "docs" / "release-readiness-v0.1.0.md"
VERIFICATION_MD = ROOT / "docs" / "verification.md"
RELEASE_POLICY_MD = ROOT / "docs" / "release-policy.md"
README = ROOT / "README.md"
ARCHITECTURE = ROOT / "docs" / "architecture.md"
BROWSER_VERIFIER = ROOT / "scripts" / "verify-browser.sh"
PLAYWRIGHT_CONFIG = ROOT / "web" / "playwright.config.ts"
E2E_SPEC = ROOT / "web" / "e2e" / "release.spec.ts"
BUNDLE_BUILDER = ROOT / "scripts" / "build_release_bundle.py"
BUNDLE_BUILDER_TESTS = ROOT / "scripts" / "test_build_release_bundle.py"
BUNDLE_VERIFIER = ROOT / "scripts" / "verify_release_bundle.py"
BUNDLE_VERIFIER_TESTS = ROOT / "scripts" / "test_verify_release_bundle.py"
BUNDLE_ACCEPTANCE = ROOT / "scripts" / "verify-release-bundle.sh"

# Canonical SemVer 2.0.0 (optional but valid prerelease/build metadata). The
# cross-source equality check, not this pattern alone, pins a release value.
SEMVER_PATTERN = re.compile(
    r"^(0|[1-9][0-9]*)[.](0|[1-9][0-9]*)[.](0|[1-9][0-9]*)"
    r"(?:-(?:0|[1-9][0-9]*|[0-9]*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:[.](?:0|[1-9][0-9]*|[0-9]*[a-zA-Z-][0-9a-zA-Z-]*))*)?"
    r"(?:[+][0-9a-zA-Z-]+(?:[.][0-9a-zA-Z-]+)*)?$"
)


def _require_canonical_semver(value: object, source: str) -> str:
    """Fail closed unless ``value`` is a canonical SemVer 2.0.0 string."""
    if not isinstance(value, str) or SEMVER_PATTERN.match(value) is None:
        raise AssertionError(f"{source}: not canonical SemVer: {value!r}")
    return value


def parse_version_file(text: str) -> str:
    """Return the canonical version from a VERSION file whose exact bytes are
    the canonical SemVer string followed by a single trailing LF newline."""
    newline = chr(10)
    if not text or not text.endswith(newline) or text.count(newline) != 1:
        raise AssertionError(
            "VERSION must be the canonical SemVer followed by exactly one LF"
        )
    return _require_canonical_semver(text[:-1], "VERSION")


def parse_directory_build_props_version_prefix(text: str) -> str:
    """Return the single VersionPrefix value from Directory.Build.props XML."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise AssertionError(
            f"Directory.Build.props is not well-formed XML: {exc}"
        ) from exc
    matches = root.findall(".//VersionPrefix")
    if len(matches) != 1:
        raise AssertionError(
            "Directory.Build.props must define exactly one VersionPrefix; "
            f"found {len(matches)}"
        )
    value = matches[0].text
    if value is None:
        raise AssertionError(
            "Directory.Build.props VersionPrefix must have a text value"
        )
    return _require_canonical_semver(
        value.strip(), "Directory.Build.props VersionPrefix"
    )


def parse_web_package_version(text: str) -> str:
    """Return the version field from web/package.json."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"web/package.json is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise AssertionError("web/package.json root must be a JSON object")
    return _require_canonical_semver(data.get("version"), "web/package.json version")


def parse_training_pyproject_version(text: str) -> str:
    """Return project.version from training/pyproject.toml."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise AssertionError(
            f"training/pyproject.toml is not valid TOML: {exc}"
        ) from exc
    project = data.get("project")
    if not isinstance(project, dict):
        raise AssertionError("training/pyproject.toml must define a [project] table")
    return _require_canonical_semver(
        project.get("version"), "training/pyproject.toml project.version"
    )


def assert_versions_agree(versions: dict[str, str]) -> None:
    """Fail closed unless every named source reports the same canonical version."""
    distinct = set(versions.values())
    if len(distinct) != 1:
        raise AssertionError(f"version sources diverge: {versions}")


def require_anchors(text: str, anchors: tuple[str, ...], source: str) -> None:
    """Fail closed unless every exact semantic anchor is present in ``text``.

    Anchors are short, meaningful phrases (not whole prose blocks) so renaming
    or deleting a required claim fails the guard without over-pinning wording.
    """
    missing = [anchor for anchor in anchors if anchor not in text]
    if missing:
        raise AssertionError(f"{source} missing required anchors: {missing}")


def forbid_phrases(text: str, phrases: tuple[str, ...], source: str) -> None:
    """Fail closed if any forbidden unstable phrase is present in ``text``.

    Used to reject branch/worktree location claims in shipped docs that would
    go stale once the pull request merges to ``main``. A guard over absence is
    exact, so re-introducing a transient phrase fails the suite immediately.
    """
    found = [phrase for phrase in phrases if phrase in text]
    if found:
        raise AssertionError(f"{source} contains unstable phrases: {found}")


def parse_release_create_assets(publish_slice: str) -> list[str]:
    """Return the ordered asset arguments of the publish ``gh release create``.

    Only the publish job slice is scanned and ``--`` option flags (with their
    values) are excluded, so the result is exactly the uploaded release assets.
    Dropping one asset from this command cannot hide behind the same name still
    appearing in the assemble/download steps.
    """
    marker = 'gh release create "$TAG"'
    if marker not in publish_slice:
        raise AssertionError("publish job is missing gh release create")
    body = publish_slice.split(marker, 1)[1].split("\n      - name:", 1)[0]
    assets: list[str] = []
    for raw in body.splitlines():
        token = raw.strip()
        if not token or token.startswith("--"):
            continue
        token = token.removesuffix("\\").strip().strip('"')
        if token:
            assets.append(token)
    return assets


def parse_provenance_subject_path(publish_slice: str) -> str:
    """Return the ``subject-path`` of the publish attest-build-provenance step.

    Scoped to the provenance step's ``with:`` block so swapping the subject for
    a non-archive asset (whose path appears elsewhere in publish) is detected.
    """
    marker = "actions/attest-build-provenance@"
    if marker not in publish_slice:
        raise AssertionError("publish job is missing attest-build-provenance")
    block = publish_slice.split(marker, 1)[1].split("\n      - name:", 1)[0]
    for raw in block.splitlines():
        stripped = raw.strip()
        if stripped.startswith("subject-path:"):
            return stripped.split("subject-path:", 1)[1].strip()
    raise AssertionError("attest-build-provenance step has no subject-path")


def parse_release_view_guard(publish_slice: str) -> tuple[str, list[str]]:
    """Return ``(condition, body)`` of the publish ``gh release view`` guard.

    The guard must be a non-negated ``if gh release view "$TAG" ...; then``
    whose branch fails closed with ``exit 1``. Inverting the condition leaves
    the anchor string in place but flips never-overwrite into never-create, so
    callers assert both the exact condition and the in-branch ``exit 1``.
    """
    lines = publish_slice.splitlines()
    start = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("if ") and 'gh release view "$TAG"' in stripped:
            start = index
            break
    if start is None:
        raise AssertionError("publish job is missing the gh release view guard")
    condition = lines[start].strip()
    body: list[str] = []
    for line in lines[start + 1:]:
        stripped = line.strip()
        if stripped == "fi":
            break
        body.append(stripped)
    else:
        raise AssertionError("gh release view guard is missing its closing fi")
    return condition, body


def parse_assemble_expected_set(assemble_slice: str) -> list[str]:
    """Return the literal artifact-name set asserted by the assemble job.

    Only the ``expected=$(printf ...)`` literal in assemble-evidence is parsed.
    Deleting a name there (while its download/upload steps still mention it)
    cannot be hidden behind global anchor presence.
    """
    marker = "expected=$(printf"
    if marker not in assemble_slice:
        raise AssertionError("assemble-evidence is missing the expected-set literal")
    body = assemble_slice.split(marker, 1)[1]
    end = body.find(")")
    if end == -1:
        raise AssertionError("expected-set literal is missing its closing paren")
    names: list[str] = []
    for token in body[:end].split("|", 1)[0].split():
        cleaned = token.removesuffix("\\").strip().strip('"')
        if cleaned and not cleaned.startswith("'"):
            names.append(cleaned)
    return names


def parse_copy_exclusive_flags(shell_text: str) -> str:
    """Return the ``flags = ...`` line from ``copy_verified_file_exclusively``.

    The exclusive-create flags must include ``os.O_EXCL``; the helper is named
    from a second call site, so the check is scoped to the function body.
    """
    marker = "copy_verified_file_exclusively() {"
    if marker not in shell_text:
        raise AssertionError(
            "verify-release-bundle.sh is missing copy_verified_file_exclusively"
        )
    body = shell_text.split(marker, 1)[1]
    end = body.find("\n}\n")
    if end == -1:
        raise AssertionError("copy_verified_file_exclusively is missing its closing brace")
    for raw in body[:end].splitlines():
        stripped = raw.strip()
        if stripped.startswith("flags = "):
            return stripped
    raise AssertionError("copy_verified_file_exclusively is missing its flags assignment")


def export_verified_output_body(shell_text: str) -> str:
    """Return the active (non-comment) body of ``export_verified_output()``.

    The destination-empty ``die`` message is shared with the earlier ``-d``
    test, so the probe is scoped here and comments are stripped: deleting or
    commenting out the ``find ... -mindepth 1`` guard fails closed even though
    the message string still appears elsewhere in the function.
    """
    marker = "export_verified_output() {"
    if marker not in shell_text:
        raise AssertionError(
            "verify-release-bundle.sh is missing export_verified_output"
        )
    body = shell_text.split(marker, 1)[1]
    end = body.find("\n}\n")
    if end == -1:
        raise AssertionError("export_verified_output is missing its closing brace")
    return "\n".join(
        line for line in body[:end].splitlines() if not line.strip().startswith("#")
    )


class SourceFingerprintTests(unittest.TestCase):
    def fingerprint(
        self, script: Path, root: Path, extra_env: dict[str, str] | None = None
    ) -> str:
        env = os.environ.copy()
        env.update(extra_env or {})
        return subprocess.check_output(
            ["python", str(script)], cwd=root, env=env, text=True
        ).strip()

    def test_ignores_ambient_git_config_and_routing_variables(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tracehelix-fingerprint-") as temp:
            root = Path(temp) / "repo"
            scripts = root / "scripts"
            scripts.mkdir(parents=True)
            script = scripts / FINGERPRINT.name
            shutil.copy2(FINGERPRINT, script)
            (root / "tracked.txt").write_text("tracked\n", encoding="utf-8")
            subprocess.run(["git", "init", "--quiet", str(root)], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "-c",
                    "user.name=TraceHelix Test",
                    "-c",
                    "user.email=test@tracehelix.invalid",
                    "add",
                    ".",
                ],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "-c",
                    "user.name=TraceHelix Test",
                    "-c",
                    "user.email=test@tracehelix.invalid",
                    "commit",
                    "--quiet",
                    "-m",
                    "fixture",
                ],
                check=True,
            )
            sentinel = root / "untracked-sentinel.txt"
            sentinel.write_text("must be fingerprinted\n", encoding="utf-8")
            baseline = self.fingerprint(script, root)

            ignore = Path(temp) / "global-ignore"
            ignore.write_text(f"{sentinel.name}\n", encoding="utf-8")
            poisoned = self.fingerprint(
                script,
                root,
                {
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "core.excludesFile",
                    "GIT_CONFIG_VALUE_0": str(ignore),
                    "GIT_COMMON_DIR": str(Path(temp) / "foreign-common-dir"),
                },
            )
            self.assertEqual(baseline, poisoned)

            (root / ".git" / "info" / "exclude").write_text(
                f"{sentinel.name}\n", encoding="utf-8"
            )
            self.assertEqual(baseline, self.fingerprint(script, root))

    def test_is_invariant_when_the_same_worktree_is_staged(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tracehelix-fingerprint-stage-") as temp:
            root = Path(temp) / "repo"
            scripts = root / "scripts"
            scripts.mkdir(parents=True)
            script = scripts / FINGERPRINT.name
            shutil.copy2(FINGERPRINT, script)
            tracked = root / "tracked.txt"
            tracked.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "init", "--quiet", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "add", "."], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "-c",
                    "user.name=TraceHelix Test",
                    "-c",
                    "user.email=test@tracehelix.invalid",
                    "commit",
                    "--quiet",
                    "-m",
                    "fixture",
                ],
                check=True,
            )
            tracked.write_text("after\n", encoding="utf-8")
            (root / "untracked.txt").write_text("new\n", encoding="utf-8")
            before_staging = self.fingerprint(script, root)
            subprocess.run(["git", "-C", str(root), "add", "--all"], check=True)
            self.assertEqual(before_staging, self.fingerprint(script, root))


class RepositoryPrivacyTests(unittest.TestCase):
    def test_private_imports_are_ignored_but_placeholder_is_tracked(self) -> None:
        # Run against a disposable repository so the guard also works from a
        # verified source bundle, which intentionally contains no .git directory.
        with tempfile.TemporaryDirectory(prefix="tracehelix-privacy-") as temp:
            root = Path(temp)
            subprocess.run(["git", "init", "--quiet", str(root)], check=True)
            shutil.copy2(ROOT / ".gitignore", root / ".gitignore")
            (root / "imports").mkdir()
            (root / "imports" / ".gitkeep").touch()
            ignored = subprocess.run(
                ["git", "check-ignore", "--quiet", "--no-index", "imports/private-trace.jsonl"],
                cwd=root,
                check=False,
            )
            placeholder = subprocess.run(
                ["git", "check-ignore", "--quiet", "--no-index", "imports/.gitkeep"],
                cwd=root,
                check=False,
            )
        self.assertEqual(0, ignored.returncode)
        self.assertNotEqual(0, placeholder.returncode)


class ContainerPinVerifierTests(unittest.TestCase):
    def run_verifier(
        self,
        dockerfile: str,
        workflow: str | None = None,
        release_workflow: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="tracehelix-container-pins-") as temp:
            root = Path(temp)
            (root / "scripts").mkdir()
            (root / ".github" / "workflows").mkdir(parents=True)
            shutil.copy2(PIN_VERIFIER, root / "scripts" / PIN_VERIFIER.name)
            (root / "Dockerfile").write_text(dockerfile, encoding="utf-8")
            (root / ".github" / "workflows" / "ci.yml").write_text(
                workflow if workflow is not None else WORKFLOW.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            # The verifier now covers release.yml with its own exact allowlist,
            # so every fixture materializes a release workflow too. Tests that
            # do not care about release.yml get the real, valid file; tests that
            # mutate it pass an explicit override.
            (root / ".github" / "workflows" / "release.yml").write_text(
                release_workflow
                if release_workflow is not None
                else RELEASE_WORKFLOW.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            return subprocess.run(
                ["python", str(root / "scripts" / PIN_VERIFIER.name)],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )

    def test_current_dockerfile_and_frontend_match_exact_allowlist(self) -> None:
        dockerfile = DOCKERFILE.read_text(encoding="utf-8")
        first_line = dockerfile.splitlines()[0]
        self.assertRegex(first_line, r"^# syntax=\S+@sha256:[0-9a-f]{64}$")
        result = self.run_verifier(dockerfile)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_rejects_a_well_formed_but_unapproved_digest(self) -> None:
        dockerfile = DOCKERFILE.read_text(encoding="utf-8")
        mutated, count = re.subn(
            r"(@sha256:)[0-9a-f]{64}",
            r"\g<1>" + "0" * 64,
            dockerfile,
            count=1,
        )
        self.assertEqual(1, count)
        result = self.run_verifier(mutated)
        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)

    def test_rejects_from_platform_bypass(self) -> None:
        dockerfile = DOCKERFILE.read_text(encoding="utf-8")
        mutated = dockerfile + "\nFROM --platform=linux/amd64 ubuntu:latest AS bypass\n"
        result = self.run_verifier(mutated)
        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)

    def test_rejects_mutable_syntax_frontend(self) -> None:
        dockerfile = DOCKERFILE.read_text(encoding="utf-8")
        lines = dockerfile.splitlines()
        lines[0] = "# syntax=docker/dockerfile:1.7"
        result = self.run_verifier("\n".join(lines) + "\n")
        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)

    def test_rejects_unapproved_external_copy_from(self) -> None:
        dockerfile = DOCKERFILE.read_text(encoding="utf-8")
        mutated = dockerfile + (
            "\nCOPY --from=alpine:latest /etc/alpine-release "
            "/tmp/unpinned-external-copy\n"
        )
        result = self.run_verifier(mutated)
        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)

    def test_rejects_stage_alias_with_wrong_case(self) -> None:
        dockerfile = DOCKERFILE.read_text(encoding="utf-8")
        mutated, count = re.subn(
            r"COPY --from=dotnet-build ",
            "COPY --from=Dotnet-Build ",
            dockerfile,
            count=1,
        )
        self.assertEqual(1, count)
        result = self.run_verifier(mutated)
        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)

    def test_rejects_unapproved_external_run_mount_from(self) -> None:
        dockerfile = DOCKERFILE.read_text(encoding="utf-8")
        mutated = dockerfile + (
            "\nRUN --mount=type=bind,from=alpine:latest,source=/,target=/probe true\n"
        )
        result = self.run_verifier(mutated)
        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)

    def test_rejects_mutable_github_action_reference(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        mutated, count = re.subn(
            r"actions/checkout@[0-9a-f]{40}",
            "actions/checkout@v5",
            workflow,
            count=1,
        )
        self.assertEqual(1, count)
        result = self.run_verifier(DOCKERFILE.read_text(encoding="utf-8"), mutated)
        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)

    def test_rejects_a_quoted_valid_action_key(self) -> None:
        # Exact action text is not enough: a quoted YAML key is an unsupported
        # syntax escape even when it names an otherwise approved full SHA.
        workflow = WORKFLOW.read_text(encoding="utf-8")
        mutated = workflow.replace("        uses: actions/checkout@", "        'uses': actions/checkout@", 1)
        result = self.run_verifier(DOCKERFILE.read_text(encoding="utf-8"), mutated)
        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)

    def test_rejects_unknown_shorthand_github_action_step(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        marker = "      - name: Generate API SBOM"
        self.assertIn(marker, workflow)
        mutated = workflow.replace(
            marker,
            "      - uses: attacker/exfiltrate@abcdef1234567890abcdef1234567890abcdef12\n"
            + marker,
            1,
        )
        result = self.run_verifier(DOCKERFILE.read_text(encoding="utf-8"), mutated)
        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)

    def test_rejects_unknown_flow_mapping_github_action_step(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        marker = "      - name: Generate API SBOM"
        self.assertIn(marker, workflow)
        mutated = workflow.replace(
            marker,
            "      - { name: Exfiltrate, uses: attacker/exfiltrate@abcdef1234567890abcdef1234567890abcdef12 }\n"
            + marker,
            1,
        )
        result = self.run_verifier(DOCKERFILE.read_text(encoding="utf-8"), mutated)
        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)

    def test_rejects_quoted_or_escaped_github_action_keys(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        marker = "      - name: Generate API SBOM"
        self.assertIn(marker, workflow)
        for malicious_step in (
            '      - { "uses": attacker/exfiltrate@abcdef1234567890abcdef1234567890abcdef12 }',
            '      - "\\u0075ses": attacker/exfiltrate@abcdef1234567890abcdef1234567890abcdef12',
        ):
            with self.subTest(step=malicious_step):
                mutated = workflow.replace(marker, malicious_step + "\n" + marker, 1)
                result = self.run_verifier(DOCKERFILE.read_text(encoding="utf-8"), mutated)
                self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)

    def test_rejects_removal_of_the_browser_acceptance_job(self) -> None:
        # Removing the dedicated browser job changes the ordered action
        # allowlist and the runs-on/node-version line counts, so the pin
        # verifier must fail closed.
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("  browser:\n", workflow)
        start = workflow.index("  browser:\n")
        end = workflow.index("  python:", start)
        mutated = workflow[:start] + workflow[end:]
        self.assertNotEqual(workflow, mutated)
        result = self.run_verifier(DOCKERFILE.read_text(encoding="utf-8"), mutated)
        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)

    def test_pin_verifier_covers_the_release_workflow(self) -> None:
        # The pin verifier must extend its exact ordered SHA allowlist to the
        # release workflow, not only ci.yml. Running against the real repo,
        # it must pass and its summary must mention the release coverage.
        result = subprocess.run(
            ["python", str(PIN_VERIFIER)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("release", result.stdout.lower(), result.stdout + result.stderr)

    def test_rejects_an_unapproved_release_runtime_selector(self) -> None:
        workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        mutated = workflow.replace("node-version: 24.18.0", "node-version: 24.19.0", 1)
        result = self.run_verifier(DOCKERFILE.read_text(encoding="utf-8"), None, mutated)
        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)

    def test_rejects_a_mutable_release_action_reference(self) -> None:
        # A mutable tag on any release.yml action must fail closed independently
        # of ci.yml, which is left at its real, valid value.
        workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        mutated, count = re.subn(
            r"actions/checkout@[0-9a-f]{40}",
            "actions/checkout@v6",
            workflow,
            count=1,
        )
        self.assertEqual(1, count)
        result = self.run_verifier(DOCKERFILE.read_text(encoding="utf-8"), None, mutated)
        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)

    def test_rejects_an_unapproved_release_action_reference(self) -> None:
        # An attacker action pinned to a full SHA still fails because it is not
        # on the exact ordered release allowlist.
        workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        marker = "      - name: Check out repository\n"
        self.assertIn(marker, workflow)
        mutated = workflow.replace(
            marker,
            "      - uses: attacker/exfiltrate@abcdef1234567890abcdef1234567890abcdef12\n"
            + marker,
            1,
        )
        result = self.run_verifier(DOCKERFILE.read_text(encoding="utf-8"), None, mutated)
        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)

    def test_rejects_quoted_release_uses_key(self) -> None:
        workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        marker = "      - name: Check out repository\n"
        self.assertIn(marker, workflow)
        mutated = workflow.replace(
            marker,
            '      - { "uses": attacker/exfiltrate@abcdef1234567890abcdef1234567890abcdef12 }\n'
            + marker,
            1,
        )
        result = self.run_verifier(DOCKERFILE.read_text(encoding="utf-8"), None, mutated)
        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)

    def test_rejects_a_missing_release_workflow_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tracehelix-container-pins-") as temp:
            root = Path(temp)
            (root / "scripts").mkdir()
            (root / ".github" / "workflows").mkdir(parents=True)
            shutil.copy2(PIN_VERIFIER, root / "scripts" / PIN_VERIFIER.name)
            (root / "Dockerfile").write_text(
                DOCKERFILE.read_text(encoding="utf-8"), encoding="utf-8"
            )
            (root / ".github" / "workflows" / "ci.yml").write_text(
                WORKFLOW.read_text(encoding="utf-8"), encoding="utf-8"
            )
            # release.yml is intentionally absent: the verifier must fail closed.
            result = subprocess.run(
                ["python", str(root / "scripts" / PIN_VERIFIER.name)],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("release.yml", result.stdout + result.stderr)


class CriticalInvariantTests(unittest.TestCase):
    """Guard reviewed DNS configuration and lifecycle script bytes."""

    RESOLVER = "resolver 127.0.0.11 valid=5s ipv6=off;"
    ZONE = "zone tracehelix_api 64k;"
    RESOLVABLE_SERVER = "server api:5080 resolve;"
    BLOCKER_REUSES_OLD_IP = '[[ "$blocker_ip" == "$old_api_ip" ]]'
    NEW_API_USES_NEW_IP = '[[ -n "$new_api_ip" && "$new_api_ip" != "$old_api_ip" ]]'
    # Intentional lifecycle edits require review and an explicit digest update.
    EXPECTED_LIFECYCLE_SHA256 = (
        "d0b9cb9518b53f375ced8dcf1239f39df61d49b60d3fdca456c882e07e5c6c5d"
    )

    @staticmethod
    def _quote_masked_chars(line: str) -> list[tuple[str, bool]]:
        """Classify each character against nginx-style quoting.

        Returns ``(character, masked)`` pairs where ``masked`` marks
        characters inside a single/double-quoted string or following a
        backslash escape (outside single quotes). The comment and brace
        scanners share this one view so meta-characters hidden by quoting
        are handled consistently instead of by two drifting state machines.
        """
        masked_chars: list[tuple[str, bool]] = []
        quote: str | None = None
        escaped = False
        for character in line:
            if escaped:
                masked_chars.append((character, True))
                escaped = False
                continue
            if character == "\\" and quote != "'":
                escaped = True
                masked_chars.append((character, quote is not None))
                continue
            if character in ("'", '"'):
                masked = quote is not None
                if quote == character:
                    quote = None
                elif quote is None:
                    quote = character
                masked_chars.append((character, masked))
                continue
            masked_chars.append((character, quote is not None))
        return masked_chars

    @classmethod
    def _strip_unquoted_comment(cls, line: str) -> str:
        result = []
        for character, masked in cls._quote_masked_chars(line):
            if character == "#" and not masked:
                break
            result.append(character)
        return "".join(result).strip()

    @classmethod
    def _active_lines(cls, text: str) -> list[str]:
        return [
            active
            for raw_line in text.splitlines()
            if (active := cls._strip_unquoted_comment(raw_line))
        ]

    @classmethod
    def _brace_delta(cls, line: str) -> int:
        delta = 0
        for character, masked in cls._quote_masked_chars(line):
            if not masked:
                delta += (character == "{") - (character == "}")
        return delta

    def _top_level_lines(self, nginx_lines: list[str]) -> list[str]:
        """Return active nginx lines that sit at http/top-level (depth zero) scope.

        The ``resolver`` directive only drives runtime DNS resolution when it
        lives at the http/top level; a copy nested inside a ``server`` or
        ``location`` block is silently inert, so a line-anywhere scan would
        rubber-stamp a weakened config. Walking brace depth with the shared
        quote-aware scanner means a directive hidden inside a block cannot
        satisfy this guard, and a file whose active braces no longer balance
        (for example a truncated commit) fails closed rather than best-effort
        matching directives against a half-parsed tree.
        """
        top_level: list[str] = []
        depth = 0
        for line in nginx_lines:
            if depth == 0:
                top_level.append(line)
            depth += self._brace_delta(line)
        if depth != 0:
            self.fail(
                "Unbalanced braces in deploy/nginx.conf active lines "
                f"(ended at depth {depth}); refusing to validate an unclosed scope"
            )
        return top_level

    def _upstream_lines(self, nginx_lines: list[str]) -> list[str]:
        opening = "upstream tracehelix_api {"
        try:
            start = nginx_lines.index(opening)
        except ValueError:
            self.fail(f"Missing active nginx block: {opening!r}")

        depth = 0
        block = []
        for index in range(start, len(nginx_lines)):
            line = nginx_lines[index]
            depth += self._brace_delta(line)
            if index != start and depth > 0:
                block.append(line)
            if index != start and depth == 0:
                return block
        self.fail(f"Unclosed active nginx block: {opening!r}")

    def _assert_nginx_invariants(self, nginx_text: str) -> None:
        nginx_lines = self._active_lines(nginx_text)
        top_level_lines = self._top_level_lines(nginx_lines)
        upstream_lines = self._upstream_lines(nginx_lines)

        checks = (
            (
                top_level_lines,
                self.RESOLVER,
                "active Docker DNS resolver at http/top-level scope",
            ),
            (
                upstream_lines,
                self.ZONE,
                "shared zone inside the tracehelix_api upstream",
            ),
            (
                upstream_lines,
                self.RESOLVABLE_SERVER,
                "runtime-resolved API server inside the tracehelix_api upstream",
            ),
        )
        for haystack, needle, label in checks:
            self.assertIn(needle, haystack, f"Missing {label}: {needle!r}")

    def _assert_lifecycle_digest(self, lifecycle_bytes: bytes) -> None:
        actual = hashlib.sha256(lifecycle_bytes).hexdigest()
        self.assertEqual(
            self.EXPECTED_LIFECYCLE_SHA256,
            actual,
            "Lifecycle script bytes changed; review the complete script and update "
            "EXPECTED_LIFECYCLE_SHA256 intentionally",
        )

    def _comment_out(self, text: str, needle: str) -> str:
        lines = text.splitlines(keepends=True)
        for index, line in enumerate(lines):
            if line.strip() == needle:
                indentation = line[: len(line) - len(line.lstrip())]
                newline = "\n" if line.endswith("\n") else ""
                lines[index] = f"{indentation}# {needle}{newline}"
                return "".join(lines)
        self.fail(f"Cannot mutate missing active line: {needle!r}")

    def test_reviewed_critical_invariants_are_unchanged(self) -> None:
        self._assert_nginx_invariants(NGINX_CONF.read_text(encoding="utf-8"))
        self._assert_lifecycle_digest(COMPOSE_LIFECYCLE.read_bytes())

    def test_commented_nginx_invariants_are_rejected(self) -> None:
        nginx_text = NGINX_CONF.read_text(encoding="utf-8")
        for needle in (self.RESOLVER, self.ZONE, self.RESOLVABLE_SERVER):
            with self.subTest(needle=needle):
                mutated = self._comment_out(nginx_text, needle)
                with self.assertRaises(AssertionError):
                    self._assert_nginx_invariants(mutated)

    def test_lifecycle_digest_rejects_semantic_weakening(self) -> None:
        lifecycle_text = COMPOSE_LIFECYCLE.read_text(encoding="utf-8")
        blocker = self.BLOCKER_REUSES_OLD_IP
        new_api = self.NEW_API_USES_NEW_IP
        mutations = {
            "comment-blocker-assertion": self._comment_out(lifecycle_text, blocker),
            "comment-new-api-assertion": self._comment_out(lifecycle_text, new_api),
            "quoted-heredoc": lifecycle_text.replace(
                blocker,
                ": <<'INERT_GUARD_TEXT'\n"
                f"{blocker}\n"
                "INERT_GUARD_TEXT",
                1,
            ),
            "backslash-heredoc": lifecycle_text.replace(
                blocker,
                ": <<\\INERT_GUARD_TEXT\n"
                f"{blocker}\n"
                "INERT_GUARD_TEXT",
                1,
            ),
            "hyphenated-heredoc": lifecycle_text.replace(
                new_api,
                ": <<'INERT-GUARD-TEXT'\n"
                f"{new_api}\n"
                "INERT-GUARD-TEXT",
                1,
            ),
            "tab-stripping-heredoc": lifecycle_text.replace(
                new_api,
                ": <<-'INERT_GUARD_TEXT'\n"
                f"\t{new_api}\n"
                "\tINERT_GUARD_TEXT",
                1,
            ),
            "unreachable-blocker-assertion": lifecycle_text.replace(
                blocker,
                f"if false; then\n  {blocker}\nfi",
                1,
            ),
            "unreachable-new-api-assertion": lifecycle_text.replace(
                new_api,
                f"if false; then\n  {new_api}\nfi",
                1,
            ),
        }

        for label, mutated in mutations.items():
            with self.subTest(label=label):
                self.assertNotEqual(lifecycle_text, mutated)
                syntax = subprocess.run(
                    ["bash", "-n"], input=mutated, text=True, capture_output=True
                )
                self.assertEqual(0, syntax.returncode, syntax.stderr)
                with self.assertRaises(AssertionError):
                    self._assert_lifecycle_digest(mutated.encode("utf-8"))

    def test_quoted_comment_and_braces_do_not_deceive_nginx_parser(self) -> None:
        quoted_line = 'guard "# not-comment }"; # real comment'
        self.assertEqual(
            'guard "# not-comment }";', self._strip_unquoted_comment(quoted_line)
        )
        self.assertEqual(0, self._brace_delta(self._strip_unquoted_comment(quoted_line)))

        nginx_text = NGINX_CONF.read_text(encoding="utf-8")
        for needle in (self.ZONE, self.RESOLVABLE_SERVER):
            with self.subTest(needle=needle):
                moved = nginx_text.replace(f"    {needle}\n", "", 1)
                moved = moved.replace(
                    "upstream tracehelix_api {\n",
                    'upstream tracehelix_api {\n    guard "{";\n',
                    1,
                )
                moved += f"\n{needle}\n"
                with self.assertRaises(AssertionError):
                    self._assert_nginx_invariants(moved)

    def test_resolver_nested_in_a_server_block_is_rejected(self) -> None:
        nginx_text = NGINX_CONF.read_text(encoding="utf-8")
        # The exact directive bytes stay active in the file, but only inside a
        # nested server block where nginx leaves resolver inert for runtime
        # resolution. A line-anywhere scan would miss this weakening, so the
        # depth-zero scope check must be what rejects it.
        self.assertIn(self.RESOLVER + "\n", nginx_text)
        moved = nginx_text.replace(
            self.RESOLVER + "\n",
            "server {\n    " + self.RESOLVER + "\n}\n",
            1,
        )
        self.assertIn(self.RESOLVER, self._active_lines(moved))
        with self.assertRaises(AssertionError):
            self._assert_nginx_invariants(moved)

    def test_unbalanced_nginx_braces_fail_closed(self) -> None:
        nginx_text = NGINX_CONF.read_text(encoding="utf-8")
        # Drop the final closing brace so active braces no longer balance. The
        # guard must refuse to bless a truncated or unclosed scope instead of
        # best-effort matching directives against a half-parsed tree.
        mutated = nginx_text.rstrip()[:-1] + "\n"
        self.assertNotEqual(nginx_text, mutated)
        with self.assertRaises(AssertionError):
            self._assert_nginx_invariants(mutated)


class VersionContractTests(unittest.TestCase):
    """Guard the authoritative version contract across every declared source."""

    def _repository_versions(self) -> dict[str, str]:
        versions: dict[str, str] = {
            "VERSION": parse_version_file(VERSION.read_text(encoding="utf-8")),
            "Directory.Build.props": parse_directory_build_props_version_prefix(
                DIRECTORY_BUILD_PROPS.read_text(encoding="utf-8")
            ),
            "web/package.json": parse_web_package_version(
                WEB_PACKAGE_JSON.read_text(encoding="utf-8")
            ),
        }
        if TRAINING_PYPROJECT.exists():
            versions["training/pyproject.toml"] = parse_training_pyproject_version(
                TRAINING_PYPROJECT.read_text(encoding="utf-8")
            )
        return versions

    def test_authoritative_version_sources_agree_on_canonical_semver(self) -> None:
        assert_versions_agree(self._repository_versions())

    def test_version_file_rejects_non_canonical_or_extra_whitespace(self) -> None:
        bad = [
            "0.1.0",        # missing required trailing LF
            "0.1.0\n\n",    # extra blank line
            " 0.1.0\n",     # leading whitespace
            "0.1.0 \n",     # trailing whitespace before newline
            "v0.1.0\n",     # non-SemVer prefix
            "0.1\n",        # partial version
            "0.1.0.0\n",    # too many components
            "0.1.x\n",      # non-numeric component
        ]
        for text in bad:
            with self.subTest(text=text):
                with self.assertRaises(AssertionError):
                    parse_version_file(text)

    def test_directory_build_props_rejects_missing_malformed_or_duplicate_prefix(
        self,
    ) -> None:
        bad = {
            "missing": "<Project><PropertyGroup></PropertyGroup></Project>",
            "duplicate": (
                "<Project><PropertyGroup>"
                "<VersionPrefix>0.1.0</VersionPrefix>"
                "<VersionPrefix>0.2.0</VersionPrefix>"
                "</PropertyGroup></Project>"
            ),
            "malformed-xml": "<Project><PropertyGroup></Project>",
            "empty-value": (
                "<Project><PropertyGroup><VersionPrefix></VersionPrefix>"
                "</PropertyGroup></Project>"
            ),
            "non-semver": (
                "<Project><PropertyGroup><VersionPrefix>v0.1</VersionPrefix>"
                "</PropertyGroup></Project>"
            ),
        }
        for label, text in bad.items():
            with self.subTest(label=label):
                with self.assertRaises(AssertionError):
                    parse_directory_build_props_version_prefix(text)

    def test_web_package_json_rejects_missing_or_non_semver_version(self) -> None:
        bad = [
            "{ }",                      # missing version key
            '{ "version": 5 }',         # non-string value
            '{ "version": "0.1" }',     # partial version
            '{ "version": "v0.1.0" }',  # non-SemVer prefix
            "not json",                 # malformed JSON
        ]
        for text in bad:
            with self.subTest(text=text):
                with self.assertRaises(AssertionError):
                    parse_web_package_version(text)

    def test_training_pyproject_rejects_missing_or_non_semver_version(self) -> None:
        bad = [
            '[project]\nname = "x"\n',                # missing version
            '[project]\nname = "x"\nversion = 5\n',   # non-string value
            '[project]\nname = "x"\nversion = "0.1"\n',  # partial version
            "not toml = [",                            # malformed TOML
        ]
        for text in bad:
            with self.subTest(text=text):
                with self.assertRaises(AssertionError):
                    parse_training_pyproject_version(text)

    def test_cross_source_divergence_fails_closed(self) -> None:
        divergent = {
            "VERSION": "0.1.0",
            "Directory.Build.props": "0.1.0",
            "web/package.json": "0.2.0",
            "training/pyproject.toml": "0.1.0",
        }
        with self.assertRaises(AssertionError):
            assert_versions_agree(divergent)


TAMPER_TOKEN = "TRACEHELIX-GUARD-TAMPER-SENTINEL"

# Short, meaningful anchor phrases (not whole prose) so renaming or deleting a
# required claim fails the guard without over-pinning incidental wording.
README_REQUIRED_ANCHORS = (
    "docs/release-readiness-v0.1.0.md",
    "local trusted single-user",
    "not a network, multi-user, or SaaS service",
    "not production-grade",
    "not yet tagged",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "CHANGELOG.md",
)
ARCHITECTURE_REQUIRED_ANCHORS = ("docs/release-readiness-v0.1.0.md",)
SECURITY_REQUIRED_ANCHORS = (
    "GitHub Security Advisories",
    "local trusted single-user",
    "Do not include traces, secrets",
)
CONTRIBUTING_REQUIRED_ANCHORS = (
    "python scripts/test_repository_guards.py",
    "python scripts/source_fingerprint.py",
    "docs/verification.md",
)
CHANGELOG_REQUIRED_ANCHORS = ("## [Unreleased]", "## [0.1.0]")
RELEASE_READINESS_REQUIRED_ANCHORS = (
    "08b3ea9",
    "open-deferred",
    "not production-grade",
    "local trusted single-user",
)
PR_TEMPLATE_REQUIRED_ANCHORS = (
    "Scope",
    "Tests and evidence",
    "Release-claim updates",
    "Privacy check",
    "Exact-snapshot note",
)

# Phrases that must NOT appear in shipped docs. A release-readiness or security
# document living on ``main`` must stay correct after the pull request merges,
# so it must not pin a transient local worktree or a named feature/release
# branch as the source-tree location (those nouns describe one checkout, not
# the merged tree every reader sees).
RELEASE_READINESS_FORBIDDEN_PHRASES = (
    "this worktree",
    "release/tracehelix-v0.1.0",
)
SECURITY_FORBIDDEN_PHRASES = ("release branch",)


class GovernanceAndScopeTests(unittest.TestCase):
    """Guard required release/governance files and their stable claims."""

    REQUIRED_FILES = (
        VERSION,
        SECURITY_MD,
        CONTRIBUTING_MD,
        CHANGELOG_MD,
        CODEOWNERS,
        PR_TEMPLATE,
        RELEASE_READINESS,
    )

    def test_required_governance_and_release_files_are_present(self) -> None:
        missing = [str(path) for path in self.REQUIRED_FILES if not path.is_file()]
        self.assertEqual([], missing, f"missing required files: {missing}")

    def test_codeowners_defaults_to_repository_owner(self) -> None:
        text = CODEOWNERS.read_text(encoding="utf-8")
        self.assertIn("* @dzikiczlowiek", text)

    def test_codeowners_owner_handle_is_required(self) -> None:
        text = CODEOWNERS.read_text(encoding="utf-8")
        self.assertIn("@dzikiczlowiek", text)
        with self.assertRaises(AssertionError):
            self.assertIn(
                "@dzikiczlowiek", text.replace("@dzikiczlowiek", TAMPER_TOKEN)
            )

    def _assert_each_anchor_required(
        self, path: Path, anchors: tuple[str, ...]
    ) -> None:
        text = path.read_text(encoding="utf-8")
        require_anchors(text, anchors, str(path))
        for anchor in anchors:
            with self.subTest(path=path.name, anchor=anchor):
                tampered = text.replace(anchor, TAMPER_TOKEN)
                self.assertNotEqual(text, tampered)
                with self.assertRaises(AssertionError):
                    require_anchors(tampered, anchors, str(path))

    def _assert_no_forbidden_phrase(
        self, path: Path, phrases: tuple[str, ...]
    ) -> None:
        text = path.read_text(encoding="utf-8")
        forbid_phrases(text, phrases, str(path))
        for phrase in phrases:
            with self.subTest(path=path.name, phrase=phrase):
                with self.assertRaises(AssertionError):
                    forbid_phrases(text + phrase, phrases, str(path))

    def test_readme_preserves_release_scope_and_governance_anchors(self) -> None:
        self._assert_each_anchor_required(README, README_REQUIRED_ANCHORS)

    def test_architecture_links_release_readiness(self) -> None:
        self._assert_each_anchor_required(ARCHITECTURE, ARCHITECTURE_REQUIRED_ANCHORS)

    def test_security_policy_has_required_anchors(self) -> None:
        self._assert_each_anchor_required(SECURITY_MD, SECURITY_REQUIRED_ANCHORS)

    def test_contributing_references_canonical_gates(self) -> None:
        self._assert_each_anchor_required(
            CONTRIBUTING_MD, CONTRIBUTING_REQUIRED_ANCHORS
        )

    def test_changelog_has_unreleased_and_planned_release(self) -> None:
        self._assert_each_anchor_required(CHANGELOG_MD, CHANGELOG_REQUIRED_ANCHORS)

    def test_release_readiness_keeps_honest_status_and_scope(self) -> None:
        self._assert_each_anchor_required(
            RELEASE_READINESS, RELEASE_READINESS_REQUIRED_ANCHORS
        )

    def test_pull_request_template_requires_release_discipline(self) -> None:
        self._assert_each_anchor_required(PR_TEMPLATE, PR_TEMPLATE_REQUIRED_ANCHORS)

    def test_release_readiness_makes_no_unstable_worktree_or_branch_claim(self) -> None:
        self._assert_no_forbidden_phrase(
            RELEASE_READINESS, RELEASE_READINESS_FORBIDDEN_PHRASES
        )

    def test_security_support_model_does_not_pin_a_transient_branch(self) -> None:
        self._assert_no_forbidden_phrase(SECURITY_MD, SECURITY_FORBIDDEN_PHRASES)


class BrowserAcceptanceGuardTests(unittest.TestCase):
    """Guard the real-process browser acceptance verifier and its wiring.

    These guards fail closed if the browser acceptance CI job, verifier script,
    or its documentation is removed or inerted. They complement the exact
    ordered CI action allowlist enforced by ``scripts/verify_container_pins.py``
    and the focused mutation test that removes the whole browser job.
    """

    REQUIRED_BROWSER_FILES = (BROWSER_VERIFIER, PLAYWRIGHT_CONFIG, E2E_SPEC)

    # Stable anchors in the browser acceptance CI job.
    CI_BROWSER_ANCHORS = (
        "  browser:",
        "name: Browser acceptance",
        "bash scripts/verify-browser.sh",
        "npm exec --offline -- playwright install --with-deps chromium",
    )

    # Stable behavioral anchors in the verifier script: a unique project label,
    # project-labelled residue queries (no foreign-resource actioning), a hard
    # teardown, and a hard-fail when success-path teardown leaves any residue.
    SCRIPT_ANCHORS = (
        'PROJECT="tracehelix-browser-$$"',
        "label=com.docker.compose.project=$PROJECT",
        "down --volumes --remove-orphans",
        "[[ $hard -ne 0 ]]",
    )

    VERIFICATION_BROWSER_ANCHORS = (
        "Browser acceptance",
        "scripts/verify-browser.sh",
        "(cd web && npm ci && npm exec --offline -- playwright install --with-deps chromium)",
        'sg docker -c "bash scripts/verify-browser.sh"',
    )

    README_BROWSER_ANCHORS = (
        "Browser acceptance",
        "scripts/verify-browser.sh",
        "(cd web && npm ci && npm exec --offline -- playwright install --with-deps chromium)",
    )

    def _require_anchors_with_mutation(
        self, path: Path, anchors: tuple[str, ...]
    ) -> None:
        text = path.read_text(encoding="utf-8")
        require_anchors(text, anchors, str(path))
        for anchor in anchors:
            with self.subTest(path=path.name, anchor=anchor):
                tampered = text.replace(anchor, TAMPER_TOKEN)
                self.assertNotEqual(text, tampered)
                with self.assertRaises(AssertionError):
                    require_anchors(tampered, anchors, str(path))

    @staticmethod
    def _require_ci_browser_job(workflow_text: str) -> None:
        for needle in BrowserAcceptanceGuardTests.CI_BROWSER_ANCHORS:
            if needle not in workflow_text:
                raise AssertionError(
                    f"ci.yml missing required browser acceptance anchor: {needle!r}"
                )

    def test_browser_acceptance_files_are_present(self) -> None:
        missing = [str(p) for p in self.REQUIRED_BROWSER_FILES if not p.is_file()]
        self.assertEqual([], missing, f"missing browser acceptance files: {missing}")

    def test_ci_browser_job_is_wired_with_pinned_steps(self) -> None:
        self._require_ci_browser_job(WORKFLOW.read_text(encoding="utf-8"))

    def test_inerting_the_browser_verifier_step_fails(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("run: bash scripts/verify-browser.sh\n", workflow)
        mutated = workflow.replace(
            "run: bash scripts/verify-browser.sh\n",
            "run: echo inerted\n",
            1,
        )
        self.assertNotEqual(workflow, mutated)
        with self.assertRaises(AssertionError):
            self._require_ci_browser_job(mutated)

    def test_verifier_script_preserves_hardening_anchors(self) -> None:
        self._require_anchors_with_mutation(BROWSER_VERIFIER, self.SCRIPT_ANCHORS)

    def test_verification_documents_canonical_browser_command(self) -> None:
        self._require_anchors_with_mutation(
            VERIFICATION_MD, self.VERIFICATION_BROWSER_ANCHORS
        )

    def test_readme_documents_browser_acceptance(self) -> None:
        self._require_anchors_with_mutation(README, self.README_BROWSER_ANCHORS)


class ReleaseBundleGuardTests(unittest.TestCase):
    """Guard deterministic bundle creation, verification, and artifact smoke."""

    REQUIRED_FILES = (
        BUNDLE_BUILDER,
        BUNDLE_BUILDER_TESTS,
        BUNDLE_VERIFIER,
        BUNDLE_VERIFIER_TESTS,
        BUNDLE_ACCEPTANCE,
    )
    CI_ANCHORS = (
        "  release-bundle:",
        "name: Release bundle acceptance",
        "timeout-minutes: 35",
        "bash scripts/verify-release-bundle.sh",
        "npm exec --offline -- playwright install --with-deps chromium",
    )
    CI_SETUP_ORDER = (
        "name: Install locked web dependencies",
        "run: npm ci",
        "npm exec --offline -- playwright install --with-deps chromium",
        "bash scripts/verify-release-bundle.sh",
    )
    SCRIPT_ANCHORS = (
        "build_release_bundle.py",
        'cmp "$OUT_A/$ARCHIVE" "$OUT_B/$ARCHIVE"',
        "verify_release_bundle.py",
        'SOURCE="$EXTRACT/tracehelix-$VERSION"',
        "scripts/test_repository_guards.py",
        "docker compose --profile tools build --pull",
        "bash scripts/verify-compose-lifecycle.sh",
        "bash scripts/verify-browser.sh",
    )
    BUILDER_EXCLUSION_ANCHORS = (
        "_FORBIDDEN_RELEASE_COMPONENTS",
        '".hermes"',
        '"test-results"',
        '"playwright-report"',
        '".db"',
        'folded_components[0] == "imports"',
    )
    VERIFIER_EXCLUSION_ANCHORS = (
        "FORBIDDEN_RELEASE_COMPONENTS",
        '".hermes"',
        '"test-results"',
        '"playwright-report"',
        '".db"',
        'components[0] == "imports"',
        "forbidden release source path",
    )
    README_ANCHORS = (
        "Deterministic release bundle",
        'sg docker -c "bash scripts/verify-release-bundle.sh"',
        "Release bundle acceptance",
        "install-from-artifact evidence",
    )
    VERIFICATION_ANCHORS = (
        "Release bundle acceptance",
        'sg docker -c "bash scripts/verify-release-bundle.sh"',
        "Two byte-identical source bundles",
    )
    READINESS_ANCHORS = (
        "deterministic local source bundle",
        "install-from-artifact evidence",
        "no public tag",
        "tag-only release workflow is present",
    )
    CHANGELOG_LIMITATION_ANCHORS = (
        "local install-from-artifact evidence",
        "tag-only release workflow is present",
        "no published release bundle",
        "downloaded-public-artifact verification",
    )
    ARCHITECTURE_BUNDLE_ANCHORS = (
        "deterministic local source-bundle",
        "install-from-artifact evidence",
        "public release",
        "downloaded-public-artifact verification",
    )
    COMPOSE_LIFECYCLE_TEARDOWN_ANCHORS = (
        "com.docker.compose.project",
        "teardown left Docker resources",
    )
    VERIFIED_EXPORT_ANCHORS = (
        "TRACEHELIX_VERIFIED_OUTPUT_DIR",
        "export_verified_output",
        "destination must be an absolute path",
        "destination must be a pre-existing empty directory",
        "destination must be outside the checkout and work directory",
        "RELEASE-MANIFEST.json",
        "rollback_verified_export",
    )
    # Exclusive-create flags and the destination-empty probe are scoped to their
    # shell functions in the assertions below: the copy helper name and the die
    # message are each referenced from a second site, so exact-text checks here
    # catch a hardening deletion that a global anchor scan would miss.
    COPY_EXCLUSIVE_FLAGS = "flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL"
    EXPORT_EMPTY_DIR_PROBE = (
        '[[ -z "$(find "$destination" -mindepth 1 -maxdepth 1 -print -quit)" ]]'
    )

    def _require_with_mutation(self, path: Path, anchors: tuple[str, ...]) -> None:
        text = path.read_text(encoding="utf-8")
        require_anchors(text, anchors, str(path))
        for anchor in anchors:
            with self.subTest(path=path.name, anchor=anchor):
                tampered = text.replace(anchor, TAMPER_TOKEN)
                self.assertNotEqual(text, tampered)
                with self.assertRaises(AssertionError):
                    require_anchors(tampered, anchors, str(path))

    def test_bundle_files_are_present(self) -> None:
        missing = [str(path) for path in self.REQUIRED_FILES if not path.is_file()]
        self.assertEqual([], missing, f"missing release bundle files: {missing}")

    def test_ci_release_bundle_job_is_required(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        require_anchors(workflow, self.CI_ANCHORS, str(WORKFLOW))
        job = workflow.split("  release-bundle:", 1)[1].split("\n  python:", 1)[0]
        require_anchors(job, self.CI_SETUP_ORDER, "release-bundle job")
        offsets = [job.index(anchor) for anchor in self.CI_SETUP_ORDER]
        self.assertEqual(sorted(offsets), offsets, "release-bundle setup is out of order")

        for anchor in self.CI_ANCHORS:
            with self.subTest(anchor=anchor):
                tampered = workflow.replace(anchor, TAMPER_TOKEN)
                self.assertNotEqual(workflow, tampered)
                with self.assertRaises((AssertionError, ValueError)):
                    require_anchors(tampered, self.CI_ANCHORS, str(WORKFLOW))
        for anchor in self.CI_SETUP_ORDER:
            with self.subTest(job_anchor=anchor):
                tampered = job.replace(anchor, TAMPER_TOKEN)
                self.assertNotEqual(job, tampered)
                with self.assertRaises(AssertionError):
                    require_anchors(tampered, self.CI_SETUP_ORDER, "release-bundle job")

    def test_acceptance_script_preserves_extracted_artifact_contract(self) -> None:
        self._require_with_mutation(BUNDLE_ACCEPTANCE, self.SCRIPT_ANCHORS)

    def test_builder_and_verifier_preserve_release_exclusions(self) -> None:
        self._require_with_mutation(BUNDLE_BUILDER, self.BUILDER_EXCLUSION_ANCHORS)
        self._require_with_mutation(BUNDLE_VERIFIER, self.VERIFIER_EXCLUSION_ANCHORS)

    def test_acceptance_script_has_fail_closed_verified_output_export(self) -> None:
        """An opt-in export can only happen after the verified extracted gates."""
        self._require_with_mutation(BUNDLE_ACCEPTANCE, self.VERIFIED_EXPORT_ANCHORS)
        script = BUNDLE_ACCEPTANCE.read_text(encoding="utf-8")
        export_at = script.rindex("\nexport_verified_output\n")
        browser_at = script.index('bash scripts/verify-browser.sh')
        self.assertGreater(export_at, browser_at)
        self.assertIn('"$OUT_A/$ARCHIVE"', script[export_at:])
        self.assertIn('RELEASE-MANIFEST.json) source="$SOURCE/$name"', script)
        append_at = script.index('EXPORTED_FILES+=("$name")')
        copy_at = script.index('copy_verified_file_exclusively "$source" "$destination/$name"')
        self.assertLess(
            append_at,
            copy_at,
            "rollback must know the destination name before an interruptible copy starts",
        )
        # Exclusive-create (O_EXCL) and the destination-empty probe are scoped to
        # their shell functions: the copy helper and die message are each
        # referenced from a second site, so a global scan cannot tell that the
        # hardening was deleted from the one function that enforces it.
        self.assertEqual(self.COPY_EXCLUSIVE_FLAGS, parse_copy_exclusive_flags(script))
        self.assertIn(
            self.EXPORT_EMPTY_DIR_PROBE, export_verified_output_body(script)
        )
        tampered_excl = script.replace(
            "os.O_WRONLY | os.O_CREAT | os.O_EXCL",
            "os.O_WRONLY | os.O_CREAT",
            1,
        )
        with self.assertRaises(AssertionError):
            self.assertEqual(
                self.COPY_EXCLUSIVE_FLAGS,
                parse_copy_exclusive_flags(tampered_excl),
            )
        tampered_empty = script.replace(
            self.EXPORT_EMPTY_DIR_PROBE,
            '[[ -z "" ]]',
            1,
        )
        with self.assertRaises(AssertionError):
            self.assertIn(
                self.EXPORT_EMPTY_DIR_PROBE,
                export_verified_output_body(tampered_empty),
            )

    def test_readme_documents_bundle_without_public_release_overclaim(self) -> None:
        self._require_with_mutation(README, self.README_ANCHORS)

    def test_verification_documents_canonical_bundle_command(self) -> None:
        self._require_with_mutation(VERIFICATION_MD, self.VERIFICATION_ANCHORS)

    def test_readiness_records_local_evidence_and_public_followup(self) -> None:
        self._require_with_mutation(RELEASE_READINESS, self.READINESS_ANCHORS)

    def test_changelog_distinguishes_local_evidence_from_public_release(self) -> None:
        changelog = CHANGELOG_MD.read_text(encoding="utf-8")
        limitations = changelog.split("### Known open limitations", 1)[1]
        require_anchors(
            limitations,
            self.CHANGELOG_LIMITATION_ANCHORS,
            "CHANGELOG known limitations",
        )
        for anchor in self.CHANGELOG_LIMITATION_ANCHORS:
            with self.subTest(anchor=anchor):
                tampered = limitations.replace(anchor, TAMPER_TOKEN)
                self.assertNotEqual(limitations, tampered)
                with self.assertRaises(AssertionError):
                    require_anchors(
                        tampered,
                        self.CHANGELOG_LIMITATION_ANCHORS,
                        "CHANGELOG known limitations",
                    )

    def test_architecture_distinguishes_local_evidence_from_public_release(self) -> None:
        self._require_with_mutation(ARCHITECTURE, self.ARCHITECTURE_BUNDLE_ANCHORS)

    def test_bundle_cleanup_preserves_term_status_after_rm_command_failure(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tracehelix-cleanup-test-") as temp:
            root = Path(temp)
            fakebin = root / "fakebin"
            tmpdir = root / "tmp"
            fakebin.mkdir()
            tmpdir.mkdir()
            rm_state = root / "rm-failed-once"

            fake_python = fakebin / "python3"
            fake_python.write_text(
                "#!/usr/bin/env bash\nexec /bin/sleep 30\n", encoding="utf-8"
            )
            fake_docker = fakebin / "docker"
            fake_docker.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            fake_rm = fakebin / "rm"
            fake_rm.write_text(
                "#!/usr/bin/env bash\n"
                ": >\"$TRACEHELIX_RM_FAIL_STATE\"\n"
                "exit 77\n",
                encoding="utf-8",
            )
            for executable in (fake_python, fake_docker, fake_rm):
                executable.chmod(0o755)

            env = os.environ.copy()
            env["PATH"] = f"{fakebin}:{env['PATH']}"
            env["TMPDIR"] = str(tmpdir)
            env["TRACEHELIX_RM_FAIL_STATE"] = str(rm_state)
            process = subprocess.Popen(
                ["bash", str(BUNDLE_ACCEPTANCE)],
                cwd=ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            try:
                deadline = time.monotonic() + 5
                while not list(tmpdir.glob("tracehelix-release-bundle-*")):
                    if process.poll() is not None:
                        self.fail(f"bundle verifier exited before TERM: {process.returncode}")
                    if time.monotonic() >= deadline:
                        self.fail("bundle verifier did not create its private work directory")
                    time.sleep(0.05)
                os.killpg(process.pid, signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=10)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)

            self.assertTrue(rm_state.is_file(), "the injected rm failure did not run")
            self.assertEqual(143, process.returncode, stderr)
            self.assertEqual([], list(tmpdir.glob("tracehelix-release-bundle-*")))
            self.assertEqual("", stdout)

    def test_compose_lifecycle_teardown_is_fail_closed(self) -> None:
        self._require_with_mutation(
            COMPOSE_LIFECYCLE, self.COMPOSE_LIFECYCLE_TEARDOWN_ANCHORS
        )


class ReleaseWorkflowGuardTests(unittest.TestCase):
    """Guard the fail-closed GitHub release workflow contract.

    These guards fail closed if ``.github/workflows/release.yml`` is missing,
    republishes on a dispatch, weakens tag/version equality, broadens
    permissions, mutably pins an action, rebuilds the bundle inside the
    publication job, or omits a required gate or release artifact. They
    complement the exact ordered SHA allowlist enforced for ``release.yml`` by
    ``scripts/verify_container_pins.py``.
    """

    # ALL_JOBS deliberately includes the handoff and publisher; publication
    # depends only on REQUIRED_GATE_JOBS and must never need itself.
    REQUIRED_GATE_JOBS = (
        "validate-tag",
        "guards",
        "dotnet",
        "web",
        "python",
        "e2e",
        "containers",
        "browser",
        "release-bundle",
        "assemble-evidence",
    )
    ALL_JOBS = REQUIRED_GATE_JOBS + ("publish",)
    REQUIRED_GATE_COMMANDS = (
        "python scripts/test_repository_guards.py",
        "python scripts/verify_container_pins.py",
        "make verify-e2e",
        "make verify-api",
        "bash scripts/verify-browser.sh",
        "bash scripts/verify-release-bundle.sh",
    )
    REQUIRED_ARTIFACT_NAMES = (
        "tracehelix-0.1.0-source.tar.gz",
        "SHA256SUMS",
        "RELEASE-MANIFEST.json",
        "tracehelix-0.1.0-source.cdx.json",
        "tracehelix-api.spdx.json",
        "tracehelix-web.spdx.json",
        "RELEASE-NOTES.md",
    )
    TOP_LEVEL_PERMISSIONS = "permissions:\n  contents: read\n"
    ELEVATED_PERMISSION_LINES = (
        "      contents: write",
        "      id-token: write",
        "      attestations: write",
    )
    FORBIDDEN_PERMISSIONS = ("write-all", "permissions: write-all")
    PUBLISH_JOB_IF = "github.event_name == 'push' && startsWith(github.ref, 'refs/tags/')"
    TAG_VERSION_GUARD = '[[ "$TAG" != "v$VERSION" ]]'
    TAG_EXTRACTION = 'TAG="${GITHUB_REF#refs/tags/}"'
    NEVER_OVERWRITE = 'gh release view "$TAG"'
    PROVENANCE_ACTION = (
        "actions/attest-build-provenance@e8998f949152b193b063cb0ec769d69d929409be"
    )
    DOWNLOAD_ACTION = (
        "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093"
    )
    UPLOAD_ACTION = (
        "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"
    )
    UNIFIED_ARTIFACT = "tracehelix-release-assets"
    # Exact publish ``gh release create`` asset list and the assemble
    # ``expected=$(printf ...)`` literal. Asserting these as ordered tuples,
    # scoped to their job slices, catches a single dropped asset that a global
    # anchor scan (the name still appears in download/upload steps) would miss.
    PUBLISH_CREATE_ASSETS = (
        "release/$ARCHIVE",
        "release/SHA256SUMS",
        "release/RELEASE-MANIFEST.json",
        "release/RELEASE-NOTES.md",
        "release/tracehelix-${VERSION}-source.cdx.json",
        "release/tracehelix-api.spdx.json",
        "release/tracehelix-web.spdx.json",
    )
    ASSEMBLE_EXPECTED_SET = (
        "$ARCHIVE",
        "SHA256SUMS",
        "RELEASE-MANIFEST.json",
        "RELEASE-NOTES.md",
        "tracehelix-${VERSION}-source.cdx.json",
        "tracehelix-api.spdx.json",
        "tracehelix-web.spdx.json",
    )
    PROVENANCE_SUBJECT = (
        "release/tracehelix-${{ needs.validate-tag.outputs.version }}-source.tar.gz"
    )
    RELEASE_VIEW_CONDITION = 'if gh release view "$TAG" >/dev/null 2>&1; then'

    def _require_with_mutation(
        self, path: Path, anchors: tuple[str, ...]
    ) -> None:
        text = path.read_text(encoding="utf-8")
        require_anchors(text, anchors, str(path))
        for anchor in anchors:
            with self.subTest(path=path.name, anchor=anchor):
                tampered = text.replace(anchor, TAMPER_TOKEN)
                self.assertNotEqual(text, tampered)
                with self.assertRaises(AssertionError):
                    require_anchors(tampered, anchors, str(path))

    def _publish_slice(self, workflow: str) -> str:
        self.assertIn("  publish:\n", workflow)
        return workflow.split("  publish:\n", 1)[1]

    def _assemble_slice(self, workflow: str) -> str:
        self.assertIn("  assemble-evidence:\n", workflow)
        return workflow.split("  assemble-evidence:\n", 1)[1].split(
            "\n  publish:\n", 1
        )[0]

    @staticmethod
    def _drop_publish_asset(workflow: str, asset: str) -> str:
        """Return ``workflow`` with ``asset`` removed from the publish
        ``gh release create`` argument list (faithful to mutation 1)."""
        marker = 'gh release create "$TAG"'
        head, rest = workflow.split(marker, 1)
        boundary = rest.find("\n      - name:")
        block_end = boundary if boundary != -1 else len(rest)
        block, tail = rest[:block_end], rest[block_end:]
        kept: list[str] = []
        removed = False
        for line in block.split("\n"):
            token = line.strip().removesuffix("\\").strip().strip('"')
            if token == asset and not removed:
                removed = True
                continue
            kept.append(line)
        if not removed:
            raise AssertionError(f"publish asset not found for mutation: {asset}")
        return head + marker + "\n".join(kept) + tail

    def test_release_workflow_file_is_present(self) -> None:
        self.assertTrue(
            RELEASE_WORKFLOW.is_file(), "missing .github/workflows/release.yml"
        )

    def test_release_workflow_uses_tag_and_dispatch_triggers_only(self) -> None:
        self._require_with_mutation(
            RELEASE_WORKFLOW,
            ("      - 'v*.*.*'", "  workflow_dispatch:"),
        )
        workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("on:\n  push:\n    tags:\n", workflow)
        # No branch-push or schedule trigger may publish; the only push trigger
        # is the constrained tag glob.
        self.assertNotIn("    branches:", workflow.split("jobs:", 1)[0])
        self.assertNotIn("  schedule:", workflow)

    def test_top_level_permissions_are_read_only(self) -> None:
        workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        header = workflow.split("jobs:", 1)[0]
        self.assertIn(self.TOP_LEVEL_PERMISSIONS, header)
        for forbidden in self.FORBIDDEN_PERMISSIONS:
            self.assertNotIn(forbidden, workflow)

    def test_publish_job_has_exact_elevated_permissions(self) -> None:
        self._require_with_mutation(
            RELEASE_WORKFLOW, self.ELEVATED_PERMISSION_LINES
        )
        publish = self._publish_slice(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        permissions = publish.split("    permissions:\n", 1)[1].split("\n    steps:", 1)[0]
        declared = {
            line.strip() for line in permissions.splitlines() if line.strip()
        }
        self.assertEqual(
            {
                "contents: write",
                "id-token: write",
                "attestations: write",
            },
            declared,
            "publish job must declare exactly the three required elevated scopes",
        )

    def test_release_workflow_pins_every_github_action_to_a_full_sha(self) -> None:
        workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        references = re.findall(
            r"uses:\s*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@\S+)",
            workflow,
        )
        self.assertGreaterEqual(
            len(references),
            8,
            "release.yml must reference the required set of pinned actions",
        )
        for reference in references:
            with self.subTest(reference=reference):
                self.assertRegex(
                    reference,
                    r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}$",
                    "release.yml action reference must be owner/repo@full-sha",
                )

    def test_release_workflow_validates_tag_equals_version(self) -> None:
        self._require_with_mutation(
            RELEASE_WORKFLOW,
            (self.TAG_EXTRACTION, self.TAG_VERSION_GUARD),
        )

    def test_dispatch_skips_the_entire_elevated_publish_job(self) -> None:
        workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        publish = self._publish_slice(workflow)
        self.assertIn(f"if: {self.PUBLISH_JOB_IF}", publish)
        tampered = workflow.replace(self.PUBLISH_JOB_IF, "always()", 1)
        self.assertNotEqual(workflow, tampered)
        self.assertNotIn(f"if: {self.PUBLISH_JOB_IF}", tampered)

    def test_publish_never_overwrites_an_existing_release(self) -> None:
        self._require_with_mutation(RELEASE_WORKFLOW, (self.NEVER_OVERWRITE,))
        workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        publish = self._publish_slice(workflow)
        condition, body = parse_release_view_guard(publish)
        self.assertEqual(self.RELEASE_VIEW_CONDITION, condition)
        self.assertIn("exit 1", body)
        # Inverting the condition flips never-overwrite into never-create while
        # the anchor string ``gh release view "$TAG"`` stays in place.
        tampered = workflow.replace(
            self.RELEASE_VIEW_CONDITION,
            'if ! gh release view "$TAG" >/dev/null 2>&1; then',
            1,
        )
        with self.assertRaises(AssertionError):
            inverted, _ = parse_release_view_guard(self._publish_slice(tampered))
            self.assertEqual(self.RELEASE_VIEW_CONDITION, inverted)

    def test_publish_needs_all_and_only_required_gates_not_itself(self) -> None:
        workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        publish = self._publish_slice(workflow)
        needs = publish.split("    needs:\n", 1)[1].split("\n    permissions:", 1)[0]
        actual = tuple(line.strip()[2:] for line in needs.splitlines() if line.strip())
        self.assertEqual(self.REQUIRED_GATE_JOBS, actual)
        self.assertNotIn("publish", actual)

    def test_release_workflow_defines_all_jobs_and_required_gate_jobs(self) -> None:
        workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        for job in self.ALL_JOBS:
            with self.subTest(job=job):
                self.assertIn(f"  {job}:\n", workflow)
        self.assertNotIn("publish", self.REQUIRED_GATE_JOBS)
        for command in self.REQUIRED_GATE_COMMANDS:
            with self.subTest(command=command):
                self.assertIn(command, workflow)

    def test_release_workflow_base_loader_shape_and_read_only_dispatch_jobs(self) -> None:
        workflow = yaml.load(
            RELEASE_WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader
        )
        self.assertEqual({"name", "on", "permissions", "concurrency", "jobs"}, set(workflow))
        self.assertIn("workflow_dispatch", workflow["on"])
        self.assertEqual(set(self.ALL_JOBS), set(workflow["jobs"]))
        self.assertEqual("always()", workflow["jobs"]["assemble-evidence"]["if"])
        self.assertEqual(
            list(self.REQUIRED_GATE_JOBS[:-1]),
            workflow["jobs"]["assemble-evidence"]["needs"],
        )
        for name, job in workflow["jobs"].items():
            with self.subTest(job=name):
                permissions = job.get("permissions", {"contents": "read"})
                if name == "publish":
                    self.assertEqual(
                        {"contents": "write", "id-token": "write", "attestations": "write"},
                        permissions,
                    )
                    self.assertEqual(self.PUBLISH_JOB_IF, job["if"])
                else:
                    self.assertEqual({"contents": "read"}, permissions)

    def test_release_bundle_uses_only_the_canonical_verified_export(self) -> None:
        workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        bundle = workflow.split("  release-bundle:\n", 1)[1].split(
            "\n  assemble-evidence:\n", 1
        )[0]
        self.assertIn("TRACEHELIX_VERIFIED_OUTPUT_DIR", bundle)
        canonical_at = bundle.index("bash scripts/verify-release-bundle.sh")
        notes_at = bundle.index("Generate release notes after canonical export")
        self.assertGreater(notes_at, canonical_at)
        self.assertNotIn("build_release_bundle.py", bundle[canonical_at:])
        self.assertIn("Generate source CycloneDX SBOM", bundle[canonical_at:])

    def test_assemble_evidence_has_exact_handoff_and_publish_downloads_only_it(self) -> None:
        workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        assemble = self._assemble_slice(workflow)
        for artifact in (
            "tracehelix-release-evidence",
            "tracehelix-0.1.0-source.cdx.json",
            "tracehelix-api.spdx.json",
            "tracehelix-web.spdx.json",
            self.UNIFIED_ARTIFACT,
        ):
            with self.subTest(artifact=artifact):
                self.assertIn(artifact, assemble)
        self.assertIn("Assert exact artifact set and verify source checksum", assemble)
        self.assertIn("sha256sum -c SHA256SUMS", assemble)
        self.assertIn("VERSION: ${{ needs.validate-tag.outputs.version }}", assemble)
        self.assertNotIn("< VERSION", assemble)
        # The expected set must be the exact ``expected=$(printf ...)`` literal,
        # not just globally present: deleting a name there (while its download
        # step still mentions it) must fail closed.
        self.assertEqual(
            list(self.ASSEMBLE_EXPECTED_SET),
            parse_assemble_expected_set(assemble),
        )
        tampered_set = workflow.replace(
            "tracehelix-api.spdx.json tracehelix-web.spdx.json | LC_ALL=C sort",
            "tracehelix-web.spdx.json | LC_ALL=C sort",
            1,
        )
        with self.assertRaises(AssertionError):
            self.assertEqual(
                list(self.ASSEMBLE_EXPECTED_SET),
                parse_assemble_expected_set(self._assemble_slice(tampered_set)),
            )
        publish = self._publish_slice(workflow)
        self.assertEqual(1, publish.count("actions/download-artifact@"))
        self.assertNotIn("actions/checkout@", publish)

    def test_source_sbom_is_generated_from_a_strictly_reverified_archive(self) -> None:
        workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        bundle = workflow.split("  release-bundle:\n", 1)[1].split(
            "\n  assemble-evidence:\n", 1
        )[0]
        verify_at = bundle.index("Re-verify and extract source for the SBOM")
        sbom_at = bundle.index("Generate source CycloneDX SBOM")
        self.assertLess(verify_at, sbom_at)
        between = bundle[verify_at:sbom_at]
        self.assertIn("scripts/verify_release_bundle.py", between)
        self.assertIn("--checksums", between)
        self.assertIn("--extract-dir", between)
        sbom = bundle[sbom_at:]
        self.assertIn("path: ${{ runner.temp }}/sbom-source/tracehelix-0.1.0", sbom)
        self.assertNotIn("path: .\n", sbom)

    def test_publish_attaches_release_notes_as_an_immutable_asset(self) -> None:
        publish = self._publish_slice(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        create = publish.split('gh release create "$TAG"', 1)[1]
        self.assertIn("--notes-file release/RELEASE-NOTES.md", create)
        self.assertIn("\n            release/RELEASE-NOTES.md \\", create)
        self.assertEqual(2, create.count("release/RELEASE-NOTES.md"))

    def test_publish_uses_immutable_artifact_handoff_without_rebuild(self) -> None:
        self._require_with_mutation(
            RELEASE_WORKFLOW, (self.UPLOAD_ACTION, self.DOWNLOAD_ACTION)
        )
        publish = self._publish_slice(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        self.assertIn(self.DOWNLOAD_ACTION, publish)
        self.assertIn(
            "Download immutable unified release assets",
            publish,
            "publish must consume only the unified assembled artifact",
        )
        self.assertIn(f"name: {self.UNIFIED_ARTIFACT}", publish)
        self.assertNotIn(
            "build_release_bundle.py",
            publish,
            "publish must not rebuild an unaudited bundle",
        )

    def test_release_workflow_includes_the_required_artifact_set(self) -> None:
        self._require_with_mutation(
            RELEASE_WORKFLOW, self.REQUIRED_ARTIFACT_NAMES
        )
        workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        publish = self._publish_slice(workflow)
        # The required set must be the exact ``gh release create`` asset list,
        # not just globally present: dropping one asset from the publish
        # command (while its name still appears in assemble/download steps)
        # must fail closed.
        self.assertEqual(
            list(self.PUBLISH_CREATE_ASSETS),
            parse_release_create_assets(publish),
        )
        for asset in self.PUBLISH_CREATE_ASSETS:
            with self.subTest(asset=asset):
                tampered = self._drop_publish_asset(workflow, asset)
                self.assertNotEqual(workflow, tampered)
                with self.assertRaises(AssertionError):
                    self.assertEqual(
                        list(self.PUBLISH_CREATE_ASSETS),
                        parse_release_create_assets(self._publish_slice(tampered)),
                    )

    def test_release_workflow_creates_checksums_provenance_and_attestation(
        self,
    ) -> None:
        self._require_with_mutation(
            RELEASE_WORKFLOW,
            ("sha256sum -c SHA256SUMS", self.PROVENANCE_ACTION),
        )
        workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        publish = self._publish_slice(workflow)
        # Presence alone cannot hide a publish-only checksum removal (the
        # string also lives in assemble) or a provenance subject swap (the
        # replacement path appears elsewhere in publish), so both are asserted
        # inside the publish slice.
        self.assertEqual(
            self.PROVENANCE_SUBJECT,
            parse_provenance_subject_path(publish),
        )
        self.assertIn("sha256sum -c SHA256SUMS", publish)
        tampered_subject = workflow.replace(
            "subject-path: " + self.PROVENANCE_SUBJECT,
            "subject-path: release/RELEASE-NOTES.md",
            1,
        )
        with self.assertRaises(AssertionError):
            self.assertEqual(
                self.PROVENANCE_SUBJECT,
                parse_provenance_subject_path(self._publish_slice(tampered_subject)),
            )
        tampered_checksum = workflow.replace(
            "( cd release && sha256sum -c SHA256SUMS )",
            "( cd release && true )",
            1,
        )
        with self.assertRaises(AssertionError):
            self.assertIn(
                "sha256sum -c SHA256SUMS",
                self._publish_slice(tampered_checksum),
            )


class ReleasePolicyGuardTests(unittest.TestCase):
    """Guard docs/release-policy.md for honest, fail-closed release claims."""

    REQUIRED_ANCHORS = (
        "docs/release-policy.md",
        "fail-closed",
        "local trusted single-user",
        "not production-grade",
        "tag equals VERSION",
        "never overwrite",
        "contents: read",
        "contents: write, id-token: write, attestations: write",
        "workflow_dispatch",
        "never publishes",
        "immutable",
        "download-artifact",
        "CycloneDX",
        "SPDX",
        "provenance",
        "attestation",
        "public-download verification",
        "no release has been created",
    )
    FORBIDDEN_OVERCLAIM_PHRASES = (
        "is production-grade",
        "publicly available",
        "has been published",
    )

    def _require_with_mutation(
        self, path: Path, anchors: tuple[str, ...]
    ) -> None:
        text = path.read_text(encoding="utf-8")
        require_anchors(text, anchors, str(path))
        for anchor in anchors:
            with self.subTest(path=path.name, anchor=anchor):
                tampered = text.replace(anchor, TAMPER_TOKEN)
                self.assertNotEqual(text, tampered)
                with self.assertRaises(AssertionError):
                    require_anchors(tampered, anchors, str(path))

    def test_release_policy_file_is_present(self) -> None:
        self.assertTrue(
            RELEASE_POLICY_MD.is_file(), "missing docs/release-policy.md"
        )

    def test_release_policy_preserves_required_anchors(self) -> None:
        self._require_with_mutation(RELEASE_POLICY_MD, self.REQUIRED_ANCHORS)

    def test_release_policy_makes_no_production_or_publication_overclaim(self) -> None:
        text = RELEASE_POLICY_MD.read_text(encoding="utf-8")
        forbid_phrases(text, self.FORBIDDEN_OVERCLAIM_PHRASES, str(RELEASE_POLICY_MD))
        for phrase in self.FORBIDDEN_OVERCLAIM_PHRASES:
            with self.subTest(phrase=phrase):
                with self.assertRaises(AssertionError):
                    forbid_phrases(text + phrase, self.FORBIDDEN_OVERCLAIM_PHRASES, str(RELEASE_POLICY_MD))


if __name__ == "__main__":
    unittest.main()
