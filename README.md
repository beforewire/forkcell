# ForkCell

[中文](README.zh-CN.md) | English

ForkCell is a governed execution-cell layer for AI agents: it adds fast workspace rollback, policy-bound runs, and reviewable receipts to local agent execution.

> Checkpoint -> governed run -> receipt -> accept, restore, or fork.

ForkCell is currently the `v0.1.0a2` preview (`0.1.0a2` Python package version). The public-preview branch is intentionally small: it contains the ForkCell control plane, a pinned governed-runtime submodule, a review patch artifact, and the minimum scripts/docs needed to understand and run the preview.

## Why ForkCell Exists

AI agents increasingly edit repositories, install packages, call APIs, and touch credentials. A plain sandbox can isolate a process, and a plain snapshot can roll files back, but teams also need to answer:

- What checkpoint did the agent start from?
- What runtime policy governed the run?
- Which egress or L7 policy events occurred?
- Was the failed run restored, accepted, or forked?
- Can a reviewer inspect a durable receipt instead of raw logs?

ForkCell turns a risky agent command into an auditable transaction.

## What ForkCell Does

ForkCell owns the transaction/control plane:

- creates filesystem checkpoints for a cell workspace;
- runs commands through a governed runtime integration;
- binds policy revision, checkpoint identity, workspace config, and command result into receipts;
- records accept/restore decisions as first-class artifacts;
- restores quickly through metadata generation switching on the native overlay backend;
- keeps fallback/degraded backend decisions explicit.

Runtime enforcement is handled by the configured governed runtime; see
`Runtime Integration` for the current preview substrate.

ForkCell does **not** implement business-semantic policy such as refund limits or claim eligibility in core. Business policy should live in an external application/PDP/tool gateway. ForkCell focuses on runtime capability policy and transaction receipts.

## Repository Layout

```text
forkcell/
  forkcell/                  # Python CLI/API and checkpoint providers
  scripts/                   # preview build, gateway, and smoke scripts
  patches/                   # runtime patch review/provenance artifacts
  docs/                      # architecture and preview docs
  upstream/openshell         # submodule: current pinned runtime fork
```

The OpenShell patch is already applied in the pinned `beforewire/openshell` submodule. The patch file under `patches/` is kept as a review/upstreaming artifact, not as a normal build-time step.

## Runtime Integration

ForkCell's preview runtime integration uses a pinned OpenShell fork:

```text
repo:    https://github.com/beforewire/openshell
branch:  forkcell-workspace-substrate
tag:     forkcell-runtime-v0.1.3-preview
commit:  393c25a86d9128ff5e38ecf537809efe58470266
```

The runtime fork carries a narrow workspace-substrate change:

- Docker driver accepts a typed `docker.workspace` / `forkcell_overlay` contract;
- the workspace backing volume is mounted at a private path;
- the supervisor prepares/chowns overlay runtime directories before privilege drop and hardening;
- runtime policy, egress, credential, and OCSF paths stay unchanged.

In this preview, OpenShell provides the runtime enforcement layer:

- sandbox lifecycle;
- process and filesystem policy;
- egress/L7 policy;
- credential/provider path;
- OCSF/log events.

See `patches/openshell.lock` and `docs/openshell-native-fast-substrate.md`.

## Install Paths

ForkCell has two preview install paths:

- **PyPI package path** installs the Python CLI/API only. Use it for the local overlay rollback demo below.
- **Source runtime path** clones this repository with submodules. Use it for the full patched OpenShell governed-runtime demo.

The PyPI package does **not** include `scripts/`, `patches/`, or the `upstream/openshell` submodule. Those files are available from the GitHub source repository.

## PyPI Quickstart

Use this path when installing from PyPI:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install forkcell==0.1.0a2

mkdir -p workspace
printf 'hello\n' > workspace/hello.txt

forkcell overlay init demo --from workspace
forkcell overlay run --checkpoint-before --restore-on-fail demo -- \
  sh -lc 'echo changed > hello.txt; exit 7'

cat workspace/hello.txt
forkcell receipt show --cell demo --latest --format md
```

The command intentionally exits with status `7`. Success means ForkCell records
`Decision: restored` in the receipt and the final `cat` prints `hello`.

This path uses the local overlay rollback backend. It demonstrates checkpoint,
restore, and receipt semantics, but it does not start the patched OpenShell
runtime or enforce OpenShell network/credential policy.

## Source Runtime Quickstart

Use this path for the full ForkCell + patched OpenShell runtime preview.

Prerequisites:

- macOS or Linux host with Docker available;
- Python 3.11+;
- Rust/Cargo for building OpenShell CLI/gateway;
- access to the public `beforewire/openshell` submodule.

Clone with submodules:

```bash
git clone --recurse-submodules https://github.com/beforewire/forkcell.git
cd forkcell
```

Or initialize submodules after cloning:

```bash
git submodule update --init --recursive
```

Install ForkCell locally:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Run the lightweight preview smoke:

```bash
./scripts/validate_public_smoke.sh
```

Build the pinned governed runtime:

```bash
./scripts/build_patched_openshell_runtime.sh
python3 -m forkcell.cli runtime install --from upstream/openshell
```

Start the local patched runtime gateway:

```bash
./scripts/start_patched_openshell_gateway.sh
export FORKCELL_OPENSHELL_BIN="$PWD/.forkcell/runtime/native-overlay/bin/openshell"
export OPENSHELL_GATEWAY_ENDPOINT="http://127.0.0.1:17671"
```

Create a native cell and run a restore-on-fail command:

```bash
mkdir -p /tmp/forkcell-demo
printf 'hello\n' >/tmp/forkcell-demo/hello.txt

python3 -m forkcell.cli native init demo --from /tmp/forkcell-demo
python3 -m forkcell.cli native run --checkpoint-before --restore-on-fail demo -- \
  sh -lc 'echo changed > hello.txt; exit 7'
python3 -m forkcell.cli receipt show --cell demo --latest --format md
cat /tmp/forkcell-demo/hello.txt
```

The command intentionally exits with status `7` inside the sandbox. Success means
ForkCell records `Decision: restored` in the receipt and the final `cat` prints
`hello`.

Stop the gateway when done:

```bash
./scripts/stop_patched_openshell_gateway.sh
```

## Current Preview Metrics

Latest validation for `0.1.0a2` on macOS + Docker Desktop:

| backend | scenario | files | MiB | checkpoint | restore | full restore path | correctness |
|---|---|---:|---:|---:|---:|---:|---|
| `native-overlay` | small repo | 500 | 1.0 | 0ms | 0ms | 550ms | 1/1 |
| `native-overlay` | medium webapp | 2408 | 13.4 | 0ms | 0ms | 445ms | 1/1 |
| `native-overlay` | dependency/cache | 6024 | 32.8 | 0ms | 0ms | 490ms | 1/1 |
| `volume-delta` | dependency/cache | 6024 | 32.8 | 679ms | 418ms | n/a | 1/1 |
| `local-overlay` | dependency/cache | 6024 | 32.8 | 221ms | 243ms | n/a | 1/1 |

Notes:

- `native-overlay` still reports `restore_sync_ms=0ms`; this means the synchronous workspace generation switch is sub-ms/rounded to zero.
- `total_restore_path_ms` includes OpenShell sandbox lifecycle and delete, but log collection now defaults to best-effort non-blocking mode. Use `--sync-logs` when a policy test needs to wait briefly for OCSF/log events.
- The source preview also validates secret-file exclusion for common local secret paths (`.env`, `.env.*`, `.ssh`, `.aws`, `*.pem`, `*.key`) and receipt/checkpoint policy binding.

A sanitized evidence summary is in `docs/evidence-summary.md`.

## Backends

- `native-overlay`: production fast path when the patched governed runtime is configured.
- `layer-clone`: compatible fallback; restore is metadata-only but run-layer preparation copies the checkpoint tree.
- `volume-delta`: governed Docker volume workspace with CAS/delta restore.
- `local-overlay`: local degraded-policy filesystem fallback for development.

In this preview, `native-overlay`, `layer-clone`, and `volume-delta` are backed by OpenShell. The backend names describe ForkCell restore strategies; the runtime integration describes the sandbox/policy engine underneath.

## Non-goals For This Preview

- no memory/process checkpoint;
- no VM/MicroVM/KVM isolation layer;
- no business-semantic policy evaluator in ForkCell core;
- no replacement for OpenShell policy/egress enforcement;
- no claim of pure macOS/Windows native isolation;
- no claim that full sandbox lifecycle is `0ms`.

## Documentation

- `docs/architecture.md` - product boundary and control-plane architecture.
- `docs/openshell-native-fast-substrate.md` - OpenShell workspace substrate design.
- `docs/testing-plan.md` - preview smoke and integration validation plan.
- `docs/benchmark-matrix.md` - benchmark matrix and performance breakdown guide.
- `docs/rust-core-boundary.md` - Rust core decision boundary.
- `docs/evidence-summary.md` - sanitized validation summary.

## About BeforeWire

ForkCell is part of BeforeWire's agent-trust infrastructure work: make agent execution reviewable, reversible, and policy-bound without pretending that local development needs a full cloud MicroVM product on day one.

## Status

`v0.1.0a2` is experimental. The preview is intended to show the product boundary and the working checkpoint/restore/receipt path before the project is promoted to a broader public release.
