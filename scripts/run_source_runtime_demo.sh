#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

CELL="${FORKCELL_SOURCE_DEMO_CELL:-demo}"
DEMO_DIR="${FORKCELL_SOURCE_DEMO_DIR:-/tmp/forkcell-demo}"
ENDPOINT="${OPENSHELL_GATEWAY_ENDPOINT:-http://127.0.0.1:17671}"

if [ ! -d upstream/openshell/.git ]; then
  git submodule update --init --recursive
fi

if [ "${FORKCELL_SKIP_RUNTIME_BUILD:-0}" != "1" ]; then
  ./scripts/build_patched_openshell_runtime.sh
  python3 -m forkcell.cli runtime install --from upstream/openshell
fi

./scripts/start_patched_openshell_gateway.sh
cleanup() {
  ./scripts/stop_patched_openshell_gateway.sh >/dev/null 2>&1 || true
}
trap cleanup EXIT

export FORKCELL_OPENSHELL_BIN="${FORKCELL_OPENSHELL_BIN:-$PWD/.forkcell/runtime/native-overlay/bin/openshell}"
export OPENSHELL_GATEWAY_ENDPOINT="$ENDPOINT"

rm -rf "$DEMO_DIR"
mkdir -p "$DEMO_DIR"
printf 'hello\n' > "$DEMO_DIR/hello.txt"

python3 -m forkcell.cli native delete "$CELL" >/dev/null 2>&1 || true
python3 -m forkcell.cli native init "$CELL" --from "$DEMO_DIR"
python3 -m forkcell.cli native run --checkpoint-before --restore-on-fail "$CELL" -- \
  sh -lc 'echo changed > hello.txt; exit 7'

receipt_md="$(python3 -m forkcell.cli receipt show --cell "$CELL" --latest --format md)"
echo "$receipt_md"

echo "$receipt_md" | grep -q 'Decision: `restored`'
final_contents="$(cat "$DEMO_DIR/hello.txt")"
if [ "$final_contents" != "hello" ]; then
  echo "expected restored file to contain 'hello', got: $final_contents" >&2
  exit 1
fi

cat <<MSG
ForkCell source runtime demo passed.
- cell: $CELL
- demo dir: $DEMO_DIR
- endpoint: $OPENSHELL_GATEWAY_ENDPOINT
- final file: $final_contents
- receipt decision: restored
MSG
