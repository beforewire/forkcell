# ForkCell Benchmark Matrix

ForkCell tracks performance by backend and by layer. The goal is to avoid claiming that Python, Docker, filesystem work, and OpenShell sandbox lifecycle are the same bottleneck.

## Run The Local Matrix

From a source checkout:

```bash
./scripts/benchmark_matrix.sh
```

Defaults:

- package under test: current source checkout;
- profiles: `tiny small`;
- backend: `local-overlay`;
- output: markdown table plus raw JSONL path.

Useful overrides:

```bash
FORKCELL_PACKAGE_SPEC=forkcell==0.1.0a2 ./scripts/benchmark_matrix.sh
FORKCELL_BENCH_PROFILES="tiny small medium" ./scripts/benchmark_matrix.sh
FORKCELL_BENCH_DIR=/tmp/forkcell-bench ./scripts/benchmark_matrix.sh
```

## Run Representative Agent Scenarios

The typical-scenario benchmark creates source-tree-like workspaces and runs a failing agent edit to trigger restore:

```bash
python3 scripts/benchmark_typical_scenarios.py --backend local-overlay --iterations 1
```

Runtime-backed paths require the patched gateway to be running:

```bash
./scripts/start_patched_openshell_gateway.sh
python3 scripts/benchmark_typical_scenarios.py --backend native-overlay --iterations 1
python3 scripts/benchmark_typical_scenarios.py --backend volume-delta --iterations 1
./scripts/stop_patched_openshell_gateway.sh
```

For policy/event tests that must wait briefly for OCSF/log events before writing the receipt:

```bash
python3 scripts/benchmark_typical_scenarios.py --backend native-overlay --sync-logs --iterations 1
```

Representative scenarios:

| scenario | files | MiB | mutation |
|---|---:|---:|---|
| `agent_patch_small_repo` | 500 | 1.0 | modify 10 / add 5 / delete 3 |
| `webapp_medium_repo` | 2408 | 13.4 | modify 50 / add 20 / delete 10 |
| `dependency_cache_workspace` | 6024 | 32.8 | modify 100 / add 50 / delete 25 |

## Breakdown Terms

For the PyPI/local-overlay path, the matrix reports:

- `init ms`: source import into the Docker volume backed overlay cell;
- `run wall ms`: end-to-end `forkcell overlay run` wall time, including command execution, checkpoint, restore, receipt write, Python CLI, and Docker calls;
- `checkpoint host ms`: host-observed checkpoint call duration;
- `checkpoint fs inner ms`: time measured inside the Docker helper while moving/scanning the active layer;
- `checkpoint Docker/CLI ms`: host duration minus inner duration; this includes Docker round trip, Python orchestration, and metadata persistence;
- `restore host ms`: host-observed restore call duration;
- `decision`: receipt decision, expected to be `restored` for the failure demo.

For the source runtime/native-overlay path, receipt breakdown separates:

- `restore_sync_ms` / `overlay_reset_ms`: ForkCell metadata generation switch;
- `restore_call_ms`: provider restore call;
- `sandbox_delete_ms`: OpenShell sandbox deletion;
- `sandbox_lifecycle_ms`: OpenShell sandbox creation/start lifecycle;
- `log_collect_ms`: best-effort non-blocking log collection by default, or sync wait when `--sync-logs` is used;
- `total_restore_path_ms`: full restore-on-fail path after command failure.

## Latest Representative Sample

This sample was run on macOS with Docker Desktop using the source checkout before `0.1.0a2` packaging. Treat it as directional, not a formal benchmark suite.

| backend | scenario | files | MiB | checkpoint ms | restore ms | log collect ms | total restore path ms | correctness |
|---|---|---:|---:|---:|---:|---:|---:|---|
| `native-overlay` | small repo | 500 | 1.0 | 0 | 0 | 10 | 550 | 1/1 |
| `native-overlay` | medium webapp | 2408 | 13.4 | 0 | 0 | 9 | 445 | 1/1 |
| `native-overlay` | dependency/cache | 6024 | 32.8 | 0 | 0 | 10 | 490 | 1/1 |
| `local-overlay` | dependency/cache | 6024 | 32.8 | 221 | 243 | n/a | n/a | 1/1 |
| `volume-delta` | dependency/cache | 6024 | 32.8 | 679 | 418 | sync | n/a | 1/1 |

## Reading The Results

- If `checkpoint fs inner ms` grows with file count, optimize filesystem scan, layer movement, or future Rust index code.
- If `checkpoint Docker/CLI ms` dominates on tiny workspaces, optimize process startup, Docker round trips, or use a resident daemon.
- If native-overlay `total_restore_path_ms` dominates while `restore_sync_ms=0`, optimize OpenShell sandbox lifecycle and pooling before rewriting ForkCell orchestration in Rust.
- If a policy benchmark needs complete OCSF/log events in the receipt, run with `--sync-logs` and report that path separately from the default fast path.
