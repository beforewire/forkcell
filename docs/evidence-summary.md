# ForkCell Preview Evidence Summary

This is a sanitized summary of validation for `0.1.0a2`.
Raw machine-local evidence logs are intentionally not tracked in the public branch.

## Runtime Integration

- OpenShell runtime fork: `beforewire/openshell`.
- Runtime branch: `forkcell-workspace-substrate`.
- Runtime commit: `393c25a86d9128ff5e38ecf537809efe58470266`.
- ForkCell commit used for this evidence: `25d7be1` plus the `0.1.0a2` benchmark/security patch.
- Host used for local benchmark: macOS arm64 with Docker Desktop.

## Validation Highlights

- Native overlay synchronous checkpoint and restore remain metadata-only: `checkpoint=0ms`, `restore_sync_ms=0ms`, `restore=0ms` in representative runs.
- Native overlay full restore path after async-style log collection is now `445-550ms` across representative workspaces.
- `local-overlay` restores representative workspaces in `233-243ms`.
- `volume-delta` restores representative workspaces in `271-418ms`, with copied/reused/removed counts recorded in receipts.
- Restore correctness passed for every run in the matrix below.
- Secret safety smoke passed: common local secret files are excluded from source import (`.env`, `.env.*`, `.ssh`, `.aws`, `*.pem`, `*.key`) and the synthetic secret did not appear in ForkCell receipt/metadata artifacts.
- Policy/checkpoint binding smoke passed: native checkpoint identity changes with policy revision, and receipt binding detects mismatched checkpoint hashes.

## Representative Benchmark Matrix

Each scenario mutates the workspace and exits with status `7` to trigger `--restore-on-fail`.

| backend | scenario | files | MiB | mutation | init ms | run wall ms | checkpoint ms | restore ms | log collect ms | full restore path ms | correctness |
|---|---|---:|---:|---|---:|---:|---:|---:|---:|---:|---|
| `native-overlay` | agent_patch_small_repo | 500 | 1.0 | M10/A5/D3 | 472 | 625 | 0 | 0 | 10 | 550 | 1/1 |
| `native-overlay` | webapp_medium_repo | 2408 | 13.4 | M50/A20/D10 | 748 | 520 | 0 | 0 | 9 | 445 | 1/1 |
| `native-overlay` | dependency_cache_workspace | 6024 | 32.8 | M100/A50/D25 | 1292 | 567 | 0 | 0 | 10 | 490 | 1/1 |
| `local-overlay` | agent_patch_small_repo | 500 | 1.0 | M10/A5/D3 | 412 | 1012 | 240 | 233 | n/a | n/a | 1/1 |
| `local-overlay` | webapp_medium_repo | 2408 | 13.4 | M50/A20/D10 | 668 | 1081 | 229 | 242 | n/a | n/a | 1/1 |
| `local-overlay` | dependency_cache_workspace | 6024 | 32.8 | M100/A50/D25 | 1154 | 1071 | 221 | 243 | n/a | n/a | 1/1 |
| `volume-delta` | agent_patch_small_repo | 500 | 1.0 | M10/A5/D3 | 398 | 1606 | 280 | 271 | sync | n/a | 1/1 |
| `volume-delta` | webapp_medium_repo | 2408 | 13.4 | M50/A20/D10 | 686 | 1840 | 427 | 341 | sync | n/a | 1/1 |
| `volume-delta` | dependency_cache_workspace | 6024 | 32.8 | M100/A50/D25 | 1235 | 2131 | 679 | 418 | sync | n/a | 1/1 |

Raw local paths from this run were under `/tmp/forkcell-typical-*` and are not tracked.

## Async Log Collection Boundary

`native-overlay` now defaults to best-effort non-blocking log collection. This removes the previous fixed wait for OCSF/log events from the critical restore path. Use `--sync-logs` when policy tests need to wait briefly for runtime events before the receipt is finalized.

## Boundary Note

`restore_sync_ms=0ms` means ForkCell's synchronous workspace restore substrate is sub-ms/rounded to zero. A full agent run still includes runtime sandbox lifecycle, command execution, optional log collection, and receipt generation. ForkCell should not be described as a `<60ms` fully serviceable MicroVM sandbox; that is a different CubeSandbox-style substrate and pooling target.
