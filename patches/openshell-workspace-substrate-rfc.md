# OpenShell Workspace Substrate RFC

Date: 2026-06-19

Status: RFC stub for a narrow upstream patch series

## Goal

Add a controlled workspace substrate hook to OpenShell so ForkCell can provide
metadata-only checkpoint/restore without enabling arbitrary host mounts or
weakening OpenShell policy, egress, credential, and OCSF event paths.

This is not a proposal to replace OpenShell's sandbox model. It keeps the
runtime governed by OpenShell and only adds a typed workspace contract that the
Docker driver and supervisor can validate and prepare before sandbox code runs.

## Non-Goals

- Do not enable host bind mounts by default.
- Do not allow untrusted agent code to call `mount`.
- Do not allow driver config to replace `/sandbox`.
- Do not add memory/process/socket checkpointing.
- Do not change policy, credential, or network governance semantics.

## Proposed API

Extend Docker driver config with a typed workspace section:

```json
{
  "docker": {
    "workspace": {
      "type": "forkcell_overlay",
      "volume": "forkcell-work-cellid",
      "target": "/sandbox/work",
      "backing_path": "/var/lib/openshell/workspace",
      "lower_subpath": "layers/base",
      "upper_subpath": "layers/run-upper",
      "work_subpath": "layers/run-work",
      "merged_subpath": "layers/merged",
      "checkpoint_id": "chk_..."
    }
  }
}
```

The existing generic `mounts` field remains for normal driver-config mounts.
The new `workspace` field is reserved for controlled workspace semantics.

## Validation Rules

- `type` must be a known workspace substrate type.
- Initial type: `forkcell_overlay`.
- `target` must be below `/sandbox`, but must not equal `/sandbox`.
- Initial allowed target: `/sandbox/work`.
- `volume` must be a Docker named volume, not a host bind path.
- Subpaths must be relative, normalized paths with no `..` traversal.
- Unknown fields should be rejected in strict mode.

## Driver Changes

Patch surface:

- `upstream/openshell/crates/openshell-driver-docker/src/lib.rs`
  - parse `docker.workspace`;
  - validate the workspace config;
  - mount the named Docker volume at a private backing path;
  - pass workspace config to the supervisor.
- `upstream/openshell/crates/openshell-core/src/driver_mounts.rs`
  - keep the reserved `/sandbox` guard;
  - add tests showing workspace config does not bypass the guard.

## Supervisor Changes

Patch surface:

- `upstream/openshell/crates/openshell-supervisor-process/src/process.rs`
  - add a pre-drop workspace setup hook;
  - create/verify backing directories;
  - mount an overlay view at the requested target before seccomp blocks mount
    syscalls;
  - emit structured setup events for receipts.

Expected event fields:

```json
{
  "event": "workspace_substrate.setup",
  "substrate": "forkcell_overlay",
  "target": "/sandbox/work",
  "checkpoint_id": "chk_...",
  "metadata_only_restore": true,
  "workspace_config_sha256": "..."
}
```

## ForkCell Changes

- Add `native-overlay` provider.
- Keep `volume-delta` as the fallback backend.
- Add `workspace_substrate`, `metadata_only_restore`,
  `workspace_config_sha256`, and parent checkpoint id to receipts.
- Record whether a restore decision used metadata-only switch or user-space
  delta apply.

## Acceptance Gates

- Small workspace restore `<100ms`.
- Medium workspace restore `<500ms`.
- Near-1GB small-delta workspace restore `<500ms`.
- Restore correctness `100%`.
- Policy/checkpoint/receipt binding preserved.
- Decision artifacts still emitted for accept, manual restore, and automatic
  restore-on-fail.
- No raw secret appears in checkpoint metadata or receipts.

## Rollout Plan

1. Add parser and validation tests for `docker.workspace`.
2. Add Docker named-volume backing mount wiring.
3. Add supervisor pre-drop setup hook and structured events.
4. Add ForkCell `native-overlay` provider behind an explicit flag.
5. Add benchmark script and gate it separately from the Phase 1
   `volume-delta` backend.
