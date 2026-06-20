#!/usr/bin/env bash
set -euo pipefail

PACKAGE_SPEC="${FORKCELL_PACKAGE_SPEC:-forkcell==0.1.0a1}"
WORKDIR="${FORKCELL_PYPI_SMOKE_DIR:-$(mktemp -d /tmp/forkcell-pypi-smoke.XXXXXX)}"
PYTHON_BIN="${PYTHON:-python3}"

mkdir -p "$WORKDIR"
cd "$WORKDIR"

"$PYTHON_BIN" -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip >/tmp/forkcell-pypi-smoke-pip-upgrade.log
python -m pip install "$PACKAGE_SPEC"

mkdir -p workspace
printf 'hello\n' > workspace/hello.txt

forkcell overlay init demo --from workspace >/tmp/forkcell-pypi-overlay-init.json
forkcell overlay run --checkpoint-before --restore-on-fail demo -- \
  sh -lc 'echo changed > hello.txt; exit 7' >/tmp/forkcell-pypi-overlay-run.json

final_contents="$(cat workspace/hello.txt)"
if [ "$final_contents" != "hello" ]; then
  echo "expected restored file to contain 'hello', got: $final_contents" >&2
  exit 1
fi

receipt_md="$(forkcell receipt show --cell demo --latest --format md)"
echo "$receipt_md" | grep -q 'Decision: `restored`'
echo "$receipt_md" | grep -q 'Backend: `local-overlay`'
echo "$receipt_md" | grep -q 'Runtime: `local`'

cat <<MSG
ForkCell PyPI quickstart smoke passed.
- package: $PACKAGE_SPEC
- workdir: $WORKDIR
- final file: $final_contents
- receipt decision: restored
MSG
