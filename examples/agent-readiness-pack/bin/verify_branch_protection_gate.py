#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PACK_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PACK_ROOT.parents[1]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def run(cmd: list[str]) -> dict[str, Any]:
    proc = subprocess.run(cmd, cwd=REPO_ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    return {"command": cmd, "exit_code": proc.returncode, "output": proc.stdout.strip()}


def run_with_json(cmd: list[str], payload: dict[str, Any]) -> dict[str, Any]:
    proc = subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        input=json.dumps(payload),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    return {
        "command": cmd,
        "exit_code": proc.returncode,
        "input": payload,
        "output": proc.stdout.strip(),
    }


def parse_json_output(result: dict[str, Any]) -> Any | None:
    if result.get("exit_code") != 0:
        return None
    try:
        return json.loads(result.get("output") or "")
    except json.JSONDecodeError:
        return None


def ruleset_body(name: str, required_check: str) -> dict[str, Any]:
    return {
        "name": name,
        "target": "branch",
        "enforcement": "active",
        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
        "rules": [
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": True,
                    "required_status_checks": [{"context": required_check}],
                },
            }
        ],
        "bypass_actors": [],
    }


def upsert_required_check_ruleset(repo: str, rulesets: dict[str, Any], ruleset_name: str, required_check: str) -> dict[str, Any]:
    parsed = parse_json_output(rulesets)
    if not isinstance(parsed, list):
        return {
            "status": "skipped",
            "reason": "rulesets_api_unavailable_or_unparseable",
        }
    existing = next((row for row in parsed if isinstance(row, dict) and row.get("name") == ruleset_name), None)
    body = ruleset_body(ruleset_name, required_check)
    if existing and existing.get("id"):
        return run_with_json(["gh", "api", "-X", "PUT", f"repos/{repo}/rulesets/{existing['id']}", "--input", "-"], body)
    return run_with_json(["gh", "api", "-X", "POST", f"repos/{repo}/rulesets", "--input", "-"], body)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify whether GitHub can enforce the BeforeWire required check")
    parser.add_argument("--repo", default="beforewire/forkcell")
    parser.add_argument("--required-check", default="BeforeWire Agent Gate")
    parser.add_argument("--ruleset-name", default="beforewire-agent-gate-required")
    parser.add_argument("--configure", action="store_true", help="Create or update a GitHub ruleset requiring the check when the API is available.")
    parser.add_argument("--output", default="results/branch-protection-gate.json")
    args = parser.parse_args()
    if not shutil.which("gh"):
        payload = {"schema": "beforewire.branch-protection-gate.v1", "generated_at": utc_now(), "status": "fail", "reason": "gh CLI missing"}
        write_json(PACK_ROOT / args.output, payload)
        print(json.dumps({"status": payload["status"], "reason": payload["reason"]}, indent=2))
        return 1
    repo_view = run(["gh", "repo", "view", args.repo, "--json", "defaultBranchRef,isPrivate,viewerPermission"])
    default_branch = "main"
    if repo_view["exit_code"] == 0:
        data = json.loads(repo_view["output"])
        default_branch = data["defaultBranchRef"]["name"]
    protection = run(["gh", "api", f"repos/{args.repo}/branches/{default_branch}/protection"])
    rulesets = run(["gh", "api", f"repos/{args.repo}/rulesets"])
    unavailable = "Upgrade to GitHub Pro" in protection["output"] or "Upgrade to GitHub Pro" in rulesets["output"]
    required_present = args.required_check in protection["output"] or args.required_check in rulesets["output"]
    configure_attempt: dict[str, Any] | None = None
    if args.configure and not required_present:
        if unavailable:
            configure_attempt = {
                "status": "skipped",
                "reason": "github_branch_protection_rulesets_unavailable_for_private_repo_plan",
            }
        else:
            configure_attempt = upsert_required_check_ruleset(args.repo, rulesets, args.ruleset_name, args.required_check)
            # Re-read authoritative state after the mutating API call.
            protection = run(["gh", "api", f"repos/{args.repo}/branches/{default_branch}/protection"])
            rulesets = run(["gh", "api", f"repos/{args.repo}/rulesets"])
            unavailable = "Upgrade to GitHub Pro" in protection["output"] or "Upgrade to GitHub Pro" in rulesets["output"]
            required_present = args.required_check in protection["output"] or args.required_check in rulesets["output"]
    payload = {
        "schema": "beforewire.branch-protection-gate.v1",
        "generated_at": utc_now(),
        "repo": args.repo,
        "default_branch": default_branch,
        "required_check": args.required_check,
        "ruleset_name": args.ruleset_name,
        "configure_requested": args.configure,
        "configure_attempt": configure_attempt,
        "repo_view": repo_view,
        "branch_protection": protection,
        "rulesets": rulesets,
        "checks": {
            "gh_authenticated": repo_view["exit_code"] == 0,
            "branch_protection_api_available": protection["exit_code"] == 0 or not unavailable,
            "required_check_present": required_present,
        },
        "status": "pass" if required_present else ("unavailable" if unavailable else "fail"),
        "reason": "GitHub branch protection/rulesets are unavailable for this private repo plan" if unavailable and not required_present else "",
    }
    write_json(PACK_ROOT / args.output, payload)
    print(json.dumps({"status": payload["status"], "checks": payload["checks"], "reason": payload["reason"], "output": args.output}, indent=2))
    return 0 if payload["status"] in {"pass", "unavailable"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
