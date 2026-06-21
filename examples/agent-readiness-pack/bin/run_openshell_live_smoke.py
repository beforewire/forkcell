#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PACK_ROOT = Path(__file__).resolve().parents[1]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return int(port)


def run(cmd: list[str], *, cwd: Path = PACK_ROOT, timeout: int = 60) -> dict[str, Any]:
    proc = subprocess.run(cmd, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False, timeout=timeout)
    return {"command": cmd, "exit_code": proc.returncode, "output": proc.stdout.strip()}


def wait_health(port: int, timeout: int = 45) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        proc = subprocess.run(["curl", "-sf", f"http://127.0.0.1:{port}/healthz"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if proc.returncode == 0:
            return True
        time.sleep(1)
    return False


def parse_ssh_host(config: str) -> str:
    for line in config.splitlines():
        line = line.strip()
        if line.startswith("Host "):
            return line.split(None, 1)[1]
    return ""


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a disposable OpenShell sandbox, apply policy, and verify egress decisions")
    parser.add_argument("--policy", default="policies/openshell.yaml")
    parser.add_argument("--output", default="results/openshell-live-smoke.json")
    args = parser.parse_args()

    openshell = shutil.which("openshell")
    gateway = shutil.which("openshell-gateway")
    if not openshell or not gateway:
        payload = {"schema": "beforewire.openshell-live-smoke.v1", "generated_at": utc_now(), "status": "fail", "error": "openshell or openshell-gateway not found"}
        write_json(PACK_ROOT / args.output, payload)
        print(json.dumps({"status": "fail", "error": payload["error"]}, indent=2))
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="bw-openshell-live."))
    server_port = free_port()
    health_port = free_port()
    endpoint = f"http://127.0.0.1:{server_port}"
    gateway_name = f"bw-live-gw-{int(time.time())}"
    sandbox_name = f"bw-live-{int(time.time())}"
    active_gateway_file = Path.home() / ".config" / "openshell" / "active_gateway"
    previous_active_gateway = active_gateway_file.read_text(encoding="utf-8").strip() if active_gateway_file.exists() else ""
    gw_out = tmp / "gateway.out"
    gw_err = tmp / "gateway.err"
    gateway_proc: subprocess.Popen[str] | None = None
    steps: dict[str, Any] = {}
    try:
        cert_dir = tmp / "certs"
        steps["generate_certs"] = run([gateway, "generate-certs", "--output-dir", str(cert_dir), "--server-san", "127.0.0.1"], timeout=30)
        config = tmp / "gateway.toml"
        config.write_text(
            f"""[openshell]
version = 1

[openshell.gateway]
bind_address = "127.0.0.1:{server_port}"
health_bind_address = "127.0.0.1:{health_port}"
log_level = "info"
compute_drivers = ["docker"]
disable_tls = true

[openshell.gateway.auth]
allow_unauthenticated_users = true

[openshell.gateway.gateway_jwt]
signing_key_path = "{cert_dir / 'jwt' / 'signing.pem'}"
public_key_path = "{cert_dir / 'jwt' / 'public.pem'}"
kid_path = "{cert_dir / 'jwt' / 'kid'}"
gateway_id = "beforewire-live-smoke"
ttl_secs = 3600

[openshell.drivers.docker]
grpc_endpoint = "http://host.openshell.internal:{server_port}"
image_pull_policy = "IfNotPresent"
""",
            encoding="utf-8",
        )
        env = os.environ.copy()
        env["OPENSHELL_DB_URL"] = f"sqlite:{tmp / 'gateway.db'}?mode=rwc"
        gateway_proc = subprocess.Popen([gateway, "--config", str(config)], cwd=PACK_ROOT, env=env, text=True, stdout=gw_out.open("w"), stderr=gw_err.open("w"))
        steps["gateway_health"] = {"ready": wait_health(health_port)}
        if not steps["gateway_health"]["ready"]:
            raise RuntimeError("temporary OpenShell gateway did not become healthy")

        steps["gateway_add"] = run([openshell, "gateway", "add", endpoint, "--local", "--name", gateway_name], timeout=30)
        if steps["gateway_add"]["exit_code"] != 0:
            raise RuntimeError("temporary gateway registration failed")
        prefix = [openshell, "--gateway", gateway_name]
        steps["sandbox_create"] = run(prefix + ["sandbox", "create", "--name", sandbox_name, "--keep", "--no-auto-providers", "--no-tty", "--from", "base", "--", "echo", "sandbox ready"], timeout=120)
        if steps["sandbox_create"]["exit_code"] != 0:
            raise RuntimeError("sandbox create failed")

        ssh_config = tmp / "ssh_config"
        ssh_conf_result = run(prefix + ["sandbox", "ssh-config", sandbox_name], timeout=30)
        steps["ssh_config"] = {"exit_code": ssh_conf_result["exit_code"]}
        ssh_config_text = ssh_conf_result["output"]
        ssh_config.write_text(ssh_config_text + "\n", encoding="utf-8")
        steps["ssh_config"]["text_preview"] = ssh_config_text[:1000]
        ssh_host = parse_ssh_host(ssh_config_text)
        if not ssh_host:
            raise RuntimeError("could not parse OpenShell SSH host")
        for _ in range(20):
            probe = run(["ssh", "-F", str(ssh_config), ssh_host, "true"], timeout=10)
            if probe["exit_code"] == 0:
                break
            time.sleep(1)
        steps["ssh_ready"] = probe
        if probe["exit_code"] != 0:
            raise RuntimeError("sandbox ssh not ready")

        steps["default_deny_get"] = run(["ssh", "-F", str(ssh_config), ssh_host, "curl", "-fsS", "--max-time", "8", "https://api.github.com/zen"], timeout=20)
        steps["policy_set"] = run(prefix + ["policy", "set", sandbox_name, "--policy", args.policy, "--wait", "--timeout", "60"], timeout=90)
        if steps["policy_set"]["exit_code"] != 0:
            raise RuntimeError("openshell policy set failed")
        steps["allowed_get"] = run(["ssh", "-F", str(ssh_config), ssh_host, "curl", "-fsS", "--max-time", "10", "https://api.github.com/zen"], timeout=30)
        steps["denied_post"] = run(
            [
                "ssh",
                "-F",
                str(ssh_config),
                ssh_host,
                "curl",
                "-fsS",
                "--max-time",
                "10",
                "-X",
                "POST",
                "-H",
                "Content-Type: application/json",
                "-d",
                "{}",
                "https://api.github.com/repos/octocat/hello-world/issues",
            ],
            timeout=30,
        )
        steps["openshell_logs"] = run(prefix + ["logs", sandbox_name, "--since", "5m", "-n", "80"], timeout=30)
        logs = steps["openshell_logs"].get("output", "").lower()
        # Treat upstream 5xx/timeout responses as policy success when OpenShell
        # logs prove the GET was allowed by the active policy.
        allowed_get_by_policy = "http:get" in logs and "allowed get" in logs and "api.github.com" in logs
        checks = {
            "sandbox_created": steps["sandbox_create"]["exit_code"] == 0,
            "policy_set_wait_succeeded": steps["policy_set"]["exit_code"] == 0,
            "default_get_denied": steps["default_deny_get"]["exit_code"] != 0,
            "allowed_get_succeeded": steps["allowed_get"]["exit_code"] == 0 or allowed_get_by_policy,
            "post_denied_after_policy": steps["denied_post"]["exit_code"] != 0,
            "logs_show_policy_decision": any(
                word in logs
                for word in ["action=deny", "action=allow", "policy_denied", "denied", "allowed"]
            ),
        }
        status = "pass" if all(checks.values()) else "fail"
    except Exception as exc:
        checks = {"exception_free": False}
        status = "fail"
        steps["exception"] = {"type": exc.__class__.__name__, "message": str(exc)}
    finally:
        if "sandbox_create" in steps and steps["sandbox_create"].get("exit_code") == 0:
            steps["sandbox_delete"] = run([openshell, "--gateway", gateway_name, "sandbox", "delete", sandbox_name], timeout=60)
        if "gateway_add" in steps and steps["gateway_add"].get("exit_code") == 0:
            steps["gateway_remove"] = run([openshell, "gateway", "remove", gateway_name], timeout=30)
        if previous_active_gateway:
            steps["gateway_restore"] = run([openshell, "gateway", "select", previous_active_gateway], timeout=30)
        if gateway_proc:
            gateway_proc.terminate()
            try:
                gateway_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                gateway_proc.kill()
        (PACK_ROOT / "logs" / "openshell-live-gateway.out").write_text(gw_out.read_text(encoding="utf-8") if gw_out.exists() else "", encoding="utf-8")
        (PACK_ROOT / "logs" / "openshell-live-gateway.err").write_text(gw_err.read_text(encoding="utf-8") if gw_err.exists() else "", encoding="utf-8")

    payload = {
        "schema": "beforewire.openshell-live-smoke.v1",
        "generated_at": utc_now(),
        "mode": "live-disposable-sandbox",
        "status": status,
        "sandbox_name": sandbox_name,
        "gateway_name": gateway_name,
        "gateway_endpoint": endpoint,
        "policy": args.policy,
        "checks": checks,
        "steps": steps,
    }
    write_json(PACK_ROOT / args.output, payload)
    print(json.dumps({"status": status, "checks": checks, "output": args.output}, indent=2))
    return 0 if status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
