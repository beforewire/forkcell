# OpenShell Patch Artifacts

ForkCell's normal preview build uses the pinned `upstream/openshell` submodule, which points at `beforewire/openshell` with the workspace-substrate patch already applied.

This directory keeps review and provenance artifacts:

- `openshell.lock` records the runtime fork commit, upstream base commit, and patch sha256.
- `openshell-workspace-substrate-2026-06-19.patch` is the review/upstreaming diff for the OpenShell `docker.workspace` substrate.
- `openshell-workspace-substrate-rfc.md` explains the intended upstream shape.

ForkCell does not apply the patch during normal builds; the patch is here so reviewers can inspect exactly what the OpenShell runtime fork changes.
