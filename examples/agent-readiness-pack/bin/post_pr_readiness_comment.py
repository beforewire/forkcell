#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

PACK_ROOT = Path(__file__).resolve().parents[1]
MARKER_PREFIX = "beforewire:readiness-summary:v1"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def rel(path: Path) -> str:
    return str(path.relative_to(PACK_ROOT))


def ci_context() -> dict[str, Any]:
    return {
        "github_actions": os.environ.get("GITHUB_ACTIONS") == "true",
        "github_run_id": os.environ.get("GITHUB_RUN_ID"),
        "github_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "github_event_name": os.environ.get("GITHUB_EVENT_NAME"),
        "github_ref": os.environ.get("GITHUB_REF"),
        "github_sha": os.environ.get("GITHUB_SHA"),
        "github_repository": os.environ.get("GITHUB_REPOSITORY"),
    }


def make_idempotency_key(repo: str, pr_number: str) -> str:
    return hashlib.sha256(f"{MARKER_PREFIX}|{repo}|{pr_number}".encode("utf-8")).hexdigest()[:24]


def make_run_url(repo: str) -> str | None:
    run_id = os.environ.get("GITHUB_RUN_ID")
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    if not run_id:
        return None
    return f"{server}/{repo}/actions/runs/{run_id}"


def render_comment(receipt: dict[str, Any], repo: str, pr_number: str, idempotency_key: str) -> str:
    summary = receipt.get("summary") or {}
    controls = receipt.get("control_results") or {}
    ci = receipt.get("ci_context") or {}
    run_url = make_run_url(repo)
    control_lines = []
    for name in sorted(controls):
        status = (controls.get(name) or {}).get("status")
        control_lines.append(f"- `{name}`: `{status}`")
    control_block = "\n".join(control_lines) if control_lines else "- No control results recorded"
    artifact_line = f"- Run: {run_url}" if run_url else "- Run: local or unavailable"
    return f"""<!-- {MARKER_PREFIX}:{idempotency_key} -->
### BeforeWire Agent Readiness

Status: `{summary.get("status")}`

- Packets: `{summary.get("passed")}/{summary.get("packets_total")}` passed, `{summary.get("failed")}` failed
- Receipt hash: `{receipt.get("receipt_hash")}`
- Repository: `{repo}`
- PR: `#{pr_number}`
- CI event: `{ci.get("github_event_name")}`
- CI SHA: `{ci.get("github_sha")}`
{artifact_line}

Controls:
{control_block}

Broker proof:
- Approval receipt: `receipts/pr-comment-approval-receipt.json`
- Idempotency key: `{idempotency_key}`
- Broker target: `github.issue_comment`
"""


def build_approval_receipt(
    receipt: dict[str, Any],
    repo: str,
    pr_number: str,
    body: str,
    idempotency_key: str,
) -> dict[str, Any]:
    approved_action = {
        "type": "github.issue_comment.upsert",
        "repo": repo,
        "pr_number": pr_number,
        "target": "github.issue_comment",
        "receipt_hash": receipt.get("receipt_hash"),
        "body_hash": sha256_text(body),
        "idempotency_key": idempotency_key,
    }
    action_digest = sha256_text(json.dumps(approved_action, sort_keys=True, separators=(",", ":")))
    payload = {
        "schema": "beforewire.broker-approval-receipt.v1",
        "generated_at": utc_now(),
        "approval_type": "policy_approval",
        "approver": "github-actions:beforewire-agent-gate",
        "policy": {
            "name": "low-risk-pr-readiness-summary",
            "constraints": [
                "target must be github.issue_comment",
                "comment body must be derived from the verified readiness receipt",
                "comment must include the stable BeforeWire idempotency marker",
                "no side effect other than creating or updating one PR comment is allowed",
            ],
        },
        "approved_action": approved_action,
        "approved_action_digest": action_digest,
        "ci_context": ci_context(),
    }
    payload["approval_receipt_hash"] = sha256_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return payload


def validate_approval(approval: dict[str, Any], receipt: dict[str, Any], repo: str, pr_number: str, body: str, idempotency_key: str) -> list[str]:
    errors: list[str] = []
    action = approval.get("approved_action") or {}
    expected = {
        "type": "github.issue_comment.upsert",
        "repo": repo,
        "pr_number": pr_number,
        "target": "github.issue_comment",
        "receipt_hash": receipt.get("receipt_hash"),
        "body_hash": sha256_text(body),
        "idempotency_key": idempotency_key,
    }
    for key, value in expected.items():
        if action.get(key) != value:
            errors.append(f"approved_action.{key} mismatch")
    if approval.get("schema") != "beforewire.broker-approval-receipt.v1":
        errors.append("approval schema mismatch")
    if approval.get("approval_receipt_hash") != sha256_text(json.dumps({k: v for k, v in approval.items() if k != "approval_receipt_hash"}, sort_keys=True, separators=(",", ":"))):
        errors.append("approval receipt hash mismatch")
    return errors


def github_request(method: str, url: str, token: str, payload: dict[str, Any] | None = None) -> Any:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "beforewire-agent-readiness-broker",
    }
    if payload is not None:
        headers["Content-Type"] = "application/json"
    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=30) as resp:
            text = resp.read().decode("utf-8")
            return json.loads(text) if text else {}
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API {method} {url} failed: HTTP {exc.code}: {detail}") from exc


def upsert_comment(repo: str, pr_number: str, token: str, body: str, idempotency_key: str) -> dict[str, Any]:
    api = "https://api.github.com"
    marker = f"<!-- {MARKER_PREFIX}:{idempotency_key} -->"
    comments = github_request("GET", f"{api}/repos/{repo}/issues/{pr_number}/comments?per_page=100", token)
    existing = None
    for comment in comments:
        if isinstance(comment, dict) and marker in str(comment.get("body") or ""):
            existing = comment
            break
    if existing:
        updated = github_request("PATCH", f"{api}/repos/{repo}/issues/comments/{existing['id']}", token, {"body": body})
        return {"operation": "updated", "comment_id": updated.get("id"), "comment_url": updated.get("html_url")}
    created = github_request("POST", f"{api}/repos/{repo}/issues/{pr_number}/comments", token, {"body": body})
    return {"operation": "created", "comment_id": created.get("id"), "comment_url": created.get("html_url")}


def main() -> int:
    parser = argparse.ArgumentParser(description="Post a PR readiness summary through a receipt-backed side-effect broker")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "beforewire/forkcell"))
    parser.add_argument("--pr-number", default=os.environ.get("GITHUB_PR_NUMBER") or "")
    parser.add_argument("--receipt", default="receipts/readiness-receipt.json")
    parser.add_argument("--approval-output", default="receipts/pr-comment-approval-receipt.json")
    parser.add_argument("--output", default="results/pr-comment-broker-results.json")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--allow-permission-skip",
        action="store_true",
        help="Return pass with a skipped operation when the GitHub token cannot write issue comments.",
    )
    args = parser.parse_args()

    receipt_path = PACK_ROOT / args.receipt
    receipt = read_json(receipt_path)
    pr_number = str(args.pr_number)
    if not pr_number:
        pr_number = str((receipt.get("ci_context") or {}).get("github_pr_number") or "")
    if not pr_number:
        raise SystemExit("missing PR number")
    if (receipt.get("summary") or {}).get("status") != "pass":
        raise SystemExit("readiness receipt must pass before broker commit")

    idempotency_key = make_idempotency_key(args.repo, pr_number)
    body = render_comment(receipt, args.repo, pr_number, idempotency_key)
    approval = build_approval_receipt(receipt, args.repo, pr_number, body, idempotency_key)
    approval_path = PACK_ROOT / args.approval_output
    write_json(approval_path, approval)
    approval_errors = validate_approval(approval, receipt, args.repo, pr_number, body, idempotency_key)

    broker_result: dict[str, Any] = {
        "schema": "beforewire.pr-comment-broker-results.v1",
        "generated_at": utc_now(),
        "repo": args.repo,
        "pr_number": pr_number,
        "target": "github.issue_comment",
        "idempotency_key": idempotency_key,
        "receipt_hash": receipt.get("receipt_hash"),
        "body_hash": sha256_text(body),
        "approval_receipt_ref": {
            "path": rel(approval_path),
            "hash": approval.get("approval_receipt_hash"),
        },
        "approval_valid": not approval_errors,
        "approval_errors": approval_errors,
        "external_call_performed": False,
        "operation": "dry_run" if args.dry_run else "pending",
        "ci_context": ci_context(),
    }

    if approval_errors:
        broker_result["status"] = "fail"
    elif args.dry_run:
        broker_result["status"] = "pass"
        broker_result["comment_preview"] = body
    else:
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            broker_result.update({"status": "fail", "operation": "skipped", "reason": "GITHUB_TOKEN missing"})
        else:
            try:
                commit = upsert_comment(args.repo, pr_number, token, body, idempotency_key)
                broker_result.update(commit)
                broker_result["external_call_performed"] = True
                broker_result["status"] = "pass"
            except RuntimeError as exc:
                if args.allow_permission_skip and "Resource not accessible by integration" in str(exc):
                    broker_result.update(
                        {
                            "status": "pass",
                            "operation": "skipped_permission_denied",
                            "reason": "workflow token cannot write issue comments",
                            "external_call_performed": False,
                        }
                    )
                else:
                    broker_result.update({"status": "fail", "operation": "failed", "reason": str(exc)})

    output_path = PACK_ROOT / args.output
    write_json(output_path, broker_result)
    print(json.dumps({k: broker_result.get(k) for k in ["status", "operation", "external_call_performed", "idempotency_key", "comment_url"]}, indent=2))
    return 0 if broker_result.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
