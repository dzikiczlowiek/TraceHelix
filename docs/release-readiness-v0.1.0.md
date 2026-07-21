# TraceHelix v0.1.0 release readiness

- **Reference release:** planned local, single-user source release `v0.1.0`
- **Status:** not yet tagged or published (no release date committed)
- **Source tree:** the reviewed source tree in this repository
- **Historical audit:** `prod-readiness-audit-08b3ea9.md` (audited commit
  `08b3ea9881763444bec2dbd1fc1dd41e2d20ecc8`), read-only and not copied into
  this tree. Findings below were re-checked against the current source, tests,
  and docs; they are not copied from the audit.

## Verdict

TraceHelix `v0.1.0` is an auditable, portfolio-ready **source** release. It is
**not production-grade** and is intended for a **local trusted single-user**
workstation with loopback-only API and browser access. It is not a network,
multi-user, or SaaS service and must not be reverse-proxied or exposed. The
table below maps each historical P1 finding to its current status; several
remain intentionally open and are carried as documented limitations of this
source release.

### Status vocabulary

- **closed** — the historical gap is addressed in the current source, with
  tests and/or architectural enforcement as evidence.
- **partially closed** — a meaningful control exists, but the historical concern
  is not fully resolved.
- **open-deferred** — the gap remains by design for this source release and is
  documented as a known limitation.

## Twelve-finding status map

| # | Historical finding | Status | Current exact evidence | v0.1.0 implication |
|---|---|---|---|---|
| P1-1 | Effective listener config could bypass the loopback validator | closed | `src/TraceHelix.Api/Program.cs` calls `LoopbackUrlValidator.RejectKestrelEndpointOverrides(builder.Configuration)` before `Build()`; `src/TraceHelix.Api/LoopbackUrlValidator.cs` throws if `Kestrel:Endpoints` has a value or children, and `Validate` requires every `URLS` entry to be http/https with an explicit port and a literal loopback/localhost host; wildcard needs `TRACEHELIX_ALLOW_WILDCARD=true` plus a container-runtime marker. Covered by `tests/TraceHelix.Api.Tests/LoopbackUrlValidatorTests.cs`. | The loopback listener guarantee holds for direct host execution on a trusted workstation. |
| P1-2 | Loopback API had no Host allowlist or Origin protection (and no auth) | partially closed | `Program.cs` sets `HostFilteringOptions.AllowedHosts = ["localhost","127.0.0.1","[::1]"]`, calls `UseHostFiltering()`, and same-origin-checks non-GET/HEAD/OPTIONS mutations (foreign/opaque `Origin` rejected; Vite dev origins allowlisted only in Development). There is still no per-launch bearer/session capability or per-run authorization; requests without `Origin` remain usable by trusted local clients. Covered by `tests/TraceHelix.Api.Tests/ApiIntegrationTests.cs`. | Reduces accidental and DNS-rebinding exposure on a trusted host; still not authentication or isolation against a hostile local process. Do not expose or reverse-proxy. |
| P1-3 | Safe reports serialize raw private traces and secrets | open-deferred | `src/TraceHelix.Infrastructure/Reporting/ReportWriters.cs` serializes the full `AnalysisReport` (which embeds `TraceRun` with raw events); HTML encoding prevents script injection but applies no redaction. `redaction-v1` (`docs/redaction-policy.md`) gates only the offline training-candidate export (`dataset export`), not CLI/API reports; `ExportReport.CreateAsync` reads the complete run. | Default reports may contain raw secrets. Treat all report artifacts as private local data; do not share without external review/redaction. |
| P1-4 | Raw databases and reports relied on ambient filesystem permissions | partially closed | `src/TraceHelix.Infrastructure/Persistence/SqliteRepositories.cs` opens the database via libc `open` with `O_NOFOLLOW`/`O_CLOEXEC` and mode `0600`, creates the parent directory `0700`, rejects group/other-writable parents, and re-asserts on every connection. Report files (`ReportFile.WriteNewAsync`) use `FileMode.CreateNew` with no explicit mode and inherit ambient umask. Covered by `tests/TraceHelix.Infrastructure.Tests/Persistence/SqliteStoreTests.cs`. | The SQLite database is owner-only on Linux/macOS; report files are not. Keep report output in a private directory. |
| P1-5 | Compare could return a torn "latest/latest" pair | open-deferred | `src/TraceHelix.Application/UseCases.cs` `CompareRuns.ExecuteAsync` performs independent `GetAsync(left)`, `GetAsync(right)`, `GetLatestAsync(left)`, `GetLatestAsync(right)` reads with no shared read transaction; `RunComparison` exposes only counts, not revision IDs or content hashes. | Comparisons describe independent metrics, not a snapshot-consistent, auditable pair. |
| P1-6 | Core query work is unbounded despite paged API responses | open-deferred | `SqliteRepositories.ListAsync`/`GetAsync`/`GetLatestAsync` select `data_json` and deserialize the full run aggregate; event paging loads the full run then slices in memory. Import bound is 256 MiB / 100,000 events per run; there is no run-count or database-size bound and no constant-memory read path. Documented in `docs/architecture.md`. | Not suitable for large or many-run query workloads; keep runs within import limits. |
| P1-7 | Analysis creation is non-idempotent and unbounded | open-deferred | `AnalyzeRun.ExecuteAsync` always creates a new `AnalysisRevision.Completed` with a fresh GUID and appends it; `Pending`/`Failed` states have no durable lifecycle; there is no idempotency key, attempt record, quota, or retention. | Retries and blind local requests create duplicate revisions; analysis is not a durable idempotent attempt lifecycle. |
| P1-8 | No schema compatibility, migration, backup, restore, or retention lifecycle | open-deferred | `SqliteStore.Initialize` uses `CREATE TABLE IF NOT EXISTS` with no `PRAGMA user_version`, migration ledger, compatibility validation, backup, restore, purge, or retention; only `PRAGMA journal_mode=DELETE` is enforced. | No supported upgrade or migration path. Treat each database as disposable; back up by copying a closed database file. |
| P1-9 | Core reads trust aggregate JSON and do not validate indexes | open-deferred | Normal read paths deserialize `data_json` without cross-checking `event_index`/`alert_index`; the indexes are written on save but not validated on read; there is no `quick_check`/`integrity` policy or rebuild workflow. The candidate exporter has stronger corruption checks than the application read path. | Latent index/aggregate divergence is not detected on the normal read path; there is no supported repair workflow. |
| P1-10 | Report output accepts SQLite-reserved sidecar paths | open-deferred | `src/TraceHelix.Cli/CliProgram.cs` `Report` rejects only exact `db == out` equality (`NormalizePath` plus `string.Equals`); it does not reject `<db>-wal`, `<db>-shm`, or `<db>-journal`. | A report `--out` can collide with SQLite's reserved sidecar namespace. Choose report paths outside the database namespace. |
| P1-11 | No self-contained, tested production distribution | partially closed | The production Docker Compose topology (nginx -> API -> SQLite) is now exercised end-to-end through a real Chromium browser acceptance suite: `scripts/verify-browser.sh` builds the digest-pinned images, brings the topology up on an ephemeral loopback port, seeds committed synthetic traces through the real containerized CLI, and runs `web/e2e/release.spec.ts` (accessible role/label selectors, no mocks, no test-only routes) with a bounded `Browser acceptance` CI job. The API still does not serve `web/dist` (Vite dev proxy only) and the training exporter is still invoked through `uv`/PATH rather than bundled. There is still no published self-contained binary, installer, service unit, signed checksum, deterministic release bundle, tag, or release workflow; the compose images are local build targets, not published artifacts. | Source-built and container-topology acceptance is now exercised in a real browser; published-artifact and install-from-artifact evidence remain a follow-up. |
| P1-12 | Repository and release governance did not enforce the tested state | partially closed | This change set adds `SECURITY.md`, `CONTRIBUTING.md`, `CHANGELOG.md`, `.github/CODEOWNERS`, `.github/pull_request_template.md`, this readiness map, and repository guards for version agreement and required files/anchors. CI already builds both images, generates SBOMs (`anchore/sbom-action`), and rejects HIGH/CRITICAL OS/library vulnerabilities (`trivy-action`). Branch protection, secret scanning, CodeQL/SAST, artifact signing, and provenance remain operator-side follow-ups and are not enforceable from this repository. | Governance documents and supply-chain scans exist; full branch/secret/SAST/signing enforcement is not part of this source release. |

## Explicit open limitations (confirmed by current code/docs)

- **Local trusted single-user only.** No multi-user, network, or SaaS claim; do
  not reverse-proxy or expose the listener.
- **Loopback only.** Direct host execution defaults to loopback; wildcard is
  fail-closed unless both the explicit opt-in and a container-runtime marker are
  present.
- **No authentication against a hostile local process.** Host filtering and
  same-origin checks reduce accidental exposure but are not auth/isolation.
- **Unversioned SQLite schema.** No migration, backup, restore, or retention
  lifecycle (P1-8).
- **Aggregate deserialization / query-scale limits.** Reads deserialize the full
  run aggregate; no constant-memory reads or multi-user throughput (P1-6).
- **Analysis revisions are not a durable idempotent attempt lifecycle** (P1-7).
- **Reports may contain raw trace content** under ambient file permissions
  (P1-3, P1-4).
- **No live AI/ML.** Analysis is deterministic rules plus six versioned
  detectors; training is an offline export/validation shell only.
- **No published release artifact.** Browser acceptance exercises the
  source-built compose topology in a real browser, but there is no deterministic
  release bundle, tag, signed checksum, release workflow, or install-from-artifact
  end-to-end evidence (P1-11).
- **No upload or arbitrary file browsing.** Raw input is never served as an
  arbitrary file path.

## Relationship to the historical audit

This document independently re-verifies the twelve P1 findings against the
current source, tests, and docs. It does not reproduce or copy the read-only
historical audit artifact, and it does not claim that every historical P1 is
fixed. Items marked `open-deferred` or `partially closed` are carried as
documented limitations of this bounded source release. A production-grade
release requires closing the open items and shipping and exercising a real
published artifact; that work is explicitly out of scope for `v0.1.0` and for
this pull request.
