#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
TMP=$(mktemp -d)
DB="$TMP/tracehelix.db"
PID=
cleanup() { [[ -z "$PID" ]] || kill "$PID" 2>/dev/null || true; [[ -z "$PID" ]] || wait "$PID" 2>/dev/null || true; rm -rf "$TMP"; }
trap cleanup EXIT INT TERM
CLI="$ROOT/src/TraceHelix.Cli/bin/Release/net10.0/TraceHelix.Cli.dll"
API="$ROOT/src/TraceHelix.Api/bin/Release/net10.0/TraceHelix.Api.dll"
first=$(dotnet "$CLI" import "$ROOT/samples/generic-jsonl/minimal.jsonl" --adapter generic-jsonl --db "$DB" --json)
id=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["runId"])' <<<"$first")

assert_kestrel_endpoint_override_rejected() {
  local safe_port wildcard_port status
  safe_port=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')
  wildcard_port=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')
  set +e
  (
    ulimit -c 0
    TRACEHELIX_DB="$DB" \
      URLS="http://127.0.0.1:$safe_port" \
      Kestrel__Endpoints__Probe__Url="http://*:$wildcard_port" \
      ASPNETCORE_ENVIRONMENT=Production \
      timeout --signal=KILL 10s dotnet "$API"
  ) >"$TMP/kestrel-override.log" 2>&1
  status=$?
  set -e
  if [[ $status -eq 0 ]] || ! grep -Fq \
    'Kestrel endpoint configuration is not supported because the API listener must remain loopback-only.' \
    "$TMP/kestrel-override.log"; then
    cat "$TMP/kestrel-override.log" >&2
    echo 'API did not prove fail-closed rejection of a Kestrel:Endpoints override.' >&2
    return 1
  fi
}

assert_host_wildcard_rejected() {
  [[ ! -e /.dockerenv && ! -e /run/.containerenv ]] || return 0
  local wildcard_port status
  wildcard_port=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')
  set +e
  (
    ulimit -c 0
    TRACEHELIX_DB="$DB" \
      TRACEHELIX_ALLOW_WILDCARD=true \
      URLS="http://0.0.0.0:$wildcard_port" \
      ASPNETCORE_ENVIRONMENT=Production \
      timeout --signal=KILL 10s dotnet "$API"
  ) >"$TMP/host-wildcard.log" 2>&1
  status=$?
  set -e
  if [[ $status -eq 0 ]] || ! grep -Fq 'Invalid listen URL configuration:' "$TMP/host-wildcard.log"; then
    cat "$TMP/host-wildcard.log" >&2
    echo 'Host API did not prove fail-closed rejection of wildcard listener opt-in reserved for containers.' >&2
    return 1
  fi
}

start_api() {
  local port=$1
  TRACEHELIX_DB="$DB" URLS="http://127.0.0.1:$port" ASPNETCORE_ENVIRONMENT=Production dotnet "$API" >"$TMP/api.log" 2>&1 &
  PID=$!
  local ready=false
  for _ in {1..100}; do
    if curl --silent --fail --max-time 1 "http://127.0.0.1:$port/health/ready" >/dev/null 2>&1; then ready=true; break; fi
    if ! kill -0 "$PID" 2>/dev/null; then wait "$PID" || true; PID=; return 1; fi
    sleep .1
  done
  if [[ "$ready" != true ]]; then kill "$PID" 2>/dev/null || true; wait "$PID" 2>/dev/null || true; PID=; return 1; fi
}

assert_kestrel_endpoint_override_rejected
assert_host_wildcard_rejected

if [[ -n "${TRACEHELIX_API_PORT:-}" ]]; then
  PORT=$TRACEHELIX_API_PORT
  start_api "$PORT" || { cat "$TMP/api.log" >&2; echo "API readiness timed out on configured port $PORT" >&2; exit 1; }
else
  for _ in {1..5}; do
    PORT=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')
    start_api "$PORT" && break
  done
  [[ -n "$PID" ]] || { cat "$TMP/api.log" >&2; echo "Unable to start API after 5 available-port attempts" >&2; exit 1; }
fi

base="http://127.0.0.1:$PORT/api/v1"
curl -fsS "$base/runs" | python3 -c 'import json,sys; assert len(json.load(sys.stdin))==1'
curl -fsS "$base/runs/$id" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["id"]==sys.argv[1] and d["eventCount"]==12' "$id"
hostile_status=$(curl --silent --show-error --header 'Host: evil.example' --output /dev/null --write-out '%{http_code}' "http://127.0.0.1:$PORT/health/ready" || true)
[[ "$hostile_status" == 400 ]]
page=$(curl -fsS "$base/runs/$id/events?limit=1")
next=$(python3 -c 'import json,sys; d=json.load(sys.stdin); assert len(d["items"])==1; assert "nextCursor" in d; print(d["nextCursor"])' <<<"$page")
[[ "$next" == "None" ]] || curl -fsS "$base/runs/$id/events?limit=1&cursor=$next" >/dev/null
problem=$(curl -sS -H 'Accept: application/problem+json' -w '\n%{http_code}\n%{content_type}' "$base/runs/$id/events?limit=999")
python3 -c 'import json,sys; lines=sys.stdin.read().splitlines(); assert lines[-2]=="400"; assert lines[-1].startswith("application/problem+json"); d=json.loads("\n".join(lines[:-2])); assert d["status"]==400 and "detail" in d' <<<"$problem"
curl -fsS -X POST "$base/runs/$id/analysis/rules" >/dev/null
curl -fsS "$base/runs/$id/alerts" >/dev/null
curl -fsS "$base/compare?left=$id&right=$id" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["left"]["eventCount"]==d["right"]["eventCount"]'
echo 'API real-process verification passed.'
