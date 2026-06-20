#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SECRET = "sk-forkcell-secret-should-not-appear-123456"


def run(cmd: list[str], *, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(cmd, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check)


def latest_receipt(cwd: Path) -> Path:
    receipts = sorted((cwd / ".forkcell" / "receipts").glob("rcpt_*.json"), key=lambda p: p.stat().st_mtime)
    if not receipts:
        raise AssertionError("expected a receipt")
    return receipts[-1]


def validate_secret_exclusion_and_restore_binding() -> None:
    with tempfile.TemporaryDirectory(prefix="forkcell-security-") as tmp:
        root = Path(tmp)
        workspace = root / "workspace"
        workspace.mkdir()
        (workspace / "app.py").write_text("print('hello')\n")
        (workspace / ".env").write_text(f"API_KEY={SECRET}\n")
        (workspace / ".env.local").write_text(f"TOKEN={SECRET}\n")
        (workspace / ".ssh").mkdir()
        (workspace / ".ssh" / "id_rsa").write_text(SECRET)

        cmd = [sys.executable, "-m", "forkcell.cli"]
        run([*cmd, "overlay", "init", "sec", "--from", str(workspace)], cwd=root)
        run(
            [
                *cmd,
                "overlay",
                "run",
                "--checkpoint-before",
                "--restore-on-fail",
                "sec",
                "--",
                "sh",
                "-lc",
                "test -f app.py && test ! -e .env && test ! -e .env.local && test ! -e .ssh/id_rsa; mkdir -p agent-output; echo changed > agent-output/out.txt; exit 7",
            ],
            cwd=root,
        )
        try:
            receipt_path = latest_receipt(root)
            receipt = json.loads(receipt_path.read_text())
            assert receipt["decision"]["result"] == "restored"
            assert receipt["checkpoints"]["before"] == receipt["checkpoints"]["restored_to"]
            assert receipt["bindings"]["receipt_binds_policy_and_checkpoint"] is True
            assert not (workspace / "agent-output").exists(), "restore should remove failing-run output"

            public_state = "\n".join(
                p.read_text(errors="ignore")
                for p in (root / ".forkcell").rglob("*")
                if p.is_file() and ("receipts" in p.parts or p.name.endswith("metadata.json"))
            )
            assert SECRET not in public_state, "secret leaked into ForkCell metadata or receipt artifacts"
        finally:
            run([*cmd, "overlay", "delete", "sec"], cwd=root, check=False)


def validate_policy_bound_checkpoint_identity() -> None:
    sys.path.insert(0, str(REPO))
    from forkcell.cli import checkpoint_receipt_bindings, native_checkpoint_sha256

    checkpoint = {
        "checkpoint_id": "chk_demo",
        "provider": "native-overlay",
        "layer": "layers/base",
        "parent": None,
        "forked_from": None,
    }
    sha_a = native_checkpoint_sha256(checkpoint, policy_revision="policy_a")
    sha_b = native_checkpoint_sha256(checkpoint, policy_revision="policy_b")
    assert sha_a != sha_b, "policy revision must be part of native checkpoint identity"

    receipt = {"checkpoints": {"before": "chk_demo"}, "bindings": {"checkpoint_sha256": sha_a}}
    ok = checkpoint_receipt_bindings(checkpoint_id="chk_demo", checkpoint_sha256=sha_a, receipt=receipt)
    assert ok["restore_checkpoint_matches_receipt"] is True
    assert ok["checkpoint_sha256_matches_receipt"] is True

    bad = checkpoint_receipt_bindings(checkpoint_id="chk_demo", checkpoint_sha256=sha_b, receipt=receipt)
    assert bad["checkpoint_sha256_matches_receipt"] is False


def main() -> int:
    validate_policy_bound_checkpoint_identity()
    validate_secret_exclusion_and_restore_binding()
    print("ForkCell security/binding validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
