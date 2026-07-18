# Training candidate export

```text
tracehelix dataset export --db tracehelix.db --out candidates.jsonl \
  --source-category fixture --license-or-consent fixture-generated \
  --mode online --context-before 4 --context-after 0
```

`source-category` and `license-or-consent` are mandatory because the current database does not store them. `online` requires a zero future window; `offline-analysis` permits up to 64 future events. Both context windows are limited to 64, and an export is limited to 100,000 events. Stored run JSON is bounded to 64 MiB per run and 512 MiB total before it is loaded.

Runs are ordered by stored `imported_at,id`; events are ordered by the unique stored sequence (with event ID as an explicit tie-breaker). Context never crosses a run. Because the current schema has no task-group or lineage columns, `task_group_id` and `lineage_id` map to the run ID rather than inventing metadata. Event text is deterministic, key-sorted canonical JSON of the complete stored canonical event (including timestamp, actor, source, artifacts, summary, and payload); `tool_name` maps only from a string `payload.tool`. Exact run/event/source/artifact schemas, lowercase SHA-256 fields, row-to-JSON metadata, and event-index parity are validated before export. Event content hashes identify the adapter's original raw record bytes, so they cannot be recomputed from the canonical event and are instead format-checked and matched to the index.

`source_hash` is SHA-256 over canonical JSON containing `run_input_sha256`, `event_content_sha256`, `adapter`, and `adapter_version`. It therefore identifies stable stored source evidence without hashing mutable SQLite/WAL bytes. `example_id` is produced only by the packaged Python `construct_candidate` privacy gate after redaction and normalization.

The database is opened read-only in a snapshot transaction. Output is compact, key-sorted UTF-8 JSONL using LF; non-empty output has a terminal LF and an empty export is a zero-byte file. Output is written through a mode-0600 same-directory temporary file, synced, and atomically replaced only after every row passes the gate. Existing output is preserved on failure. Existing symlink/reparse-point outputs are rejected rather than followed or replaced.

The CLI discovers this repository's `training/pyproject.toml` only by walking upward from its application directory, an intentional monorepo coupling; environment variables cannot redirect it. It runs uv with `run --offline --frozen --no-sync --project <training>` after the normal locked restore, preventing index access, lock updates, and environment synchronization. Process arguments are passed without a shell, and discovery paths and child stderr are never included in failure diagnostics.
