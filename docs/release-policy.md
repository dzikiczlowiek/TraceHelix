# Release policy

This is `docs/release-policy.md`. It states the fail-closed release contract for
TraceHelix `v0.1.0` and the exact verification a reviewer must repeat. It is a
**local trusted single-user source release** and is **not production-grade**.
No release has been created yet; this document describes the workflow and the
verification a reviewer runs, not an announcement of a published artifact.

The authoritative version sources (`VERSION`, `Directory.Build.props`
`VersionPrefix`, `web/package.json`, `training/pyproject.toml`) are held to exact
equality by `scripts/test_repository_guards.py`. The release workflow at
`.github/workflows/release.yml` is the only path that may create a GitHub
Release, and it is fail-closed at every gate below.

## Triggers and the tag contract

- The workflow runs on a `push` to a tag matching `v*.*.*`, and on
  `workflow_dispatch` only.
- A `workflow_dispatch` run is a **dry-run**. It runs every required gate and
  stages the exact release artifact set, but it **never publishes**: it cannot
  create a tag and cannot create a GitHub Release.
- On a tag event the `validate-tag` job requires the tag to equal `v<VERSION>`
  exactly, where `<VERSION>` is the canonical SemVer read from `VERSION`. A
  malformed tag or a tag/version mismatch fails closed before any publication
  step. The `publish` job re-asserts the same equality a second time.

In short: the **tag equals VERSION** or the workflow refuses to proceed.

## Required gates before publication

The `publish` job `needs:` every required gate and only runs after all of them
succeed on the exact checked-out commit:

1. repository guards (`scripts/test_repository_guards.py`) and the dependency pin
   verifier (`scripts/verify_container_pins.py`);
2. .NET build, formatting, and tests;
3. web lint, generated-contract check, typecheck, tests, build, and audit;
4. Python Ruff, mypy, and pytest;
5. real-process CLI/API end-to-end (`make verify-e2e` and `make verify-api`);
6. digest-pinned container build, API and web SPDX SBOM generation, and Trivy
   HIGH/CRITICAL rejection;
7. browser acceptance through the production topology; and
8. deterministic extracted release-bundle acceptance (`scripts/verify-release-bundle.sh`).

A skipped or failed gate means no publication. There is no manual override and
no `if: always()` path that proceeds past a failed gate.

## Permissions

- The top-level workflow permission is `contents: read` only.
- Only the `publish` job requests elevated scopes, and exactly these three:
  `contents: write, id-token: write, attestations: write`.
- No job ever uses `permissions: write-all` or any other broad scope.
- Every GitHub Action is pinned to a full-length immutable commit SHA, and tools
  and images stay pinned under the existing repository conventions.

## Artifact set and immutable handoff

The `release-bundle` job is the only audited producer of the source bundle. Its
canonical script builds twice, strictly verifies and extracts the archive, then
runs the extracted policy, install, image, lifecycle, and browser gates. Only
after those gates it optionally exports the exact verified
`tracehelix-0.1.0-source.tar.gz`, `SHA256SUMS`, and extracted
`RELEASE-MANIFEST.json` to an empty external directory. Release notes are then
generated after that canonical export. The separately pinned source CycloneDX
action produces `tracehelix-0.1.0-source.cdx.json`; the container producer
creates `tracehelix-api.spdx.json` and `tracehelix-web.spdx.json` SPDX SBOMs.

The read-only `assemble-evidence` job always runs after every producer/gate. It
fails unless every prerequisite succeeded, downloads the release evidence plus
source/API/web SBOM artifacts, verifies the source checksum, rejects any file
set other than these seven files, and uploads exactly one **immutable**
`tracehelix-release-assets` artifact. The `publish` job uses
`actions/download-artifact` only for that unified artifact and **never**
rebuilds the bundle. A second, unaudited build inside publication is explicitly
forbidden.

## Checksums, provenance, and attestation

- `publish` re-verifies `sha256sum -c SHA256SUMS` over the source archive before
  doing anything else.
- On a tag event `publish` records build **provenance** and an **attestation**
  for the source archive using `actions/attest-build-provenance`, which requires
  `id-token: write`.
- The attestation binds the published artifact to the exact workflow run that
  produced and verified it.

## Never overwrite

- `publish` checks `gh release view "$TAG"` before creating anything; if a
  release already exists for that tag, the job fails rather than overwriting it.
- Tags are never force-pushed by this workflow; a release is created exactly
  once for a given tag; it must never overwrite an existing release or tag.

## Dry-run, tag, and public-download verification

This is the exact verification a reviewer repeats.

### Dry-run (no publication)

From a clean checkout of the default branch, trigger the workflow with
`workflow_dispatch`. The run must build every artifact, pass every gate, and
print a dry-run line stating that dispatch never publishes. No release or tag may
appear. Locally, the same artifact set and gates are reproduced by
`scripts/verify-release-bundle.sh` (see `docs/verification.md`).

### Tag publication

After an independent exact-snapshot review, push a tag `v<VERSION>` whose value
equals `VERSION`. Every gate runs on that tag; only then does `publish` create the
GitHub Release with the immutable artifact set, checksum, provenance, and
attestation. The release must contain exactly the artifact set listed above and
nothing rebuilt inside `publish`.

### Public-download verification

Once a release has been created, a reviewer downloads the published assets from
the GitHub Release (not from the workflow run) and repeats:

```bash
sha256sum -c SHA256SUMS   # over tracehelix-<VERSION>-source.tar.gz
tar -xOzf tracehelix-<VERSION>-source.tar.gz \
  "tracehelix-<VERSION>/RELEASE-MANIFEST.json" | python3 -m json.tool
```

then extracts the archive and re-runs `scripts/test_repository_guards.py`,
`scripts/verify_container_pins.py`, and `scripts/verify-release-bundle.sh` from
the extracted source, exactly as the local install-from-artifact gate does.
Public-download verification is a release follow-up procedure; because no release has been created yet, these steps have not been exercised against a published
artifact. Review the attestation against the published digest before trusting any
downloaded asset.
