#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
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


def file_hash(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    return sha256_bytes(path.read_bytes())


def redact(value: Any) -> Any:
    if isinstance(value, str):
        redacted = value
        for marker in FAKE_SECRET_MARKERS:
            redacted = redacted.replace(marker, "[REDACTED_FAKE_SECRET]")
        return re.sub(r"(?i)(api[_-]?key|token|secret|password)=([^\s&]+)", r"\1=[REDACTED]", redacted)
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    return value


def load_packets() -> list[dict[str, Any]]:
    packets = []
    for path in sorted((PACK_ROOT / "packets").glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        data["_path"] = str(path.relative_to(PACK_ROOT))
        packets.append(data)
    return packets


def read_deps_lock() -> list[dict[str, Any]]:
    path = PACK_ROOT / "deps" / "deps-lock.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def dependency_by_name(name: str) -> dict[str, Any]:
    for item in read_deps_lock():
        if item.get("name") == name:
            return item
    return {}


def ensure_local_opa_on_path() -> dict[str, Any]:
    local_opa = PACK_ROOT / "deps" / "opa" / "opa"
    if local_opa.exists():
        os.environ["PATH"] = f"{local_opa.parent}{os.pathsep}{os.environ.get('PATH', '')}"
    opa = shutil.which("opa")
    result: dict[str, Any] = {"path": opa}
    if not opa:
        result["available"] = False
        return result
    proc = subprocess.run([opa, "version"], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    result.update({"available": proc.returncode == 0, "version_output": proc.stdout.strip()})
    return result


def enriched_intent(packet: dict[str, Any]) -> dict[str, Any]:
    intent = dict(packet.get("action_intent") or {})
    baseline = intent.get("baseline_file")
    current = intent.get("current_file")
    if baseline and current:
        intent["baseline_hash"] = file_hash(PACK_ROOT / str(baseline)) or ""
        intent["current_hash"] = file_hash(PACK_ROOT / str(current)) or ""
    return intent


def digest_intent(intent: dict[str, Any]) -> str:
    payload = redact(intent)
    return sha256_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def decision_to_effect(decision: str) -> str:
    return "ask" if decision == "escalate" else decision


def matched_rules(reason: str | None) -> list[str]:
    if not reason or reason.startswith("runtime_error:"):
        return []
    return sorted([part for part in reason.split(",") if part])


async def evaluate_packets() -> dict[str, Any]:
    opa = ensure_local_opa_on_path()
    try:
        from agent_control_specification import AgentControl, EnforcementMode, InterventionPoint
    except Exception as exc:
        return {
            "schema": "beforewire.agt-govern-results.v1",
            "generated_at": utc_now(),
            "adapter": "microsoft-agent-governance-toolkit",
            "adapter_mode": "unavailable",
            "status": "fail",
            "error": f"{exc.__class__.__name__}: {exc}",
            "opa": opa,
            "results": [],
        }

    manifest = PACK_ROOT / "policies" / "agt-manifest.yaml"
    policy = PACK_ROOT / "policies" / "agt.rego"
    control = AgentControl.from_path(str(manifest))
    rows = []
    for packet in load_packets():
        intent = enriched_intent(packet)
        expected_effect = (packet.get("expected") or {}).get("agt_effect")
        snapshot = {"tool_call": {"name": "beforewire_action", "args": intent}}
        result = await control.evaluate_intervention_point(
            InterventionPoint.PRE_TOOL_CALL,
            snapshot,
            EnforcementMode.EVALUATE_ONLY,
        )
        decision = getattr(result.verdict.decision, "value", str(result.verdict.decision))
        effect = decision_to_effect(decision)
        reason = result.verdict.reason or ""
        rows.append(
            {
                "packet_id": packet["id"],
                "path": packet.get("_path"),
                "intervention_point": "pre_tool_call",
                "mode": "evaluate_only",
                "tool_name": "beforewire_action",
                "action_intent_digest": digest_intent(intent),
                "expected_effect": expected_effect,
                "verdict": {
                    "decision": decision,
                    "effect": effect,
                    "reason": reason,
                    "matched_rules": matched_rules(reason),
                    "action_identity": result.action_identity,
                },
                "status": "pass" if effect == expected_effect and not reason.startswith("runtime_error:") else "fail",
            }
        )

    sdk_version = importlib.metadata.version("agent-control-specification")
    failed = sum(1 for row in rows if row["status"] != "pass")
    return {
        "schema": "beforewire.agt-govern-results.v1",
        "generated_at": utc_now(),
        "adapter": "microsoft-agent-governance-toolkit",
        "adapter_mode": "agt-acs-python-sdk+opa-local",
        "status": "pass" if failed == 0 else "fail",
        "summary": {"packets_total": len(rows), "passed": len(rows) - failed, "failed": failed},
        "agt_source": dependency_by_name("agent-governance-toolkit"),
        "sdk": {"package": "agent-control-specification", "version": sdk_version},
        "opa": opa,
        "manifest": {"path": "policies/agt-manifest.yaml", "sha256": file_hash(manifest)},
        "policy": {"path": "policies/agt.rego", "sha256": file_hash(policy), "query": "data.beforewire.agent_gate.verdict"},
        "results": rows,
    }


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(redact(payload), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate readiness packets through the local AGT/ACS govern adapter")
    parser.add_argument("--repo", default="../..", help="Accepted for parity with the readiness runner; no repo content is uploaded")
    parser.add_argument("--output", default="results/agt-govern-results.json")
    args = parser.parse_args()

    payload = asyncio.run(evaluate_packets())
    output = PACK_ROOT / args.output if not Path(args.output).is_absolute() else Path(args.output)
    write_json(output, payload)
    summary = payload.get("summary") or {"packets_total": 0, "passed": 0, "failed": 0}
    print(json.dumps({"status": payload.get("status"), **summary, "output": str(output.relative_to(PACK_ROOT))}, indent=2))
    return 0 if payload.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
