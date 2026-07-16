# TraceHelix

TraceHelix is a local, auditable CLI-first analyzer for AI-agent execution traces. This verified slice imports generic JSONL into SQLite, applies deterministic step labels and six evidence-linked sequence detectors, compares runs, and exports JSON or self-contained HTML reports. It makes no causal claims.

## Prerequisites

- .NET SDK 10 (pinned by `global.json`)
- Node.js/npm for the build-only React/Vite shell
- Python 3.11+ and `uv` for the offline training-package shell

## Build and test

```bash
dotnet restore TraceHelix.slnx --locked-mode
dotnet format TraceHelix.slnx --verify-no-changes
dotnet build TraceHelix.slnx -c Release --no-restore
dotnet test TraceHelix.slnx -c Release --no-build
npm --prefix web ci
npm --prefix web run lint
npm --prefix web run typecheck
npm --prefix web run test -- --run
npm --prefix web run build
uv sync --project training --locked
uv run --project training ruff check training
uv run --project training mypy training/src
uv run --project training pytest -q training/tests
```

## CLI workflow

Build first, then run the real binary:

```bash
CLI="dotnet src/TraceHelix.Cli/bin/Release/net10.0/TraceHelix.Cli.dll"
$CLI import samples/generic-jsonl/minimal.jsonl --adapter generic-jsonl --db /tmp/tracehelix.db --json
$CLI analyze <run-id> --db /tmp/tracehelix.db --classifier rules --json
$CLI list --db /tmp/tracehelix.db --json
$CLI show <run-id> --db /tmp/tracehelix.db --events --alerts --json
$CLI compare <run-id> <run-id> --db /tmp/tracehelix.db --json
$CLI report <run-id> --db /tmp/tracehelix.db --format json --out /tmp/report.json
$CLI report <run-id> --db /tmp/tracehelix.db --format html --out /tmp/report.html

Report output is fail-closed: a report creates a new file and never overwrites any existing file or alias.
```

Or run `TRACEHELIX_VERIFY_DIR=/tmp/tracehelix-verification make verify-e2e`. Machine-readable command results, including partial-import diagnostics, go to stdout with no mixed prose. Human-readable usage, import-limit, operational, and I/O errors go to stderr. Exit codes are `0` success, `2` usage, `3` import, `4` analysis, and `5` storage/I/O.

Generic JSONL imports default to limits of 256 MiB total input, 1 MiB per record, 100,000 events, and 100,000 nonblank records. Every nonblank record consumes the record budget whether valid or malformed, bounding retained diagnostics. Cancellation is covered with pre-cancelled real operations and detector tests; real-process OS Ctrl+C timing remains a follow-up.

The web and Python directories are intentionally buildable shells only. This slice does not implement an API product, UI features, model preparation/training, ONNX, or live-AI evaluation.
