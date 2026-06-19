# ForkCell Preview Evidence Summary

This is a sanitized summary of validation for `v0.1.0-preview`.
Raw machine-local evidence logs are intentionally not tracked in the public-preview branch.

## Runtime Integration

- OpenShell runtime fork: `beforewire/openshell`.
- Runtime branch: `forkcell-workspace-substrate`.
- Runtime commit: `393c25a86d9128ff5e38ecf537809efe58470266`.
- ForkCell branch: `public-preview`.

## Validation Highlights

- Native overlay synchronous restore: `0ms` reported for small, medium, and pruned profiles.
- Native overlay correctness matrix: `7/7` cases passed.
- Native overlay policy smoke: deny host, allow GET, and L7 deny passed.
- Receipt binding: policy revision, checkpoint identity, workspace config, and decision artifacts recorded.
- CI-style integration gate: passed before preview packaging.

## README Path Validation

A fresh clone of the public-preview branch was validated through the README path:

- `git clone --recurse-submodules`;
- editable Python install;
- `validate_public_smoke.sh`;
- patched governed-runtime build;
- local gateway start;
- native `--restore-on-fail` demo;
- receipt inspection and final file-content restore check.

The tiny README demo produced:

- base import: `288ms`;
- checkpoint mark: `0ms`;
- restore duration: `0ms`;
- `restore_sync_ms`: `0ms`;
- `total_restore_path_ms`: `726ms`, including runtime sandbox delete/lifecycle and log collection.

## Boundary Note

`restore_sync_ms=0ms` means ForkCell's synchronous restore substrate is sub-ms/rounded to zero. A full agent run still includes runtime sandbox lifecycle, command execution, log collection, and receipt generation.
