#!/usr/bin/env python3
"""Fail unless Docker image and CI action/tool references match approved pins."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
import re
import shlex


ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
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
EXPECTED_ACTIONS = [
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
    "astral-sh/setup-uv@1e862dfacbd1d6d858c55d9b792c756523627244",
]
EXPECTED_CI_LINES = Counter(
    {
        "os: [ubuntu-24.04, macos-15, windows-2025]": 1,
        "runs-on: ubuntu-24.04": 4,
        "version: 0.11.29": 2,
        "python-version: '3.11.15'": 2,
        "syft-version: v1.42.3": 2,
        "version: v0.72.0": 2,
        "node-version: 24.18.0": 1,
    }
)
SYNTAX = re.compile(r"^#\s*syntax=(?P<reference>\S+)\s*$", re.IGNORECASE)
USES = re.compile(
    r"(?<![A-Za-z0-9_])(?P<quote>[\"']?)uses(?P=quote)\s*:\s*(?P<reference>[^\s,}\]]+)",
)
USES_TOKEN = re.compile(r"(?<![A-Za-z0-9_])uses(?![A-Za-z0-9_])")
YAML_ESCAPE = re.compile(r"\\(?:u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}|x[0-9a-fA-F]{2})")


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


def verify_workflow(failures: list[str]) -> int:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    actual_actions: list[str] = []
    for line_number, line in enumerate(workflow.splitlines(), start=1):
        code = line.split("#", 1)[0]
        if YAML_ESCAPE.search(code):
            failures.append(
                f"ci.yml:{line_number}: YAML escape sequences are forbidden by the canonical workflow policy."
            )
        matches = list(USES.finditer(code))
        token_count = len(USES_TOKEN.findall(code))
        if len(matches) != token_count:
            failures.append(
                f"ci.yml:{line_number}: unsupported uses key syntax; use a canonical plain or quoted mapping key."
            )
        actual_actions.extend(match.group("reference") for match in matches)
    if actual_actions != EXPECTED_ACTIONS:
        failures.append(
            "CI action references differ from the approved ordered SHA allowlist.\n"
            f"Expected: {EXPECTED_ACTIONS}\nActual:   {actual_actions}"
        )
    stripped_lines = Counter(line.strip() for line in workflow.splitlines())
    for line, expected_count in EXPECTED_CI_LINES.items():
        actual_count = stripped_lines[line]
        if actual_count != expected_count:
            failures.append(
                f"CI exact pin line {line!r} must occur {expected_count} time(s); found {actual_count}."
            )
    if re.search(r"(?:runs-on:|node-version:|python-version:)\s*[^#\n]*latest", workflow):
        failures.append("CI runner and runtime selectors must not use mutable 'latest' labels.")
    return len(actual_actions)


def main() -> None:
    failures: list[str] = []
    image_count, stage_reference_count = verify_dockerfile(failures)
    action_count = verify_workflow(failures)
    if failures:
        raise SystemExit("\n".join(failures))
    print(
        "Dependency pin verification passed "
        f"(1 syntax frontend, {image_count} external images, "
        f"{stage_reference_count} stage references, {action_count} CI actions)."
    )


if __name__ == "__main__":
    main()
