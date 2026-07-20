# Verification

TraceHelix uses deterministic offline tests, real-process verification, repository policy guards, and container security gates. Generated databases, reports, SBOMs, vulnerability caches, and full command logs should remain outside Git (for example under `/tmp/tracehelix-verification`).

## Development evidence

The implementation follows RED/GREEN increments. Focused tests cover domain contracts, application orchestration, adapters, classifiers, detectors, import limits, CLI behavior, API routing and host enforcement, SQLite identity and permissions, and web interaction state. The committed suites are the durable evidence; transient RED output is not stored in the repository.

The initial preferred SQLite dependency restore failed because `SQLitePCLRaw.lib.e_sqlite3` 2.1.11 had a high-severity advisory treated as an error. The implementation retained SQLite and selected the patched native package explicitly rather than suppressing the warning.

## Real-process assertions

`scripts/verify-e2e.sh` runs import → analyze → list/show → compare → JSON/HTML reports against the committed synthetic fixture. It starts with a clean output directory because report creation never overwrites an existing path. It parses every JSON artifact, requires all six alert codes, checks every event's provenance and content hash, verifies report artifact SHA-256 values, and verifies that HTML output is self-contained.

`scripts/verify-api.sh` starts the production API on loopback against a CLI-created database and verifies readiness, pagination and cursors, ProblemDetails, run detail, rules analysis, alerts, comparison, host filtering, and listener fail-closed behavior.

`scripts/verify-compose-lifecycle.sh` starts Compose on a unique loopback port and verifies health, API isolation, edge routing, restart recovery, force-recreate recovery, Docker DNS re-resolution, container hardening, CLI network isolation, owner-only SQLite permissions, foreign-Origin mutation rejection without persistence, and successful same-origin mutation through the real nginx proxy.

## Local and CI gates

CI runs on pushes to `main` and on every pull request. GitHub Actions, setup runtimes, security tools, and external container/stage references are checked against exact repository allowlists. The `ubuntu-24.04` and `windows-2025` hosted-runner labels avoid `latest` aliases, but GitHub can update the underlying VM images; CI runner hosts are not content-addressed.

| Area | CI coverage | Local equivalent |
|---|---|---|
| Repository policy | Guard tests plus exact Docker image/stage, CI Action SHA, runtime, and security-tool allowlists | `python scripts/test_repository_guards.py && python scripts/verify_container_pins.py` |
| .NET | Exact SDK from `global.json`; locked restore, formatting, Release build, and tests on Ubuntu and Windows | `dotnet restore TraceHelix.slnx --locked-mode && dotnet format TraceHelix.slnx --verify-no-changes --no-restore && dotnet build TraceHelix.slnx -c Release --no-restore && dotnet test TraceHelix.slnx -c Release --no-build --no-restore` |
| Real-process CLI/API | Release build followed by both shell verifiers | `make verify-e2e && make verify-api` after the Release build |
| Web | Locked install, generated OpenAPI contract gate, ESLint, TypeScript, Vitest/jsdom, production build, and high-severity dependency audit | `cd web && npm ci && npm run lint && npm run check:api && npm run typecheck && npm exec -- vitest run && npm run build && npm audit --audit-level=high` |
| Python training | Locked uv environment, Ruff, strict mypy, and pytest | `cd training && uv sync --locked && uv run ruff check . && uv run mypy . && uv run pytest` |
| Containers | Digest-pinned build, Compose restart/recreate lifecycle, API and web SBOMs, Trivy HIGH/CRITICAL rejection | `docker compose --profile tools build --pull && bash scripts/verify-compose-lifecycle.sh`; generate and scan artifacts outside Git |

The container gate expects BuildKit/Docker access and network access to resolve exact pinned dependencies and current vulnerability data. Runtime trace analysis and training tests remain deterministic and offline; no gate makes a live AI or network model call.

## Exact-snapshot review

Use `python scripts/source_fingerprint.py` before and after every independent review, and once more after staging. The v2 fingerprint binds:

- the base `HEAD`;
- every tracked or non-ignored untracked path present in the prospective working tree;
- each path's file type, Unix mode, and exact content.

The implementation isolates itself from ambient Git routing and ignore configuration and intentionally excludes index-only state. Staging unchanged bytes therefore preserves the fingerprint; any documentation, source, test, configuration, content, type, or mode change produces a new snapshot and invalidates earlier review verdicts. Before commit, require the reviewed fingerprint both before and after `git add --all`, inspect the staged diff and tree ID, then commit without editing files and require a clean worktree afterward.

A passing release candidate requires all configured gates and independent adversarial reviews to report zero blockers on the same exact snapshot. Green tests from a different fingerprint are not substitute evidence.
