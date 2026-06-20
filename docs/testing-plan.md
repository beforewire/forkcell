# ForkCell Preview Testing Plan

## Lightweight Smoke

Run before opening or packaging the preview branch:

```bash
./scripts/validate_public_smoke.sh
```

It verifies:

- submodule presence;
- Python module compilation;
- shell script syntax;
- CLI help path;
- no raw `docs/evidence/` files tracked;
- no obvious local path/private key markers in public files.


## PyPI Path Smoke

Run this before or after publishing a PyPI preview package:

```bash
FORKCELL_PACKAGE_SPEC=forkcell==0.1.0a1 ./scripts/validate_pypi_quickstart.sh
```

It verifies the package-only path: install from PyPI, initialize a local overlay
cell, run a failing command, restore the workspace, and inspect the receipt.

## Benchmark Matrix

Run the local benchmark matrix when evaluating performance changes:

```bash
./scripts/benchmark_matrix.sh
```

Use `FORKCELL_BENCH_PROFILES="tiny small medium"` for a broader run. See
`docs/benchmark-matrix.md` for metric definitions and interpretation.

## Source Runtime Demo

Run the one-command source runtime path when Docker and Rust are available:

```bash
./scripts/run_source_runtime_demo.sh
```

Use `FORKCELL_SKIP_RUNTIME_BUILD=1` when the patched runtime binaries are already
installed under `.forkcell/runtime/native-overlay`.

## Runtime Integration

Maintainer-only integration path:

```bash
./scripts/build_patched_openshell_runtime.sh
python3 -m forkcell.cli runtime install --from upstream/openshell
./scripts/start_patched_openshell_gateway.sh
```

Then run native cell workflows and inspect receipts.

## Release Minimum

A preview branch is acceptable when:

- `beforewire/openshell` exists and is private during staging, or intentionally made public for release;
- `beforewire/forkcell` exists and is private during staging, or intentionally made public for release;
- the public-preview branch is an orphan/minimal history suitable for public release;
- `beforewire/openshell` has a clear fork notice and pinned runtime tag;
- `upstream/openshell` is a submodule pinned to the runtime fork;
- README explains value, architecture, quickstart, and non-goals;
- license/notice/security/contributing files exist;
- `validate_public_smoke.sh` passes;
- no runtime state, raw evidence, or private key material is tracked.
