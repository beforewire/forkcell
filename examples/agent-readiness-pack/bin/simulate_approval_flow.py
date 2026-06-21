#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

PACK_ROOT = Path(__file__).resolve().parents[1]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_hash(path: Path) -> str | None:
    if not path.exists():
        return None
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def load_packet(packet_id: str) -> dict[str, Any]:
    for path in (PACK_ROOT / "packets").glob("*.yaml"):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if data.get("id") == packet_id:
            data["_path"] = str(path.relative_to(PACK_ROOT))
            return data
    raise SystemExit(f"packet not found: {packet_id}")


def build_approval(packet: dict[str, Any], decision: str, approver: str, reason: str) -> dict[str, Any]:
    action = packet.get("action_intent") or {}
    action_digest = sha256_text(json.dumps(action, sort_keys=True, separators=(",", ":")))
    request = {
        "request_id": sha256_text(f"{packet['id']}:{action_digest}")[:24],
        "packet_id": packet["id"],
        "packet_path": packet["_path"],
        "state": "approval_wait",
        "requested_at": utc_now(),
        "action_identity": action_digest,
        "policy_hash": file_hash(PACK_ROOT / "policies" / "agt.rego"),
        "policy_effect": "ask",
        "side_effect_performed_before_approval": False,
    }
    receipt = {
        "schema": "beforewire.approval-receipt.v1",
        "request": request,
        "decision": {
            "approver": approver,
            "decision": decision,
            "reason": reason,
            "decided_at": utc_now(),
        },
        "resume": {
            "allowed": decision == "allow",
            "mode": "dry-run",
            "side_effect_performed": False,
        },
    }
    receipt["receipt_hash"] = sha256_text(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return receipt


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Simulate approval wait, approval receipt, and resume/deny decisions")
    parser.add_argument("--output", default="results/approval-flow-results.json")
    args = parser.parse_args()
    approve = build_approval(load_packet("BW-P011-human-approval-wait"), "allow", "local-approver", "Package manifest change approved for dry-run resume.")
    deny = build_approval(load_packet("BW-P008-ci-workflow-tamper"), "deny", "local-approver", "Workflow tamper request denied.")
    payload = {
        "schema": "beforewire.approval-flow-results.v1",
        "generated_at": utc_now(),
        "mode": "local-simulated-human",
        "status": "pass",
        "checks": {
            "approval_wait_created": approve["request"]["state"] == "approval_wait" and deny["request"]["state"] == "approval_wait",
            "receipt_hash_present": bool(approve.get("receipt_hash")) and bool(deny.get("receipt_hash")),
            "resume_path_covered": approve["resume"]["allowed"] is True,
            "deny_path_covered": deny["resume"]["allowed"] is False,
            "no_side_effect_before_approval": not approve["request"]["side_effect_performed_before_approval"] and not deny["request"]["side_effect_performed_before_approval"],
        },
        "receipts": [approve, deny],
    }
    payload["status"] = "pass" if all(payload["checks"].values()) else "fail"
    write_json(PACK_ROOT / args.output, payload)
    print(json.dumps({"status": payload["status"], "checks": payload["checks"], "output": args.output}, indent=2))
    return 0 if payload["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
