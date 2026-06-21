#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import urlretrieve

PACK_ROOT = Path(__file__).resolve().parents[1]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def read_lock() -> list[dict[str, Any]]:
    return json.loads((PACK_ROOT / "deps" / "deps-lock.json").read_text(encoding="utf-8"))


def run(cmd: list[str], log: Path, cwd: Path = PACK_ROOT) -> int:
    proc = subprocess.run(cmd, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as fh:
        fh.write("$ " + " ".join(cmd) + "\n")
        fh.write(proc.stdout)
        if not proc.stdout.endswith("\n"):
            fh.write("\n")
        fh.write(f"exit={proc.returncode}\n\n")
    return proc.returncode


def ensure_git_dep(item: dict[str, Any], log: Path) -> dict[str, Any]:
    path = PACK_ROOT / item["path"]
    if path.exists():
        return {"name": item["name"], "path": item["path"], "status": "present"}
    path.parent.mkdir(parents=True, exist_ok=True)
    rc = run(["git", "clone", "--filter=blob:none", item["remote"], str(path)], log)
    if rc != 0:
        return {"name": item["name"], "path": item["path"], "status": "fail", "step": "clone"}
    rc = run(["git", "checkout", item["commit"]], log, cwd=path)
    return {"name": item["name"], "path": item["path"], "status": "present" if rc == 0 else "fail", "commit": item.get("commit")}


def opa_download_url() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "darwin" and machine in {"arm64", "aarch64"}:
        return "https://openpolicyagent.org/downloads/latest/opa_darwin_arm64_static"
    if system == "darwin":
        return "https://openpolicyagent.org/downloads/latest/opa_darwin_amd64_static"
    if system == "linux" and machine in {"arm64", "aarch64"}:
        return "https://openpolicyagent.org/downloads/latest/opa_linux_arm64_static"
    return "https://openpolicyagent.org/downloads/latest/opa_linux_amd64_static"


def ensure_opa(log: Path) -> dict[str, Any]:
    target = PACK_ROOT / "deps" / "opa" / "opa"
    if target.exists():
        version = subprocess.run([str(target), "version"], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
        return {"name": "opa", "path": "deps/opa/opa", "status": "present", "version_output": version.stdout.strip()}
    target.parent.mkdir(parents=True, exist_ok=True)
    url = opa_download_url()
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"$ download {url} -> {target}\n")
    urlretrieve(url, target)
    target.chmod(0o755)
    digest = sha256_bytes(target.read_bytes())
    version = subprocess.run([str(target), "version"], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    return {"name": "opa", "path": "deps/opa/opa", "status": "present", "sha256": digest, "version_output": version.stdout.strip()}


def ensure_python_deps(log: Path) -> dict[str, Any]:
    rc_yaml = run([sys.executable, "-m", "pip", "install", "pyyaml"], log)
    agt_sdk = PACK_ROOT / "deps" / "agent-governance-toolkit" / "policy-engine" / "sdk" / "python"
    rc_agt = 1
    if agt_sdk.exists():
        rc_agt = run([sys.executable, "-m", "pip", "install", str(agt_sdk)], log)
    return {"pyyaml": rc_yaml == 0, "agent_control_specification": rc_agt == 0}


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap readiness-pack deps for local or CI execution")
    parser.add_argument("--install-python-deps", action="store_true")
    args = parser.parse_args()
    log = PACK_ROOT / "logs" / "bootstrap-readiness-pack.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("", encoding="utf-8")
    deps = []
    for item in read_lock():
        if item.get("remote"):
            deps.append(ensure_git_dep(item, log))
    deps.append(ensure_opa(log))
    python_deps = ensure_python_deps(log) if args.install_python_deps else {"skipped": True}
    payload = {
        "schema": "beforewire.bootstrap-readiness-pack.v1",
        "generated_at": utc_now(),
        "deps": deps,
        "python_deps": python_deps,
        "status": "pass" if all(d.get("status") == "present" for d in deps) and (not args.install_python_deps or all(python_deps.values())) else "fail",
    }
    write_json(PACK_ROOT / "results" / "bootstrap-readiness-pack.json", payload)
    print(json.dumps({"status": payload["status"], "deps": deps, "python_deps": python_deps}, indent=2))
    return 0 if payload["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
