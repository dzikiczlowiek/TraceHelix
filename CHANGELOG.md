# Changelog

All notable changes to TraceHelix are recorded here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). The
authoritative version sources are `VERSION`, `Directory.Build.props`
(`VersionPrefix`), `web/package.json`, and `training/pyproject.toml`, held to
exact equality by `scripts/test_repository_guards.py`.

## [Unreleased]

### Added
- Authoritative `VERSION` file and `Directory.Build.props` `VersionPrefix`, held
  to exact equality with `web/package.json` and `training/pyproject.toml` by
  repository guards with mutation tests.
- Release-readiness map (`docs/release-readiness-v0.1.0.md`) and bounded `v0.1.0`
  scope caveats in `README.md` and `docs/architecture.md`.
- Governance baseline: `SECURITY.md`, `CONTRIBUTING.md`, `.github/CODEOWNERS`,
  and `.github/pull_request_template.md`.
- Repository guards for version agreement, required governance/release file
  presence, and stable release-scope anchors (with mutation tests).
- Real-process browser acceptance through the production Docker Compose topology
  (nginx -> API -> SQLite) in Chromium: `scripts/verify-browser.sh` plus the
  `web/e2e/release.spec.ts` Playwright suite, with accessible role/label
  selectors, no retries, no mocks, no test-only routes, and project-labelled
  fail-closed teardown.
- Dedicated bounded `Browser acceptance` CI job (ubuntu-24.04, exact pinned
  Actions, Node 24.18.0) that installs `@playwright/test` 1.61.1, the Chromium
  browser with OS dependencies, and runs the verifier.
- Repository guards for the browser acceptance job, verifier, and documentation
  with focused mutation tests, and exact CI Action/line-count pins for the new
  job.
- Canonical local browser-acceptance command in `README.md` and
  `docs/verification.md`.
- Deterministic Git-object source bundle builder with canonical gzip/tar metadata,
  generated `RELEASE-MANIFEST.json`, and `SHA256SUMS` sidecar.
- Fail-closed bundle verifier and safe extractor with adversarial coverage for
  checksums, archive boundaries, paths, types, metadata, manifests, bounds, and
  partial-write cleanup.
- Canonical install-from-artifact gate that builds twice, compares bytes, then
  runs policy, digest-pinned Compose lifecycle, and Playwright from the verified
  extracted source; also wired as the bounded `Release bundle acceptance` CI job.

### Changed
- `docs/release-readiness-v0.1.0.md` P1-11 remains `partially closed`: deterministic
  local source-bundle and install-from-artifact evidence now exist, while the
  public tag/release, downloaded-public-artifact verification, and signing remain
  follow-ups.

## [0.1.0]

Planned local, single-user source release. Not yet tagged or published; no
release date is committed. This entry describes the intended scope of the first
source release and is not a release announcement.

### Scope
- Local trusted single-user analyzer; loopback-only v1 API and accessible React
  run browser.
- Generic JSONL import into SQLite, deterministic rules analysis, append-only
  analysis revisions, independent run comparison, and fail-closed JSON/HTML
  reports.
- Offline training-package export shell with the closed `redaction-v1` policy.

### Known open limitations
- Not production-grade: no authentication against a hostile local process;
  unversioned SQLite schema with no migration, backup, restore, or retention
  lifecycle; aggregate-deserialization query-scale limits; non-idempotent
  analysis revisions; and default reports that still serialize raw trace content
  under ambient file permissions.
- Not a network, multi-user, or SaaS service; do not reverse-proxy or expose it.
- No upload, arbitrary file browsing, live AI/ML, or ONNX inference. Browser
  acceptance now exercises the source-built compose topology, but there is no
  published release bundle, tag, release workflow, or install-from-artifact
  evidence.
