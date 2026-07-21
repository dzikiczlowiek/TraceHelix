#!/usr/bin/env python3
"""Regression tests for repository-level reproducibility and supply-chain guards."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
FINGERPRINT = ROOT / "scripts" / "source_fingerprint.py"
PIN_VERIFIER = ROOT / "scripts" / "verify_container_pins.py"
DOCKERFILE = ROOT / "Dockerfile"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
NGINX_CONF = ROOT / "deploy" / "nginx.conf"
COMPOSE_LIFECYCLE = ROOT / "scripts" / "verify-compose-lifecycle.sh"


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


if __name__ == "__main__":
    unittest.main()
