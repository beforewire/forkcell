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
