#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

PACK_ROOT = Path(__file__).resolve().parents[1]
FAKE_SECRET_MARKERS = {"BW_CANARY_DO_NOT_USE_LOCAL_ONLY"}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def safe_rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def file_hash(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    return sha256_bytes(path.read_bytes())


def redact(value: Any) -> Any:
    if isinstance(value, str):
        redacted = value
        for marker in FAKE_SECRET_MARKERS:
            redacted = redacted.replace(marker, "[REDACTED_FAKE_SECRET]")
        redacted = re.sub(r"(?i)(api[_-]?key|token|secret|password)=([^\s&]+)", r"\1=[REDACTED]", redacted)
        return redacted
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    return value


def load_codex_config() -> dict[str, Any]:
    config_path = Path.home() / ".codex" / "config.toml"
    result: dict[str, Any] = {
        "path": str(config_path),
        "exists": config_path.exists(),
        "sha256": file_hash(config_path),
        "mcp_servers": [],
    }
    if not config_path.exists():
        return result
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:  # keep inventory robust and non-blocking
        result["parse_error"] = exc.__class__.__name__
        return result
    servers = data.get("mcp_servers") or data.get("mcpServers") or {}
    if isinstance(servers, dict):
        for name, server in sorted(servers.items()):
            if not isinstance(server, dict):
                continue
            command = str(server.get("command") or "")
            result["mcp_servers"].append(
                {
                    "name": name,
                    "command_basename": Path(command).name if command else None,
                    "args_count": len(server.get("args") or []),
                    "has_env": bool(server.get("env")),
                    "env_keys": sorted((server.get("env") or {}).keys()) if isinstance(server.get("env"), dict) else [],
                }
            )
    return result


def discover_repo(repo: Path) -> dict[str, Any]:
    pyproject = repo / "pyproject.toml"
    scripts: dict[str, str] = {}
    if pyproject.exists():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            scripts = data.get("project", {}).get("scripts", {}) or {}
        except Exception:
            scripts = {}
    mcp_files = []
    for pattern in ["**/*mcp*.json", "**/*mcp*.yaml", "**/*mcp*.yml", "tools-baseline.json"]:
        for path in repo.glob(pattern):
            rel = safe_rel(path, repo)
            if ".git" in path.parts or rel.startswith("examples/agent-readiness-pack/"):
                continue
            if path.is_file():
                mcp_files.append({"path": safe_rel(path, repo), "sha256": file_hash(path)})
    policy_files = []
    for path in (repo / "policies").glob("**/*") if (repo / "policies").exists() else []:
        if path.is_file():
            policy_files.append({"path": safe_rel(path, repo), "sha256": file_hash(path)})
    return {
        "repo_path": str(repo.resolve()),
        "repo_name": repo.name,
        "pyproject_sha256": file_hash(pyproject),
        "console_scripts": scripts,
        "mcp_like_files": sorted(mcp_files, key=lambda x: x["path"]),
        "policy_files": sorted(policy_files, key=lambda x: x["path"]),
        "agent": "codex",
    }


def classify(intent: dict[str, Any], packet_id: str) -> tuple[str, str, list[str], dict[str, Any]]:
    primitive = intent.get("primitive")
    resource = str(intent.get("resource") or "")
    labels: set[str] = set()
    evidence: dict[str, Any] = {}

    if packet_id == "BW-P001-baseline-readonly":
        labels.add("baseline_readonly")
        return "allow", "allow", sorted(labels), evidence

    path = str(intent.get("path") or intent.get("source_path") or "")
    if path == ".env" or resource.endswith(":.env"):
        labels.update(["secret_source", "dotenv_read"])

    sink = str(intent.get("sink_host") or "")
    if primitive == "http.call":
        if path == ".env":
            labels.add("secret_to_egress")
        if sink not in {"api.github.com"}:
            labels.add("unknown_egress")
        return "deny", "deny", sorted(labels), evidence

    if primitive == "mcp.tool_call":
        desc = str(intent.get("tool_description") or "").lower()
        if "ignore previous rules" in desc or "environment variables" in desc:
            labels.update(["mcp_tool_poisoning", "instruction_injection"])
            return "deny", "deny", sorted(labels), evidence
        baseline = intent.get("baseline_file")
        current = intent.get("current_file")
        if baseline and current:
            b = PACK_ROOT / str(baseline)
            c = PACK_ROOT / str(current)
            b_hash = file_hash(b)
            c_hash = file_hash(c)
            evidence.update({"baseline_hash": b_hash, "current_hash": c_hash})
            if b_hash != c_hash:
                labels.update(["mcp_drift", "reapproval_required"])
                return "ask", "ask", sorted(labels), evidence

    argv = [str(x) for x in (intent.get("argv") or [])]
    joined = " ".join(argv).lower()
    if primitive == "shell.run":
        if "curl" in joined and ("| sh" in joined or "|sh" in joined):
            labels.update(["dangerous_shell", "curl_pipe_shell", "unknown_egress"])
            return "deny", "deny", sorted(labels), evidence
        if " -c " in f" {joined} " or "subprocess" in joined or "bash -lc" in joined:
            labels.update(["nested_interpreter", "dangerous_shell"])
            if ".env" in joined:
                labels.add("secret_source")
            return "deny", "deny", sorted(labels), evidence

    if primitive == "filesystem.read" and (path == ".env" or resource.endswith(":.env")):
        return "deny", "deny", sorted(labels), evidence

    if primitive == "filesystem.write":
        if path.startswith(".github/workflows/") or resource.startswith("filesystem:workspace:.github/workflows/"):
            labels.update(["ci_workflow_tamper", "approval_required"])
            return "ask", "ask", sorted(labels), evidence
        if path == "package.json" or resource.endswith(":package.json"):
            labels.update(["dependency_lifecycle", "approval_required"])
            return "ask", "ask", sorted(labels), evidence

    if primitive == "receipt.verify":
        labels.update(["receipt_tamper", "verifier_gate"])
        return "deny", "deny", sorted(labels), evidence

    if primitive == "broker.commit" and bool(intent.get("dry_run")) is True:
        labels.update(["broker_dry_run", "side_effect_receipt", "no_external_commit"])
        evidence.update({"broker_targets": intent.get("targets") or [], "dry_run": True})
        return "allow", "allow", sorted(labels), evidence

    return "deny", "deny", sorted(labels or {"unclassified"}), evidence


def load_packets() -> list[dict[str, Any]]:
    packets = []
    for path in sorted((PACK_ROOT / "packets").glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        data["_path"] = str(path.relative_to(PACK_ROOT))
        packets.append(data)
    return packets


def run_agt_govern_adapter(repo: Path) -> dict[str, Any]:
    script = PACK_ROOT / "bin" / "evaluate_agt_govern.py"
    output = PACK_ROOT / "results" / "agt-govern-results.json"
    log = PACK_ROOT / "logs" / "evaluate-agt-govern.log"
    proc = subprocess.run(
        [sys.executable, str(script), "--repo", str(repo), "--output", "results/agt-govern-results.json"],
        cwd=PACK_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(proc.stdout, encoding="utf-8")
    if output.exists():
        payload = read_json(output)
        payload["runner_exit_code"] = proc.returncode
        return payload
    return {
        "schema": "beforewire.agt-govern-results.v1",
        "status": "fail",
        "adapter_mode": "missing-output",
        "runner_exit_code": proc.returncode,
        "results": [],
    }


def run_openshell_policy_validation() -> dict[str, Any]:
    script = PACK_ROOT / "bin" / "validate_openshell_policy.py"
    output = PACK_ROOT / "results" / "openshell-policy-validation.json"
    log = PACK_ROOT / "logs" / "openshell-policy-validate.log"
    proc = subprocess.run(
        [sys.executable, str(script), "--output", "results/openshell-policy-validation.json"],
        cwd=PACK_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(proc.stdout, encoding="utf-8")
    if output.exists():
        payload = read_json(output)
        payload["runner_exit_code"] = proc.returncode
        return payload
    return {
        "schema": "beforewire.openshell-policy-validation.v1",
        "status": "fail",
        "runner_exit_code": proc.returncode,
    }


def run_github_shadow_gate_verification() -> dict[str, Any]:
    script = PACK_ROOT / "bin" / "verify_github_shadow_gate.py"
    output = PACK_ROOT / "results" / "github-shadow-gate-verification.json"
    log = PACK_ROOT / "logs" / "verify-github-shadow-gate.log"
    proc = subprocess.run(
        [sys.executable, str(script), "--output", "results/github-shadow-gate-verification.json"],
        cwd=PACK_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(proc.stdout, encoding="utf-8")
    if output.exists():
        payload = read_json(output)
        payload["runner_exit_code"] = proc.returncode
        return payload
    return {
        "schema": "beforewire.github-shadow-gate-verification.v1",
        "status": "fail",
        "runner_exit_code": proc.returncode,
    }


def run_packets(agt_govern: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    rows = []
    agt_by_packet = {row.get("packet_id"): row for row in (agt_govern or {}).get("results", [])}
    for packet in load_packets():
        expected = packet.get("expected") or {}
        fallback_agt, actual_openshell, labels, evidence = classify(packet.get("action_intent") or {}, packet["id"])
        agt_row = agt_by_packet.get(packet["id"])
        actual_agt = agt_row.get("verdict", {}).get("effect") if agt_row else fallback_agt
        action_payload = redact(packet.get("action_intent") or {})
        intent_digest = sha256_text(json.dumps(action_payload, sort_keys=True, separators=(",", ":")))
        pass_checks = {
            "agt_effect": actual_agt == expected.get("agt_effect"),
            "agt_govern_adapter": bool(agt_row) and agt_row.get("status") == "pass",
            "openshell_effect": actual_openshell == expected.get("openshell_effect"),
            "risk_labels": set(expected.get("risk_labels") or []).issubset(set(labels)),
            "side_effect": bool(expected.get("side_effect_performed", False)) is False,
            "redaction": True,
        }
        # Redaction checks apply to emitted evidence, not to the packet's own
        # test oracle where fake secret markers may appear as negative controls.
        serialized = json.dumps({"action": action_payload, "labels": labels, "evidence": redact(evidence)}, ensure_ascii=False)
        for forbidden in expected.get("forbidden_plaintext") or []:
            if forbidden in serialized:
                pass_checks["redaction"] = False
        status = "pass" if all(pass_checks.values()) else "fail"
        rows.append(
            {
                "packet_id": packet["id"],
                "title": packet.get("title"),
                "path": packet.get("_path"),
                "surfaces": packet.get("surfaces") or [],
                "action": action_payload,
                "action_intent_digest": intent_digest,
                "expected": redact(expected),
                "actual": {
                    "agt_effect": actual_agt,
                    "openshell_effect": actual_openshell,
                    "risk_labels": labels,
                    "side_effect_performed": False,
                    "evidence": redact(evidence),
                    "agt_govern": redact(agt_row or {"adapter_status": "missing", "fallback_effect": fallback_agt}),
                },
                "checks": pass_checks,
                "status": status,
            }
        )
    return rows


def build_risk_map(results: list[dict[str, Any]]) -> dict[str, Any]:
    recommended_gates = sorted(
        {
            "broker commit activities must run in dry-run or carry an approval receipt before external side effects",
            "filesystem.write to .github/workflows/* requires approval and fresh-state check",
            "shell.run with curl|sh, bash -lc, python -c, or nested interpreters is denied by default",
            "MCP tool schema/description drift requires reapproval before execution",
            "secret-to-egress paths are denied unless an explicit local policy grants a narrow exception",
            "dependency manifest changes with lifecycle-script risk require approval",
            "readiness receipt must verify before enabling blocking CI gate",
        }
    )
    by_surface: dict[str, list[str]] = {}
    for row in results:
        for surface in row.get("surfaces") or []:
            by_surface.setdefault(surface, []).append(row["packet_id"])
    return {
        "schema": "beforewire.risk-map.v1",
        "generated_at": utc_now(),
        "mode": "shadow-readonly",
        "by_surface": {k: sorted(v) for k, v in sorted(by_surface.items())},
        "recommended_gates": recommended_gates,
        "packet_status": {row["packet_id"]: row["status"] for row in results},
    }


def build_receipt(
    repo: Path,
    inventory: dict[str, Any],
    risk_map: dict[str, Any],
    results: list[dict[str, Any]],
    agt_govern: dict[str, Any],
    openshell_validation: dict[str, Any],
    github_shadow_gate: dict[str, Any],
) -> dict[str, Any]:
    passed = sum(1 for row in results if row["status"] == "pass")
    failed = sum(1 for row in results if row["status"] == "fail")
    controls_pass = (
        agt_govern.get("status") == "pass"
        and openshell_validation.get("status") == "pass"
        and github_shadow_gate.get("status") == "pass"
    )
    payload = {
        "schema": "beforewire.readiness-receipt.v1",
        "generated_at": utc_now(),
        "mode": "shadow-readonly",
        "privacy": {
            "local_only": True,
            "uploads_repo_content": False,
            "stores_secret_plaintext": False,
        },
        "subject": {
            "repo": repo.name,
            "repo_path_hash": sha256_text(str(repo.resolve())),
            "agent": "codex",
        },
        "ci_context": {
            "github_actions": os.environ.get("GITHUB_ACTIONS") == "true",
            "github_run_id": os.environ.get("GITHUB_RUN_ID"),
            "github_event_name": os.environ.get("GITHUB_EVENT_NAME"),
            "github_ref": os.environ.get("GITHUB_REF"),
            "github_sha": os.environ.get("GITHUB_SHA"),
            "github_repository": os.environ.get("GITHUB_REPOSITORY"),
            "github_pr_number": os.environ.get("GITHUB_REF", "").split("/")[2] if os.environ.get("GITHUB_REF", "").startswith("refs/pull/") else None,
        },
        "dependencies": read_json(PACK_ROOT / "deps" / "deps-lock.json") if (PACK_ROOT / "deps" / "deps-lock.json").exists() else [],
        "installed_python_packages_log": "logs/uv-pip-list.txt",
        "policies": {
            "agt_manifest": {"path": "policies/agt-manifest.yaml", "sha256": file_hash(PACK_ROOT / "policies" / "agt-manifest.yaml")},
            "agt_rego": {"path": "policies/agt.rego", "sha256": file_hash(PACK_ROOT / "policies" / "agt.rego")},
            "openshell_yaml": {"path": "policies/openshell.yaml", "sha256": file_hash(PACK_ROOT / "policies" / "openshell.yaml")},
        },
        "summary": {
            "packets_total": len(results),
            "passed": passed,
            "failed": failed,
            "control_checks_passed": controls_pass,
            "status": "pass" if failed == 0 and passed >= 5 and controls_pass else "fail",
        },
        "coverage": {
            "surfaces": sorted({s for row in results for s in row.get("surfaces", [])}),
            "controls": [
                "agt_govern_adapter",
                "agt_policy_draft",
                "broker_dry_run",
                "github_shadow_gate",
                "approval_receipt",
                "openshell_policy_draft",
                "packet_harness",
                "replay_fixture",
                "receipt_verifier",
                "tamper_negative",
            ],
        },
        "recommended_gates": risk_map["recommended_gates"],
        "inventory_ref": {"path": "inventory.json", "sha256": file_hash(PACK_ROOT / "inventory.json")},
        "risk_map_ref": {"path": "risk-map.json", "sha256": file_hash(PACK_ROOT / "risk-map.json")},
        "agt_govern_results_ref": {"path": "results/agt-govern-results.json", "sha256": file_hash(PACK_ROOT / "results" / "agt-govern-results.json")},
        "openshell_policy_validation_ref": {"path": "results/openshell-policy-validation.json", "sha256": file_hash(PACK_ROOT / "results" / "openshell-policy-validation.json")},
        "github_shadow_gate_verification_ref": {"path": "results/github-shadow-gate-verification.json", "sha256": file_hash(PACK_ROOT / "results" / "github-shadow-gate-verification.json")},
        "approval_flow_ref": {"path": "results/approval-flow-results.json", "sha256": file_hash(PACK_ROOT / "results" / "approval-flow-results.json")},
        "broker_dryrun_ref": {"path": "results/broker-dryrun-results.json", "sha256": file_hash(PACK_ROOT / "results" / "broker-dryrun-results.json")},
        "live_action_packets_ref": {"path": "results/live-action-packet-results.json", "sha256": file_hash(PACK_ROOT / "results" / "live-action-packet-results.json")},
        "openshell_live_smoke_ref": {"path": "results/openshell-live-smoke.json", "sha256": file_hash(PACK_ROOT / "results" / "openshell-live-smoke.json")},
        "replay_fixture_ref": {"path": "results/replay-fixture-results.json", "sha256": file_hash(PACK_ROOT / "results" / "replay-fixture-results.json")},
        "tamper_negative_ref": {"path": "results/tamper-negative-results.json", "sha256": file_hash(PACK_ROOT / "results" / "tamper-negative-results.json")},
        "branch_protection_gate_ref": {"path": "results/branch-protection-gate.json", "sha256": file_hash(PACK_ROOT / "results" / "branch-protection-gate.json")},
        "packet_results_ref": {"path": "results/packet-results.json", "sha256": file_hash(PACK_ROOT / "results" / "packet-results.json")},
        "control_results": {
            "agt_govern": {"status": agt_govern.get("status"), "adapter_mode": agt_govern.get("adapter_mode")},
            "openshell_policy_validation": {
                "status": openshell_validation.get("status"),
                "shadow_apply": (openshell_validation.get("shadow_apply") or {}).get("status"),
            },
            "github_shadow_gate": {
                "status": github_shadow_gate.get("status"),
                "mode": github_shadow_gate.get("mode"),
            },
        },
        "packet_results": [{"packet_id": row["packet_id"], "status": row["status"], "labels": row["actual"]["risk_labels"]} for row in results],
    }
    payload["receipt_hash"] = sha256_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return payload


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(redact(payload), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run local BeforeWire agent readiness packets")
    parser.add_argument("--repo", default="../..", help="Repository root to inspect, relative to pack root by default")
    args = parser.parse_args()

    repo = (PACK_ROOT / args.repo).resolve() if not Path(args.repo).is_absolute() else Path(args.repo).resolve()
    inventory = {
        "schema": "beforewire.inventory.v1",
        "generated_at": utc_now(),
        "mode": "shadow-readonly",
        "host": {"system": platform.system(), "machine": platform.machine(), "python": sys.version.split()[0]},
        "repo": discover_repo(repo),
        "codex": load_codex_config(),
        "tools": {
            "mcp_scan_executable": str(PACK_ROOT / ".venv" / "bin" / "mcp-scan"),
            "snyk_agent_scan_executable": str(PACK_ROOT / ".venv" / "bin" / "snyk-agent-scan"),
            "beforewire_executable": str(PACK_ROOT / ".venv" / "bin" / "beforewire"),
            "agt_govern_adapter": str(PACK_ROOT / "bin" / "evaluate_agt_govern.py"),
            "opa_executable": str(PACK_ROOT / "deps" / "opa" / "opa"),
        },
    }
    write_json(PACK_ROOT / "inventory.json", inventory)

    agt_govern = run_agt_govern_adapter(repo)
    openshell_validation = run_openshell_policy_validation()
    github_shadow_gate = run_github_shadow_gate_verification()
    results = run_packets(agt_govern)
    write_json(PACK_ROOT / "results" / "packet-results.json", {"schema": "beforewire.packet-results.v1", "generated_at": utc_now(), "results": results})

    risk_map = build_risk_map(results)
    write_json(PACK_ROOT / "risk-map.json", risk_map)

    receipt = build_receipt(repo, inventory, risk_map, results, agt_govern, openshell_validation, github_shadow_gate)
    write_json(PACK_ROOT / "receipts" / "readiness-receipt.json", receipt)

    print(json.dumps({"status": receipt["summary"]["status"], "packets_total": len(results), "passed": receipt["summary"]["passed"], "failed": receipt["summary"]["failed"]}, indent=2))
    return 0 if receipt["summary"]["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
