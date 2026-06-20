#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PACKAGE_SPEC="${FORKCELL_PACKAGE_SPEC:-$REPO_ROOT}"
WORKDIR="${FORKCELL_BENCH_DIR:-$(mktemp -d /tmp/forkcell-bench.XXXXXX)}"
PROFILES="${FORKCELL_BENCH_PROFILES:-tiny small}"
PYTHON_BIN="${PYTHON:-python3}"

mkdir -p "$WORKDIR"
cd "$WORKDIR"

"$PYTHON_BIN" -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip >/tmp/forkcell-bench-pip-upgrade.log
python -m pip install "$PACKAGE_SPEC" >/tmp/forkcell-bench-install.log

ms_now() {
  python - <<'PY'
import time
print(time.time_ns() // 1_000_000)
PY
}

make_workspace() {
  local profile="$1"
  local workspace="$2"
  PROFILE="$profile" WORKSPACE="$workspace" python - <<'PY'
import os
from pathlib import Path
profile = os.environ["PROFILE"]
root = Path(os.environ["WORKSPACE"])
if root.exists():
    import shutil
    shutil.rmtree(root)
root.mkdir(parents=True)
if profile == "tiny":
    (root / "hello.txt").write_text("hello\n")
elif profile == "small":
    for d in range(5):
        sub = root / f"dir-{d:02d}"
        sub.mkdir()
        for f in range(10):
            (sub / f"file-{f:02d}.txt").write_text((f"{d}:{f}:" + "x" * 256 + "\n"))
elif profile == "medium":
    for d in range(20):
        sub = root / f"dir-{d:02d}"
        sub.mkdir()
        for f in range(50):
            (sub / f"file-{f:02d}.txt").write_text((f"{d}:{f}:" + "x" * 512 + "\n"))
else:
    raise SystemExit(f"unknown benchmark profile: {profile}")
PY
}

summarize() {
  local profile="$1"
  local cell="$2"
  local init_json="$3"
  local run_wall_ms="$4"
  local final_file="$5"
  PROFILE="$profile" CELL="$cell" INIT_JSON="$init_json" RUN_WALL_MS="$run_wall_ms" FINAL_FILE="$final_file" python - <<'PY'
import json, os
from pathlib import Path
profile = os.environ["PROFILE"]
cell = os.environ["CELL"]
init = json.loads(Path(os.environ["INIT_JSON"]).read_text())
receipts = sorted(Path(".forkcell/receipts").glob("rcpt_*.json"), key=lambda p: p.stat().st_mtime)
receipt = json.loads(receipts[-1].read_text())
before = receipt.get("checkpoints", {}).get("before_metrics") or {}
restore = receipt.get("checkpoints", {}).get("restore_metrics") or {}
base = init.get("base_stats") or {}
checkpoint_host = before.get("duration_ms")
checkpoint_inner = before.get("inner_duration_ms")
checkpoint_orchestration = None
if checkpoint_host is not None and checkpoint_inner is not None:
    checkpoint_orchestration = max(0, checkpoint_host - checkpoint_inner)
summary = {
    "profile": profile,
    "cell": cell,
    "files": base.get("files"),
    "dirs": base.get("dirs"),
    "bytes": base.get("bytes"),
    "init_ms": init.get("import_ms"),
    "run_wall_ms": int(os.environ["RUN_WALL_MS"]),
    "checkpoint_host_ms": checkpoint_host,
    "checkpoint_fs_inner_ms": checkpoint_inner,
    "checkpoint_docker_orchestration_ms": checkpoint_orchestration,
    "restore_host_ms": restore.get("duration_ms"),
    "decision": (receipt.get("decision") or {}).get("result"),
    "exit_code": (receipt.get("run") or {}).get("exit_code"),
    "final_file": os.environ["FINAL_FILE"],
    "receipt": str(receipts[-1]),
}
print(json.dumps(summary, sort_keys=True))
PY
}

results_jsonl="$WORKDIR/results.jsonl"
: > "$results_jsonl"

for profile in $PROFILES; do
  cell="bench-${profile}"
  workspace="$WORKDIR/workspace-${profile}"
  make_workspace "$profile" "$workspace"
  init_json="$WORKDIR/init-${profile}.json"
  forkcell overlay init "$cell" --from "$workspace" > "$init_json"
  start_ms="$(ms_now)"
  forkcell overlay run --checkpoint-before --restore-on-fail "$cell" -- \
    sh -lc 'printf changed > benchmark-marker.txt; exit 7' >/tmp/forkcell-bench-run.json
  end_ms="$(ms_now)"
  final_file="missing"
  if [ -f "$workspace/benchmark-marker.txt" ]; then
    final_file="$(cat "$workspace/benchmark-marker.txt")"
  fi
  summarize "$profile" "$cell" "$init_json" "$((end_ms - start_ms))" "$final_file" >> "$results_jsonl"
done

PACKAGE_SPEC_FOR_REPORT="$PACKAGE_SPEC" python - "$results_jsonl" <<'PY'
import json, os, sys
rows = [json.loads(line) for line in open(sys.argv[1]) if line.strip()]
print("# ForkCell Benchmark Matrix")
print()
print(f"package: `{os.environ.get('PACKAGE_SPEC_FOR_REPORT', 'unknown')}`")
print()
print("| profile | files | bytes | init ms | run wall ms | checkpoint host ms | checkpoint fs inner ms | checkpoint Docker/CLI ms | restore host ms | decision | restored marker |")
print("|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|")
for r in rows:
    print("| {profile} | {files} | {bytes} | {init_ms} | {run_wall_ms} | {checkpoint_host_ms} | {checkpoint_fs_inner_ms} | {checkpoint_docker_orchestration_ms} | {restore_host_ms} | {decision} | {final_file} |".format(**r))
print()
print("Raw JSONL:", sys.argv[1])
PY
