#!/usr/bin/env python3
"""Regression tests for repository-level reproducibility and supply-chain guards."""

from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
