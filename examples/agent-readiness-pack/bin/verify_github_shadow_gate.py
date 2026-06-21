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
REPO_ROOT = PACK_ROOT.parents[1]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def file_hash(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    return sha256_bytes(path.read_bytes())


def get_on_block(workflow: dict[str, Any]) -> Any:
    # PyYAML still applies YAML 1.1 booleans, so "on" may parse as True.
    return workflow.get("on", workflow.get(True))


def verify(path: Path) -> dict[str, Any]:
    checks: dict[str, bool] = {}
    reasons: list[str] = []
    exists = path.exists()
    checks["file_exists"] = exists
    if not exists:
        return {"checks": checks, "reasons": ["workflow file is missing"], "status": "fail"}

    text = path.read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)
    checks["yaml_parses"] = isinstance(workflow, dict)
    if not isinstance(workflow, dict):
        return {"checks": checks, "reasons": ["workflow YAML did not parse to a map"], "status": "fail"}

    on_block = get_on_block(workflow)
    jobs = workflow.get("jobs") or {}
    permissions = workflow.get("permissions") or {}
    all_runs = "\n".join(
        str(step.get("run", ""))
        for job in jobs.values()
        if isinstance(job, dict)
        for step in (job.get("steps") or [])
        if isinstance(step, dict)
    )
    checks["is_pr_specific_gate_named"] = "beforewire-agent-gate" in str(workflow.get("name", "")).lower()
    checks["has_pull_request_or_manual_trigger"] = isinstance(on_block, dict) and (
        "pull_request" in on_block or "workflow_dispatch" in on_block
    )
    checks["least_privilege_permissions"] = permissions == {"contents": "read", "issues": "write", "pull-requests": "read"}
    checks["bootstraps_readiness_pack"] = "bootstrap_readiness_pack.py" in all_runs
    checks["generates_pr_specific_receipt"] = "bin/run_readiness_pack.py --repo ../.." in all_runs
    checks["verifies_readiness_receipt"] = "verify_readiness_receipt.py" in all_runs
    checks["posts_receipt_pr_comment"] = "post_pr_readiness_comment.py" in all_runs and "GITHUB_TOKEN" in text
    checks["comment_limited_to_same_repo_prs"] = "github.event.pull_request.head.repo.full_name == github.repository" in text
    checks["uploads_readiness_artifact"] = "actions/upload-artifact@v4" in text
    checks["does_not_configure_branch_protection"] = "branch_protection" not in text and "required_status_checks" not in text
    checks["job_is_blocking_when_required"] = True
    for job in jobs.values():
        if not isinstance(job, dict):
            continue
        if job.get("continue-on-error") is True:
            checks["job_is_blocking_when_required"] = False
        for step in job.get("steps") or []:
            if isinstance(step, dict) and step.get("continue-on-error") is True:
                checks["job_is_blocking_when_required"] = False
    for key, ok in sorted(checks.items()):
        if not ok:
            reasons.append(key)
    return {"checks": checks, "reasons": reasons, "status": "pass" if all(checks.values()) else "fail"}


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the installed GitHub Actions PR-specific readiness gate")
    parser.add_argument("--workflow", default=".github/workflows/beforewire-agent-gate.yml")
    parser.add_argument("--output", default="results/github-shadow-gate-verification.json")
    args = parser.parse_args()

    workflow_path = REPO_ROOT / args.workflow
    result = verify(workflow_path)
    payload = {
        "schema": "beforewire.github-shadow-gate-verification.v1",
        "generated_at": utc_now(),
        "mode": "pr-specific-blockable",
        "workflow": {"path": args.workflow, "sha256": file_hash(workflow_path)},
        **result,
    }
    output = PACK_ROOT / args.output
    write_json(output, payload)
    print(json.dumps({"status": payload["status"], "checks": payload["checks"], "output": args.output}, indent=2))
    return 0 if payload["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
