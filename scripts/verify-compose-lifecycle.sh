#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
PROJECT="tracehelix-lifecycle-$$"
BLOCKER=""
PORT=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')
DC=(docker compose --project-name "$PROJECT" --file "$ROOT/compose.yaml")

cleanup() {
  if [[ -n "$BLOCKER" ]]; then
    docker rm --force "$BLOCKER" >/dev/null 2>&1 || true
  fi
  "${DC[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

wait_ready() {
  for _ in {1..60}; do
    if curl --silent --fail --max-time 1 "http://127.0.0.1:$PORT/health/ready" >/dev/null 2>&1; then
      return 0
    fi
    sleep .25
  done
  "${DC[@]}" ps >&2 || true
  return 1
}

export TRACEHELIX_PORT="$PORT"
"${DC[@]}" up --detach --no-build
wait_ready

api_id=$("${DC[@]}" ps --quiet api)
web_id=$("${DC[@]}" ps --quiet web)
docker inspect "$api_id" "$web_id" | python3 -c '
import json, sys
api, web = json.load(sys.stdin)
project = sys.argv[1]
internal = f"{project}_tracehelix-internal"
edge = f"{project}_tracehelix-edge"
for container in (api, web):
    host = container["HostConfig"]
    assert host["ReadonlyRootfs"] is True
    assert "ALL" in (host.get("CapDrop") or [])
    assert any(option.startswith("no-new-privileges") for option in (host.get("SecurityOpt") or []))
    assert container["Config"]["User"] not in ("", "0", "root")
assert set(api["NetworkSettings"]["Networks"]) == {internal}
assert not (api["HostConfig"].get("PortBindings") or {})
assert set(web["NetworkSettings"]["Networks"]) == {internal, edge}
bindings = web["HostConfig"]["PortBindings"]["8080/tcp"]
assert len(bindings) == 1 and bindings[0]["HostIp"] == "127.0.0.1"
' "$PROJECT"

cli_name="$PROJECT-cli-probe"
"${DC[@]}" run --detach --no-deps --name "$cli_name" --entrypoint sh cli -c 'sleep 30' >/dev/null
cli_id=$(docker inspect --format '{{.Id}}' "$cli_name")
docker inspect "$cli_id" | python3 -c '
import json, sys
container = json.load(sys.stdin)[0]
host = container["HostConfig"]
assert host["NetworkMode"] == "none"
assert host["ReadonlyRootfs"] is True
assert "ALL" in (host.get("CapDrop") or [])
assert any(option.startswith("no-new-privileges") for option in (host.get("SecurityOpt") or []))
assert container["Config"]["User"] not in ("", "0", "root")
imports = [mount for mount in container["Mounts"] if mount["Destination"] == "/imports"]
assert len(imports) == 1 and imports[0]["RW"] is False
'
docker rm --force "$cli_id" >/dev/null
"${DC[@]}" run --rm --no-deps --volume "$ROOT/samples/generic-jsonl:/probe-imports:ro" cli \
  import /probe-imports/minimal.jsonl --adapter generic-jsonl --db /data/tracehelix.db --json >/dev/null
"${DC[@]}" run --rm --no-deps --entrypoint sh cli -c 'test "$(stat -c %a /data/tracehelix.db)" = 600'

"${DC[@]}" restart api
wait_ready

old_api_id=$("${DC[@]}" ps --quiet api)
old_api_ip=$(docker inspect --format "{{(index .NetworkSettings.Networks \"${PROJECT}_tracehelix-internal\").IPAddress}}" "$old_api_id")
[[ -n "$old_api_ip" ]]
"${DC[@]}" rm --stop --force api >/dev/null
BLOCKER="$PROJECT-old-api-ip"
docker run --detach --name "$BLOCKER" --network "${PROJECT}_tracehelix-internal" --entrypoint /bin/sh tracehelix-web:local -c 'sleep 60' >/dev/null
blocker_ip=$(docker inspect --format "{{(index .NetworkSettings.Networks \"${PROJECT}_tracehelix-internal\").IPAddress}}" "$BLOCKER")
[[ "$blocker_ip" == "$old_api_ip" ]]
"${DC[@]}" up --detach --no-deps api
new_api_id=$("${DC[@]}" ps --quiet api)
new_api_ip=$(docker inspect --format "{{(index .NetworkSettings.Networks \"${PROJECT}_tracehelix-internal\").IPAddress}}" "$new_api_id")
[[ -n "$new_api_ip" && "$new_api_ip" != "$old_api_ip" ]]
wait_ready
docker rm --force "$BLOCKER" >/dev/null
BLOCKER=""

run_id=$(curl --silent --fail "http://127.0.0.1:$PORT/api/v1/runs" | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["id"])')
foreign_origin_status=$(curl --silent --show-error --request POST --header 'Origin: https://evil.example' --output /dev/null --write-out '%{http_code}' "http://127.0.0.1:$PORT/api/v1/runs/$run_id/analysis/rules")
[[ "$foreign_origin_status" == 403 ]]
latest_status=$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' "http://127.0.0.1:$PORT/api/v1/runs/$run_id/analysis/latest")
[[ "$latest_status" == 404 ]]
same_origin_status=$(curl --silent --show-error --request POST --header "Origin: http://127.0.0.1:$PORT" --output /dev/null --write-out '%{http_code}' "http://127.0.0.1:$PORT/api/v1/runs/$run_id/analysis/rules")
[[ "$same_origin_status" == 200 ]]

hostile_status=$(curl --silent --show-error --header 'Host: evil.example' --output /dev/null --write-out '%{http_code}' "http://127.0.0.1:$PORT/" || true)
[[ "$hostile_status" == 000 ]]

echo 'Compose lifecycle verification passed.'
