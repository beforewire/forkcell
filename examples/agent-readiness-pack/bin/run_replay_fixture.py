#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PACK_ROOT = Path(__file__).resolve().parents[1]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build and replay action/approval evidence fixtures")
    parser.add_argument("--output", default="results/replay-fixture-results.json")
    args = parser.parse_args()
    packet_path = PACK_ROOT / "results" / "packet-results.json"
    approval_path = PACK_ROOT / "results" / "approval-flow-results.json"
    if not packet_path.exists():
        subprocess.run([sys.executable, str(PACK_ROOT / "bin" / "run_readiness_pack.py"), "--repo", "../.."], cwd=PACK_ROOT, check=True)
    if not approval_path.exists():
        subprocess.run([sys.executable, str(PACK_ROOT / "bin" / "simulate_approval_flow.py")], cwd=PACK_ROOT, check=True)
    packets = read_json(packet_path)["results"]
    approvals = read_json(approval_path)["receipts"]
    trace = {
        "schema": "beforewire.action-trace.v1",
        "generated_at": utc_now(),
        "events": [
            {
                "packet_id": row["packet_id"],
                "action_intent_digest": row["action_intent_digest"],
                "effect": row["actual"]["agt_effect"],
                "status": row["status"],
            }
            for row in packets
        ],
        "approval_receipts": approvals,
    }
    trace_path = PACK_ROOT / "fixtures" / "replay" / "action-trace.json"
    write_json(trace_path, trace)
    replayed = read_json(trace_path)
    checks = {
        "trace_written": trace_path.exists(),
        "packet_event_count_matches": len(replayed["events"]) == len(packets),
        "all_packet_digests_match": all(
            event["action_intent_digest"] == row["action_intent_digest"]
            for event, row in zip(replayed["events"], packets, strict=True)
        ),
        "approval_receipts_replay": all(r.get("receipt_hash") for r in replayed["approval_receipts"]),
    }
    result = {
        "schema": "beforewire.replay-fixture-results.v1",
        "generated_at": utc_now(),
        "trace": {"path": str(trace_path.relative_to(PACK_ROOT)), "sha256": sha256_text(json.dumps(trace, sort_keys=True, separators=(",", ":")))},
        "checks": checks,
        "status": "pass" if all(checks.values()) else "fail",
    }
    write_json(PACK_ROOT / args.output, result)
    print(json.dumps({"status": result["status"], "checks": checks, "output": args.output}, indent=2))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
