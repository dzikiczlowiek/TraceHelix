# Contributing

TraceHelix is a local, single-user source release. Contributions must preserve
the deterministic offline guarantees, the exact-snapshot review discipline, and
the bounded `v0.1.0` scope described in
[`docs/release-readiness-v0.1.0.md`](docs/release-readiness-v0.1.0.md).

## Development flow

Work in short RED/GREEN increments: add a focused failing test, run it and
confirm it fails for the intended reason, then make the minimal change to pass
it and rerun. Do not change application behavior, the `Dockerfile`,
`compose.yaml`, CI workflows, lock files, generated OpenAPI, or generated
TypeScript as part of routine changes. Do not add upload, live AI/ML,
authentication, schema migration, backup/restore, release workflow, Playwright,
or bundled-distribution code without an explicit scoped plan.

## Canonical verification commands

The complete local/CI gate matrix and the exact-snapshot review rules live in
[`docs/verification.md`](docs/verification.md). At minimum, before requesting
review, run the repository policy guards and capture an exact snapshot:

```bash
python scripts/test_repository_guards.py
python scripts/verify_container_pins.py
python scripts/source_fingerprint.py
```

Then run the dotnet, web, and training gates exactly as documented in
[`docs/verification.md`](docs/verification.md) (locked restore, formatting
verification, Release build and tests, web lint/check:api/typecheck/Vitest/build
and high-severity audit, and the training Ruff/mypy/pytest suite). Do not run
`uv sync` concurrently with dotnet tests.

## Exact-snapshot expectations

Run `python scripts/source_fingerprint.py` before and after every independent
review and once more after staging. The v2 fingerprint binds `HEAD` plus the
type, Unix mode, and exact content of every tracked or non-ignored untracked
path, so staging unchanged bytes preserves the value while any source, test,
config, content, type, or mode change produces a new snapshot. The reviewed
fingerprint must match before and after `git add --all`; inspect the staged diff
and tree ID before committing, and require a clean worktree afterward. Green
tests from a different fingerprint are not substitute evidence.

## Commits

Keep commits focused and in English. Do not commit generated databases, reports,
SBOMs, vulnerability caches, build output, dependency trees, or raw/private
traces; these are ignored by Git, and incidental artifacts invalidate the
exact-snapshot fingerprint. See [`SECURITY.md`](SECURITY.md) before sharing any
trace-derived excerpt.
