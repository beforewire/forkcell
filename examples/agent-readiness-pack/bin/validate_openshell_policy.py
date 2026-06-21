#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

PACK_ROOT = Path(__file__).resolve().parents[1]
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def file_hash(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    return sha256_bytes(path.read_bytes())


def clean_output(text: str) -> str:
    return ANSI_RE.sub("", text).strip()


def add_error(errors: list[dict[str, str]], field: str, message: str) -> None:
    errors.append({"field": field, "message": message})


def validate_paths(policy: dict[str, Any], errors: list[dict[str, str]]) -> None:
    fs = policy.get("filesystem_policy") or {}
    if not isinstance(fs, dict):
        add_error(errors, "filesystem_policy", "must be an object")
        return
    paths: list[str] = []
    for key in ("read_only", "read_write"):
        values = fs.get(key) or []
        if not isinstance(values, list):
            add_error(errors, f"filesystem_policy.{key}", "must be a list")
            continue
        for idx, value in enumerate(values):
            field = f"filesystem_policy.{key}[{idx}]"
            if not isinstance(value, str):
                add_error(errors, field, "must be a string")
                continue
            paths.append(value)
            if not value.startswith("/"):
                add_error(errors, field, "must be absolute")
            if ".." in Path(value).parts:
                add_error(errors, field, "must not contain traversal")
            if len(value) > 4096:
                add_error(errors, field, "must be 4096 characters or fewer")
            if key == "read_write" and value == "/":
                add_error(errors, field, "read-write path must not be root")
    if len(paths) > 256:
        add_error(errors, "filesystem_policy", "combined read_only/read_write paths must not exceed 256")


def validate_process(policy: dict[str, Any], errors: list[dict[str, str]]) -> None:
    process = policy.get("process") or {}
    if not isinstance(process, dict):
        add_error(errors, "process", "must be an object")
        return
    for key in ("run_as_user", "run_as_group"):
        value = str(process.get(key, "sandbox"))
        if value in {"root", "0"}:
            add_error(errors, f"process.{key}", "must not be root or 0")


def validate_landlock(policy: dict[str, Any], errors: list[dict[str, str]]) -> None:
    landlock = policy.get("landlock") or {}
    if not isinstance(landlock, dict):
        add_error(errors, "landlock", "must be an object")
        return
    compatibility = landlock.get("compatibility", "best_effort")
    if compatibility not in {"best_effort", "hard_requirement"}:
        add_error(errors, "landlock.compatibility", "must be best_effort or hard_requirement")


def validate_network(policy: dict[str, Any], errors: list[dict[str, str]]) -> list[dict[str, Any]]:
    network = policy.get("network_policies") or {}
    matrix: list[dict[str, Any]] = []
    if not isinstance(network, dict):
        add_error(errors, "network_policies", "must be a map")
        return matrix
    for name, entry in sorted(network.items()):
        if not isinstance(entry, dict):
            add_error(errors, f"network_policies.{name}", "must be an object")
            continue
        endpoints = entry.get("endpoints")
        binaries = entry.get("binaries")
        if not isinstance(endpoints, list) or not endpoints:
            add_error(errors, f"network_policies.{name}.endpoints", "must be a non-empty list")
            endpoints = []
        if not isinstance(binaries, list) or not binaries:
            add_error(errors, f"network_policies.{name}.binaries", "must be a non-empty list")
            binaries = []
        binary_paths: list[str] = []
        for idx, binary in enumerate(binaries):
            field = f"network_policies.{name}.binaries[{idx}].path"
            path = binary.get("path") if isinstance(binary, dict) else None
            if not isinstance(path, str) or not path.startswith("/"):
                add_error(errors, field, "must be an absolute executable path")
            else:
                binary_paths.append(path)
        for idx, endpoint in enumerate(endpoints):
            field = f"network_policies.{name}.endpoints[{idx}]"
            if not isinstance(endpoint, dict):
                add_error(errors, field, "must be an object")
                continue
            host = endpoint.get("host")
            port = endpoint.get("port")
            if not isinstance(host, str) or not host:
                add_error(errors, f"{field}.host", "must be a non-empty string")
            elif host in {"*", "**"} or host.startswith("*.") and host.count(".") == 1:
                add_error(errors, f"{field}.host", "wildcard is too broad")
            if not isinstance(port, int) or port < 1 or port > 65535:
                add_error(errors, f"{field}.port", "must be an integer TCP port")
            protocol = endpoint.get("protocol")
            if protocol is not None and protocol not in {"rest", "websocket", "graphql"}:
                add_error(errors, f"{field}.protocol", "must be rest, websocket, graphql, or omitted")
            enforcement = endpoint.get("enforcement")
            if enforcement is not None and enforcement not in {"audit", "enforce"}:
                add_error(errors, f"{field}.enforcement", "must be audit or enforce")
            access = endpoint.get("access")
            if access is not None and access not in {"read-only", "read-write", "full"}:
                add_error(errors, f"{field}.access", "must be read-only, read-write, or full")
            if access is not None and "rules" in endpoint:
                add_error(errors, field, "access and rules are mutually exclusive")
            matrix.append(
                {
                    "policy": name,
                    "host": host,
                    "port": port,
                    "protocol": protocol,
                    "enforcement": enforcement or "enforce",
                    "access": access or "custom",
                    "binaries": binary_paths,
                }
            )
    return matrix


def validate_schema(policy: dict[str, Any]) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    errors: list[dict[str, str]] = []
    if policy.get("version") != 1:
        add_error(errors, "version", "must be integer 1")
    validate_paths(policy, errors)
    validate_process(policy, errors)
    validate_landlock(policy, errors)
    matrix = validate_network(policy, errors)
    return errors, matrix


def run_policy_prover(policy_path: Path, credentials_path: Path, registry_path: Path) -> dict[str, Any]:
    openshell = shutil.which("openshell")
    if not openshell:
        return {"available": False, "status": "skip", "reason": "openshell CLI not found on PATH"}
    cmd = [
        openshell,
        "policy",
        "prove",
        "--policy",
        str(policy_path),
        "--credentials",
        str(credentials_path),
        "--registry",
        str(registry_path),
        "--compact",
    ]
    proc = subprocess.run(cmd, cwd=PACK_ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    return {
        "available": True,
        "status": "pass" if proc.returncode == 0 else "review",
        "exit_code": proc.returncode,
        "command": " ".join(cmd),
        "output": clean_output(proc.stdout),
    }


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate and shadow-apply the local OpenShell policy")
    parser.add_argument("--policy", default="policies/openshell.yaml")
    parser.add_argument("--credentials", default="fixtures/openshell-credentials.yaml")
    parser.add_argument("--registry", default="deps/OpenShell/crates/openshell-prover/registry")
    parser.add_argument("--output", default="results/openshell-policy-validation.json")
    args = parser.parse_args()

    policy_path = PACK_ROOT / args.policy
    credentials_path = PACK_ROOT / args.credentials
    registry_path = PACK_ROOT / args.registry
    policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    schema_errors, network_matrix = validate_schema(policy)
    prover = run_policy_prover(policy_path, credentials_path, registry_path)
    schema_status = "pass" if not schema_errors else "fail"
    shadow_apply_status = "ready_to_apply" if schema_status == "pass" and prover.get("status") == "pass" else "review"
    payload = {
        "schema": "beforewire.openshell-policy-validation.v1",
        "generated_at": utc_now(),
        "mode": "shadow-readonly",
        "policy": {"path": args.policy, "sha256": file_hash(policy_path)},
        "credentials_descriptor": {"path": args.credentials, "sha256": file_hash(credentials_path), "contains_secret_values": False},
        "schema_validation": {"status": schema_status, "errors": schema_errors},
        "openshell_policy_prove": prover,
        "shadow_apply": {
            "status": shadow_apply_status,
            "live_apply_performed": False,
            "reason": "shadow-readonly mode; no live sandbox name was targeted",
            "ready_command_template": f"openshell policy set <sandbox-name> --policy {args.policy} --wait",
            "network_matrix": network_matrix,
        },
    }
    payload["status"] = "pass" if schema_status == "pass" and prover.get("status") == "pass" else "fail"
    output = PACK_ROOT / args.output
    write_json(output, payload)
    print(json.dumps({"status": payload["status"], "schema": schema_status, "prover": prover.get("status"), "shadow_apply": shadow_apply_status, "output": args.output}, indent=2))
    return 0 if payload["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
