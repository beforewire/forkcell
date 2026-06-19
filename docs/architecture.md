# ForkCell Architecture

ForkCell is the transaction layer above a governed runtime.

```text
Agent command
  -> ForkCell checkpoint manager
  -> ForkCell policy/receipt binding
  -> governed runtime sandbox/gateway/supervisor/policy layer
  -> ForkCell receipt and decision artifacts
  -> accept, restore, or fork
```

## Layer Responsibilities

| Layer | Responsibility |
| --- | --- |
| ForkCell | checkpoint identity, restore/fork workflow, receipts, decisions, backend selection |
| OpenShell | sandbox lifecycle, process/filesystem/network/L7 policy, credential path, OCSF/log events |
| beforewire/openshell | pinned runtime fork carrying the workspace-substrate patch |
| Business app/PDP | business-semantic policy, tool schemas, approvals, domain evidence |

ForkCell deliberately does not evaluate business rules such as refund limits. It records and enforces runtime transactions and can later call an external policy decision point if a product layer needs business policy.

## Core Objects

- `Cell`: a governed workspace with backend metadata.
- `Checkpoint`: filesystem point-in-time metadata and identity.
- `Run`: command execution under a selected backend/runtime.
- `PolicyRevision`: hash-derived runtime policy revision.
- `Receipt`: durable JSON/Markdown evidence for command, policy, checkpoint, events, and decision.
- `Decision`: accept/restore/fork outcome linked to a receipt and checkpoint.

## Native Overlay Fast Path

The fast path uses a patched governed runtime and ForkCell metadata:

1. The runtime mounts the ForkCell workspace backing volume through the `docker.workspace` contract.
2. The supervisor prepares overlay runtime dirs before child privilege drop.
3. ForkCell checkpoint records metadata only.
4. ForkCell restore switches to a new generation (`run-upper-N`, `run-work-N`, `merged-N`) and queues old generations for GC.

This makes synchronous restore sub-ms in local validation. Full run latency still includes runtime sandbox lifecycle and receipt collection.

## Backend Names vs Runtime Names

ForkCell backend names describe restore strategies:

- `native-overlay`
- `layer-clone`
- `volume-delta`
- `local-overlay`

Runtime names describe the sandbox/policy substrate. The current preview runtime is OpenShell; future runtimes can reuse the same ForkCell backend vocabulary if they expose equivalent workspace and policy hooks.
