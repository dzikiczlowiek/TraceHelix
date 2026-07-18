import hashlib
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any, cast

import pytest

from tracehelix_training.contracts import TrainingCandidate, canonical_json_bytes
from tracehelix_training import extract
from tracehelix_training.extract import ExportOptions, export_candidates
from tracehelix_training.io import ExportError


def gid(label: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"tracehelix-test:{label}"))


def make_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript("""
    CREATE TABLE runs(id TEXT PRIMARY KEY,input_hash TEXT NOT NULL,adapter TEXT NOT NULL,adapter_version TEXT NOT NULL,imported_at TEXT NOT NULL,data_json TEXT NOT NULL,UNIQUE(input_hash,adapter,adapter_version));
    CREATE TABLE event_index(run_id TEXT NOT NULL,sequence INTEGER NOT NULL,event_id TEXT NOT NULL,kind TEXT NOT NULL,content_hash TEXT NOT NULL,PRIMARY KEY(run_id,sequence),FOREIGN KEY(run_id) REFERENCES runs(id));
    CREATE TABLE analyses(id TEXT PRIMARY KEY,run_id TEXT NOT NULL,created_at TEXT NOT NULL,data_json TEXT NOT NULL);
    CREATE TABLE alert_index(analysis_id TEXT NOT NULL,ordinal INTEGER NOT NULL,run_id TEXT NOT NULL,code TEXT NOT NULL,start_sequence INTEGER NOT NULL,end_sequence INTEGER NOT NULL,PRIMARY KEY(analysis_id,ordinal));
    """)
    for run_id, imported, values in (
        (gid("run-b"), "2026-01-01T00:00:00.0000000+00:00", ["b0"]),
        (gid("run-a"), "2026-01-01T00:00:00.0000000+00:00", ["a0", "a1", "a2"]),
    ):
        events = []
        for sequence, event_id in enumerate(values):
            payload = (
                {"tool": "read", "nested": {"authorization": "Bearer abcdefghijklmnop"}}
                if event_id == "a1"
                else {}
            )
            event = {
                "id": gid(event_id),
                "runId": run_id,
                "sequence": sequence,
                "timestamp": "2026-01-01T00:00:00Z",
                "kind": "ToolCall",
                "actor": "agent",
                "summary": "contact person@example.com" if event_id == "a1" else event_id,
                "payload": payload,
                "source": {
                    "adapter": "generic-jsonl",
                    "inputSha256": hashlib.sha256(run_id.encode()).hexdigest(),
                    "relativePath": "fixture.jsonl",
                    "byteOffset": sequence,
                    "line": sequence + 1,
                    "jsonPointer": "/",
                },
                "artifacts": [],
                "contentSha256": hashlib.sha256(event_id.encode()).hexdigest(),
            }
            events.append(event)
            connection.execute(
                "INSERT INTO event_index VALUES(?,?,?,?,?)",
                (run_id, sequence, gid(event_id), "ToolCall", event["contentSha256"]),
            )
        run = {
            "id": run_id,
            "name": run_id,
            "inputSha256": hashlib.sha256(run_id.encode()).hexdigest(),
            "adapter": "generic-jsonl",
            "adapterVersion": "1.0",
            "importedAt": imported,
            "events": events,
            "diagnostics": [],
        }
        connection.execute(
            "INSERT INTO runs VALUES(?,?,?,?,?,?)",
            (run_id, run["inputSha256"], "generic-jsonl", "1.0", imported, json.dumps(run)),
        )
    connection.commit()
    connection.close()


def options(**changes: object) -> ExportOptions:
    values = {
        "source_category": "fixture",
        "license_or_consent": "fixture-generated",
        "mode": "online",
        "context_before": 2,
        "context_after": 0,
    }
    values.update(changes)
    return ExportOptions(**values)  # type: ignore[arg-type]


def read_rows(path: Path) -> list[TrainingCandidate]:
    assert path.read_bytes().endswith(b"\n") or path.read_bytes() == b""
    return [TrainingCandidate.model_validate_json(line) for line in path.read_text().splitlines()]


def test_deterministic_redacted_export_and_provenance(tmp_path: Path) -> None:
    database, first, second = tmp_path / "db.sqlite", tmp_path / "one.jsonl", tmp_path / "two.jsonl"
    make_db(database)
    assert export_candidates(database, first, options()) == 4
    assert export_candidates(database, second, options()) == 4
    assert first.read_bytes() == second.read_bytes()
    rows = read_rows(first)
    assert [row.event_id for row in rows] == [gid("a0"), gid("a1"), gid("a2"), gid("b0")]
    assert all(row.task_group_id == row.run_id == row.lineage_id for row in rows)
    assert all(row.adapter == "generic-jsonl" and row.adapter_version == "1.0" for row in rows)
    assert b"person@example.com" not in first.read_bytes()
    assert b"abcdefghijklmnop" not in first.read_bytes()
    expected = {
        "adapter": "generic-jsonl",
        "adapter_version": "1.0",
        "event_content_sha256": hashlib.sha256(b"a1").hexdigest(),
        "run_input_sha256": hashlib.sha256(gid("run-a").encode()).hexdigest(),
    }
    assert rows[1].source_hash == hashlib.sha256(canonical_json_bytes(expected)).hexdigest()
    assert rows[-1].context_before == []


def test_online_and_offline_context_are_run_bounded(tmp_path: Path) -> None:
    database, output = tmp_path / "db", tmp_path / "out"
    make_db(database)
    export_candidates(database, output, options(mode="offline-analysis", context_after=1))
    rows = read_rows(output)
    assert [event.event_id for event in rows[0].context_after] == [gid("a1")]
    assert rows[2].context_after == []
    with pytest.raises(ExportError):
        export_candidates(database, output, options(context_after=1))


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-01-01T00:00:00+14:00",
        "2026-01-01T00:00:00-14:00",
        "2026-01-01T00:00:00.1234567+14:00",
    ],
)
def test_datetimeoffset_boundary_timestamps_are_accepted(timestamp: str) -> None:
    assert extract._timestamp(timestamp) == timestamp


@pytest.mark.parametrize(
    "timestamp",
    ["2026-01-01T00:00:00+14:01", "2026-01-01T00:00:00-14:01", "2026-01-01T00:00:00+23:00"],
)
def test_datetimeoffset_out_of_domain_offsets_are_rejected(timestamp: str) -> None:
    with pytest.raises(ExportError, match="stored timestamp is malformed"):
        extract._timestamp(timestamp)


@pytest.mark.parametrize("corruption", ["duplicate", "nan", "infinity"])
def test_stored_json_must_be_strict_and_have_unique_keys(tmp_path: Path, corruption: str) -> None:
    database = tmp_path / "db"
    make_db(database)
    connection = sqlite3.connect(database)
    stored = connection.execute(
        "SELECT data_json FROM runs WHERE id=?", (gid("run-b"),)
    ).fetchone()[0]
    if corruption == "duplicate":
        stored = stored.replace('"name":', '"name":"duplicate","name":', 1)
    elif corruption == "nan":
        stored = stored.replace('"payload": {}', '"payload": NaN', 1)
    else:
        stored = stored.replace('"payload": {}', '"payload": Infinity', 1)
    connection.execute("UPDATE runs SET data_json=? WHERE id=?", (stored, gid("run-b")))
    connection.commit()
    connection.close()
    with pytest.raises(ExportError, match="stored run is malformed"):
        export_candidates(database, tmp_path / "out", options())


def test_failures_preserve_output_and_leave_no_temp(tmp_path: Path) -> None:
    database, output = tmp_path / "db", tmp_path / "out"
    make_db(database)
    output.write_text("existing")
    connection = sqlite3.connect(database)
    connection.execute("UPDATE runs SET data_json='not json' WHERE id=?", (gid("run-b"),))
    connection.commit()
    connection.close()
    with pytest.raises(ExportError, match="stored run is malformed"):
        export_candidates(database, output, options())
    assert output.read_text() == "existing"
    assert list(tmp_path.glob(".out.*.tmp")) == []


@pytest.mark.parametrize(
    "changes",
    [
        {"source_category": ""},
        {"license_or_consent": ""},
        {"mode": "bad"},
        {"context_before": -1},
        {"context_after": 65},
    ],
)
def test_invalid_options_fail(tmp_path: Path, changes: dict[str, object]) -> None:
    database = tmp_path / "db"
    make_db(database)
    with pytest.raises(ExportError):
        export_candidates(database, tmp_path / "out", options(**changes))


def test_orphaned_event_index_rejects_without_replacing_output(tmp_path: Path) -> None:
    database, output = tmp_path / "db", tmp_path / "out"
    make_db(database)
    output.write_text("existing")
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute(
        "INSERT INTO event_index VALUES(?,?,?,?,?)",
        (gid("orphan"), 0, gid("orphan-event"), "Message", "0" * 64),
    )
    connection.commit()
    connection.close()

    with pytest.raises(ExportError, match="stored event index is inconsistent"):
        export_candidates(database, output, options())
    assert output.read_text() == "existing"
    assert list(tmp_path.glob(".out.*.tmp")) == []


def test_bad_schema_alias_and_empty_database(tmp_path: Path) -> None:
    bad = tmp_path / "bad"
    sqlite3.connect(bad).close()
    with pytest.raises(ExportError):
        export_candidates(bad, tmp_path / "out", options())
    database = tmp_path / "db"
    make_db(database)
    with pytest.raises(ExportError):
        export_candidates(database, database, options())
    connection = sqlite3.connect(database)
    connection.execute("DELETE FROM event_index")
    connection.execute("DELETE FROM runs")
    connection.commit()
    connection.close()
    output = tmp_path / "empty"
    assert export_candidates(database, output, options()) == 0
    assert output.read_bytes() == b""


@pytest.mark.parametrize(
    "payload", [[1, {"authorization": "Bearer abcdefghijklmnop"}], "scalar", 7, True, None]
)
def test_schema_faithful_non_object_payloads_are_preserved_and_redacted(
    tmp_path: Path, payload: object
) -> None:
    database, output = tmp_path / "db", tmp_path / "out"
    make_db(database)
    connection = sqlite3.connect(database)
    raw = json.loads(
        connection.execute("SELECT data_json FROM runs WHERE id=?", (gid("run-b"),)).fetchone()[0]
    )
    raw["events"][0]["payload"] = payload
    connection.execute("UPDATE runs SET data_json=? WHERE id=?", (json.dumps(raw), gid("run-b")))
    connection.commit()
    connection.close()
    export_candidates(database, output, options())
    row = next(item for item in read_rows(output) if item.run_id == gid("run-b"))
    event = json.loads(row.event_text)
    if isinstance(payload, list):
        assert "abcdefghijklmnop" not in row.event_text
        assert event["payload"][0] == 1
    else:
        assert event["payload"] == payload
    assert row.tool_name is None


def test_excessive_runs_are_rejected_before_metadata_materialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "db"
    make_db(database)
    monkeypatch.setattr(extract, "_MAX_RUNS", 1)
    metadata_selected = False
    original_connect = sqlite3.connect

    class TracedConnection(sqlite3.Connection):
        pass

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        kwargs["factory"] = TracedConnection
        connection = cast(sqlite3.Connection, original_connect(*args, **kwargs))
        connection.set_trace_callback(mark)
        return connection

    def mark(sql: str) -> None:
        nonlocal metadata_selected
        normalized = " ".join(sql.upper().split())
        if normalized.startswith("SELECT ID,INPUT_HASH,ADAPTER,ADAPTER_VERSION,IMPORTED_AT"):
            metadata_selected = True

    monkeypatch.setattr(extract, "_connect", connect)
    with pytest.raises(ExportError, match="workload exceeds"):
        export_candidates(database, tmp_path / "out", options())
    assert not metadata_selected
    assert not (tmp_path / "out").exists()


def test_oversize_is_rejected_before_any_data_json_select(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "db"
    make_db(database)
    monkeypatch.setattr(extract, "_MAX_RUN_JSON_BYTES", 1)
    selected_json = False
    original_connect = sqlite3.connect

    class TracedConnection(sqlite3.Connection):
        pass

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        kwargs["factory"] = TracedConnection
        connection = cast(sqlite3.Connection, original_connect(*args, **kwargs))
        connection.set_trace_callback(mark)
        return connection

    def mark(sql: str) -> None:
        nonlocal selected_json
        if sql.lstrip().upper().startswith("SELECT DATA_JSON"):
            selected_json = True

    monkeypatch.setattr(extract, "_connect", connect)
    with pytest.raises(ExportError, match="stored run exceeds"):
        export_candidates(database, tmp_path / "out", options())
    assert not selected_json


def test_rejects_noncanonical_domain_values_and_wrong_declared_schema(tmp_path: Path) -> None:
    database = tmp_path / "db"
    make_db(database)
    connection = sqlite3.connect(database)
    raw = json.loads(
        connection.execute("SELECT data_json FROM runs WHERE id=?", (gid("run-a"),)).fetchone()[0]
    )
    raw["events"][0]["timestamp"] = "2026-01-01"
    connection.execute("UPDATE runs SET data_json=? WHERE id=?", (json.dumps(raw), gid("run-a")))
    connection.commit()
    connection.close()
    with pytest.raises(ExportError, match="timestamp"):
        export_candidates(database, tmp_path / "out", options())

    wrong = tmp_path / "wrong"
    connection = sqlite3.connect(wrong)
    connection.executescript("""
    CREATE TABLE runs(id BLOB PRIMARY KEY,input_hash TEXT NOT NULL,adapter TEXT NOT NULL,adapter_version TEXT NOT NULL,imported_at TEXT NOT NULL,data_json TEXT NOT NULL);
    CREATE TABLE event_index(run_id TEXT NOT NULL,sequence INTEGER NOT NULL,event_id TEXT NOT NULL,kind TEXT NOT NULL,content_hash TEXT NOT NULL,PRIMARY KEY(run_id,sequence),FOREIGN KEY(run_id) REFERENCES runs(id));
    """)
    connection.close()
    with pytest.raises(ExportError, match="unsupported database schema"):
        export_candidates(wrong, tmp_path / "other", options())


def test_wal_sidecar_output_is_rejected_without_touching_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "source.sqlite"
    make_db(database)
    writer = sqlite3.connect(database)
    assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    # A committed write creates WAL and shared-memory files; retaining the connection
    # prevents SQLite's last-close checkpoint from removing them.
    writer.execute("UPDATE runs SET imported_at=imported_at")
    writer.commit()
    sidecars = [Path(f"{database}{suffix}") for suffix in ("-wal", "-shm", "-journal")]
    existing = [path for path in sidecars if path.exists()]
    assert {path.name for path in existing} >= {"source.sqlite-wal", "source.sqlite-shm"}
    before = {path: path.read_bytes() for path in [database, *existing]}
    opened = False

    def forbidden_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        nonlocal opened
        opened = True
        raise AssertionError("read connection must not be opened")

    monkeypatch.setattr(extract, "_connect", forbidden_connect)
    try:
        with pytest.raises(ExportError, match="database and output paths are invalid"):
            export_candidates(database, Path(f"{database}-wal"), options())
        assert not opened
        assert {path: path.read_bytes() for path in before} == before
        assert list(tmp_path.glob(".source.sqlite-wal.*.tmp")) == []
    finally:
        writer.close()


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_resolved_sidecar_aliases_are_rejected_before_connect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    database = real / "source.sqlite"
    make_db(database)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    output = alias / f"source.sqlite{suffix}"
    output.write_bytes(b"existing-sidecar")
    monkeypatch.chdir(tmp_path)

    def forbidden_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        raise AssertionError("read connection must not be opened")

    monkeypatch.setattr(extract, "_connect", forbidden_connect)
    with pytest.raises(ExportError, match="database and output paths are invalid"):
        export_candidates(
            Path("real/source.sqlite"), Path(f"alias/source.sqlite{suffix}"), options()
        )
    assert output.read_bytes() == b"existing-sidecar"
    assert list(real.glob(f".source.sqlite{suffix}.*.tmp")) == []
