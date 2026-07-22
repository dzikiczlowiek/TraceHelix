#!/usr/bin/env python3
"""Fail unless Docker image and CI/release action/tool references match approved pins.

Both ``.github/workflows/ci.yml`` and ``.github/workflows/release.yml`` are checked
against their own exact ordered full-SHA action allowlists and exact runtime/tool
pin line counts, with the same fail-closed rules: every ``uses`` key must be a
canonical plain mapping key referencing an approved immutable
commit SHA, no YAML escape sequences are allowed, and no runner/runtime selector
may use a mutable ``latest`` label. The summary explicitly reports the release
workflow action coverage.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
import re
import shlex


ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
EXPECTED_SYNTAX = (
    "docker/dockerfile:1.7@sha256:"
    "a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e"
)
EXPECTED_EXTERNAL_IMAGES = [
    "mcr.microsoft.com/dotnet/sdk:10.0.302@sha256:ed034a8bf0b24ded0cbbac07e17825d8e9ebfe21e308191d0f7421eaf5ad4664",
    "mcr.microsoft.com/dotnet/aspnet:10.0.10@sha256:1fa23fc4872d95fd71c2833ebe65d7e84a43b2d51a31d119516852f13d9505a7",
    "node:24-alpine@sha256:a0b9bf06e4e6193cf7a0f58816cc935ff8c2a908f81e6f1a95432d679c54fbfd",
    "nginxinc/nginx-unprivileged:stable-alpine@sha256:dcea25a6593307a74b09e59a47f8695c4d56943750e45add532ae0bf8b24bfd6",
]
EXPECTED_CI_ACTIONS = [
    "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd",
    "actions/setup-dotnet@d4c94342e560b34958eacfc5d055d21461ed1c5d",
    "astral-sh/setup-uv@1e862dfacbd1d6d858c55d9b792c756523627244",
    "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd",
    "actions/setup-dotnet@d4c94342e560b34958eacfc5d055d21461ed1c5d",
    "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd",
    "anchore/sbom-action@17ae1740179002c89186b61233e0f892c3118b11",
    "anchore/sbom-action@17ae1740179002c89186b61233e0f892c3118b11",
    "aquasecurity/trivy-action@ed142fd0673e97e23eac54620cfb913e5ce36c25",
    "aquasecurity/trivy-action@ed142fd0673e97e23eac54620cfb913e5ce36c25",
    "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd",
    "actions/setup-dotnet@d4c94342e560b34958eacfc5d055d21461ed1c5d",
    "actions/setup-node@2028fbc5c25fe9cf00d9f06a71cc4710d4507903",
    "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd",
    "actions/setup-node@2028fbc5c25fe9cf00d9f06a71cc4710d4507903",
    "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd",
    "actions/setup-node@2028fbc5c25fe9cf00d9f06a71cc4710d4507903",
    "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd",
    "astral-sh/setup-uv@1e862dfacbd1d6d858c55d9b792c756523627244",
]
# Exact ordered full-SHA action allowlist for the release workflow. Every entry
# is a full-length immutable commit SHA; mutable tags (@vN) are forbidden.
EXPECTED_RELEASE_ACTIONS = [
    "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd",
    "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd",
    "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd",
    "actions/setup-dotnet@d4c94342e560b34958eacfc5d055d21461ed1c5d",
    "astral-sh/setup-uv@1e862dfacbd1d6d858c55d9b792c756523627244",
    "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd",
    "actions/setup-dotnet@d4c94342e560b34958eacfc5d055d21461ed1c5d",
    "actions/setup-node@2028fbc5c25fe9cf00d9f06a71cc4710d4507903",
    "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd",
    "astral-sh/setup-uv@1e862dfacbd1d6d858c55d9b792c756523627244",
    "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd",
    "actions/setup-dotnet@d4c94342e560b34958eacfc5d055d21461ed1c5d",
    "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd",
    "anchore/sbom-action@17ae1740179002c89186b61233e0f892c3118b11",
    "anchore/sbom-action@17ae1740179002c89186b61233e0f892c3118b11",
    "aquasecurity/trivy-action@ed142fd0673e97e23eac54620cfb913e5ce36c25",
    "aquasecurity/trivy-action@ed142fd0673e97e23eac54620cfb913e5ce36c25",
    "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd",
    "actions/setup-node@2028fbc5c25fe9cf00d9f06a71cc4710d4507903",
    "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd",
    "actions/setup-node@2028fbc5c25fe9cf00d9f06a71cc4710d4507903",
    "anchore/sbom-action@17ae1740179002c89186b61233e0f892c3118b11",
    "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
    "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd",
    "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093",
    "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093",
    "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093",
    "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093",
    "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
    "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd",
    "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093",
    "actions/attest-build-provenance@e8998f949152b193b063cb0ec769d69d929409be",
]
EXPECTED_CI_LINES = Counter(
    {
        "os: [ubuntu-24.04, macos-15, windows-2025]": 1,
        "runs-on: ${{ matrix.os }}": 1,
        "runs-on: ubuntu-24.04": 6,
        "version: 0.11.29": 2,
        "python-version: '3.11.15'": 2,
        "syft-version: v1.42.3": 2,
        "version: v0.72.0": 2,
        "node-version: 24.18.0": 3,
    }
)
EXPECTED_RELEASE_LINES = Counter(
    {
        "runs-on: ubuntu-24.04": 11,
        "version: 0.11.29": 2,
        "python-version: '3.11.15'": 2,
        "syft-version: v1.42.3": 3,
        "version: v0.72.0": 2,
        "node-version: 24.18.0": 3,
    }
)
SYNTAX = re.compile(r"^#\s*syntax=(?P<reference>\S+)\s*$", re.IGNORECASE)
USES = re.compile(
    r"^\s*uses:\s*(?P<reference>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[^\s#]+)\s*(?:#.*)?$"
)
USES_TOKEN = re.compile(r"(?<![A-Za-z0-9_])uses(?![A-Za-z0-9_])")
YAML_ESCAPE = re.compile(r"\\(?:u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}|x[0-9a-fA-F]{2})")
SELECTOR_LINE = re.compile(
    r"^(?:\s*(?:runs-on|node-version|python-version|syft-version|os):|\s{10}version:)\s*[^#\n]+?(?:\s+#.*)?$"
)
FULL_SHA_ACTION = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}$")


def logical_instructions(lines: list[str]) -> list[tuple[int, str]]:
    instructions: list[tuple[int, str]] = []
    buffer = ""
    start = 0
    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not buffer and (not stripped or stripped.startswith("#")):
            continue
        if not buffer:
            start = line_number
        continued = line.rstrip().endswith("\\")
        fragment = line.rstrip()[:-1] if continued else line
        buffer = f"{buffer} {fragment.strip()}".strip()
        if not continued:
            instructions.append((start, buffer))
            buffer = ""
    if buffer:
        raise ValueError(f"Dockerfile:{start}: unterminated line continuation")
    return instructions


def parse_parts(instruction: str, line_number: int) -> list[str]:
    try:
        return shlex.split(instruction, comments=False, posix=True)
    except ValueError as error:
        raise ValueError(f"Dockerfile:{line_number}: malformed instruction: {error}") from error


def parse_from(parts: list[str], line_number: int) -> tuple[str, str | None]:
    index = 1
    while index < len(parts) and parts[index].startswith("--"):
        if "=" not in parts[index]:
            raise ValueError(f"Dockerfile:{line_number}: malformed FROM option: {parts[index]}")
        index += 1
    if index >= len(parts):
        raise ValueError(f"Dockerfile:{line_number}: FROM has no image reference")
    image = parts[index]
    remaining = parts[index + 1 :]
    if not remaining:
        return image, None
    if len(remaining) == 2 and remaining[0].upper() == "AS":
        return image, remaining[1]
    raise ValueError(f"Dockerfile:{line_number}: unsupported FROM syntax")


def validate_stage_reference(
    reference: str, line_number: int, known_stages: set[str], failures: list[str]
) -> None:
    if reference in known_stages:
        return
    if reference not in EXPECTED_EXTERNAL_IMAGES:
        failures.append(
            f"Dockerfile:{line_number}: external stage reference must be a known stage "
            f"or an approved digest-pinned image: {reference}"
        )


def verify_dockerfile(failures: list[str]) -> tuple[int, int]:
    lines = DOCKERFILE.read_text(encoding="utf-8").splitlines()
    syntax = SYNTAX.fullmatch(lines[0]) if lines else None
    actual_syntax = syntax.group("reference") if syntax else None
    if actual_syntax != EXPECTED_SYNTAX:
        failures.append(f"Dockerfile:1: syntax frontend must exactly match {EXPECTED_SYNTAX}")

    try:
        instructions = logical_instructions(lines)
    except ValueError as error:
        failures.append(str(error))
        instructions = []

    known_stages: set[str] = set()
    external_images: list[str] = []
    stage_references = 0
    for line_number, instruction in instructions:
        try:
            parts = parse_parts(instruction, line_number)
        except ValueError as error:
            failures.append(str(error))
            continue
        if not parts:
            continue
        operation = parts[0].upper()
        if operation == "FROM":
            try:
                image, stage = parse_from(parts, line_number)
            except ValueError as error:
                failures.append(str(error))
                continue
            if image not in known_stages:
                external_images.append(image)
            if stage is not None:
                known_stages.add(stage)
            continue
        if operation == "COPY":
            references = [part.split("=", 1)[1] for part in parts[1:] if part.startswith("--from=")]
            if any(part == "--from" for part in parts[1:]):
                failures.append(f"Dockerfile:{line_number}: COPY --from must use --from=<reference>")
            if len(references) > 1:
                failures.append(f"Dockerfile:{line_number}: COPY has multiple --from options")
            for reference in references:
                stage_references += 1
                validate_stage_reference(reference, line_number, known_stages, failures)
            continue
        if operation == "RUN":
            for part in parts[1:]:
                if not part.startswith("--mount="):
                    continue
                options = dict(
                    item.split("=", 1) for item in part.removeprefix("--mount=").split(",") if "=" in item
                )
                reference = options.get("from")
                if reference:
                    stage_references += 1
                    validate_stage_reference(reference, line_number, known_stages, failures)

    if external_images != EXPECTED_EXTERNAL_IMAGES:
        failures.append(
            "Dockerfile external image references differ from the approved ordered allowlist.\n"
            f"Expected: {EXPECTED_EXTERNAL_IMAGES}\nActual:   {external_images}"
        )
    return len(external_images), stage_references


def verify_workflow(
    path: Path,
    expected_actions: list[str],
    expected_lines: Counter,
    label: str,
    failures: list[str],
) -> int:
    """Verify one workflow against its exact ordered action and line allowlist.

    Applies the same fail-closed rules to ``ci.yml`` and ``release.yml``: every
    ``uses`` must be a canonical plain mapping key, no YAML
    escape sequences, no mutable runner/runtime ``latest`` labels, and the
    ordered action references and exact pin-line counts must match exactly.
    """
    if not path.is_file():
        failures.append(f"{path.name}: required workflow is missing")
        return 0
    workflow = path.read_text(encoding="utf-8")
    actual_actions: list[str] = []
    for line_number, line in enumerate(workflow.splitlines(), start=1):
        code = line.split("#", 1)[0]
        if YAML_ESCAPE.search(code):
            failures.append(
                f"{path.name}:{line_number}: YAML escape sequences are forbidden by the canonical workflow policy."
            )
        match = USES.fullmatch(code)
        token_count = len(USES_TOKEN.findall(code))
        if match is None and token_count:
            failures.append(
                f"{path.name}:{line_number}: unsupported uses key syntax; use only a canonical plain '- uses:' action step."
            )
        if match is not None:
            actual_actions.append(match.group("reference"))
    if actual_actions != expected_actions:
        failures.append(
            f"{label} action references differ from the approved ordered SHA allowlist.\n"
            f"Expected: {expected_actions}\nActual:   {actual_actions}"
        )
    selector_lines = Counter(
        line.strip()
        for line in workflow.splitlines()
        if SELECTOR_LINE.fullmatch(line.split("#", 1)[0])
    )
    if selector_lines != expected_lines:
        failures.append(
            f"{path.name} runtime/tool selectors differ from the approved exact allowlist.\n"
            f"Expected: {expected_lines}\nActual:   {selector_lines}"
        )
    for reference in actual_actions:
        if FULL_SHA_ACTION.fullmatch(reference) is None:
            failures.append(
                f"{path.name}: action reference is not an immutable full commit SHA: {reference}"
            )
    return len(actual_actions)


def main() -> None:
    failures: list[str] = []
    image_count, stage_reference_count = verify_dockerfile(failures)
    ci_action_count = verify_workflow(
        CI_WORKFLOW, EXPECTED_CI_ACTIONS, EXPECTED_CI_LINES, "CI", failures
    )
    release_action_count = verify_workflow(
        RELEASE_WORKFLOW,
        EXPECTED_RELEASE_ACTIONS,
        EXPECTED_RELEASE_LINES,
        "Release",
        failures,
    )
    if failures:
        raise SystemExit("\n".join(failures))
    print(
        "Dependency pin verification passed "
        f"(1 syntax frontend, {image_count} external images, "
        f"{stage_reference_count} stage references, "
        f"{ci_action_count} CI actions, "
        f"{release_action_count} release actions)."
    )


if __name__ == "__main__":
    main()
