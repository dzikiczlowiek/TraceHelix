"""Fail-closed version 1 contracts for TraceHelix training data.

IDs hash canonical JSON (sorted keys, compact separators, UTF-8, no NaN). JSON
Schema consumers MUST also run :func:`contract_validator` for relational rules.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal, TypeAlias, cast

from jsonschema import Draft202012Validator, ValidationError as JsonSchemaError
from jsonschema.validators import extend
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    model_validator,
)

SHA256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
NonEmpty = Annotated[str, Field(min_length=1)]


def _strict_count(value: Any) -> int:
    if type(value) is not int:
        raise ValueError("count must be an integer")
    return value


def _strict_confidence(value: Any) -> float:
    if type(value) not in (int, float):
        raise ValueError("confidence must be a JSON number")
    normalized = float(value)
    if not 0 <= normalized <= 1:
        raise ValueError("confidence must be finite and between 0 and 1")
    return normalized


Confidence = Annotated[
    float, Field(ge=0, le=1, allow_inf_nan=False), BeforeValidator(_strict_confidence)
]
Count = Annotated[int, Field(ge=0), BeforeValidator(_strict_count)]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError("timestamp must be timezone-aware UTC")
    return value.astimezone(timezone.utc)


def _utc_json(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


UtcDateTime = Annotated[datetime, AfterValidator(_utc), PlainSerializer(_utc_json, return_type=str)]


class Label(str, Enum):
    EXPLORE = "Explore"
    PLAN = "Plan"
    EXECUTE = "Execute"
    VERIFY = "Verify"
    RECOVER = "Recover"
    UNKNOWN = "Unknown"


class SourceCategory(str, Enum):
    FAKE = "fake"
    FIXTURE = "fixture"
    LIVE = "live"


class LabelStatus(str, Enum):
    RULE_DERIVED = "rule_derived"
    TEACHER_SINGLE = "teacher_single"
    TEACHER_CONSENSUS = "teacher_consensus"
    JUDGE_RESOLVED = "judge_resolved"
    SYNTHETIC_INVARIANT = "synthetic_invariant"
    QUARANTINED = "quarantined"


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)


class ContextEvent(ContractModel):
    event_id: NonEmpty
    event_text: str
    event_kind: NonEmpty
    tool_name: str | None = None


class _CandidateContent(ContractModel):
    """Validated canonical preimage for a candidate identity."""

    schema_version: Literal["1.0.0"]
    run_id: NonEmpty
    event_id: NonEmpty
    task_group_id: NonEmpty
    lineage_id: NonEmpty
    context_before: list[ContextEvent]
    event_text: str
    context_after: list[ContextEvent]
    event_kind: NonEmpty
    tool_name: str | None = None
    source_category: SourceCategory
    source_hash: SHA256
    adapter: NonEmpty
    adapter_version: NonEmpty
    redaction_version: NonEmpty
    license_or_consent: NonEmpty


class TrainingCandidate(ContractModel):
    schema_version: Literal["1.0.0"]
    example_id: SHA256
    run_id: NonEmpty
    event_id: NonEmpty
    task_group_id: NonEmpty
    lineage_id: NonEmpty
    context_before: list[ContextEvent]
    event_text: str
    context_after: list[ContextEvent]
    event_kind: NonEmpty
    tool_name: str | None = None
    source_category: SourceCategory
    source_hash: SHA256
    adapter: NonEmpty
    adapter_version: NonEmpty
    redaction_version: NonEmpty
    license_or_consent: NonEmpty

    @model_validator(mode="after")
    def valid_candidate(self) -> TrainingCandidate:
        ids = (
            [e.event_id for e in self.context_before]
            + [self.event_id]
            + [e.event_id for e in self.context_after]
        )
        if len(ids) != len(set(ids)):
            raise ValueError("current and context event IDs must be unique")
        candidate_fields = set(TrainingCandidate.model_fields) - {"example_id"}
        normalized = _normalized_candidate_content(
            self.model_dump(mode="json", include=candidate_fields)
        )
        if self.example_id != candidate_identity(normalized):
            raise ValueError("example_id does not match canonical candidate content")
        return self

    def observable_event_ids(self) -> set[str]:
        return {
            self.event_id,
            *(e.event_id for e in self.context_before),
            *(e.event_id for e in self.context_after),
        }


TeacherStatus: TypeAlias = Literal["teacher_single", "teacher_consensus", "judge_resolved"]
Split: TypeAlias = Literal["train", "validation", "test"]


class TrainingExample(TrainingCandidate):
    label: Label | None
    label_status: LabelStatus
    confidence: Confidence | None
    evidence_event_ids: Annotated[list[NonEmpty], Field(json_schema_extra={"uniqueItems": True})]
    observable_reason: NonEmpty | None
    teacher_model: NonEmpty | None
    teacher_revision: NonEmpty | None
    prompt_version: NonEmpty | None
    prompt_hash: SHA256 | None
    request_hash: SHA256 | None
    response_hash: SHA256 | None
    created_at: UtcDateTime
    split: Split | None
    quarantine_reason: NonEmpty | None

    @model_validator(mode="after")
    def valid_label_state(self) -> TrainingExample:
        if (
            len(self.evidence_event_ids) != len(set(self.evidence_event_ids))
            or not set(self.evidence_event_ids) <= self.observable_event_ids()
        ):
            raise ValueError("evidence event IDs must be unique and observable")
        teacher = (
            self.teacher_model,
            self.teacher_revision,
            self.prompt_version,
            self.prompt_hash,
            self.request_hash,
            self.response_hash,
        )
        all_teacher = all(x is not None for x in teacher)
        no_teacher = all(x is None for x in teacher)
        if self.label_status == LabelStatus.QUARANTINED:
            if (
                any(
                    (
                        self.label is not None,
                        self.confidence is not None,
                        bool(self.evidence_event_ids),
                        self.observable_reason is not None,
                        self.split is not None,
                    )
                )
                or self.quarantine_reason is None
            ):
                raise ValueError("quarantined examples cannot masquerade as accepted labels")
            if not (all_teacher or no_teacher):
                raise ValueError("quarantine provenance must be complete or absent")
        else:
            if (
                any(
                    x is None
                    for x in (self.label, self.confidence, self.observable_reason, self.split)
                )
                or not self.evidence_event_ids
                or self.quarantine_reason is not None
            ):
                raise ValueError("accepted example is incomplete")
            teacher_status = self.label_status in {
                LabelStatus.TEACHER_SINGLE,
                LabelStatus.TEACHER_CONSENSUS,
                LabelStatus.JUDGE_RESOLVED,
            }
            if teacher_status and not all_teacher:
                raise ValueError("teacher-derived examples require complete provenance")
            if not teacher_status and not no_teacher:
                raise ValueError("non-teacher examples cannot claim provenance")
        return self


class TeacherLabel(ContractModel):
    schema_version: Literal["1.0.0"]
    example_id: SHA256
    available_event_ids: Annotated[
        list[NonEmpty], Field(min_length=1, json_schema_extra={"uniqueItems": True})
    ]
    abstained: bool
    accepted: bool
    label: Label | None
    confidence: Confidence | None
    evidence_event_ids: Annotated[list[NonEmpty], Field(json_schema_extra={"uniqueItems": True})]
    observable_reason: NonEmpty | None
    quarantine_reason: NonEmpty | None

    @model_validator(mode="after")
    def valid_decision(self) -> TeacherLabel:
        if (
            len(self.available_event_ids) != len(set(self.available_event_ids))
            or len(self.evidence_event_ids) != len(set(self.evidence_event_ids))
            or not set(self.evidence_event_ids) <= set(self.available_event_ids)
        ):
            raise ValueError("event IDs invalid")
        if self.accepted and not self.abstained:
            if (
                self.label is None
                or self.confidence is None
                or not self.evidence_event_ids
                or self.observable_reason is None
                or self.quarantine_reason is not None
            ):
                raise ValueError("accepted decision incomplete")
        elif not self.accepted and self.abstained:
            if (
                self.label is not None
                or self.confidence is not None
                or self.evidence_event_ids
                or self.observable_reason is not None
                or self.quarantine_reason is None
            ):
                raise ValueError("invalid abstention")
        else:
            raise ValueError("decision must be exactly accepted or abstained")
        return self


_PATH_COMPONENT = r"[^/\\:\x00-\x1f\x7f-\x9f]*[^/\\:\x00-\x1f\x7f-\x9f. ]"
_PATH = re.compile(rf"^{_PATH_COMPONENT}(?:/{_PATH_COMPONENT})*$")
_SCHEMA_PATH_PATTERN = _PATH.pattern
_RESERVED_PATH_COMPONENT = re.compile(r"^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", re.I)


class ManifestFile(ContractModel):
    path: Annotated[str, Field(min_length=1, pattern=_PATH.pattern)]
    sha256: SHA256
    byte_size: Count

    @model_validator(mode="after")
    def safe_path(self) -> ManifestFile:
        parts = self.path.split("/")
        if any(p in {".", ".."} or _RESERVED_PATH_COMPONENT.match(p) for p in parts) or re.match(
            r"^[A-Za-z]:", self.path
        ):
            raise ValueError("path must be a portable normalized relative POSIX path")
        return self


SplitCounts: TypeAlias = dict[Literal["train", "validation", "test"], Count]
SourceCounts: TypeAlias = dict[Literal["fake", "fixture", "live"], Count]
SplitSourceCounts: TypeAlias = dict[Literal["train", "validation", "test"], SourceCounts]
StatusCounts: TypeAlias = dict[
    Literal[
        "rule_derived",
        "teacher_single",
        "teacher_consensus",
        "judge_resolved",
        "synthetic_invariant",
        "quarantined",
    ],
    Count,
]
ClassCounts: TypeAlias = dict[
    Literal["Explore", "Plan", "Execute", "Verify", "Recover", "Unknown"], Count
]


class DatasetManifest(ContractModel):
    schema_version: Literal["1.0.0"]
    manifest_id: SHA256
    created_at: UtcDateTime
    files: Annotated[
        list[ManifestFile], Field(min_length=1, json_schema_extra={"uniqueItems": True})
    ]
    split_counts: SplitCounts
    source_counts: SourceCounts
    split_source_counts: SplitSourceCounts
    quarantined_source_counts: SourceCounts
    status_counts: StatusCounts
    class_counts: ClassCounts
    requested_count: Count
    completed_count: Count
    accepted_count: Count
    retried_count: Count
    quarantined_count: Count
    config_version: NonEmpty
    prompt_version: NonEmpty
    redaction_version: NonEmpty

    @model_validator(mode="after")
    def consistent(self) -> DatasetManifest:
        if (
            len(self.split_counts),
            len(self.source_counts),
            len(self.status_counts),
            len(self.class_counts),
            len(self.split_source_counts),
            len(self.quarantined_source_counts),
        ) != (3, 3, 6, 6, 3, 3) or any(len(v) != 3 for v in self.split_source_counts.values()):
            raise ValueError("count maps require exact categories")
        if len({f.path.casefold() for f in self.files}) != len(self.files):
            raise ValueError("manifest paths must be unique ignoring case")
        if (
            self.completed_count > self.requested_count
            or self.accepted_count + self.quarantined_count != self.completed_count
        ):
            raise ValueError("lifecycle counts inconsistent")
        if (
            sum(self.split_counts.values()) != self.accepted_count
            or sum(self.class_counts.values()) != self.accepted_count
            or sum(self.source_counts.values()) != self.completed_count
            or sum(self.status_counts.values()) != self.completed_count
            or self.status_counts["quarantined"] != self.quarantined_count
        ):
            raise ValueError("totals inconsistent")
        if sum(self.quarantined_source_counts.values()) != self.quarantined_count:
            raise ValueError("quarantined source total inconsistent")
        for split in ("train", "validation", "test"):
            if sum(self.split_source_counts[split].values()) != self.split_counts[split]:
                raise ValueError("split/source row inconsistent")
        for source in ("fake", "fixture", "live"):
            if (
                self.split_source_counts["train"][source]
                + self.split_source_counts["validation"][source]
                + self.split_source_counts["test"][source]
                + self.quarantined_source_counts[source]
                != self.source_counts[source]
            ):
                raise ValueError("split/source column inconsistent")
        if self.manifest_id != manifest_identity(
            self.model_dump(mode="json", exclude={"manifest_id"})
        ):
            raise ValueError("manifest_id mismatch")
        return self


def canonical_json_bytes(value: BaseModel | Any) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def candidate_identity(content_without_id: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(content_without_id)).hexdigest()


def _normalized_candidate_content(content: Any) -> dict[str, Any]:
    """Validate and JSON-normalize the exact candidate identity preimage."""

    return _CandidateContent.model_validate(content).model_dump(mode="json")


def manifest_identity(content_without_id: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(content_without_id)).hexdigest()


def construct_candidate(**content: Any) -> TrainingCandidate:
    content = dict(content)
    content.pop("example_id", None)
    normalized = _normalized_candidate_content(content)
    return TrainingCandidate.model_validate(
        {"example_id": candidate_identity(normalized), **normalized}
    )


def _invariants(validator: Any, enabled: bool, instance: Any, schema: Any) -> Any:
    del validator
    if enabled:
        model = cast(
            type[BaseModel] | None,
            {
                x.__name__: x
                for x in (TrainingCandidate, TrainingExample, TeacherLabel, DatasetManifest)
            }.get(schema.get("title")),
        )
        if model:
            try:
                model.model_validate(instance)
            except Exception as error:
                yield JsonSchemaError(str(error))


_ContractValidator = extend(Draft202012Validator, {"x-tracehelix-invariants": _invariants})  # type: ignore[no-untyped-call]


def contract_validator(schema: dict[str, Any]) -> Any:
    return _ContractValidator(schema)


def generate_schemas(directory: str) -> None:
    from pathlib import Path

    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    for model, name in (
        (TrainingCandidate, "training-candidate"),
        (TrainingExample, "training-example"),
        (TeacherLabel, "teacher-label"),
        (DatasetManifest, "dataset-manifest"),
    ):
        schema = model.model_json_schema(mode="serialization")
        if "created_at" in schema["properties"]:
            schema["properties"]["created_at"]["format"] = "date-time"
        if model is DatasetManifest:
            schema["$defs"]["ManifestFile"]["properties"]["path"]["pattern"] = _SCHEMA_PATH_PATTERN
        schema.update(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": f"https://tracehelix.dev/schemas/{name}.schema.json",
                "$comment": "TraceHelix contract_validator is REQUIRED for relational identity/count/provenance invariants.",
                "x-tracehelix-invariants": True,
            }
        )
        (root / f"{name}.schema.json").write_text(
            json.dumps(schema, indent=2, sort_keys=True) + "\n"
        )
