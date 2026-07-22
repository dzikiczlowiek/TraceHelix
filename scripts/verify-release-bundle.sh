#!/usr/bin/env bash
# Canonical deterministic source-bundle and install-from-artifact verifier.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
WORK=""
VERIFIED_OUTPUT_DIR=${TRACEHELIX_VERIFIED_OUTPUT_DIR:-}

note() { printf 'verify-release-bundle: %s\n' "$*" >&2; }
die() { note "ERROR: $*"; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

cleanup() {
  local rc=$?
  trap - EXIT INT TERM
  if [[ -n "$WORK" ]]; then
    if ! rm -rf -- "$WORK"; then
      note "WARNING: rm cleanup failed; using independent fallback for owned work directory: $WORK"
      if [[ ( -e "$WORK" || -L "$WORK" ) ]] && ! find "$WORK" -depth -delete; then
        note "ERROR: could not remove owned work directory: $WORK"
      fi
    fi
  fi
  exit "$rc"
}
trap cleanup EXIT INT TERM

export_verified_output() {
  local destination
  [[ -n "$VERIFIED_OUTPUT_DIR" ]] || return 0
  destination=$VERIFIED_OUTPUT_DIR
  [[ "$destination" = /* ]] || die "destination must be an absolute path"
  python3 "$ROOT/scripts/release_assets.py" export-verified \
    --archive "$OUT_A/$ARCHIVE" \
    --checksums "$OUT_A/SHA256SUMS" \
    --manifest "$SOURCE/RELEASE-MANIFEST.json" \
    --version "$VERSION" \
    --destination "$destination" \
    --forbidden-root "$ROOT" \
    --forbidden-root "$WORK"
  note "exported verified archive, checksum, and extracted manifest to $destination"
}

for command in python3 git cmp sha256sum docker node npm find; do
  have "$command" || die "$command is required"
done
docker info >/dev/null 2>&1 || die "Docker daemon is unreachable (locally run through the docker group)"
docker compose version >/dev/null 2>&1 || die "docker compose plugin is required"

WORK=$(mktemp -d -t tracehelix-release-bundle-XXXXXXXX) || die "cannot create temporary directory"
case "$WORK" in
  "$ROOT"|"$ROOT"/*) die "temporary directory must be outside the checkout" ;;
esac
chmod 700 "$WORK"
OUT_A="$WORK/build-a"
OUT_B="$WORK/build-b"
EXTRACT="$WORK/extracted"
mkdir -m 700 "$OUT_A" "$OUT_B"

note "building the committed HEAD twice"
python3 "$ROOT/scripts/build_release_bundle.py" --output-dir "$OUT_A" >"$WORK/build-a.json"
python3 "$ROOT/scripts/build_release_bundle.py" --output-dir "$OUT_B" >"$WORK/build-b.json"
VERSION=$(tr -d '\r\n' <"$ROOT/VERSION")
ARCHIVE="tracehelix-${VERSION}-source.tar.gz"

cmp "$OUT_A/$ARCHIVE" "$OUT_B/$ARCHIVE" >/dev/null || die "two builds are not byte-identical"
cmp "$OUT_A/SHA256SUMS" "$OUT_B/SHA256SUMS" >/dev/null || die "checksum sidecars differ"
(cd "$OUT_A" && sha256sum -c SHA256SUMS)
(cd "$OUT_B" && sha256sum -c SHA256SUMS)

note "verifying before extraction"
python3 "$ROOT/scripts/verify_release_bundle.py" \
  --archive "$OUT_A/$ARCHIVE" \
  --checksums "$OUT_A/SHA256SUMS" \
  --extract-dir "$EXTRACT" >"$WORK/verify.json"
SOURCE="$EXTRACT/tracehelix-$VERSION"
[[ -d "$SOURCE" && "$SOURCE" != "$ROOT" ]] || die "verified extracted source root is missing or aliases checkout"
[[ -f "$SOURCE/scripts/verify-release-bundle.sh" ]] || die "bundle omitted its canonical verifier"

# From this point onward every project path is rooted in the verified artifact.
# No command receives or references the original checkout path.
note "running policy and bundle tests from extracted source"
(
  cd "$SOURCE"
  unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES
  python3 scripts/test_build_release_bundle.py
  python3 scripts/test_verify_release_bundle.py
  python3 scripts/test_repository_guards.py
  python3 scripts/verify_container_pins.py
)

note "installing locked web dependencies inside extracted source"
(
  cd "$SOURCE/web"
  npm ci
)

note "building digest-pinned production images from extracted source"
(
  cd "$SOURCE"
  docker compose --profile tools build --pull
)

note "running Compose lifecycle from extracted source"
(
  cd "$SOURCE"
  bash scripts/verify-compose-lifecycle.sh
)

note "running browser acceptance from extracted source"
(
  cd "$SOURCE"
  bash scripts/verify-browser.sh
)

# Export is deliberately last: no caller receives artifacts until both builds,
# strict archive verification, extracted policy tests, installation, image build,
# lifecycle, and browser acceptance have completed successfully.
export_verified_output

DIGEST=$(sha256sum "$OUT_A/$ARCHIVE" | cut -d' ' -f1)
note "PASS archive=$ARCHIVE sha256=$DIGEST source=$SOURCE"
