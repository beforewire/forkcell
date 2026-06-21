#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PACK_ROOT = Path(__file__).resolve().parents[1]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def run(name: str, cmd: list[str]) -> dict[str, Any]:
    proc = subprocess.run(cmd, cwd=PACK_ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    log = PACK_ROOT / "logs" / f"acceptance-{name}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(proc.stdout, encoding="utf-8")
    return {"name": name, "command": cmd, "exit_code": proc.returncode, "output": proc.stdout.strip(), "log": str(log.relative_to(PACK_ROOT))}


def read_status(path: str) -> str | None:
    p = PACK_ROOT / path
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8")).get("status")


def readiness_receipt_passes() -> bool:
    p = PACK_ROOT / "receipts" / "readiness-receipt.json"
    if not p.exists():
        return False
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    return (payload.get("summary") or {}).get("status") == "pass"


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run full BeforeWire readiness acceptance")
    parser.add_argument("--repo", default="../..")
    parser.add_argument("--github-repo", default="beforewire/forkcell")
    parser.add_argument("--skip-openshell-live", action="store_true")
    parser.add_argument(
        "--allow-external-unavailable",
        action="store_true",
        help="Pass local PLG acceptance when all local controls pass but branch protection is unavailable externally.",
    )
    parser.add_argument(
        "--configure-branch-protection",
        action="store_true",
        help="Try to create/update a GitHub ruleset requiring the BeforeWire Agent Gate check.",
    )
    parser.add_argument("--output", default="results/acceptance-results.json")
    args = parser.parse_args()
    py = sys.executable
    steps = [
        ("readiness_pack_initial", [py, "bin/run_readiness_pack.py", "--repo", args.repo]),
        ("approval_flow", [py, "bin/simulate_approval_flow.py"]),
        ("broker_dryrun", [py, "bin/run_broker_dryrun.py"]),
        ("live_action_packets", [py, "bin/run_live_action_packets.py", "--repo", args.repo]),
        ("replay_fixture", [py, "bin/run_replay_fixture.py"]),
        ("tamper_negative", [py, "bin/run_tamper_negative.py"]),
    ]
    if not args.skip_openshell_live:
        steps.append(("openshell_live_smoke", [py, "bin/run_openshell_live_smoke.py"]))
    steps.extend(
        [
            (
                "branch_protection_gate",
                [py, "bin/verify_branch_protection_gate.py", "--repo", args.github_repo]
                + (["--configure"] if args.configure_branch_protection else []),
            ),
            ("readiness_pack_final", [py, "bin/run_readiness_pack.py", "--repo", args.repo]),
            ("receipt_verify", [py, "bin/verify_readiness_receipt.py", "receipts/readiness-receipt.json"]),
        ]
    )
    results = [run(name, cmd) for name, cmd in steps]
    status_map = {
        "agt_govern": read_status("results/agt-govern-results.json"),
        "openshell_policy_validation": read_status("results/openshell-policy-validation.json"),
        "github_shadow_gate": read_status("results/github-shadow-gate-verification.json"),
        "approval_flow": read_status("results/approval-flow-results.json"),
        "broker_dryrun": read_status("results/broker-dryrun-results.json"),
        "live_action_packets": read_status("results/live-action-packet-results.json"),
        "replay_fixture": read_status("results/replay-fixture-results.json"),
        "tamper_negative": read_status("results/tamper-negative-results.json"),
        "openshell_live_smoke": read_status("results/openshell-live-smoke.json"),
        "branch_protection_gate": read_status("results/branch-protection-gate.json"),
    }
    hard_failures = [r for r in results if r["exit_code"] != 0]
    branch_unavailable = status_map.get("branch_protection_gate") == "unavailable"
    checks = {
        "commands_succeeded": not hard_failures,
        "readiness_receipt_pass": readiness_receipt_passes(),
        "all_local_controls_pass": all(
            status_map.get(k) == "pass"
            for k in [
                "agt_govern",
                "openshell_policy_validation",
                "github_shadow_gate",
                "approval_flow",
                "broker_dryrun",
                "live_action_packets",
                "replay_fixture",
                "tamper_negative",
            ]
        )
        and (args.skip_openshell_live or status_map.get("openshell_live_smoke") == "pass"),
        "branch_protection_available_or_present": status_map.get("branch_protection_gate") == "pass",
    }
    strict_pass = checks["commands_succeeded"] and checks["readiness_receipt_pass"] and checks["all_local_controls_pass"] and checks["branch_protection_available_or_present"]
    local_plg_pass = (
        args.allow_external_unavailable
        and checks["commands_succeeded"]
        and checks["readiness_receipt_pass"]
        and checks["all_local_controls_pass"]
        and branch_unavailable
    )
    payload = {
        "schema": "beforewire.acceptance-results.v1",
        "generated_at": utc_now(),
        "profile": "local-plg" if args.allow_external_unavailable else "strict-merge-blocking",
        "status": "pass" if strict_pass or local_plg_pass else "fail",
        "strict_status": "pass" if strict_pass else "fail",
        "status_map": status_map,
        "checks": checks,
        "external_blockers": ["github_branch_protection_unavailable_for_private_repo_plan"] if branch_unavailable else [],
        "notes": [
            "Local PLG acceptance treats external GitHub plan limitations as warnings; strict merge-blocking acceptance still requires branch protection/rulesets."
        ]
        if local_plg_pass
        else [],
        "steps": results,
    }
    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = PACK_ROOT / output_path
    write_json(output_path, payload)
    print(json.dumps({"status": payload["status"], "strict_status": payload["strict_status"], "profile": payload["profile"], "checks": checks, "external_blockers": payload["external_blockers"], "output": str(output_path.relative_to(PACK_ROOT))}, indent=2))
    return 0 if payload["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
