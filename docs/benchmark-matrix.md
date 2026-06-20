# ForkCell Benchmark Matrix

ForkCell tracks performance by backend and by layer. The goal is to avoid
claiming that Python, Docker, filesystem work, and OpenShell sandbox lifecycle
are the same bottleneck.

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
FORKCELL_PACKAGE_SPEC=forkcell==0.1.0a1 ./scripts/benchmark_matrix.sh
FORKCELL_BENCH_PROFILES="tiny small medium" ./scripts/benchmark_matrix.sh
FORKCELL_BENCH_DIR=/tmp/forkcell-bench ./scripts/benchmark_matrix.sh
```

## Breakdown Terms

For the PyPI/local-overlay path, the matrix reports:

- `init ms`: source import into the Docker volume backed overlay cell;
- `run wall ms`: end-to-end `forkcell overlay run` wall time, including command execution, checkpoint, restore, receipt write, Python CLI, and Docker calls;
- `checkpoint host ms`: host-observed checkpoint call duration;
- `checkpoint fs inner ms`: time measured inside the Docker helper while moving/scanning the active layer;
- `checkpoint Docker/CLI ms`: host duration minus inner duration; this includes Docker round trip, Python orchestration, and metadata persistence;
- `restore host ms`: host-observed restore call duration;
- `decision`: receipt decision, expected to be `restored` for the failure demo.

For the source runtime/native-overlay path, receipt breakdown already separates:

- `restore_sync_ms` / `overlay_reset_ms`: ForkCell metadata generation switch;
- `restore_call_ms`: provider restore call;
- `sandbox_delete_ms`: OpenShell sandbox deletion;
- `sandbox_lifecycle_ms`: OpenShell sandbox creation/start lifecycle;
- `log_collect_ms`: log/event collection;
- `total_restore_path_ms`: full restore-on-fail path after command failure.

## Latest Local Sample

This sample was run on macOS with Docker Desktop using the source checkout after
`0.1.0a1` was published. Treat it as directional, not a formal benchmark suite.

| profile | files | bytes | init ms | run wall ms | checkpoint host ms | checkpoint fs inner ms | checkpoint Docker/CLI ms | restore host ms | decision | restored marker |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|
| tiny | 1 | 6 | 246 | 1034 | 245 | 51 | 194 | 239 | restored | missing |
| small | 50 | 13050 | 241 | 1027 | 236 | 43 | 193 | 259 | restored | missing |

## Reading The Results

- If `checkpoint fs inner ms` grows with file count, optimize filesystem scan,
  layer movement, or future Rust index code.
- If `checkpoint Docker/CLI ms` dominates on tiny workspaces, optimize process
  startup, Docker round trips, or use a resident daemon.
- If native-overlay `total_restore_path_ms` dominates while `restore_sync_ms=0`,
  optimize OpenShell sandbox lifecycle, pooling, and log collection rather than
  rewriting ForkCell orchestration in Rust.
