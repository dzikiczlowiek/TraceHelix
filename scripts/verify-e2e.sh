#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
work=${TRACEHELIX_VERIFY_DIR:-$(mktemp -d)}
if [[ -n "${TRACEHELIX_VERIFY_DIR:-}" ]]; then
  mkdir -p "$work"
  find "$work" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
fi
mkdir -p "$work"
db="$work/tracehelix.db"
cli=(dotnet "$root/src/TraceHelix.Cli/bin/Release/net10.0/TraceHelix.Cli.dll")
"${cli[@]}" import "$root/samples/generic-jsonl/minimal.jsonl" --adapter generic-jsonl --db "$db" --json > "$work/import.json"
run_id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["runId"])' "$work/import.json")
"${cli[@]}" analyze "$run_id" --db "$db" --classifier rules --json > "$work/analyze.json"
"${cli[@]}" import "$root/samples/generic-jsonl/minimal-variant.jsonl" --adapter generic-jsonl --db "$db" --json > "$work/import-variant.json"
variant_id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["runId"])' "$work/import-variant.json")
"${cli[@]}" analyze "$variant_id" --db "$db" --classifier rules --json > "$work/analyze-variant.json"
"${cli[@]}" list --db "$db" --json > "$work/list.json"
"${cli[@]}" show "$run_id" --db "$db" --events --alerts --json > "$work/show.json"
"${cli[@]}" compare "$run_id" "$variant_id" --db "$db" --json > "$work/compare.json"
"${cli[@]}" report "$run_id" --db "$db" --format json --out "$work/report.json" > "$work/report-command.json"
"${cli[@]}" report "$run_id" --db "$db" --format html --out "$work/report.html" > "$work/report-html-command.json"
"${cli[@]}" import "$root/samples/generic-jsonl/minimal.jsonl" --adapter generic-jsonl --db "$db" --json > "$work/duplicate.json"
printf 'not-json\n' > "$work/malformed.jsonl"
set +e
"${cli[@]}" import "$work/malformed.jsonl" --adapter generic-jsonl --db "$db" --json > "$work/malformed.stdout" 2> "$work/malformed.stderr"
malformed_exit=$?
set -e
test "$malformed_exit" -ne 0
test ! -s "$work/malformed.stderr"
python3 - "$work" <<'PY'
import hashlib, json, pathlib, sys
work=pathlib.Path(sys.argv[1])
for path in work.glob("*.json"):
    json.loads(path.read_text(encoding="utf-8"))
show=json.loads((work/"show.json").read_text())
comparison=json.loads((work/"compare.json").read_text())
assert comparison["leftRunId"] != comparison["rightRunId"]
expected={f"THX00{i}_{name}" for i,name in enumerate(["NO_PROGRESS_LOOP","PLAN_LOOP","VERIFICATION_GAP","PREMATURE_SUCCESS","RECOVERY_STORM","TOOL_ERROR_CASCADE"],1)}
assert {a["code"] for a in show["alerts"]} == expected
for event in show["events"]:
    source=event["source"]
    assert source["adapter"] and source["inputSha256"] and source["relativePath"]
    assert source["line"] is not None and source["byteOffset"] is not None and source["jsonPointer"] is not None
    assert event["contentSha256"]
assert json.loads((work/"duplicate.json").read_text())["outcome"] == "Duplicate"
for artifact, command in [("report.json", "report-command.json"), ("report.html", "report-html-command.json")]:
    expected=hashlib.sha256((work/artifact).read_bytes()).hexdigest()
    assert json.loads((work/command).read_text())["artifactSha256"] == expected
assert (work/"report.html").read_text(encoding="utf-8").startswith("<!doctype html>")
print(f"verified workflow artifacts: {work}")
PY
