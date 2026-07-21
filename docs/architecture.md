# Architecture

TraceHelix is a local-first modular monolith with a separately built browser frontend.

## Application layers

- **Domain** owns immutable run, canonical event, classification, alert, analysis revision, comparison, and report contracts.
- **Application** owns ports and use-case orchestration. `AnalyzeRun` creates a new analysis revision instead of overwriting an earlier classifier result.
- **Infrastructure** supplies the streaming generic JSONL adapter, SHA-256 hashing, raw SQLite repositories, deterministic rules, six versioned detectors, and report writers.
- **CLI** is the import, analysis, inspection, comparison, and report interface. Its JSON output is the stable machine interface for this slice.
- **API** exposes health, run summaries and detail, sequence-paged events, latest analysis and alerts, deterministic rules analysis, and independent run comparison under `/api/v1`.
- **Web** is an accessible React client for browsing runs, loading event pages, inspecting evidence, running deterministic analysis, viewing alerts, and comparing two runs. Generated OpenAPI types bind the client to the API contract.
- **Training** contains the locked, offline Python export and validation tooling. It does not make live model calls.

## Persistence and data flow

Generic JSONL is imported through the CLI into a local SQLite database. The API opens the same database and the web client reaches it only through the API. Raw input is never served as an arbitrary file path.

SQLite uses `Microsoft.Data.Sqlite` with explicit schema creation rather than EF Core migrations. Runs have a unique `(input_hash, adapter, adapter_version)` identity. Events are stored as an immutable aggregate JSON document; analysis revisions are append-only rows. On Linux and macOS, the existing database directory is canonicalized before the connection string is built and must not be writable by group or other users. Before every SQLite connection, the database is atomically opened or created with `O_NOFOLLOW`, verified as seekable before any permission change, and restricted through the open handle to owner-only `0600`; a later final-component symlink substitution is therefore rejected without changing its target. Canonicalizing the parent means later replacement of a directory symlink alias cannot redirect connections. These controls rely on the database-directory owner being the trusted local operator. On Windows, the final database symlink is rechecked before every connection, but this release does not claim atomic reparse-point protection or owner-only ACL enforcement against a hostile process running under the same account. Initialization explicitly requires `PRAGMA journal_mode=DELETE`, operations disable pooling and release their connections after use, and successful completion leaves no persistent WAL or shared-memory sidecars.

The aggregate JSON design favors auditability and deterministic reconstruction over query-scale performance: run detail, event paging, analysis, and comparison deserialize the stored run aggregate before projecting a response. Import limits bound a run to 100,000 events and 256 MiB input, but this release does not claim constant-memory reads or multi-user query throughput.

This is a documented deviation from the plan's preferred EF Core implementation, not from SQLite or transactional behavior.

## Listener and host boundary

Direct host execution is loopback-only by default. `URLS` and `ASPNETCORE_URLS` are validated before Kestrel starts, and framework-native `Kestrel:Endpoints` overrides are rejected because they could otherwise outrank the intended binding. Production host filtering accepts only localhost and loopback host names.

Loopback binding and Host filtering reduce accidental network exposure. State-changing browser requests with an `Origin` header must be same-origin in production; opaque or foreign origins are rejected before endpoint execution, while the two Vite origins are allowlisted only in Development. Requests without `Origin` remain available to trusted local CLI clients, so this is not authentication against a hostile local process. This release assumes a trusted single-user workstation and is not suitable as-is for a shared or hostile local host or a remote deployment.

The explicit `TRACEHELIX_ALLOW_WILDCARD=true` opt-in is honored only when a Docker/OCI runtime marker (`/.dockerenv` or `/run/.containerenv`) is present. Compose uses it for the containerized API on its unexposed internal Docker network; the same environment variable is fail-closed during normal host execution.

## Container topology

The multi-stage Dockerfile produces two runtime images:

- a non-root .NET API image that also contains the CLI binary;
- a non-root nginx image containing the static web build.

Compose connects the API only to `tracehelix-internal`, which is marked `internal: true`. The web container joins both that network and an edge network; it is the only service with a published port, bound to `127.0.0.1`. Nginx validates the incoming Host header, adds browser security headers, and resolves the API through Docker DNS so API restart, recreation, or IP replacement does not strand the proxy.

The API, web, and CLI containers use read-only root filesystems, drop all Linux capabilities, and set `no-new-privileges`. The CLI has no network namespace, mounts `imports/` read-only, and shares only the named SQLite data volume with the API.

Dockerfile syntax, `FROM` dependencies, external `COPY --from`/`RUN --mount` stage references, GitHub Action SHAs, language/tool versions, runtime images, and the .NET SDK are checked against repository allowlists. CI uses versioned hosted-runner labels, but GitHub updates the image behind each label; the CI host image is therefore not content-addressed. CI builds both images, exercises runtime topology plus restart and force-recreate recovery, generates SBOMs, and rejects HIGH or CRITICAL operating-system and library vulnerabilities.

## Scope and claims

All trace data stays local. Databases, raw/private traces, generated reports, model artifacts, dependency trees, and build/cache output are ignored by Git. Reports and comparisons describe observed evidence and explicitly avoid causal claims.

Deferred capabilities include browser upload, arbitrary source-file browsing, sequence zoom/alignment, browser-level Playwright coverage, learned classifiers, ONNX inference, and live AI integration.
