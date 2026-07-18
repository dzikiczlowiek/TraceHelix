"""Read TraceHelix SQLite snapshots and export privacy-gated candidates."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, NoReturn

from tracehelix_training.contracts import (
    TrainingCandidate,
    canonical_json_bytes,
    construct_candidate,
)
from tracehelix_training.io import ExportError, atomic_write_candidates

_connect = sqlite3.connect

_MAX_CONTEXT = 64
_MAX_EVENTS = 100_000
# Runs may contain no events, so cap them independently at the same deterministic
# workload ceiling as indexed events.
_MAX_RUNS = 100_000
# Bound JSON before SQLite materializes it into Python. Kept separate for boundary tests.
_MAX_RUN_JSON_BYTES = 64 * 1024 * 1024
_MAX_TOTAL_RUN_JSON_BYTES = 512 * 1024 * 1024
_HASH_LENGTH = 64
_RUN_KEYS = {
    "id",
    "name",
    "inputSha256",
    "adapter",
    "adapterVersion",
    "importedAt",
    "events",
    "diagnostics",
}
_EVENT_KEYS = {
    "id",
    "runId",
    "sequence",
    "timestamp",
    "kind",
    "actor",
    "summary",
    "payload",
    "source",
    "artifacts",
    "contentSha256",
}
_SOURCE_KEYS = {"adapter", "inputSha256", "relativePath", "byteOffset", "line", "jsonPointer"}
_ARTIFACT_KEYS = {"name", "path", "sha256"}
_KINDS = {
    "Message",
    "Reasoning",
    "ToolCall",
    "ToolResult",
    "FileChange",
    "Artifact",
    "Status",
    "Error",
    "Unknown",
}
_DIAGNOSTIC_KEYS = {"line", "byteOffset", "code", "message"}
# name, declared type, NOT NULL, primary-key position. These are the actual
# SqliteStore declarations; value validation still respects SQLite dynamic typing.
_EXPECTED = {
    "runs": [
        ("id", "TEXT", 0, 1),
        ("input_hash", "TEXT", 1, 0),
        ("adapter", "TEXT", 1, 0),
        ("adapter_version", "TEXT", 1, 0),
        ("imported_at", "TEXT", 1, 0),
        ("data_json", "TEXT", 1, 0),
    ],
    "event_index": [
        ("run_id", "TEXT", 1, 1),
        ("sequence", "INTEGER", 1, 2),
        ("event_id", "TEXT", 1, 0),
        ("kind", "TEXT", 1, 0),
        ("content_hash", "TEXT", 1, 0),
    ],
}


@dataclass(frozen=True)
class ExportOptions:
    source_category: str
    license_or_consent: str
    mode: str = "online"
    context_before: int = 4
    context_after: int = 0

    def validate(self) -> None:
        if self.source_category not in {"fake", "fixture", "live"}:
            raise ExportError("source category is required and must be explicit")
        if not self.license_or_consent.strip():
            raise ExportError("license or consent is required and must be explicit")
        if self.mode not in {"online", "offline-analysis"}:
            raise ExportError("mode must be online or offline-analysis")
        if type(self.context_before) is not int or not 0 <= self.context_before <= _MAX_CONTEXT:
            raise ExportError("context-before is outside the supported range")
        if type(self.context_after) is not int or not 0 <= self.context_after <= _MAX_CONTEXT:
            raise ExportError("context-after is outside the supported range")
        if self.mode == "online" and self.context_after != 0:
            raise ExportError("online mode requires context-after zero")


def _path_key(path: Path) -> str:
    return os.path.normcase(os.fspath(path.resolve(strict=False)))


def _same_file(left: Path, right: Path) -> bool:
    try:
        return left.samefile(right)
    except (FileNotFoundError, OSError):
        return _path_key(left) == _path_key(right)


def _invalid_export_paths(database: Path, output: Path) -> bool:
    try:
        source_key = _path_key(database)
        forbidden = {
            source_key,
            *(f"{source_key}{suffix}" for suffix in ("-wal", "-shm", "-journal")),
        }
        return (
            not database.is_file() or _same_file(database, output) or _path_key(output) in forbidden
        )
    except (OSError, RuntimeError):
        return True


def _schema(connection: sqlite3.Connection) -> None:
    # Table names are fixed constants, never supplied by a caller.
    for pragma, expected in (
        ("PRAGMA table_info(runs)", _EXPECTED["runs"]),
        ("PRAGMA table_info(event_index)", _EXPECTED["event_index"]),
    ):
        actual = [
            (str(row[1]), str(row[2]).upper(), row[3], row[5]) for row in connection.execute(pragma)
        ]
        if actual != expected:
            raise ExportError("unsupported database schema")
    foreign_keys = list(connection.execute("PRAGMA foreign_key_list(event_index)"))
    unique_indexes = [
        row[1] for row in connection.execute("PRAGMA index_list(runs)") if row[2] == 1
    ]
    has_import_identity = any(
        [
            column[0]
            for column in connection.execute(
                "SELECT name FROM pragma_index_info(?) ORDER BY seqno", (name,)
            )
        ]
        == ["input_hash", "adapter", "adapter_version"]
        for name in unique_indexes
    )
    if not has_import_identity or not any(
        row[2] == "runs" and row[3] == "run_id" and row[4] == "id" for row in foreign_keys
    ):
        raise ExportError("unsupported database schema")
    version = connection.execute("PRAGMA user_version").fetchone()
    if version is None or version[0] != 0:
        raise ExportError("unsupported database schema")


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExportError("stored metadata is malformed")
    return value


def _guid(value: Any) -> str:
    if not isinstance(value, str):
        raise ExportError("stored identifier is malformed")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise ExportError("stored identifier is malformed") from None
    if parsed.int == 0 or str(parsed) != value:
        raise ExportError("stored identifier is malformed")
    return value


def _timestamp(value: Any) -> str:
    if not isinstance(value, str) or "T" not in value:
        raise ExportError("stored timestamp is malformed")
    # DateTimeOffset's System.Text.Json representation has an explicit Z or ±HH:mm
    # offset and at most seven fractional second digits.
    import re

    match = re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,7})?(?:Z|([+-])(\d{2}):(\d{2}))",
        value,
    )
    if match is None:
        raise ExportError("stored timestamp is malformed")
    offset_hour = int(match.group(2) or 0)
    offset_minute = int(match.group(3) or 0)
    if offset_minute > 59 or offset_hour > 14 or (offset_hour == 14 and offset_minute != 0):
        raise ExportError("stored timestamp is malformed")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ExportError("stored timestamp is malformed") from None
    if parsed.utcoffset() is None:
        raise ExportError("stored timestamp is malformed")
    return value


def _bounded_int(value: Any, minimum: int, maximum: int) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _hash(value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value) != _HASH_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ExportError("stored hash is malformed")
    return value


def _optional_text(value: Any) -> bool:
    return value is None or isinstance(value, str)


def _event(
    raw: Any, run_id: str, input_hash: str, adapter: str, index: tuple[Any, ...]
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ExportError("stored event is malformed")
    if (
        set(raw) != _EVENT_KEYS
        or _guid(raw["id"]) != raw["id"]
        or raw["runId"] != run_id
        or not _bounded_int(raw["sequence"], 0, 9_223_372_036_854_775_807)
        or raw["kind"] not in _KINDS
    ):
        raise ExportError("stored event is malformed")
    source, artifacts = raw["source"], raw["artifacts"]
    if (
        _timestamp(raw["timestamp"]) != raw["timestamp"]
        or not isinstance(raw["actor"], str)
        or not raw["actor"].strip()
        or not _optional_text(raw["summary"])
        or not isinstance(source, dict)
        or set(source) != _SOURCE_KEYS
        or not isinstance(source["adapter"], str)
        or not source["adapter"].strip()
        or not isinstance(source["relativePath"], str)
        or not source["relativePath"].strip()
        or not _optional_text(source["jsonPointer"])
        or not (
            source["byteOffset"] is None
            or _bounded_int(source["byteOffset"], 0, 9_223_372_036_854_775_807)
        )
        or not (source["line"] is None or _bounded_int(source["line"], 1, 2_147_483_647))
        or not isinstance(artifacts, list)
    ):
        raise ExportError("stored event is malformed")
    if source["inputSha256"] != input_hash or source["adapter"] != adapter:
        raise ExportError("stored event is inconsistent")
    _hash(source["inputSha256"])
    for artifact in artifacts:
        if (
            not isinstance(artifact, dict)
            or set(artifact) != _ARTIFACT_KEYS
            or not isinstance(artifact["name"], str)
            or not _optional_text(artifact["path"])
            or not _optional_text(artifact["sha256"])
        ):
            raise ExportError("stored event is malformed")
        if artifact["sha256"] is not None:
            _hash(artifact["sha256"])
    event_id, sequence, kind, content_hash = index
    if (raw["id"], raw["sequence"], raw["kind"], raw["contentSha256"]) != (
        event_id,
        sequence,
        kind,
        content_hash,
    ):
        raise ExportError("stored event index is inconsistent")
    payload = raw["payload"]
    # The canonical schema intentionally permits every JSON value as payload.
    tool = payload.get("tool") if isinstance(payload, dict) else None
    if isinstance(payload, dict) and "tool" in payload and not isinstance(tool, str):
        raise ExportError("stored event is malformed")
    return {
        "event_id": _guid(event_id),
        # The deterministic evidence preimage is the complete stored canonical event.
        "event_text": canonical_json_bytes(raw).decode(),
        "event_kind": _text(kind),
        "tool_name": tool,
        "content_hash": _hash(content_hash),
    }


def _strict_json_loads(value: str) -> Any:
    def object_from_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate object key")
            result[key] = item
        return result

    def reject_constant(_: str) -> NoReturn:
        raise ValueError("non-standard JSON constant")

    return json.loads(value, object_pairs_hook=object_from_pairs, parse_constant=reject_constant)


def _rows(connection: sqlite3.Connection, options: ExportOptions) -> Iterator[TrainingCandidate]:
    total = connection.execute("SELECT COUNT(*) FROM event_index").fetchone()[0]
    reachable = connection.execute(
        "SELECT COUNT(*) FROM event_index INNER JOIN runs ON event_index.run_id = runs.id"
    ).fetchone()[0]
    if not _bounded_int(total, 0, _MAX_EVENTS) or not _bounded_int(reachable, 0, _MAX_EVENTS):
        raise ExportError("export workload exceeds the supported limit")
    if reachable != total:
        raise ExportError("stored event index is inconsistent")
    # Preflight with one fixed aggregate before even metadata can be materialized.
    # SQLite raises on integer SUM overflow; translate that without exposing details.
    try:
        preflight = connection.execute(
            "SELECT COUNT(*),"
            "COALESCE(SUM(length(CAST(data_json AS BLOB))),0),"
            "COALESCE(MAX(length(CAST(data_json AS BLOB))),0),"
            "COALESCE(SUM(CASE WHEN typeof(data_json)!='text' THEN 1 ELSE 0 END),0) "
            "FROM runs"
        ).fetchone()
    except sqlite3.Error:
        raise ExportError("candidate export failed") from None
    if preflight is None or len(preflight) != 4:
        raise ExportError("stored run aggregate is malformed")
    run_count, aggregate_bytes, maximum_bytes, non_text_count = preflight
    if not _bounded_int(run_count, 0, _MAX_RUNS):
        if type(run_count) is int and run_count > _MAX_RUNS:
            raise ExportError("export workload exceeds the supported limit")
        raise ExportError("stored run aggregate is malformed")
    if (
        not _bounded_int(aggregate_bytes, 0, _MAX_TOTAL_RUN_JSON_BYTES)
        or not _bounded_int(maximum_bytes, 0, _MAX_RUN_JSON_BYTES)
        or not _bounded_int(non_text_count, 0, run_count)
        or maximum_bytes > aggregate_bytes
        or (run_count == 0 and (aggregate_bytes != 0 or maximum_bytes != 0))
    ):
        if type(aggregate_bytes) is int and aggregate_bytes > _MAX_TOTAL_RUN_JSON_BYTES:
            raise ExportError("export workload exceeds the supported limit")
        if type(maximum_bytes) is int and maximum_bytes > _MAX_RUN_JSON_BYTES:
            raise ExportError("stored run exceeds the supported limit")
        raise ExportError("stored run aggregate is malformed")
    if non_text_count != 0:
        raise ExportError("stored run exceeds the supported limit")

    # This bounded phase deliberately cannot materialize data_json. Per-row checks
    # remain as defense in depth before any JSON-bearing SELECT is issued.
    metadata = list(
        connection.execute(
            "SELECT id,input_hash,adapter,adapter_version,imported_at,"
            "typeof(data_json),length(CAST(data_json AS BLOB)) FROM runs "
            "ORDER BY imported_at,id LIMIT ?",
            (_MAX_RUNS + 1,),
        )
    )
    if len(metadata) != run_count:
        raise ExportError("stored run aggregate is malformed")
    total_bytes = 0
    validated_events = 0
    for row in metadata:
        storage_type, data_bytes = row[5], row[6]
        if storage_type != "text" or not _bounded_int(data_bytes, 0, _MAX_RUN_JSON_BYTES):
            raise ExportError("stored run exceeds the supported limit")
        total_bytes += data_bytes
        if total_bytes > _MAX_TOTAL_RUN_JSON_BYTES:
            raise ExportError("export workload exceeds the supported limit")
    for run_id_value, input_hash, adapter, adapter_version, imported_at, _, _ in metadata:
        run_id = _guid(run_id_value)
        data_row = connection.execute("SELECT data_json FROM runs WHERE id=?", (run_id,)).fetchone()
        if data_row is None or not isinstance(data_row[0], str):
            raise ExportError("stored run is malformed")
        try:
            run = _strict_json_loads(data_row[0])
        except (TypeError, ValueError):
            raise ExportError("stored run is malformed") from None
        if (
            not isinstance(run, dict)
            or set(run) != _RUN_KEYS
            or run.get("id") != run_id
            or run.get("inputSha256") != input_hash
            or run.get("adapter") != adapter
            or run.get("adapterVersion") != adapter_version
            or run.get("importedAt") != imported_at
            or not isinstance(run.get("name"), str)
            or not run["name"].strip()
            or not isinstance(run.get("events"), list)
            or not isinstance(run.get("diagnostics"), list)
        ):
            raise ExportError("stored run is malformed")
        _hash(input_hash)
        _text(adapter)
        _text(adapter_version)
        _timestamp(imported_at)
        for diagnostic in run["diagnostics"]:
            if (
                not isinstance(diagnostic, dict)
                or set(diagnostic) != _DIAGNOSTIC_KEYS
                or not (
                    diagnostic["line"] is None or _bounded_int(diagnostic["line"], 1, 2_147_483_647)
                )
                or not (
                    diagnostic["byteOffset"] is None
                    or _bounded_int(diagnostic["byteOffset"], 0, 9_223_372_036_854_775_807)
                )
                or not isinstance(diagnostic["code"], str)
                or not isinstance(diagnostic["message"], str)
            ):
                raise ExportError("stored run is malformed")
        indexed = list(
            connection.execute(
                "SELECT event_id,sequence,kind,content_hash FROM event_index "
                "WHERE run_id=? ORDER BY sequence,event_id",
                (run_id,),
            )
        )
        if (
            len(indexed) != len(run["events"])
            or len({row[0] for row in indexed}) != len(indexed)
            or any(
                type(row[1]) is not int or row[1] != position
                for position, row in enumerate(indexed)
            )
            or any(
                not isinstance(item, dict) or item.get("sequence") != position
                for position, item in enumerate(run["events"])
            )
        ):
            raise ExportError("stored event index is inconsistent")
        events = [
            _event(run["events"][position], run_id, input_hash, adapter, row)
            for position, row in enumerate(indexed)
        ]
        validated_events += len(events)
        for position, event in enumerate(events):
            before = events[max(0, position - options.context_before) : position]
            after = events[position + 1 : position + 1 + options.context_after]
            source_preimage = {
                "adapter": _text(adapter),
                "adapter_version": _text(adapter_version),
                "event_content_sha256": event["content_hash"],
                "run_input_sha256": _text(input_hash),
            }
            source_hash = hashlib.sha256(canonical_json_bytes(source_preimage)).hexdigest()
            context_keys = ("event_id", "event_text", "event_kind", "tool_name")
            yield construct_candidate(
                schema_version="1.0.0",
                run_id=run_id,
                event_id=event["event_id"],
                task_group_id=run_id,
                lineage_id=run_id,
                context_before=[{key: item[key] for key in context_keys} for item in before],
                event_text=event["event_text"],
                context_after=[{key: item[key] for key in context_keys} for item in after],
                event_kind=event["event_kind"],
                tool_name=event["tool_name"],
                source_category=options.source_category,
                source_hash=source_hash,
                adapter=adapter,
                adapter_version=adapter_version,
                redaction_version="redaction-v1",
                license_or_consent=options.license_or_consent,
            )
    if validated_events != total:
        raise ExportError("stored event index is inconsistent")


def export_candidates(database: Path, output: Path, options: ExportOptions) -> int:
    """Export from a read transaction; no source content is written before gating."""
    options.validate()
    if _invalid_export_paths(database, output):
        raise ExportError("database and output paths are invalid")
    try:
        uri = database.resolve().as_uri() + "?mode=ro"
        with _connect(uri, uri=True) as connection:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            _schema(connection)
            return atomic_write_candidates(output, _rows(connection, options))
    except ExportError:
        raise
    except (sqlite3.Error, OSError):
        raise ExportError("candidate export failed") from None
    except Exception:
        raise ExportError("candidate export failed") from None


def main(argv: list[str] | None = None) -> int:
    class PrivateParser(argparse.ArgumentParser):
        def error(self, message: str) -> NoReturn:
            raise ExportError("invalid command arguments")

    parser = PrivateParser(prog="python -m tracehelix_training.extract")
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--source-category", required=True)
    parser.add_argument("--license-or-consent", required=True)
    parser.add_argument("--mode", required=True)
    parser.add_argument("--context-before", required=True, type=int)
    parser.add_argument("--context-after", required=True, type=int)
    try:
        supplied = sys.argv[1:] if argv is None else argv
        option_names = [item for item in supplied if item.startswith("--")]
        if len(option_names) != len(set(option_names)):
            raise ExportError("invalid command arguments")
        args = parser.parse_args(supplied)
        count = export_candidates(
            args.db,
            args.out,
            ExportOptions(
                args.source_category,
                args.license_or_consent,
                args.mode,
                args.context_before,
                args.context_after,
            ),
        )
        print(json.dumps({"candidateCount": count}, separators=(",", ":"), sort_keys=True))
        return 0
    except ExportError as error:
        print(f"Dataset export error: {error}", file=sys.stderr)
        return 6


if __name__ == "__main__":
    raise SystemExit(main())
