# Verification record

This file records the RED/GREEN commands used while implementing the first slice. Full, freshly generated output should be retained outside Git (for example `/tmp/tracehelix-verification`).

## TDD evidence

| Increment | RED command and observed failure | GREEN command and observed result |
|---|---|---|
| Bootstrap | `dotnet test TraceHelix.slnx` → exit 1, `MSB1009` solution missing | `dotnet test TraceHelix.slnx -c Release` → exit 0, six smoke projects passed |
| Domain contracts | `dotnet test tests/TraceHelix.Domain.Tests/... -c Release` → exit 1, trace/analysis types absent | same command → exit 0, 8 tests |
| Application ports/use cases | `dotnet test tests/TraceHelix.Application.Tests/... -c Release` → exit 1, ports/use cases absent | same command → exit 0, 2 tests |
| Adapter/classifier/detectors | `dotnet test tests/TraceHelix.Infrastructure.Tests/... -c Release` → exit 1, infrastructure types absent | same command → exit 0, 14 tests after persistence/report behavior coverage |
| Input/classifier hardening | same focused infrastructure command → exit 1, typed malformed record threw and tool-call build classified `Unknown` | same command → exit 0, 16 tests |
| CLI | `dotnet test tests/TraceHelix.Cli.Tests/... -c Release` → exit 1, `CliProgram` absent | same command → exit 0, 2 tests |

The initial preferred SQLite dependency restore also failed with exit 1 because `SQLitePCLRaw.lib.e_sqlite3` 2.1.11 had a high-severity advisory treated as an error. The implementation retained SQLite and explicitly selected patched native package 3.53.3; restore then passed without suppressing the warning.

## Workflow assertions

`scripts/verify-e2e.sh` runs import → analyze → list/show → compare → JSON/HTML reports against the committed synthetic fixture. It starts with a clean output directory because report creation never overwrites an existing path. It parses every JSON artifact, requires exactly all six alert codes, checks every event's provenance and content hash, verifies report artifact SHA-256 values, and verifies the HTML is self-contained. Generated databases and reports remain outside Git.

## Local and CI gates

CI runs on pushes to `main` and on every pull request. The equivalent local gates are:

| Area | CI coverage | Local equivalent |
|---|---|---|
| .NET | Locked restore, format verification, Release build, and tests on Ubuntu and Windows | `dotnet restore TraceHelix.slnx --locked-mode && dotnet format TraceHelix.slnx --verify-no-changes --no-restore && dotnet build TraceHelix.slnx -c Release --no-restore && dotnet test TraceHelix.slnx -c Release --no-build --no-restore` |
| Real-process E2E | Ubuntu Release build followed by the committed shell verifier | `make verify-e2e` (after the Release build above) |
| Web | Locked install, ESLint, TypeScript checks, Vitest, production build, and high-severity dependency audit | `cd web && npm ci && npm run lint && npm run typecheck && npm exec -- vitest run && npm run build && npm audit --audit-level=high` |
| Python training | Locked uv environment, Ruff, strict mypy, and pytest | `cd training && uv sync --locked && uv run ruff check . && uv run mypy . && uv run pytest` |

All fixtures and classifiers exercised by these gates are deterministic and offline. CI requires no secrets and makes no live AI or network model calls; network access is used only to install locked tool and package dependencies.
