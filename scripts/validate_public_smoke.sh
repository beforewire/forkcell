#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if ! git -C upstream/openshell rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "initializing OpenShell runtime submodule..."
  git submodule update --init --recursive
fi

test -f patches/openshell.lock
test -f patches/openshell-workspace-substrate-2026-06-19.patch
test -d upstream/openshell

expected_runtime_commit=$(awk '/^[[:space:]]+commit:/ { print $2; exit }' patches/openshell.lock)
actual_runtime_commit=$(git -C upstream/openshell rev-parse HEAD)
if [ "$actual_runtime_commit" != "$expected_runtime_commit" ]; then
  echo "OpenShell submodule commit mismatch: expected $expected_runtime_commit, got $actual_runtime_commit" >&2
  exit 1
fi

git -C upstream/openshell apply --reverse --check ../../patches/openshell-workspace-substrate-2026-06-19.patch

python3 -m py_compile forkcell/*.py
bash -n scripts/*.sh
python3 -m forkcell.cli --help >/dev/null

git ls-files | grep -q '^docs/evidence/' && {
  echo "raw docs/evidence files should not be tracked in public preview" >&2
  exit 1
} || true

if git ls-files | grep -E '(^\.forkcell/|^\.forkcell-build/|^upstream/openshell/target|^upstream/openshell/target-linux-docker)' >/dev/null; then
  echo "runtime state/build outputs are tracked" >&2
  exit 1
fi

if command -v rg >/dev/null 2>&1; then
  private_home_pattern="/Users/[^[:space:]/]+"
  mac_temp_pattern="/var/folders/[[:alnum:]_/-]+"
  if rg -n --glob '!scripts/validate_public_smoke.sh' "${private_home_pattern}|${mac_temp_pattern}" README.md README.zh-CN.md docs forkcell scripts patches pyproject.toml .gitignore .gitmodules >/tmp/forkcell-public-smoke-secrets.txt 2>/dev/null; then
    echo "potential private path marker found:" >&2
    cat /tmp/forkcell-public-smoke-secrets.txt >&2
    exit 1
  fi
fi

cat <<MSG
ForkCell public preview smoke passed.
- Python modules compile
- Shell scripts parse
- OpenShell submodule is present and matches patches/openshell.lock
- Runtime patch artifact reverse-applies cleanly
- No raw docs/evidence or runtime state is tracked
MSG
