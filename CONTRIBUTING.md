# Contributing

ForkCell has two repositories in the preview setup:

- `beforewire/forkcell`: control plane, checkpoint/restore, receipts, decisions, docs.
- `beforewire/openshell`: pinned OpenShell runtime fork carrying the workspace-substrate patch.

## Setup

```bash
git clone --recurse-submodules https://github.com/beforewire/forkcell.git
cd forkcell
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
./scripts/validate_public_smoke.sh
```

## Runtime Work

Changes to OpenShell runtime behavior belong in `beforewire/openshell` first. Update the ForkCell submodule pointer and `patches/openshell.lock` when the runtime changes.

## ForkCell Work

Changes to ForkCell control plane behavior should include:

- focused CLI/API tests or a preview smoke update;
- receipt/binding evidence when changing policy or restore paths;
- documentation updates for user-visible behavior.

## Do Not Commit

- `.forkcell/` runtime state;
- `.forkcell-build/` build cache;
- OpenShell build outputs under `upstream/openshell/target*`;
- private keys, JWTs, TLS key material, tokens, or raw customer data;
- machine-local raw evidence logs unless explicitly sanitized.
