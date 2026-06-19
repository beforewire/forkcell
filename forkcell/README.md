# ForkCell Facade

This package contains ForkCell-specific facade/glue code over the current governed-runtime integration.

Current surfaces:

- `forkcell.cli`: product CLI for cells, checkpoints, restore decisions, receipts, events, and review status.
- `forkcell.api`: small Python agent API facade over the CLI/shared state, intended for E2B-style local agent workflows.
- `forkcell.native`: fast governed-runtime workspace substrates (`native-overlay` and `layer-clone`).
- `forkcell.volume`: governed named-volume workspace with CAS incremental checkpoint and delta restore.

The Python facade intentionally shells out to `python -m forkcell.cli` for now so the API and CLI share receipts, decisions, and review evidence while the runtime patch remains narrow.
