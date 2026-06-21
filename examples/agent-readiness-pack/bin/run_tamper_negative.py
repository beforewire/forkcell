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


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Tamper with a readiness receipt and assert the verifier fails")
    parser.add_argument("--receipt", default="receipts/readiness-receipt.json")
    parser.add_argument("--output", default="results/tamper-negative-results.json")
    args = parser.parse_args()
    original = PACK_ROOT / args.receipt
    if not original.exists():
        subprocess.run([sys.executable, str(PACK_ROOT / "bin" / "run_readiness_pack.py"), "--repo", "../.."], cwd=PACK_ROOT, check=True)
    payload = json.loads(original.read_text(encoding="utf-8"))
    payload["summary"]["status"] = "pass-tampered"
    tampered = PACK_ROOT / "fixtures" / "tamper" / "readiness-receipt.tampered.json"
    write_json(tampered, payload)
    proc = subprocess.run(
        [sys.executable, str(PACK_ROOT / "bin" / "verify_readiness_receipt.py"), str(tampered)],
        cwd=PACK_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    result = {
        "schema": "beforewire.tamper-negative-results.v1",
        "generated_at": utc_now(),
        "tampered_receipt": str(tampered.relative_to(PACK_ROOT)),
        "verifier_exit_code": proc.returncode,
        "verifier_output": proc.stdout.strip(),
        "checks": {
            "tampered_file_written": tampered.exists(),
            "verifier_failed": proc.returncode != 0,
            "hash_mismatch_detected": "mismatch" in proc.stdout.lower(),
        },
    }
    result["status"] = "pass" if all(result["checks"].values()) else "fail"
    write_json(PACK_ROOT / args.output, result)
    print(json.dumps({"status": result["status"], "checks": result["checks"], "output": args.output}, indent=2))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
