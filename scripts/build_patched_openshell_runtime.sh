#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

OPENSHELL_DIR="$PWD/upstream/openshell"
SUPERVISOR_BUILD_SCRIPT="$PWD/.forkcell-build/scripts/build-supervisor.sh"
STAMP="$(date +%Y-%m-%d)"
EVIDENCE="${FORKCELL_EVIDENCE:-.forkcell/artifacts/patched-runtime-build-${STAMP}.md}"

if ! git -C "$OPENSHELL_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "OpenShell submodule not found: $OPENSHELL_DIR" >&2
  echo "Run: git submodule update --init --recursive" >&2
  exit 2
fi

mkdir -p "$(dirname "$SUPERVISOR_BUILD_SCRIPT")" "$(dirname "$EVIDENCE")"
cat > "$SUPERVISOR_BUILD_SCRIPT" <<'EOF'
#!/usr/bin/env bash
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends ca-certificates curl build-essential pkg-config cmake perl
export CARGO_HOME=/cargo-home
export RUSTUP_HOME=/rustup-home
if [ ! -x /cargo-home/bin/cargo ]; then
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain 1.95.0
fi
. /cargo-home/env
cd /src
CARGO_INCREMENTAL=0 CARGO_PROFILE_DEV_DEBUG=0 cargo build -p openshell-sandbox --target-dir /src/target-linux-docker
/cargo-home/bin/cargo --version
/src/target-linux-docker/debug/openshell-sandbox --version
EOF
chmod +x "$SUPERVISOR_BUILD_SCRIPT"

echo "building patched macOS OpenShell CLI/gateway..."
(
  cd "$OPENSHELL_DIR"
  CARGO_INCREMENTAL=0 CARGO_PROFILE_DEV_DEBUG=0 cargo build -p openshell-server -p openshell-cli
  ./target/debug/openshell --version
  ./target/debug/openshell-gateway --version
)

echo "building patched Linux supervisor inside Docker..."
timeout "${FORKCELL_BUILD_TIMEOUT_SECONDS:-900}" docker run --rm --user root --entrypoint /bin/bash \
  -v "$OPENSHELL_DIR:/src" \
  -v "$PWD/.forkcell-build/cargo-home:/cargo-home" \
  -v "$PWD/.forkcell-build/rustup-home:/rustup-home" \
  -v "$PWD/.forkcell-build/scripts:/scripts:ro" \
  ghcr.io/nvidia/openshell-community/sandboxes/base:latest /scripts/build-supervisor.sh

echo "patched runtime built:"
ls -lh \
  "$OPENSHELL_DIR/target/debug/openshell" \
  "$OPENSHELL_DIR/target/debug/openshell-gateway" \
  "$OPENSHELL_DIR/target-linux-docker/debug/openshell-sandbox"

python3 - <<'PY' "$EVIDENCE" "$OPENSHELL_DIR"
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

evidence = Path(sys.argv[1])
openshell_dir = Path(sys.argv[2])
bins = {
    "openshell": openshell_dir / "target/debug/openshell",
    "openshell_gateway": openshell_dir / "target/debug/openshell-gateway",
    "openshell_sandbox": openshell_dir / "target-linux-docker/debug/openshell-sandbox",
}

summary = {
    "build_validated": True,
    "binaries": {},
}
for name, path in bins.items():
    if name == "openshell_sandbox":
        # The supervisor is a Linux binary; ask Docker to execute it rather
        # than trying to run it on the macOS host.
        version = subprocess.check_output(
            [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{openshell_dir}:/src",
                "--entrypoint",
                "/src/target-linux-docker/debug/openshell-sandbox",
                "ghcr.io/nvidia/openshell-community/sandboxes/base:latest",
                "--version",
            ],
            text=True,
        ).strip()
    else:
        version = subprocess.check_output([str(path), "--version"], text=True).strip()
    summary["binaries"][name] = {
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "version": version,
        "executable": bool(path.stat().st_mode & 0o111),
    }
    assert summary["binaries"][name]["executable"], summary
    assert summary["binaries"][name]["size_bytes"] > 0, summary

lines = [
    "# ForkCell Patched Runtime Build",
    "",
    f"Date: `{datetime.now(timezone.utc).date().isoformat()}`",
    "",
    "## Summary",
    "",
    f"- Build validated: `{summary['build_validated']}`",
]
for name, item in summary["binaries"].items():
    lines.append(
        f"- `{name}`: `{item['version']}`, `{item['size_bytes']}` bytes, executable `{item['executable']}`"
    )
lines.extend(
    [
        "",
        "## Summary JSON",
        "",
        "```json",
        json.dumps(summary, indent=2, sort_keys=True),
        "```",
    ]
)
evidence.write_text("\n".join(lines) + "\n")
print(f"evidence={evidence}")
print(json.dumps(summary, indent=2, sort_keys=True))
PY
