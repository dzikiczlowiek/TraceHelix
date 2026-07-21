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
README = ROOT / "README.md"
ARCHITECTURE = ROOT / "docs" / "architecture.md"

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
        ignored = subprocess.run(
            ["git", "check-ignore", "--quiet", "--no-index", "imports/private-trace.jsonl"],
            cwd=ROOT,
            check=False,
        )
        placeholder = subprocess.run(
            ["git", "check-ignore", "--quiet", "--no-index", "imports/.gitkeep"],
            cwd=ROOT,
            check=False,
        )
        self.assertEqual(0, ignored.returncode)
        self.assertNotEqual(0, placeholder.returncode)


class ContainerPinVerifierTests(unittest.TestCase):
    def run_verifier(
        self, dockerfile: str, workflow: str | None = None
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


class CriticalInvariantTests(unittest.TestCase):
    """Guard reviewed DNS configuration and lifecycle script bytes."""

    RESOLVER = "resolver 127.0.0.11 valid=5s ipv6=off;"
    ZONE = "zone tracehelix_api 64k;"
    RESOLVABLE_SERVER = "server api:5080 resolve;"
    BLOCKER_REUSES_OLD_IP = '[[ "$blocker_ip" == "$old_api_ip" ]]'
    NEW_API_USES_NEW_IP = '[[ -n "$new_api_ip" && "$new_api_ip" != "$old_api_ip" ]]'
    # Intentional lifecycle edits require review and an explicit digest update.
    EXPECTED_LIFECYCLE_SHA256 = (
        "7281d33200e9fd0e44c35e874fb1ca37e5cd94ca79a54bf33fa1489eaf3f1716"
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


if __name__ == "__main__":
    unittest.main()
