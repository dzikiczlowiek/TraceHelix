#!/usr/bin/env bash
# Canonical browser-acceptance verifier for the v0.1.0 local trusted single-user
# source release. One command exercises the real production Docker Compose
# topology (nginx web -> API -> SQLite) in Chromium, from a clean state, with two
# committed synthetic JSONL traces seeded through the real containerized CLI.
#
# It does not change the production compose or lifecycle invariants and never
# touches foreign Docker projects: every Docker command is scoped to the unique
# `tracehelix-browser-<pid>` project created here. Locally, run it through the
# docker group, e.g.:
#   sg docker -c "bash scripts/verify-browser.sh"
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
WEB="$ROOT/web"
COMPOSE_FILE="$ROOT/compose.yaml"
READY_TIMEOUT="${TRACEHELIX_READY_TIMEOUT:-90}"
RESIDUE_LIMIT=20

WORK=""
IMPORTS=""
PROJECT=""
PORT=""
DC=()
CLEANED_UP=""

log() { printf '%s\n' "$*" >&2; }
note() { printf 'verify-browser: %s\n' "$*" >&2; }
warn() { printf 'verify-browser: WARNING: %s\n' "$*" >&2; }
die() { printf 'verify-browser: ERROR: %s\n' "$*" >&2; exit 1; }

have() { command -v "$1" >/dev/null 2>&1; }

require_commands() {
  have docker || die "docker CLI not found. Install Docker; locally run this script via 'sg docker -c \"bash scripts/verify-browser.sh\"'."
  docker info >/dev/null 2>&1 || die "docker daemon is unreachable. Ensure Docker is running and your user can access it (locally try 'sg docker -c \"...\"')."
  docker compose version >/dev/null 2>&1 || die "docker compose plugin not found."
  have python3 || die "python3 not found (used for port allocation and JSON capture)."
  have curl || die "curl not found (used for bounded readiness polling)."
  have node || die "node not found; Playwright requires Node."
  have npm || die "npm not found."
  [[ -f "$COMPOSE_FILE" ]] || die "compose file not found at $COMPOSE_FILE."
  [[ -d "$WEB/node_modules/@playwright/test" ]] || die "@playwright/test is not installed under $WEB/node_modules. Run 'cd web && npm ci' first."
  [[ -f "$WEB/playwright.config.ts" ]] || die "web/playwright.config.ts not found."
  [[ -d "$WEB/e2e" ]] || die "web/e2e acceptance suite not found."

  # Chromium presence check via shell globbing (no find/grep dependency). The
  # default cache matches Playwright's PLAYWRIGHT_BROWSERS_PATH default.
  local browsers_path="${PLAYWRIGHT_BROWSERS_PATH:-$HOME/.cache/ms-playwright}"
  [[ -d "$browsers_path" ]] || die "Playwright browsers cache not found at '$browsers_path'. Run 'cd web && npm exec -- playwright install --with-deps chromium' first."
  local candidate found=0
  for candidate in "$browsers_path"/chromium-*; do
    if [[ -d "$candidate" ]]; then found=1; break; fi
  done
  [[ $found -eq 1 ]] || die "Chromium for Playwright was not found under '$browsers_path'. Run 'cd web && npm exec -- playwright install --with-deps chromium' first."
}

# Bind an ephemeral socket to discover a free loopback port, then close it.
# This is inherently a TOCTOU race (the port can be taken between close and the
# compose bind); it is accepted and minimized because compose binds immediately
# afterward, and any loss is reported with bounded diagnostics below rather than
# retried or masked.
allocate_port() {
  python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()'
}

capture_run_id() {
  local file="$1" output
  output=$("${DC[@]}" run --rm --no-deps --volume "$IMPORTS:/seed:ro" cli \
    import "/seed/$file" --adapter generic-jsonl --db /data/tracehelix.db --json) \
    || die "seeding $file failed; last CLI output line: ${output##*$'\n'}"
  python3 -c 'import json,sys; data=json.load(sys.stdin); print(data["runId"])' <<<"$output"
}

wait_ready() {
  local deadline=$((SECONDS + READY_TIMEOUT))
  while ((SECONDS < deadline)); do
    if curl --silent --fail --max-time 1 "http://127.0.0.1:$PORT/health/ready" >/dev/null 2>&1; then
      return 0
    fi
    sleep .25
  done
  return 1
}

bounded() {
  # Print up to RESIDUE_LIMIT lines from stdin so diagnostics never stream
  # unbounded logs. Foreign resources are never actioned; this only displays.
  head -n "$RESIDUE_LIMIT"
}

collect_residue() {
  # Exact project-label filters scope every query to this verifier's own
  # resources only; foreign projects/networks/volumes are never matched.
  RESIDUE_CONTAINERS=$(docker ps -a --filter "label=com.docker.compose.project=$PROJECT" --format '{{.Names}}' 2>/dev/null || true)
  RESIDUE_NETWORKS=$(docker network ls --filter "label=com.docker.compose.project=$PROJECT" --format '{{.Name}}' 2>/dev/null || true)
  RESIDUE_VOLUMES=$(docker volume ls --filter "label=com.docker.compose.project=$PROJECT" --format '{{.Name}}' 2>/dev/null || true)
}

cleanup() {
  local rc=$1
  # Re-entrancy guard: the EXIT trap can fire after an INT/TERM handler.
  [[ -z "${CLEANED_UP:-}" ]] || return "$rc"
  CLEANED_UP=1
  # Remove traps first so cleanup cannot recurse into itself.
  trap - EXIT INT TERM
  set +e

  local down_rc=0
  if [[ -n "$PROJECT" ]]; then
    docker compose --project-name "$PROJECT" --file "$COMPOSE_FILE" \
      down --volumes --remove-orphans >/dev/null 2>&1 || down_rc=$?
    collect_residue

    if [[ $rc -eq 0 ]]; then
      # Success path: a failed teardown or any project-labelled residue is a
      # hard failure, not a warning. The verifier must leave nothing behind.
      local hard=0
      [[ $down_rc -eq 0 ]] || hard=1
      [[ -z "$RESIDUE_CONTAINERS" ]] || hard=1
      [[ -z "$RESIDUE_NETWORKS" ]] || hard=1
      [[ -z "$RESIDUE_VOLUMES" ]] || hard=1
      if [[ $hard -ne 0 ]]; then
        note "teardown left residue or failed for project '$PROJECT' (down_rc=$down_rc)."
        [[ -z "$RESIDUE_CONTAINERS" ]] || { note "residue containers:"; printf '%s\n' "$RESIDUE_CONTAINERS" | bounded >&2; }
        [[ -z "$RESIDUE_NETWORKS" ]] || { note "residue networks:"; printf '%s\n' "$RESIDUE_NETWORKS" | bounded >&2; }
        [[ -z "$RESIDUE_VOLUMES" ]] || { note "residue volumes:"; printf '%s\n' "$RESIDUE_VOLUMES" | bounded >&2; }
        note "inspect with: docker ps -a --filter 'label=com.docker.compose.project=$PROJECT'"
        rc=1
      fi
    else
      # Failure path: preserve the original nonzero result. Emit bounded
      # teardown/residue diagnostics so the failure is diagnosable.
      [[ $down_rc -eq 0 ]] || note "teardown exited $down_rc for project '$PROJECT' (original result $rc preserved)."
      [[ -z "$RESIDUE_CONTAINERS" ]] || { note "residue containers (original result $rc preserved):"; printf '%s\n' "$RESIDUE_CONTAINERS" | bounded >&2; }
      [[ -z "$RESIDUE_NETWORKS" ]] || { note "residue networks (original result $rc preserved):"; printf '%s\n' "$RESIDUE_NETWORKS" | bounded >&2; }
      [[ -z "$RESIDUE_VOLUMES" ]] || { note "residue volumes (original result $rc preserved):"; printf '%s\n' "$RESIDUE_VOLUMES" | bounded >&2; }
    fi
  fi

  [[ -z "$WORK" ]] || rm -rf "$WORK"
  return "$rc"
}

require_commands

WORK=$(mktemp -d -t tracehelix-browser-XXXXXXXX) || die "unable to create a temporary working directory."
case "$WORK" in
  "$ROOT"|"$ROOT"/*) die "refusing to use a working directory inside the repository: $WORK" ;;
esac
chmod 700 "$WORK"

PROJECT="tracehelix-browser-$$"
PORT=$(allocate_port) || die "unable to allocate an available loopback port."
IMPORTS="$WORK/imports"
mkdir -p "$IMPORTS"
cp "$ROOT/samples/generic-jsonl/minimal.jsonl" "$IMPORTS/minimal.jsonl"
cp "$ROOT/samples/generic-jsonl/minimal-variant.jsonl" "$IMPORTS/minimal-variant.jsonl"

DC=(docker compose --project-name "$PROJECT" --file "$COMPOSE_FILE")
trap 'rc=$?; cleanup "$rc"; exit "$rc"' EXIT
trap 'cleanup 130; exit 130' INT
trap 'cleanup 143; exit 143' TERM

export TRACEHELIX_PORT="$PORT"
note "project=$PROJECT port=$PORT work=$WORK"

note "building digest-pinned production images (api, web)..."
"${DC[@]}" build >/dev/null

note "starting api and web..."
"${DC[@]}" up --detach api web >/dev/null

if ! wait_ready; then
  note "services did not become ready within ${READY_TIMEOUT}s on port $PORT."
  "${DC[@]}" ps >&2 2>/dev/null | bounded >&2 || true
  "${DC[@]}" logs --no-color --tail 80 api web >&2 2>/dev/null || true
  die "readiness polling failed (port $PORT may have been taken: the free-port probe is a best-effort TOCTOU check); see bounded service diagnostics above."
fi

note "seeding committed synthetic traces through the real containerized CLI..."
FIRST_ID=$(capture_run_id minimal.jsonl)
SECOND_ID=$(capture_run_id minimal-variant.jsonl)
[[ -n "$FIRST_ID" && -n "$SECOND_ID" ]] || die "seeded run IDs were empty."
[[ "$FIRST_ID" != "$SECOND_ID" ]] || die "seeded run IDs are not distinct: '$FIRST_ID'."
note "seeded runs (distinct): $FIRST_ID $SECOND_ID"

note "running Playwright Chromium acceptance against http://127.0.0.1:$PORT ..."
export PLAYWRIGHT_BASE_URL="http://127.0.0.1:$PORT"
set +e
( cd "$WEB" && npm exec -- playwright test --reporter=list )
playwright_rc=$?
set -e
if [[ $playwright_rc -ne 0 ]]; then
  note "Playwright exited $playwright_rc; emitting bounded service diagnostics before teardown."
  "${DC[@]}" logs --no-color --tail 40 api web >&2 2>/dev/null || true
  exit "$playwright_rc"
fi

note "browser acceptance passed (project=$PROJECT port=$PORT)."
