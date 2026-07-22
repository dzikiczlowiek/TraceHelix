# Verification

TraceHelix uses deterministic offline tests, real-process verification, repository policy guards, and container security gates. Generated databases, reports, SBOMs, vulnerability caches, and full command logs should remain outside Git (for example under `/tmp/tracehelix-verification`).

## Development evidence

The implementation follows RED/GREEN increments. Focused tests cover domain contracts, application orchestration, adapters, classifiers, detectors, import limits, CLI behavior, API routing and host enforcement, SQLite identity and permissions, and web interaction state. The committed suites are the durable evidence; transient RED output is not stored in the repository.

The initial preferred SQLite dependency restore failed because `SQLitePCLRaw.lib.e_sqlite3` 2.1.11 had a high-severity advisory treated as an error. The implementation retained SQLite and selected the patched native package explicitly rather than suppressing the warning.

## Real-process assertions

`scripts/verify-e2e.sh` runs import → analyze → list/show → compare → JSON/HTML reports against the committed synthetic fixture. It starts with a clean output directory because report creation never overwrites an existing path. It parses every JSON artifact, requires all six alert codes, checks every event's provenance and content hash, verifies report artifact SHA-256 values, and verifies that HTML output is self-contained.

`scripts/verify-api.sh` starts the production API on loopback against a CLI-created database and verifies readiness, pagination and cursors, ProblemDetails, run detail, rules analysis, alerts, comparison, host filtering, and listener fail-closed behavior.

`scripts/verify-compose-lifecycle.sh` starts Compose on a unique loopback port and verifies health, API isolation, edge routing, restart recovery, force-recreate recovery, Docker DNS re-resolution, container hardening, CLI network isolation, owner-only SQLite permissions, foreign-Origin mutation rejection without persistence, and successful same-origin mutation through the real nginx proxy.

`scripts/verify-browser.sh` is the canonical browser acceptance verifier. From a clean state it builds the digest-pinned production images, brings up the real Docker Compose topology (nginx web -> API -> SQLite) on an ephemeral loopback port, seeds two committed synthetic JSONL traces through the real containerized CLI, and runs the Playwright Chromium suite (`web/e2e/release.spec.ts`) against nginx with no implicit dev server. It is fail-closed on teardown: a failed teardown or any project-labelled container/network/volume residue turns a green Playwright run into a hard failure. Every Docker command is scoped to a unique `tracehelix-browser-<pid>` project, so no foreign Docker project is ever matched or actioned.

## Local and CI gates

CI runs on pushes to `main` and on every pull request. On pull requests every
checkout explicitly uses `${{ github.event.pull_request.head.sha || github.sha }}`:
hosted evidence therefore executes the exact PR head rather than GitHub's
synthetic merge ref; push runs resolve the same expression to `github.sha`.
GitHub Actions, setup runtimes, security tools, and external container/stage
references are checked against exact repository allowlists. The `ubuntu-24.04`
and `windows-2025` hosted-runner labels avoid `latest` aliases, but GitHub can
update the underlying VM images; CI runner hosts are not content-addressed.

| Area | CI coverage | Local equivalent |
|---|---|---|
| Repository policy | Guard tests plus exact Docker image/stage, CI Action SHA, runtime, and security-tool allowlists | `python scripts/test_repository_guards.py && python scripts/verify_container_pins.py` |
| .NET | Exact SDK from `global.json`; locked restore, formatting, Release build, and tests on Ubuntu and Windows | `dotnet restore TraceHelix.slnx --locked-mode && dotnet format TraceHelix.slnx --verify-no-changes --no-restore && dotnet build TraceHelix.slnx -c Release --no-restore && dotnet test TraceHelix.slnx -c Release --no-build --no-restore` |
| Real-process CLI/API | Release build followed by both shell verifiers | `make verify-e2e && make verify-api` after the Release build |
| Web | Locked install, generated OpenAPI contract gate, ESLint, TypeScript, Vitest/jsdom, production build, and high-severity dependency audit | `cd web && npm ci && npm run lint && npm run check:api && npm run typecheck && npm exec -- vitest run && npm run build && npm audit --audit-level=high` |
| Python training | Locked uv environment, Ruff, strict mypy, and pytest | `cd training && uv sync --locked && uv run ruff check . && uv run mypy . && uv run pytest` |
| Containers | Digest-pinned build, Compose restart/recreate lifecycle, API and web SBOMs, Trivy HIGH/CRITICAL rejection | `docker compose --profile tools build --pull && bash scripts/verify-compose-lifecycle.sh`; generate and scan artifacts outside Git |
| Browser acceptance | Real production topology in Chromium, project-labelled cleanup, no retries, no mocks, no test-only routes | From the repository root, install once with `(cd web && npm ci && npm exec --offline -- playwright install --with-deps chromium)`, then run `sg docker -c "bash scripts/verify-browser.sh"` (the Docker group is needed locally) |
| Release bundle acceptance | Two byte-identical source bundles from committed Git objects, canonical checksum, fail-closed verification and safe extraction, then policy, Compose lifecycle, and browser gates from the extracted artifact | After the same one-time Playwright Chromium install, run `sg docker -c "bash scripts/verify-release-bundle.sh"` from the repository root |

The container and release-bundle gates expect BuildKit/Docker access and network access to resolve exact pinned dependencies. The release bundle itself is deterministic and contains only eligible committed source plus `RELEASE-MANIFEST.json`; `.hermes`, caches, build/test/browser outputs, local databases/bytecode, environment files, and private imports are excluded by both builder and verifier. Dependencies are restored from lock files during the extracted-artifact smoke. Runtime trace analysis and training tests remain deterministic and offline; no gate makes a live AI or network model call.

## Release workflow proof

No public GitHub Release exists yet. The release workflow is available for a
read-only dispatch proof from `main`; dispatch runs every gate and assembles
workflow evidence, but the job with write scopes is skipped:

```bash
gh workflow run release.yml --ref main
gh run watch
RUN_ID=$(gh run list --workflow release.yml --branch main --limit 1 --json databaseId --jq '.[0].databaseId')
gh run download "$RUN_ID" --name tracehelix-release-assets --dir /tmp/tracehelix-release-proof
gh release list
```

Record the run URL, successful gate list, downloaded `tracehelix-release-assets`
file inventory, and the final empty `gh release list` (or the absence of a new
release) as the no-release assertion. The unified evidence must contain exactly
the source archive, `SHA256SUMS`, `RELEASE-MANIFEST.json`, release notes, and
source/API/web SBOMs. The `assemble-evidence` job output is the deterministic
SHA-256 over all seven names and bytes; retain it with the run evidence because
`publish` independently compares it after download. It is not a public eighth
asset. The source checksum must also pass in that download.

After an independent exact-snapshot review, create and push only a matching tag:

```bash
VERSION=$(tr -d '\r\n' < VERSION)
git tag "v$VERSION"
git push origin "v$VERSION"
gh run watch
```

Only that pushed `refs/tags/v<VERSION>` event starts `publish`; it downloads the
assembled evidence and never rebuilds the archive. After a future release is
visible, verify as an anonymous/public consumer in a fresh directory (do not
send a GitHub token):

```bash
mkdir -p /tmp/tracehelix-public-check && cd /tmp/tracehelix-public-check
BASE="https://github.com/dzikiczlowiek/TraceHelix/releases/download/v$VERSION"
curl -fLO "$BASE/tracehelix-${VERSION}-source.tar.gz"
curl -fLO "$BASE/SHA256SUMS"
curl -fLO "$BASE/RELEASE-MANIFEST.json"
sha256sum -c SHA256SUMS
tar -xzf "tracehelix-${VERSION}-source.tar.gz"
cd "tracehelix-${VERSION}"
python3 scripts/test_repository_guards.py
python3 scripts/verify_container_pins.py
```

Use a fresh clone or extracted archive for the remaining consumer checks rather
than trusting the releasing checkout. Verify the published provenance attestation
against the archive digest before relying on the downloaded asset.

## Exact-snapshot review

Use `python scripts/source_fingerprint.py` before and after every independent review, and once more after staging. The v2 fingerprint binds:

- the base `HEAD`;
- every tracked or non-ignored untracked path present in the prospective working tree;
- each path's file type, Unix mode, and exact content.

The implementation isolates itself from ambient Git routing and ignore configuration and intentionally excludes index-only state. Staging unchanged bytes therefore preserves the fingerprint; any documentation, source, test, configuration, content, type, or mode change produces a new snapshot and invalidates earlier review verdicts. Before commit, require the reviewed fingerprint both before and after `git add --all`, inspect the staged diff and tree ID, then commit without editing files and require a clean worktree afterward.

A passing release candidate requires all configured gates and independent adversarial reviews to report zero blockers on the same exact snapshot. Green tests from a different fingerprint are not substitute evidence.

Repository guards strictly parse both workflow files, reject duplicate YAML
keys, and compare the complete canonical parsed semantics against reviewed
SHA-256 pins. This makes mutations to producer commands, permissions,
prerequisite handling, concurrency, or publication wiring fail even when the
edited text preserves an old guard substring.
