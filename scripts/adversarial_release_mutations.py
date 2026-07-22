#!/usr/bin/env python3
"""Bounded regression harness for the release-parser bypass classes."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import workflow_contract as contract  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github/workflows/ci.yml"
RELEASE = ROOT / ".github/workflows/release.yml"


def rejected(label: str, text: str, *, ci: bool = False) -> None:
    try:
        validator = contract.validate_ci if ci else contract.validate_release
        validator(contract.load_strict(text))
    except contract.WorkflowContractError:
        print(f"rejected: {label}")
        return
    raise RuntimeError(f"accepted adversarial mutation: {label}")


def main() -> int:
    source = RELEASE.read_text(encoding="utf-8")
    ci_source = CI.read_text(encoding="utf-8")
    verify = 'python3 scripts/release_assets.py verify-publish --directory release --version "$VERSION" --tag "$TAG" --expected-digest "$HANDOFF_DIGEST"'
    create = 'python3 scripts/release_assets.py create-release --directory release --version "$VERSION" --tag "$TAG" --expected-digest "$HANDOFF_DIGEST"'
    subject = "          subject-path: release/tracehelix-${{ needs.validate-tag.outputs.version }}-source.tar.gz\n"
    cases = {
        "duplicate YAML subject-path last-wins": source.replace(subject, subject + "          subject-path: release/RELEASE-NOTES.md\n", 1),
        "commented checksum helper": source.replace(verify, f"# {verify}\n          true", 1),
        "dead-code exit/empty-dir guard": source.replace(verify, f"if false; then {verify}; fi", 1),
        "reassigned expected set": source.replace('python3 scripts/release_assets.py assemble --directory unified --version "$VERSION" --github-output "$GITHUB_OUTPUT"', "expected=decoy\n          expected=weakened", 1),
        "never-overwrite inversion": source.replace('python3 scripts/release_assets.py assert-release-absent --tag "$TAG"', 'if ! gh release view "$TAG"; then true; fi', 1),
        "decoy gh release create": source.replace(create, f"gh release create \"$TAG\" --verify-tag -- release/EXTRA-ASSET\n          # {create}", 1),
        "extra -- asset": source.replace(create, f"{create} -- release/EXTRA-ASSET", 1),
        "release e2e gate no-op": source.replace("        run: make verify-e2e", "        run: true", 1),
        "release repository guards no-op": source.replace(
            "        run: python scripts/test_repository_guards.py",
            "        run: true",
            1,
        ),
        "assemble prerequisite failure no-op": source.replace(
            "              exit 1\n            }\n          done",
            "              true\n            }\n          done",
            1,
        ),
        "release cancellation enabled": source.replace(
            "  cancel-in-progress: false", "  cancel-in-progress: true", 1
        ),
    }
    ci_cases = {
        "CI dotnet test no-op": ci_source.replace(
            "        run: dotnet test TraceHelix.slnx --configuration Release --no-build --no-restore",
            "        run: true",
            1,
        ),
        "CI injected write-permission job": ci_source
        + "\n  injected-permission:\n"
        + "    runs-on: ubuntu-24.04\n"
        + "    permissions:\n"
        + "      issues: write\n"
        + "    steps:\n"
        + "      - run: true\n",
    }
    try:
        for label, mutated in cases.items():
            rejected(label, mutated)
        for label, mutated in ci_cases.items():
            rejected(label, mutated, ci=True)
        # The real-process asset suite covers reassigned exclusive flags,
        # nested unified content, and every altered downloaded asset (including
        # SHA256SUMS) rather than inspecting implementation text.
        result = subprocess.run(
            [sys.executable, "scripts/test_release_assets.py"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        print(result.stdout, end="")
        if result.returncode:
            print(result.stderr, end="", file=sys.stderr)
            return result.returncode
        print("rejected by behavior suite: omitted canonical asset, exclusive-create reassignment/race, checksum mutation, non-empty export, nested unified artifact, altered downloaded asset digest")
    except RuntimeError as exc:
        print(f"adversarial_release_mutations: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
