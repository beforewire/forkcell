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


def load_packet() -> dict[str, Any]:
    path = PACK_ROOT / "packets" / "BW-P012-broker-dryrun-side-effect.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["_path"] = str(path.relative_to(PACK_ROOT))
    return data


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run broker side-effect commit activity in dry-run mode")
    parser.add_argument("--output", default="results/broker-dryrun-results.json")
    args = parser.parse_args()
    packet = load_packet()
    intent = packet["action_intent"]
    targets = intent["targets"]
    activities = []
    for target in targets:
        activity = {
            "target": target,
            "dry_run": True,
            "external_call_performed": False,
            "idempotency_key": f"{intent['idempotency_key']}:{target}",
            "would_commit": {
                "github.pr_comment": "POST /repos/{owner}/{repo}/issues/{pull_number}/comments",
                "github.issue_create": "POST /repos/{owner}/{repo}/issues",
                "deploy.trigger": "POST /deployments",
                "slack.message": "POST /chat.postMessage",
            }.get(target, "unknown"),
        }
        activity["activity_hash"] = sha256_text(json.dumps(activity, sort_keys=True, separators=(",", ":")))
        activities.append(activity)
    receipt = {
        "schema": "beforewire.broker-receipt.v1",
        "generated_at": utc_now(),
        "packet_id": packet["id"],
        "packet_path": packet["_path"],
        "broker": "local-dry-run",
        "dry_run": True,
        "idempotency_key": intent["idempotency_key"],
        "targets": targets,
        "activities": activities,
        "side_effect_performed": False,
    }
    receipt["receipt_hash"] = sha256_text(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    checks = {
        "all_targets_covered": sorted(a["target"] for a in activities) == sorted(targets),
        "no_external_call_performed": all(a["external_call_performed"] is False for a in activities),
        "receipt_hash_present": bool(receipt["receipt_hash"]),
        "idempotency_key_present": bool(receipt["idempotency_key"]),
    }
    payload = {
        "schema": "beforewire.broker-dryrun-results.v1",
        "generated_at": utc_now(),
        "status": "pass" if all(checks.values()) else "fail",
        "checks": checks,
        "receipt": receipt,
    }
    write_json(PACK_ROOT / args.output, payload)
    print(json.dumps({"status": payload["status"], "checks": checks, "output": args.output}, indent=2))
    return 0 if payload["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
