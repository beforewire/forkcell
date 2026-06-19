#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

PORT="${OPENSHELL_PATCHED_GATEWAY_PORT:-17671}"
HEALTH_PORT="${OPENSHELL_PATCHED_GATEWAY_HEALTH_PORT:-17672}"
RUNTIME_DIR=".forkcell/openshell-patched"
PID_FILE="$RUNTIME_DIR/gateway.pid"
CONFIG_FILE="$RUNTIME_DIR/gateway.toml"
LOG_FILE="$RUNTIME_DIR/logs/gateway.log"
CERT_DIR="$RUNTIME_DIR/cert-bundle"
OPENSHELL_BIN="$PWD/upstream/openshell/target/debug/openshell"
GATEWAY_BIN="$PWD/upstream/openshell/target/debug/openshell-gateway"
SUPERVISOR_BIN="$PWD/upstream/openshell/target-linux-docker/debug/openshell-sandbox"
if [ -f "$PWD/.forkcell/runtime-lock.json" ]; then
  while IFS='=' read -r key value; do
    case "$key" in
      openshell) OPENSHELL_BIN="$value" ;;
      openshell_gateway) GATEWAY_BIN="$value" ;;
      openshell_sandbox) SUPERVISOR_BIN="$value" ;;
    esac
  done < <(python3 - <<'PY'
import json
from pathlib import Path
lock = json.loads(Path(".forkcell/runtime-lock.json").read_text())
for key in ("openshell", "openshell_gateway", "openshell_sandbox"):
    print(f"{key}={lock['binaries'][key]['path']}")
PY
)
fi

for bin in "$OPENSHELL_BIN" "$GATEWAY_BIN" "$SUPERVISOR_BIN"; do
  if [ ! -x "$bin" ]; then
    echo "required patched OpenShell binary not found or not executable: $bin" >&2
    exit 2
  fi
done

mkdir -p "$RUNTIME_DIR"/{config,state,cache,logs}

if [ ! -f "$CERT_DIR/jwt/signing.pem" ] || [ ! -f "$CERT_DIR/jwt/public.pem" ] || [ ! -f "$CERT_DIR/jwt/kid" ]; then
  rm -rf "$CERT_DIR"
  mkdir -p "$CERT_DIR"
  "$GATEWAY_BIN" generate-certs --output-dir "$CERT_DIR" >/dev/null
fi

cat > "$CONFIG_FILE" <<EOF
[openshell.gateway.auth]
allow_unauthenticated_users = true

[openshell.gateway.gateway_jwt]
signing_key_path = "$PWD/$CERT_DIR/jwt/signing.pem"
public_key_path = "$PWD/$CERT_DIR/jwt/public.pem"
kid_path = "$PWD/$CERT_DIR/jwt/kid"
gateway_id = "forkcell-local"
ttl_secs = 0

[openshell.drivers.docker]
supervisor_bin = "$SUPERVISOR_BIN"
EOF

if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  if curl -fsS "http://127.0.0.1:$HEALTH_PORT/health" >/dev/null 2>&1; then
    echo "patched OpenShell gateway already running: http://127.0.0.1:$PORT"
    echo "export FORKCELL_OPENSHELL_BIN=\"$OPENSHELL_BIN\""
    echo "export OPENSHELL_GATEWAY_ENDPOINT=\"http://127.0.0.1:$PORT\""
    exit 0
  fi
  kill "$(cat "$PID_FILE")" 2>/dev/null || true
fi

: > "$LOG_FILE"
XDG_CONFIG_HOME="$PWD/$RUNTIME_DIR/config" \
XDG_STATE_HOME="$PWD/$RUNTIME_DIR/state" \
XDG_CACHE_HOME="$PWD/$RUNTIME_DIR/cache" \
OPENSHELL_LOG_LEVEL="${OPENSHELL_LOG_LEVEL:-info}" \
nohup "$GATEWAY_BIN" \
  --config "$PWD/$CONFIG_FILE" \
  --disable-tls \
  --port "$PORT" \
  --health-port "$HEALTH_PORT" \
  --drivers docker \
  > "$LOG_FILE" 2>&1 < /dev/null &
echo $! > "$PID_FILE"

for _ in $(seq 1 50); do
  if curl -fsS "http://127.0.0.1:$HEALTH_PORT/health" >/dev/null 2>&1; then
    echo "patched OpenShell gateway started: http://127.0.0.1:$PORT"
    echo "log=$PWD/$LOG_FILE"
    echo "export FORKCELL_OPENSHELL_BIN=\"$OPENSHELL_BIN\""
    echo "export OPENSHELL_GATEWAY_ENDPOINT=\"http://127.0.0.1:$PORT\""
    exit 0
  fi
  sleep 0.2
done

echo "patched OpenShell gateway failed to become healthy; tailing log:" >&2
tail -80 "$LOG_FILE" >&2 || true
exit 1
