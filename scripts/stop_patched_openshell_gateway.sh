#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

RUNTIME_DIR=".forkcell/openshell-patched"
PID_FILE="$RUNTIME_DIR/gateway.pid"

if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  kill "$(cat "$PID_FILE")" 2>/dev/null || true
  for _ in $(seq 1 50); do
    if ! kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      rm -f "$PID_FILE"
      echo "patched OpenShell gateway stopped"
      exit 0
    fi
    sleep 0.1
  done
  kill -9 "$(cat "$PID_FILE")" 2>/dev/null || true
  rm -f "$PID_FILE"
  echo "patched OpenShell gateway killed"
else
  rm -f "$PID_FILE"
  echo "patched OpenShell gateway not running"
fi
