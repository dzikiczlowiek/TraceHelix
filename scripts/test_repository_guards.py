#!/usr/bin/env python3
"""Regression tests for repository-level reproducibility and supply-chain guards."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import tomllib
import unittest
import xml.etree.ElementTree as ET



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

    RESOLVER = "resolver 127.0.0.11 valid=1s ipv6=off;"
    API_VARIABLE = "set $tracehelix_api api:5080;"
    API_PROXY_PASS = "proxy_pass http://$tracehelix_api$request_uri;"
    API_LOCATION = "location ~ ^/(api|health)/ {"
    API_SERVER_NAME = "server_name 127.0.0.1 localhost;"
    BLOCKER_REUSES_OLD_IP = '[[ "$blocker_ip" == "$old_api_ip" ]]'
    NEW_API_USES_NEW_IP = '[[ -n "$new_api_ip" && "$new_api_ip" != "$old_api_ip" ]]'
    WEB_ID_UNCHANGED = '[[ "$web_id_after_api_recreate" == "$web_id" ]]'
    # Intentional lifecycle edits require review and an explicit digest update.
    EXPECTED_LIFECYCLE_SHA256 = (
        "bc04e2f9c23c0dcd5feff2877d56e7fc6c53ed3d2fd26abedcbbda45f3d1359b"
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

    def _all_direct_block_lines(
        self, nginx_lines: list[str], opening: str
    ) -> list[list[str]]:
        """Return direct active directives for every exact nginx block opening."""
        blocks: list[list[str]] = []
        for start, line in enumerate(nginx_lines):
            if line != opening:
                continue
            depth = self._brace_delta(opening)
            self.assertEqual(1, depth, f"Malformed nginx block opening: {opening!r}")
            direct: list[str] = []
            for child in nginx_lines[start + 1 :]:
                next_depth = depth + self._brace_delta(child)
                if depth == 1 and next_depth >= 1:
                    direct.append(child)
                depth = next_depth
                if depth == 0:
                    blocks.append(direct)
                    break
            else:
                self.fail(f"Unclosed active nginx block: {opening!r}")
        return blocks

    def _direct_block_lines(
        self, nginx_lines: list[str], opening: str
    ) -> list[str]:
        blocks = self._all_direct_block_lines(nginx_lines, opening)
        self.assertEqual(1, len(blocks), f"Expected one active nginx block: {opening!r}")
        return blocks[0]

    def _assert_nginx_invariants(self, nginx_text: str) -> None:
        nginx_lines = self._active_lines(nginx_text)
        top_level_lines = self._top_level_lines(nginx_lines)
        resolver_lines = [line for line in nginx_lines if line.startswith("resolver ")]
        self.assertEqual(
            [self.RESOLVER],
            resolver_lines,
            "Require exactly one active, canonical Docker DNS resolver; nested or decoy resolvers are forbidden",
        )
        self.assertEqual(
            [self.RESOLVER],
            [line for line in top_level_lines if line.startswith("resolver ")],
            "Docker DNS resolver must be active at http/top-level scope",
        )
        self.assertFalse(
            any(line.startswith("upstream ") for line in nginx_lines),
            "The stale upstream resolve form is forbidden; proxy through the runtime DNS variable instead",
        )
        self.assertEqual(
            [self.API_VARIABLE],
            [line for line in nginx_lines if line.startswith("set ")],
            "Require exactly one active API runtime DNS variable",
        )
        self.assertEqual(
            [self.API_PROXY_PASS],
            [line for line in nginx_lines if line.startswith("proxy_pass ")],
            "Require exactly one API proxy_pass preserving the original request URI",
        )

        api_server_blocks = [
            direct
            for direct in self._all_direct_block_lines(nginx_lines, "server {")
            if self.API_SERVER_NAME in direct and self.API_LOCATION in direct
        ]
        self.assertEqual(
            1,
            len(api_server_blocks),
            "API proxy location must be a direct child of the allowed-host server block",
        )
        location_direct = self._direct_block_lines(nginx_lines, self.API_LOCATION)
        required_location_directives = (
            self.API_VARIABLE,
            self.API_PROXY_PASS,
            "proxy_http_version 1.1;",
            "proxy_set_header Host $http_host;",
            "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
            "proxy_set_header X-Forwarded-Proto $scheme;",
            "proxy_hide_header Server;",
        )
        for directive in required_location_directives:
            self.assertIn(
                directive,
                location_direct,
                f"Missing required direct API proxy directive: {directive!r}",
            )

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

    def test_commented_nginx_runtime_dns_invariants_are_rejected(self) -> None:
        nginx_text = NGINX_CONF.read_text(encoding="utf-8")
        for needle in (self.RESOLVER, self.API_VARIABLE, self.API_PROXY_PASS):
            with self.subTest(needle=needle):
                mutated = self._comment_out(nginx_text, needle)
                with self.assertRaises(AssertionError):
                    self._assert_nginx_invariants(mutated)

    def test_lifecycle_digest_rejects_semantic_weakening(self) -> None:
        lifecycle_text = COMPOSE_LIFECYCLE.read_text(encoding="utf-8")
        blocker = self.BLOCKER_REUSES_OLD_IP
        new_api = self.NEW_API_USES_NEW_IP
        web_id = self.WEB_ID_UNCHANGED
        mutations = {
            "comment-blocker-assertion": self._comment_out(lifecycle_text, blocker),
            "comment-new-api-assertion": self._comment_out(lifecycle_text, new_api),
            "comment-web-identity-assertion": self._comment_out(lifecycle_text, web_id),
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
            "unreachable-web-identity-assertion": lifecycle_text.replace(
                web_id,
                f"if false; then\n  {web_id}\nfi",
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

    def test_runtime_dns_structure_rejects_stale_decoys_and_relocation(self) -> None:
        quoted_line = 'guard "# not-comment }"; # real comment'
        self.assertEqual(
            'guard "# not-comment }";', self._strip_unquoted_comment(quoted_line)
        )
        self.assertEqual(0, self._brace_delta(self._strip_unquoted_comment(quoted_line)))

        nginx_text = NGINX_CONF.read_text(encoding="utf-8")
        nested_resolver = nginx_text.replace(
            self.RESOLVER + "\n",
            "server {\n    " + self.RESOLVER + "\n}\n",
            1,
        )
        variable_outside_location = nginx_text.replace(
            "        " + self.API_VARIABLE + "\n", "", 1
        ).replace(
            self.API_SERVER_NAME + "\n",
            self.API_SERVER_NAME + "\n    " + self.API_VARIABLE + "\n",
            1,
        )
        mutations = {
            "active-stale-upstream": nginx_text
            + "\nupstream tracehelix_api {\n    server api:5080 resolve;\n}\n",
            "second-resolver-decoy": nginx_text
            + "\nresolver 127.0.0.11 valid=5s ipv6=off;\n",
            "nested-resolver": nested_resolver,
            "variable-outside-api-location": variable_outside_location,
            "altered-proxy-uri": nginx_text.replace(
                self.API_PROXY_PASS, "proxy_pass http://$tracehelix_api;", 1
            ),
            "altered-api-location": nginx_text.replace(
                self.API_LOCATION, "location ~ ^/api/ {", 1
            ),
            "dropped-host-forwarding": self._comment_out(
                nginx_text, "proxy_set_header Host $http_host;"
            ),
        }
        for label, mutated in mutations.items():
            with self.subTest(label=label):
                with self.assertRaises(AssertionError):
                    self._assert_nginx_invariants(mutated)

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


class WorkflowContractGuardTests(unittest.TestCase):
    """Execute the strict parsed-workflow contract, not textual workflow scans."""

    def test_repository_workflow_contract(self) -> None:
        result = subprocess.run(
            ["python3", str(ROOT / "scripts" / "workflow_contract.py")],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_release_asset_behavior_and_adversarial_mutation_harness_are_green(self) -> None:
        result = subprocess.run(
            ["python3", "scripts/adversarial_release_mutations.py"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_semantic_digests_match_reviewed_workflow_pins(self) -> None:
        import workflow_contract

        ci = workflow_contract.load_strict(WORKFLOW.read_text(encoding="utf-8"))
        release = workflow_contract.load_strict(
            RELEASE_WORKFLOW.read_text(encoding="utf-8")
        )
        self.assertEqual(
            workflow_contract.CI_SEMANTIC_SHA256,
            workflow_contract.semantic_digest(ci),
        )
        self.assertEqual(
            workflow_contract.RELEASE_SEMANTIC_SHA256,
            workflow_contract.semantic_digest(release),
        )

    def test_workflow_contract_cli_rejects_lone_surrogate_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tracehelix-workflow-surrogate-") as tmp:
            root = Path(tmp)
            scripts = root / "scripts"
            workflows = root / ".github" / "workflows"
            scripts.mkdir()
            workflows.mkdir(parents=True)
            shutil.copy2(ROOT / "scripts" / "workflow_contract.py", scripts)
            ci = WORKFLOW.read_text(encoding="utf-8").replace(
                "name: CI", 'name: "\\ud83d evil"', 1
            )
            self.assertNotEqual(WORKFLOW.read_text(encoding="utf-8"), ci)
            (workflows / "ci.yml").write_text(ci, encoding="utf-8")
            shutil.copy2(RELEASE_WORKFLOW, workflows / "release.yml")

            result = subprocess.run(
                ["python3", str(scripts / "workflow_contract.py")],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertEqual("", result.stdout)
        self.assertIn("workflow_contract:", result.stderr)
        self.assertIn("workflow semantics are not hashable", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("UnicodeEncodeError", result.stderr)

    def test_strict_loader_rejects_duplicate_keys_at_any_mapping_depth(self) -> None:
        import workflow_contract

        for text in ("name: one\nname: two\n", "a:\n  b: one\n  b: two\n"):
            with self.subTest(text=text):
                with self.assertRaises(workflow_contract.WorkflowContractError):
                    workflow_contract.load_strict(text)

    def test_strict_loader_rejects_duplicate_subject_path_last_wins(self) -> None:
        import workflow_contract

        text = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        needle = "          subject-path: release/tracehelix-${{ needs.validate-tag.outputs.version }}-source.tar.gz\n"
        mutated = text.replace(needle, needle + "          subject-path: release/RELEASE-NOTES.md\n", 1)
        with self.assertRaises(workflow_contract.WorkflowContractError):
            workflow_contract.load_strict(mutated)

    def test_contract_rejects_dead_code_decoy_and_merge_checkout(self) -> None:
        import workflow_contract

        release = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        ci = WORKFLOW.read_text(encoding="utf-8")
        mutations = (
            release.replace(
                'python3 scripts/release_assets.py verify-publish --directory release --version "$VERSION" --tag "$TAG" --expected-digest "$HANDOFF_DIGEST"',
                'if false; then python3 scripts/release_assets.py verify-publish --directory release --version "$VERSION" --tag "$TAG" --expected-digest "$HANDOFF_DIGEST"; fi',
                1,
            ),
            release.replace(
                'python3 scripts/release_assets.py create-release --directory release --version "$VERSION" --tag "$TAG"',
                'gh release create "$TAG" -- release/EXTRA-ASSET',
                1,
            ),
        )
        for mutated in mutations:
            with self.subTest(mutated=mutated[-80:]):
                with self.assertRaises(workflow_contract.WorkflowContractError):
                    workflow_contract.validate_release(workflow_contract.load_strict(mutated))
        merge_ref = ci.replace(workflow_contract.CHECKOUT_REF, "${{ github.sha }}", 1)
        with self.assertRaises(workflow_contract.WorkflowContractError):
            workflow_contract.validate_ci(workflow_contract.load_strict(merge_ref))


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
