#!/usr/bin/env python3
"""Strict YAML loading and semantic contracts for CI and release workflows."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github/workflows/ci.yml"
RELEASE = ROOT / ".github/workflows/release.yml"
CHECKOUT_REF = "${{ github.event.pull_request.head.sha || github.sha }}"
RELEASE_CHECKOUT_REF = "${{ github.sha }}"
CI_SEMANTIC_SHA256 = "3bb0ed87541243a046cc0ae4102cf30647322bea23966bfd4a6f56f47be2928e"
RELEASE_SEMANTIC_SHA256 = "b5cf4e35ae7bd24152b8c3422bc6ed3b70c9ed7ba5cfca38398f252dca47cf6c"
SHA = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}$")


class WorkflowContractError(Exception):
    pass


class StrictLoader(yaml.SafeLoader):
    """Safe YAML loader that preserves ``on`` and rejects every duplicate key."""


StrictLoader.yaml_implicit_resolvers = copy.deepcopy(yaml.SafeLoader.yaml_implicit_resolvers)
for initial, resolvers in StrictLoader.yaml_implicit_resolvers.items():
    StrictLoader.yaml_implicit_resolvers[initial] = [
        resolver for resolver in resolvers if resolver[0] != "tag:yaml.org,2002:bool"
    ]


def _mapping(loader: StrictLoader, node: yaml.MappingNode, deep: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise WorkflowContractError("workflow mapping keys must be strings")
        if key in result:
            raise WorkflowContractError(f"duplicate YAML key: {key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def load_strict(text: str) -> dict[str, Any]:
    try:
        value = yaml.load(text, Loader=StrictLoader)
    except (yaml.YAMLError, RecursionError) as exc:
        raise WorkflowContractError(f"invalid workflow YAML: {exc}") from exc
    if not isinstance(value, dict):
        raise WorkflowContractError("workflow root must be a mapping")
    return value


def _mapping_value(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WorkflowContractError(f"{label} must be a mapping")
    return value


def _list_value(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise WorkflowContractError(f"{label} must be a list")
    return value


def _jobs(workflow: dict[str, Any]) -> dict[str, Any]:
    return _mapping_value(workflow.get("jobs"), "jobs")


def _steps(job: dict[str, Any], label: str) -> list[dict[str, Any]]:
    return [
        _mapping_value(step, f"{label} step")
        for step in _list_value(job.get("steps"), f"{label}.steps")
    ]


def _step(job: dict[str, Any], name: str, label: str) -> dict[str, Any]:
    matches = [step for step in _steps(job, label) if step.get("name") == name]
    if len(matches) != 1:
        raise WorkflowContractError(f"{label} must have exactly one step named {name!r}")
    return matches[0]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise WorkflowContractError(message)


def semantic_digest(data: dict[str, Any]) -> str:
    """Hash complete parsed workflow semantics, ignoring comments/formatting.

    The parsed YAML may contain strings with lone surrogate code points
    (e.g. ``"\\ud83d"`` in double quotes). ``json.dumps(..., ensure_ascii=False)``
    passes them through to ``.encode("utf-8")``, which raises an unhandled
    ``UnicodeEncodeError``. Fail closed with a clear contract error instead
    of exposing a traceback on the release-authoritative path.
    """
    try:
        encoded = json.dumps(
            data,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise WorkflowContractError(f"workflow semantics are not hashable: {exc}") from exc
    return hashlib.sha256(encoded).hexdigest()


def _run(step: dict[str, Any]) -> object:
    value = step.get("run")
    return value.rstrip("\n") if isinstance(value, str) else value


def _validate_actions(workflow: dict[str, Any], label: str) -> None:
    for job_name, raw_job in _jobs(workflow).items():
        job = _mapping_value(raw_job, f"{label}.jobs.{job_name}")
        for step in _steps(job, f"{label}.jobs.{job_name}"):
            uses = step.get("uses")
            if uses is not None:
                _require(isinstance(uses, str) and SHA.fullmatch(uses) is not None, f"{label} has an unpinned action")


def _validate_checkout_refs(workflow: dict[str, Any], expected_ref: str, label: str) -> None:
    count = 0
    for job_name, raw_job in _jobs(workflow).items():
        job = _mapping_value(raw_job, f"{label}.jobs.{job_name}")
        for step in _steps(job, f"{label}.jobs.{job_name}"):
            if isinstance(step.get("uses"), str) and step["uses"].startswith("actions/checkout@"):
                count += 1
                inputs = _mapping_value(step.get("with"), f"{label} checkout inputs")
                _require(
                    inputs == {"ref": expected_ref, "persist-credentials": "false"},
                    f"{label} checkout must use exact ref {expected_ref} without persisted credentials",
                )
    _require(count > 0, f"{label} has no checkout steps")


def validate_ci(workflow: dict[str, Any]) -> None:
    _require(
        semantic_digest(workflow) == CI_SEMANTIC_SHA256,
        "CI parsed semantics differ from the reviewed semantic pin",
    )
    triggers = _mapping_value(workflow.get("on"), "ci.on")
    _require(triggers == {"push": {"branches": ["main"]}, "pull_request": None}, "CI triggers must be main push and pull_request")
    _require(workflow.get("permissions") == {"contents": "read"}, "CI permissions must be contents: read")
    _validate_actions(workflow, "CI")
    _validate_checkout_refs(workflow, CHECKOUT_REF, "CI")
    jobs = _jobs(workflow)
    containers = _mapping_value(jobs.get("containers"), "CI containers job")
    guard = _step(containers, "Verify repository security guards", "CI containers")
    _require(_run(guard) == "python scripts/test_repository_guards.py", "CI must execute repository guards")


def validate_release(workflow: dict[str, Any]) -> None:
    _require(
        semantic_digest(workflow) == RELEASE_SEMANTIC_SHA256,
        "release parsed semantics differ from the reviewed semantic pin",
    )
    _require(set(workflow) == {"name", "on", "permissions", "concurrency", "jobs"}, "release root keys changed")
    _require(workflow.get("on") == {"push": {"tags": ["v*.*.*"]}, "workflow_dispatch": None}, "release triggers must be tag push and dispatch")
    _require(workflow.get("permissions") == {"contents": "read"}, "release top-level permissions must be read-only")
    _validate_actions(workflow, "release")
    _validate_checkout_refs(workflow, RELEASE_CHECKOUT_REF, "release")
    jobs = _jobs(workflow)
    required = {
        "validate-tag", "guards", "dotnet", "web", "python", "e2e", "containers", "browser", "release-bundle", "assemble-evidence", "publish",
    }
    _require(set(jobs) == required, "release job set changed")
    producer_needs = ["validate-tag", "guards", "dotnet", "web", "python", "e2e", "containers", "browser", "release-bundle"]
    assemble = _mapping_value(jobs["assemble-evidence"], "assemble-evidence")
    _require(assemble.get("needs") == producer_needs, "assemble needs must name every producer once")
    _require(assemble.get("permissions") == {"contents": "read"}, "assemble must remain read-only")
    _require(assemble.get("outputs") == {"handoff_digest": "${{ steps.assemble.outputs.handoff_digest }}"}, "assemble must expose its digest output")
    assemble_checkout = _step(assemble, "Check out repository", "assemble-evidence")
    _require(
        assemble_checkout.get("uses")
        == "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd"
        and assemble_checkout.get("with")
        == {"ref": RELEASE_CHECKOUT_REF, "persist-credentials": "false"},
        "assemble must check out the exact release commit before invoking repository code",
    )
    expected_downloads = {
        "Download immutable release evidence": {"name": "tracehelix-release-evidence", "path": "unified"},
        "Download source CycloneDX SBOM": {"name": "tracehelix-0.1.0-source.cdx.json", "path": "unified"},
        "Download API SPDX SBOM": {"name": "tracehelix-api.spdx.json", "path": "unified"},
        "Download web SPDX SBOM": {"name": "tracehelix-web.spdx.json", "path": "unified"},
    }
    for name, inputs in expected_downloads.items():
        _require(_step(assemble, name, "assemble-evidence").get("with") == inputs, f"{name} inputs changed")
    upload = _step(assemble, "Upload immutable unified release assets", "assemble-evidence")
    _require(upload.get("with") == {"name": "tracehelix-release-assets", "path": "unified", "if-no-files-found": "error", "retention-days": 14}, "unified upload inputs changed")
    assemble_step = _step(assemble, "Assemble and bind exact release assets", "assemble-evidence")
    _require(_run(assemble_step) == 'set -euo pipefail\npython3 scripts/release_assets.py assemble --directory unified --version "$VERSION" --github-output "$GITHUB_OUTPUT"', "assemble must invoke canonical Python assembler exactly")
    _require(assemble_step.get("env") == {"VERSION": "${{ needs.validate-tag.outputs.version }}"}, "assemble version input changed")
    publish = _mapping_value(jobs["publish"], "publish")
    _require(publish.get("if") == "github.event_name == 'push' && startsWith(github.ref, 'refs/tags/')", "publish must remain tag-only")
    _require(publish.get("needs") == producer_needs + ["assemble-evidence"], "publish needs changed")
    _require(publish.get("permissions") == {"contents": "write", "id-token": "write", "attestations": "write"}, "publish permissions changed")
    publish_checkout = _step(publish, "Check out repository", "publish")
    _require(
        publish_checkout.get("uses")
        == "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd"
        and publish_checkout.get("with")
        == {"ref": RELEASE_CHECKOUT_REF, "persist-credentials": "false"},
        "publish must check out the exact release commit before invoking repository code",
    )
    download = _step(publish, "Download immutable unified release assets", "publish")
    _require(download.get("with") == {"name": "tracehelix-release-assets", "path": "release"}, "publish download inputs changed")
    verify = _step(publish, "Verify exact handoff digest and release preconditions", "publish")
    _require(_run(verify) == 'set -euo pipefail\nTAG="${GITHUB_REF#refs/tags/}"\npython3 scripts/release_assets.py verify-publish --directory release --version "$VERSION" --tag "$TAG" --expected-digest "$HANDOFF_DIGEST"', "publish must invoke canonical verification exactly")
    _require(verify.get("env") == {"VERSION": "${{ needs.validate-tag.outputs.version }}", "HANDOFF_DIGEST": "${{ needs.assemble-evidence.outputs.handoff_digest }}"}, "publish digest inputs changed")
    absent = _step(publish, "Refuse to overwrite an existing release", "publish")
    _require(_run(absent) == 'set -euo pipefail\nTAG="${GITHUB_REF#refs/tags/}"\npython3 scripts/release_assets.py assert-release-absent --tag "$TAG"', "never-overwrite guard must invoke canonical helper")
    provenance = _step(publish, "Attest build provenance for the source archive", "publish")
    _require(provenance.get("uses", "").startswith("actions/attest-build-provenance@"), "provenance action missing")
    _require(provenance.get("with") == {"subject-path": "release/tracehelix-${{ needs.validate-tag.outputs.version }}-source.tar.gz"}, "provenance subject must be source archive")
    create = _step(publish, "Create the GitHub release (tag only, never overwrite)", "publish")
    _require(_run(create) == 'set -euo pipefail\nTAG="${GITHUB_REF#refs/tags/}"\npython3 scripts/release_assets.py create-release --directory release --version "$VERSION" --tag "$TAG" --expected-digest "$HANDOFF_DIGEST"', "release creation must reverify the bound handoff and use canonical fixed argv")
    _require(create.get("env") == {"GH_TOKEN": "${{ github.token }}", "VERSION": "${{ needs.validate-tag.outputs.version }}", "HANDOFF_DIGEST": "${{ needs.assemble-evidence.outputs.handoff_digest }}"}, "release creation inputs changed")


def validate_repository() -> None:
    validate_ci(load_strict(CI.read_text(encoding="utf-8")))
    validate_release(load_strict(RELEASE.read_text(encoding="utf-8")))


def main() -> int:
    try:
        validate_repository()
    except (OSError, WorkflowContractError) as exc:
        print(f"workflow_contract: {exc}", file=sys.stderr)
        return 1
    print("workflow contract: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
