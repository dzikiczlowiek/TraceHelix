# Canonical event schema v1

The generic JSONL importer defaults to four limits: 256 MiB total input, 1 MiB per record, 100,000 events, and 100,000 nonblank records. Every nonblank record counts toward the record limit, including malformed records. Limit failures are typed as `TOTAL_BYTES_LIMIT`, `RECORD_BYTES_LIMIT`, `EVENT_COUNT_LIMIT`, or `RECORD_COUNT_LIMIT`; the CLI exits 3.

`schemas/trace-event.schema.json` describes the exchange shape. The C# contract is `TraceHelix.Domain.Traces.TraceEvent`.

Every event has a non-negative run-local sequence, UTC-capable timestamp, kind, actor, optional summary, preserved JSON payload, artifact references, and a SHA-256 of the exact JSONL record bytes (excluding its line ending). `SourceReference` always records:

- adapter ID (`generic-jsonl`) and adapter version at run level;
- SHA-256 of the complete input;
- relative source path;
- zero-based UTF-8 byte offset;
- one-based line number;
- JSON pointer (`/` for the source record).

The generic input accepts one JSON object per UTF-8 line. Recognized properties are `timestamp`, `kind`, `actor`, `summary`, and `payload`; unknown properties remain available when the entire record is used as payload. Blank lines are ignored. Malformed records produce `INVALID_JSON` import diagnostics with line and offset rather than being silently dropped. CRLF is excluded from content hashing and offsets remain byte-based, so Unicode does not skew provenance.

Kinds: `Message`, `Reasoning`, `ToolCall`, `ToolResult`, `FileChange`, `Artifact`, `Status`, `Error`, and `Unknown`. Labels: `Explore`, `Plan`, `Execute`, `Verify`, `Recover`, and `Unknown`.
