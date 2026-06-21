#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a BeforeWire readiness receipt")
    parser.add_argument("receipt")
    args = parser.parse_args()
    path = Path(args.receipt)
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    claimed = payload.get("receipt_hash")
    if not claimed:
        print("FAIL: missing receipt_hash")
        return 1
    unsigned = dict(payload)
    unsigned.pop("receipt_hash", None)
    expected = sha256_text(json.dumps(unsigned, sort_keys=True, separators=(",", ":")))
    if expected != claimed:
        print("FAIL: receipt_hash mismatch")
        print(f"expected={expected}")
        print(f"actual={claimed}")
        return 1
    if payload.get("summary", {}).get("status") != "pass":
        print("FAIL: readiness status is not pass")
        return 1
    if payload.get("summary", {}).get("passed", 0) < 5:
        print("FAIL: fewer than 5 packets passed")
        return 1
    print("PASS: readiness receipt verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
