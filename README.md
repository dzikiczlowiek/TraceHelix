# TraceHelix

TraceHelix is a local, auditable analyzer for AI-agent execution traces. It imports generic JSONL into SQLite, applies deterministic step labels and evidence-linked detectors, compares runs, and exposes a loopback-only v1 API and accessible React run browser. Analysis requests create append-only deterministic revisions; TraceHelix makes no causal claims.

## Release scope (v0.1.0)

TraceHelix `v0.1.0` is a planned **local trusted single-user source release**.
It is an auditable, portfolio-ready source tree; it is **not production-grade**
and **not yet tagged** or published. It runs loopback-only on a single trusted
workstation and is **not a network, multi-user, or SaaS service**: do not
reverse-proxy it, expose it to a network, or run it on a host shared with an
untrusted process. It provides no authentication against a hostile local process
and no schema migration, backup, restore, retention, upload, live AI/ML, or
arbitrary file-browsing capability. The twelve-finding status map and explicit
open limitations are in [`docs/release-readiness-v0.1.0.md`](docs/release-readiness-v0.1.0.md).

Governance: [`SECURITY.md`](SECURITY.md), [`CONTRIBUTING.md`](CONTRIBUTING.md),
[`CHANGELOG.md`](CHANGELOG.md).

## Prerequisites

- .NET SDK 10.0.302 exactly (enforced by `global.json`; install this SDK before running host commands)
- Node.js/npm for the build-only React/Vite shell
- Python 3.11+ and `uv` for the offline training-package shell

## Build and test

```bash
python scripts/test_repository_guards.py
python scripts/verify_container_pins.py
dotnet restore TraceHelix.slnx --locked-mode
uv sync --project training --locked
dotnet format TraceHelix.slnx --verify-no-changes
dotnet build TraceHelix.slnx -c Release --no-restore
dotnet test TraceHelix.slnx -c Release --no-build
npm --prefix web ci
npm --prefix web run lint
npm --prefix web run typecheck
npm --prefix web run test -- --run
npm --prefix web run build
npm --prefix web audit --audit-level=high
make verify-api
uv run --project training ruff check training
uv run --project training mypy training/src
uv run --project training pytest -q training/tests
```

For exact-snapshot reviews, run `python scripts/source_fingerprint.py` before and after the review and after staging. The v2 fingerprint covers `HEAD` plus the type, Unix mode, and exact content of every tracked or non-ignored untracked path in the prospective working tree; staging unchanged bytes preserves the value.

## CLI workflow

Build first, then run the real binary:

```bash
CLI="dotnet src/TraceHelix.Cli/bin/Release/net10.0/TraceHelix.Cli.dll"
TRACEHELIX_WORK=$(mktemp -d)
chmod 700 "$TRACEHELIX_WORK"
DB="$TRACEHELIX_WORK/tracehelix.db"
$CLI import samples/generic-jsonl/minimal.jsonl --adapter generic-jsonl --db "$DB" --json
$CLI analyze <run-id> --db "$DB" --classifier rules --json
$CLI list --db "$DB" --json
$CLI show <run-id> --db "$DB" --events --alerts --json
$CLI compare <run-id> <run-id> --db "$DB" --json
$CLI report <run-id> --db "$DB" --format json --out "$TRACEHELIX_WORK/report.json"
$CLI report <run-id> --db "$DB" --format html --out "$TRACEHELIX_WORK/report.html"

Report output is fail-closed: a report creates a new file and never overwrites any existing file or alias.
```

Or run `TRACEHELIX_VERIFY_DIR=/tmp/tracehelix-verification make verify-e2e`. Machine-readable command results, including partial-import diagnostics, go to stdout with no mixed prose. Human-readable usage, import-limit, operational, and I/O errors go to stderr. Exit codes are `0` success, `2` usage, `3` import, `4` analysis, and `5` storage/I/O.

Generic JSONL imports default to limits of 256 MiB total input, 1 MiB per record, 100,000 events, and 100,000 nonblank records. Every nonblank record consumes the record budget whether valid or malformed, bounding retained diagnostics. Cancellation is covered with pre-cancelled real operations and detector tests; real-process OS Ctrl+C timing remains a follow-up.

## Local API and web browser

Set `TRACEHELIX_DB` to a CLI-created database and run `dotnet run --project src/TraceHelix.Api`. The default listener is `http://127.0.0.1:5080`; the API provides health, run list/detail, sequence-paged events (maximum 200), latest analysis/alerts, rules analysis, and independent comparison under `/api/v1`. Development exposes the generated contract at `/openapi/v1.json`. Regenerate both committed artifacts from endpoint metadata with `cd web && npm run generate:api`; verify they were already current with `cd web && npm run check:api`. `web/src/api.ts` consumes the generated schema types in `web/src/api/generated.ts`.

Run `npm --prefix web run dev`; Vite binds to loopback and proxies API requests. Deep links support `/runs/{id}` and `/compare?left={id}&right={id}`. The UI displays raw counts and denominators and does not assert causal proof.

## Docker Compose

Build and start the production API and static web UI:

```bash
docker compose up --build -d
```

Open <http://127.0.0.1:8080>. The host port is loopback-only by default. To choose another loopback port:

```bash
TRACEHELIX_PORT=8181 docker compose up --build -d
```

TraceHelix keeps SQLite state in the named `tracehelix-data` volume. To import a trace, place it in `imports/` and run the containerized CLI:

```bash
cp /path/to/trace.jsonl imports/trace.jsonl
docker compose run --rm cli import /imports/trace.jsonl \
  --adapter generic-jsonl --db /data/tracehelix.db --json
```

The API and CLI share the same SQLite volume, while `imports/` is mounted read-only. Inspect status and logs with `docker compose ps` and `docker compose logs --tail=100`. Stop containers without deleting data with `docker compose down`. Delete containers **and the persistent database volume** only when intended with `docker compose down --volumes`.

The deployment uses separate non-root API and nginx images on a Docker-internal bridge network. Only nginx publishes a host port, and that port is bound to host loopback. The API permits a wildcard listener only when both the explicit opt-in and a Docker/OCI runtime marker are present; the same flag fails closed during direct host execution. Nginx resolves the API through Docker DNS so API restart or recreation does not strand the public path. Run `docker compose --profile tools build --pull && bash scripts/verify-compose-lifecycle.sh` to exercise the pinned build, restart, and recreation recovery.

## Browser acceptance

The real-process browser acceptance verifier exercises the production Docker Compose topology (nginx -> API -> SQLite) in Chromium, seeding two committed synthetic JSONL traces through the real containerized CLI and asserting accessible role/label selectors with no retries, mocks, or test-only routes. Install dependencies and the browser once, then run the verifier from the repository root through the Docker group (Docker access is needed locally):

```bash
(cd web && npm ci && npm exec -- playwright install --with-deps chromium)
sg docker -c "bash scripts/verify-browser.sh"
```

`scripts/verify-browser.sh` brings up its own `tracehelix-browser-<pid>` project on an ephemeral loopback port and is fail-closed on teardown: a green Playwright run that leaves any project-labelled container, network, or volume residue is reported as a hard failure, and no foreign Docker project is ever matched. The same verifier runs in CI as the bounded `Browser acceptance` job (`timeout-minutes: 20`, exact pinned Actions, Node 24.18.0).

See [`docs/architecture.md`](docs/architecture.md) for trust boundaries and data flow, and [`docs/verification.md`](docs/verification.md) for the complete local/CI gate matrix and exact-snapshot review rules.

Deferred intentionally: import upload, source-file/source viewer endpoints, arbitrary file access, sequence zoom/alignment, ML/training, ONNX, and live AI.
