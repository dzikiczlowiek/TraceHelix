"""Isolated regression tests for the version 1 training-data contracts."""

from __future__ import annotations

import json
import math
import traceback as traceback_module
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, cast

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import BaseModel, ValidationError

from tracehelix_training.contracts import (
    DatasetManifest,
    TeacherLabel,
    TrainingCandidate,
    TrainingExample,
    candidate_identity,
    canonical_json_bytes,
    construct_candidate,
    contract_validator,
    generate_schemas,
    manifest_identity,
)
from tracehelix_training.redact import ScanFailedError, load_default_config, redact
from traceback_privacy import is_tracehelix_training_frame

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "schemas"
HASH = "a" * 64
OTHER_HASH = "b" * 64
TEACHER_FIELDS = (
    "teacher_model",
    "teacher_revision",
    "prompt_version",
    "prompt_hash",
    "request_hash",
    "response_hash",
)


def event(event_id: str) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "event_text": "observed",
        "event_kind": "message",
        "tool_name": None,
    }


def candidate_content() -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "run_id": "run-1",
        "event_id": "e1",
        "task_group_id": "task-1",
        "lineage_id": "line-1",
        "context_before": [event("e0")],
        "event_text": "ran tests",
        "context_after": [event("e2")],
        "event_kind": "tool_result",
        "tool_name": "pytest",
        "source_category": "fixture",
        "source_hash": HASH,
        "adapter": "tracehelix",
        "adapter_version": "1.0.0",
        "redaction_version": "redaction-v1",
        "license_or_consent": "generated fixture",
    }


def candidate() -> dict[str, Any]:
    value = candidate_content()
    return {"example_id": candidate_identity(value), **value}


def example() -> dict[str, Any]:
    return {
        **candidate(),
        "label": "Verify",
        "label_status": "teacher_single",
        "confidence": 0.9,
        "evidence_event_ids": ["e1", "e2"],
        "observable_reason": "Tests were observed.",
        "teacher_model": "teacher",
        "teacher_revision": "2026-07-01",
        "prompt_version": "v1",
        "prompt_hash": HASH,
        "request_hash": HASH,
        "response_hash": HASH,
        "created_at": "2026-07-17T12:00:00Z",
        "split": "train",
        "quarantine_reason": None,
    }


def teacher() -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "example_id": candidate()["example_id"],
        "available_event_ids": ["e0", "e1", "e2"],
        "abstained": False,
        "accepted": True,
        "label": "Verify",
        "confidence": 0.8,
        "evidence_event_ids": ["e1"],
        "observable_reason": "The current event reports tests.",
        "quarantine_reason": None,
    }


def manifest() -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": "1.0.0",
        "created_at": "2026-07-17T12:00:00Z",
        "files": [{"path": "train.jsonl", "sha256": HASH, "byte_size": 10}],
        "split_counts": {"train": 1, "validation": 0, "test": 0},
        "source_counts": {"fake": 0, "fixture": 1, "live": 0},
        "split_source_counts": {
            "train": {"fake": 0, "fixture": 1, "live": 0},
            "validation": {"fake": 0, "fixture": 0, "live": 0},
            "test": {"fake": 0, "fixture": 0, "live": 0},
        },
        "quarantined_source_counts": {"fake": 0, "fixture": 0, "live": 0},
        "status_counts": {
            "rule_derived": 0,
            "teacher_single": 1,
            "teacher_consensus": 0,
            "judge_resolved": 0,
            "synthetic_invariant": 0,
            "quarantined": 0,
        },
        "class_counts": {
            "Explore": 0,
            "Plan": 0,
            "Execute": 0,
            "Verify": 1,
            "Recover": 0,
            "Unknown": 0,
        },
        "requested_count": 1,
        "completed_count": 1,
        "accepted_count": 1,
        "retried_count": 0,
        "quarantined_count": 0,
        "config_version": "dataset-v1",
        "prompt_version": "v1",
        "redaction_version": "redact-v1",
    }
    return reidentify(value)


def reidentify(value: dict[str, Any]) -> dict[str, Any]:
    value.pop("manifest_id", None)
    value["manifest_id"] = manifest_identity(value)
    return value


def schema(name: str) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads((SCHEMAS / f"{name}.schema.json").read_text()))


def assert_rejected(model: type[BaseModel], name: str, value: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(value)
    assert list(contract_validator(schema(name)).iter_errors(value))


@pytest.mark.parametrize(
    ("model", "name", "factory"),
    [
        (TrainingCandidate, "training-candidate", candidate),
        (TrainingExample, "training-example", example),
        (TeacherLabel, "teacher-label", teacher),
        (DatasetManifest, "dataset-manifest", manifest),
    ],
)
def test_positive_fixtures_round_trip(
    model: type[BaseModel], name: str, factory: Callable[[], dict[str, Any]]
) -> None:
    value = factory()
    contract_validator(schema(name)).validate(value)
    assert json.loads(canonical_json_bytes(model.model_validate(value))) == value


def test_retry_count_is_attempt_metadata_not_a_lifecycle_total() -> None:
    value = manifest()
    value["retried_count"] = 3
    DatasetManifest.model_validate(reidentify(value))


@pytest.mark.parametrize("bad", [True, False, "1", 1.0])
def test_count_types_fail_closed_for_pydantic_and_contract(bad: Any) -> None:
    value = manifest()
    value["files"][0]["byte_size"] = bad
    reidentify(value)
    with pytest.raises(ValidationError):
        DatasetManifest.model_validate(value)
    assert list(contract_validator(schema("dataset-manifest")).iter_errors(value))
    # Draft 2020-12 defines integer mathematically, so 1.0 is an integer in generic
    # validators even though this Python boundary deliberately requires type(value) is int.
    generic_errors = list(Draft202012Validator(schema("dataset-manifest")).iter_errors(value))
    assert bool(generic_errors) is (type(bad) is not float)


@pytest.mark.parametrize("bad", [True, False, "0.5", float("nan"), float("inf"), -0.1, 1.1])
def test_confidence_types_and_range_fail_closed(bad: Any) -> None:
    value = teacher()
    value["confidence"] = bad
    with pytest.raises(ValidationError):
        TeacherLabel.model_validate(value)
    assert list(contract_validator(schema("teacher-label")).iter_errors(value))
    generic_errors = list(Draft202012Validator(schema("teacher-label")).iter_errors(value))
    # NaN is outside JSON's value domain; jsonschema therefore leaves rejection to
    # parsing/application validation. All actual JSON values have structural parity.
    assert bool(generic_errors) is not (isinstance(bad, float) and math.isnan(bad))


@pytest.mark.parametrize("confidence", [0, 1, 0.5])
def test_confidence_json_numbers_normalize_to_float_with_validator_parity(
    confidence: int | float,
) -> None:
    value = teacher()
    value["confidence"] = confidence
    parsed = TeacherLabel.model_validate(value)
    assert type(parsed.confidence) is float
    assert not list(Draft202012Validator(schema("teacher-label")).iter_errors(value))
    assert not list(contract_validator(schema("teacher-label")).iter_errors(value))


def test_numeric_json_schema_types_remain_integer_and_number() -> None:
    manifest_schema = schema("dataset-manifest")
    assert manifest_schema["properties"]["requested_count"]["type"] == "integer"
    assert manifest_schema["$defs"]["ManifestFile"]["properties"]["byte_size"]["type"] == "integer"
    assert schema("teacher-label")["properties"]["confidence"]["anyOf"][0]["type"] == "number"


def test_wrong_hash_shaped_candidate_id_is_relationally_rejected() -> None:
    value = candidate()
    value["example_id"] = OTHER_HASH
    assert not list(Draft202012Validator(schema("training-candidate")).iter_errors(value))
    assert_rejected(TrainingCandidate, "training-candidate", value)


@pytest.mark.parametrize("bad", ["short", "G" * 64])
def test_nonhash_candidate_id_is_structurally_rejected(bad: str) -> None:
    value = candidate()
    value["example_id"] = bad
    assert list(Draft202012Validator(schema("training-candidate")).iter_errors(value))


def test_construct_candidate_is_repeatable() -> None:
    assert construct_candidate(**candidate_content()) == construct_candidate(**candidate_content())


def test_construct_candidate_redacts_current_and_context_content_before_identity() -> None:
    content = candidate_content()
    top_secret = "Bearer synthetic-current-token"
    before_secret = "APP_PASSWORD='two words'"
    after_secret = "person@[2001:db8::1]"
    content["event_text"] = top_secret
    content["context_before"][0]["event_text"] = before_secret
    content["context_after"][0]["event_text"] = after_secret

    first = construct_candidate(**content)
    second = construct_candidate(**content)
    encoded = canonical_json_bytes(first).decode()
    assert all(secret not in encoded for secret in [top_secret, "two words", after_secret])
    assert "<REDACTED:AUTH:1>" in first.event_text
    assert "<REDACTED:ENV_SECRET:1>" in first.context_before[0].event_text
    assert "<REDACTED:EMAIL:1>" in first.context_after[0].event_text
    assert first == second
    assert first.example_id == candidate_identity(
        first.model_dump(mode="json", exclude={"example_id"})
    )


def test_construct_candidate_rejects_version_mismatch_and_report_injection() -> None:
    mismatched = candidate_content()
    mismatched["redaction_version"] = "arbitrary-caller-version"
    with pytest.raises(ValueError, match="candidate redaction gate failed"):
        construct_candidate(**mismatched)

    injected_reports = [
        {
            "secret_scan_passed": True,
            "version": "redaction-v1",
            "config_hash": HASH,
            "input_hash": HASH,
            "output_hash": HASH,
        },
        {
            "secret_scan_passed": False,
            "version": "redaction-v1",
            "config_hash": OTHER_HASH,
            "input_hash": OTHER_HASH,
            "output_hash": OTHER_HASH,
        },
    ]
    for injected_report in injected_reports:
        forged = candidate_content()
        forged["redaction_report"] = injected_report
        with pytest.raises(ValidationError):
            construct_candidate(**forged)

    failed_scan = candidate_content()
    scan_canary = "candidate-failed-scan-private-canary"
    failed_scan["event_text"] = "APP_PASSWORD=" + scan_canary + "x" * 4097
    with pytest.raises(ScanFailedError) as caught:
        construct_candidate(**failed_scan)
    traceback = caught.value.__traceback__
    while traceback is not None:
        if is_tracehelix_training_frame(traceback.tb_frame.f_code.co_filename):
            assert all(
                scan_canary not in repr(value) for value in traceback.tb_frame.f_locals.values()
            )
        traceback = traceback.tb_next


def test_construct_candidate_binds_a_second_redaction_report_to_the_exact_final_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = candidate_content()
    content.pop("tool_name")
    content["context_before"][0].pop("tool_name")
    calls: list[Any] = []
    real_redact = redact

    def recording_redact(value: Any, config: Any) -> Any:
        calls.append(value)
        return real_redact(value, config)

    monkeypatch.setattr("tracehelix_training.contracts.redact", recording_redact)
    result = construct_candidate(**content)
    final_payload = result.model_dump(mode="json", exclude={"example_id"})

    assert len(calls) == 2
    assert calls[1] == final_payload
    assert calls[1]["tool_name"] is None
    assert calls[1]["context_before"][0]["tool_name"] is None
    assert result.example_id == candidate_identity(calls[1])


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("secret_scan_passed", False),
        ("version", "redaction-v999"),
        ("config_hash", OTHER_HASH),
        ("input_hash", OTHER_HASH),
        ("output_hash", OTHER_HASH),
    ],
)
@pytest.mark.parametrize("call_to_tamper", [1, 2])
def test_construct_candidate_rejects_mismatched_redaction_report(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    bad_value: Any,
    call_to_tamper: int,
) -> None:
    call_count = 0
    real_redact = redact

    def tampered_redact(value: Any, config: Any) -> Any:
        nonlocal call_count
        call_count += 1
        output, report = real_redact(value, config)
        if call_count == call_to_tamper:
            report = replace(report, **{field: bad_value})
        return output, report

    monkeypatch.setattr("tracehelix_training.contracts.redact", tampered_redact)
    with pytest.raises(ValueError, match="candidate redaction gate failed"):
        construct_candidate(**candidate_content())


def test_construct_candidate_rejects_non_idempotent_second_redaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_count = 0
    real_redact = redact

    def tampered_redact(value: Any, config: Any) -> Any:
        nonlocal call_count
        call_count += 1
        output, report = real_redact(value, config)
        if call_count == 2:
            assert isinstance(output, dict)
            output = {**output, "run_id": "tampered-after-normalization"}
            report = replace(report, output_hash=candidate_identity(output))
        return output, report

    monkeypatch.setattr("tracehelix_training.contracts.redact", tampered_redact)
    with pytest.raises(ValueError, match="candidate redaction gate failed"):
        construct_candidate(**candidate_content())


def test_construct_candidate_validation_failure_leaks_no_traceback_canary() -> None:
    canary = "TRACEBACK_PRIVATE_CANARY_7f41"
    invalid = candidate_content()
    invalid["run_id"] = canary
    invalid["unexpected"] = True

    with pytest.raises(Exception) as caught:
        construct_candidate(**invalid)

    error: BaseException | None = caught.value
    while error is not None:
        assert canary not in str(error)
        assert canary not in repr(error)
        assert canary not in "".join(traceback_module.format_exception(error))
        traceback = error.__traceback__
        while traceback is not None:
            if is_tracehelix_training_frame(traceback.tb_frame.f_code.co_filename):
                assert all(
                    canary not in repr(value) for value in traceback.tb_frame.f_locals.values()
                )
            traceback = traceback.tb_next
        error = error.__cause__ or error.__context__


@pytest.mark.parametrize(
    ("omit_top_level", "omit_context"), [(True, False), (False, True), (True, True)]
)
def test_construct_candidate_normalizes_omitted_tool_name_defaults(
    omit_top_level: bool, omit_context: bool
) -> None:
    content = candidate_content()
    if omit_top_level:
        content.pop("tool_name")
    if omit_context:
        for context_event in content["context_before"] + content["context_after"]:
            context_event.pop("tool_name")

    result = construct_candidate(**content)
    normalized = result.model_dump(mode="json", exclude={"example_id"})

    expected_tool_name = None if omit_top_level else "pytest"
    assert normalized["tool_name"] == expected_tool_name
    assert all(
        "tool_name" in item for item in normalized["context_before"] + normalized["context_after"]
    )
    assert result.example_id == candidate_identity(normalized)
    canonical = json.loads(canonical_json_bytes(result))
    assert canonical["tool_name"] == expected_tool_name
    if omit_context:
        assert all(
            item["tool_name"] is None
            for item in canonical["context_before"] + canonical["context_after"]
        )
    TrainingCandidate.model_validate(result.model_dump(mode="json"))


def test_omitted_and_explicit_null_tool_names_have_identical_identity() -> None:
    omitted = candidate_content()
    omitted.pop("tool_name")
    for context_event in omitted["context_before"] + omitted["context_after"]:
        context_event.pop("tool_name")
    explicit = candidate_content()
    explicit["tool_name"] = None

    assert construct_candidate(**omitted).example_id == construct_candidate(**explicit).example_id


@pytest.mark.parametrize(("field", "value"), [("unexpected", True), ("run_id", "")])
def test_construct_candidate_rejects_invalid_and_extra_content(field: str, value: Any) -> None:
    invalid = candidate_content()
    invalid[field] = value
    with pytest.raises(ValidationError):
        construct_candidate(**invalid)


def test_training_example_identity_excludes_all_label_fields() -> None:
    value = example()
    value.update(label="Plan", confidence=0.1, observable_reason="Another supported label.")
    assert TrainingExample.model_validate(value).example_id == candidate()["example_id"]


@pytest.mark.parametrize(
    ("model", "name", "factory"),
    [
        (TrainingCandidate, "training-candidate", candidate),
        (TrainingExample, "training-example", example),
    ],
)
def test_candidate_contracts_accept_any_nonempty_redaction_version(
    model: type[BaseModel], name: str, factory: Callable[[], dict[str, Any]]
) -> None:
    value = factory()
    value["redaction_version"] = "legacy-compatible-policy"
    identity_content = {
        key: item
        for key, item in value.items()
        if key in TrainingCandidate.model_fields and key != "example_id"
    }
    value["example_id"] = candidate_identity(identity_content)

    model.model_validate(value)
    Draft202012Validator(schema(name)).validate(value)
    generated = model.model_json_schema(mode="serialization")
    redaction_schema = generated["properties"]["redaction_version"]
    assert redaction_schema["minLength"] == 1
    assert "const" not in redaction_schema


@pytest.mark.parametrize("bad_version", ["", "legacy-compatible-policy"])
def test_construct_candidate_export_gate_requires_packaged_redaction_version(
    bad_version: str,
) -> None:
    mismatched = candidate_content()
    mismatched["redaction_version"] = bad_version
    assert load_default_config().version != mismatched["redaction_version"]
    with pytest.raises(ValueError, match="candidate redaction gate failed"):
        construct_candidate(**mismatched)


def quarantined() -> dict[str, Any]:
    value = example()
    value.update(
        label=None,
        label_status="quarantined",
        confidence=None,
        evidence_event_ids=[],
        observable_reason=None,
        split=None,
        quarantine_reason="ambiguous",
    )
    return value


def test_quarantine_accepts_complete_six_field_teacher_provenance() -> None:
    TrainingExample.model_validate(quarantined())


def test_nonteacher_quarantine_accepts_all_null_provenance() -> None:
    value = quarantined()
    for field in TEACHER_FIELDS:
        value[field] = None
    TrainingExample.model_validate(value)


@pytest.mark.parametrize("mask", range(1, 63))
def test_every_partial_teacher_provenance_combination_is_rejected(mask: int) -> None:
    value = quarantined()
    for index, field in enumerate(TEACHER_FIELDS):
        if mask & (1 << index):
            value[field] = None
    if mask != 63:
        assert_rejected(TrainingExample, "training-example", value)


@pytest.mark.parametrize(
    ("status", "with_teacher", "valid"),
    [
        ("teacher_consensus", True, True),
        ("teacher_consensus", False, False),
        ("rule_derived", False, True),
        ("rule_derived", True, False),
    ],
)
def test_accepted_teacher_and_nonteacher_provenance_rules(
    status: str, with_teacher: bool, valid: bool
) -> None:
    value = example()
    value["label_status"] = status
    if not with_teacher:
        for field in TEACHER_FIELDS:
            value[field] = None
    if valid:
        TrainingExample.model_validate(value)
    else:
        assert_rejected(TrainingExample, "training-example", value)


def mixed_manifest() -> dict[str, Any]:
    value = manifest()
    value.update(requested_count=4, completed_count=4, accepted_count=3, quarantined_count=1)
    value["split_counts"] = {"train": 1, "validation": 1, "test": 1}
    value["source_counts"] = {"fake": 2, "fixture": 1, "live": 1}
    value["split_source_counts"] = {
        "train": {"fake": 1, "fixture": 0, "live": 0},
        "validation": {"fake": 0, "fixture": 1, "live": 0},
        "test": {"fake": 0, "fixture": 0, "live": 1},
    }
    value["quarantined_source_counts"] = {"fake": 1, "fixture": 0, "live": 0}
    value["status_counts"] = {
        "rule_derived": 1,
        "teacher_single": 1,
        "teacher_consensus": 0,
        "judge_resolved": 1,
        "synthetic_invariant": 0,
        "quarantined": 1,
    }
    value["class_counts"] = {
        "Explore": 1,
        "Plan": 1,
        "Execute": 0,
        "Verify": 1,
        "Recover": 0,
        "Unknown": 0,
    }
    return reidentify(value)


def test_positive_mixed_source_split_matrix() -> None:
    DatasetManifest.model_validate(mixed_manifest())


@pytest.mark.parametrize("defect", ["row", "column", "quarantine-source"])
def test_source_split_defects_are_independently_rejected(defect: str) -> None:
    value = mixed_manifest()
    if defect == "row":
        value["split_source_counts"]["train"] = {"fake": 0, "fixture": 0, "live": 0}
    elif defect == "column":
        value["source_counts"] = {"fake": 1, "fixture": 2, "live": 1}
    else:
        value["quarantined_source_counts"] = {"fake": 0, "fixture": 1, "live": 0}
    assert_rejected(DatasetManifest, "dataset-manifest", reidentify(value))


def test_generic_validator_may_accept_relational_matrix_defect_contract_rejects() -> None:
    value = mixed_manifest()
    value["split_source_counts"]["train"] = {"fake": 0, "fixture": 0, "live": 0}
    reidentify(value)
    assert not list(Draft202012Validator(schema("dataset-manifest")).iter_errors(value))
    assert list(contract_validator(schema("dataset-manifest")).iter_errors(value))


@pytest.mark.parametrize(
    "path",
    [
        ".",
        "..",
        "../x",
        "a/../x",
        "/abs",
        "a//b",
        "a\\..\\x",
        "C:\\x",
        "\\\\server\\share",
        "data:stream",
        "dir/name:stream",
        "CON",
        "con.jsonl",
        "dir/PrN.txt",
        "AUX",
        "nul.bin",
        "COM1",
        "com9.log",
        "LPT1",
        "lPt9.txt",
        "name.",
        "dir/name ",
        "control\x00name",
        "dir/control\x1fname",
        "delete\x7fname",
        "next-line\x85name",
    ],
)
def test_unsafe_manifest_paths_rejected(path: str) -> None:
    value = manifest()
    value["files"][0]["path"] = path
    assert_rejected(DatasetManifest, "dataset-manifest", reidentify(value))


def test_case_insensitive_duplicate_manifest_path_aliases_are_rejected() -> None:
    value = manifest()
    value["files"] = [
        {"path": "Data/x", "sha256": HASH, "byte_size": 1},
        {"path": "data/X", "sha256": OTHER_HASH, "byte_size": 2},
    ]
    assert_rejected(DatasetManifest, "dataset-manifest", reidentify(value))


def test_nested_posix_manifest_path_is_valid() -> None:
    value = manifest()
    value["files"][0]["path"] = "nested/data/train.jsonl"
    DatasetManifest.model_validate(reidentify(value))


@pytest.mark.parametrize(
    "files",
    [
        [],
        [
            {"path": "x", "sha256": HASH, "byte_size": 1},
            {"path": "x", "sha256": OTHER_HASH, "byte_size": 2},
        ],
    ],
)
def test_empty_or_duplicate_manifest_paths_rejected(files: list[dict[str, Any]]) -> None:
    value = manifest()
    value["files"] = files
    assert_rejected(DatasetManifest, "dataset-manifest", reidentify(value))


@pytest.mark.parametrize(
    ("factory", "model", "name"),
    [
        (example, TrainingExample, "training-example"),
        (manifest, DatasetManifest, "dataset-manifest"),
    ],
)
@pytest.mark.parametrize("timestamp", ["2026-07-17T12:00:00", "2026-07-17T14:00:00+02:00"])
def test_timestamps_require_utc(
    factory: Callable[[], dict[str, Any]], model: type[BaseModel], name: str, timestamp: str
) -> None:
    value = factory()
    value["created_at"] = timestamp
    if model is DatasetManifest:
        reidentify(value)
    assert_rejected(model, name, value)


def test_example_utc_plus_zero_is_normalized_to_z() -> None:
    value = example()
    value["created_at"] = "2026-07-17T12:00:00+00:00"
    assert json.loads(canonical_json_bytes(TrainingExample.model_validate(value)))[
        "created_at"
    ].endswith("Z")


def test_manifest_equivalent_noncanonical_utc_is_explicitly_rejected() -> None:
    value = manifest()
    value["created_at"] = "2026-07-17T12:00:00+00:00"
    # Identity is defined over canonical model output, so a raw noncanonical spelling
    # cannot carry a matching identity and is deliberately rejected.
    assert_rejected(DatasetManifest, "dataset-manifest", reidentify(value))


def test_canonical_utc_outputs_end_in_z() -> None:
    for parsed in (
        TrainingExample.model_validate(example()),
        DatasetManifest.model_validate(manifest()),
    ):
        assert json.loads(canonical_json_bytes(parsed))["created_at"].endswith("Z")


def test_manifest_stale_identity_is_rejected_separately() -> None:
    value = manifest()
    value["config_version"] = "dataset-v2"
    assert_rejected(DatasetManifest, "dataset-manifest", value)


def test_manifest_new_field_identity_is_deterministic() -> None:
    value = manifest()
    value["retried_count"] = 7
    first = reidentify(value)["manifest_id"]
    assert first == reidentify(value)["manifest_id"]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda v: v.update(example_id="short"),
        lambda v: v.update(extra=True),
        lambda v: v.update(available_event_ids=[]),
        lambda v: v.update(available_event_ids=["e1", "e1"]),
    ],
)
def test_generic_schema_rejects_hash_extra_minitems_and_uniqueitems(
    mutation: Callable[[dict[str, Any]], None],
) -> None:
    value = teacher()
    mutation(value)
    assert list(Draft202012Validator(schema("teacher-label")).iter_errors(value))


@pytest.mark.parametrize("path", ["data:stream", "name.", "name ", "control\x1fname"])
def test_generic_schema_rejects_structurally_unsafe_path(path: str) -> None:
    value = manifest()
    value["files"][0]["path"] = path
    assert list(Draft202012Validator(schema("dataset-manifest")).iter_errors(value))


def test_generic_schema_rejects_path_pattern_and_datetime_format() -> None:
    value = manifest()
    value["files"][0]["path"] = "../x"
    assert list(
        Draft202012Validator(
            schema("dataset-manifest"), format_checker=FormatChecker()
        ).iter_errors(value)
    )
    value = manifest()
    value["created_at"] = "not-a-date"
    assert list(
        Draft202012Validator(
            schema("dataset-manifest"), format_checker=FormatChecker()
        ).iter_errors(value)
    )


@pytest.mark.parametrize("field", ["available_event_ids", "evidence_event_ids"])
def test_teacher_label_rejects_duplicate_ids(field: str) -> None:
    value = teacher()
    value[field] = ["e1", "e1"]
    assert_rejected(TeacherLabel, "teacher-label", value)


def test_teacher_label_requires_hash_shaped_example_id() -> None:
    value = teacher()
    value["example_id"] = "example-1"
    assert_rejected(TeacherLabel, "teacher-label", value)


def test_teacher_label_rejects_unavailable_evidence() -> None:
    value = teacher()
    value["evidence_event_ids"] = ["missing"]
    assert_rejected(TeacherLabel, "teacher-label", value)


def test_honest_teacher_abstention_is_valid() -> None:
    value = teacher()
    value.update(
        abstained=True,
        accepted=False,
        label=None,
        confidence=None,
        evidence_event_ids=[],
        observable_reason=None,
        quarantine_reason="insufficient",
    )
    TeacherLabel.model_validate(value)


@pytest.mark.parametrize(("accepted", "abstained"), [(True, True), (False, False)])
def test_teacher_label_rejects_illegal_boolean_combinations(
    accepted: bool, abstained: bool
) -> None:
    value = teacher()
    value.update(accepted=accepted, abstained=abstained)
    assert_rejected(TeacherLabel, "teacher-label", value)


def test_schema_generation_exactly_matches_checked_in_bytes(tmp_path: Path) -> None:
    generate_schemas(str(tmp_path))
    for checked_in in (
        sorted(SCHEMAS.glob("*training*.json"))
        + sorted(SCHEMAS.glob("teacher-label*.json"))
        + sorted(SCHEMAS.glob("dataset-manifest*.json"))
    ):
        assert (tmp_path / checked_in.name).read_bytes() == checked_in.read_bytes()


def test_canonical_serialization_repeatable_and_rejects_nonfinite() -> None:
    first = canonical_json_bytes(TrainingExample.model_validate(example()))
    assert first == canonical_json_bytes(json.loads(first))
    with pytest.raises(ValueError):
        canonical_json_bytes({"bad": float("inf")})
