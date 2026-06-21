# BeforeWire Agent Readiness Pack

This directory contains the local-only PLG readiness pack for the `forkcell`
repository. It is intentionally kept under `examples/agent-readiness-pack/` so
intermediate dependencies, packet fixtures, generated policies, results, logs,
and receipts stay isolated from the main source tree.

The pack is no longer only a deterministic shadow fixture. It has two levels:

- `run_readiness_pack.py`: local packet evaluation, sanitized inventory, policy
  draft generation, and a CI-verifiable readiness receipt.
- `run_acceptance.py`: end-to-end local acceptance that adds live primitive
  checks, OpenShell disposable sandbox smoke, approval receipt replay, broker
  dry-run side-effect receipts, and tamper-negative verification.

Privacy boundary:

- Does not upload source code, prompts, MCP schemas, or tool descriptions.
- Does not store secret plaintext in generated evidence.
- Uses only local artifacts except for public dependency installs/clones and the
  GitHub metadata APIs used to verify branch protection availability.

## What It Proves

Current packet and control coverage:

- Agent/MCP/tool surface discovery from repo files and local Codex config.
- 12 readiness packets covering readonly baseline, dotenv read, secret-to-egress,
  MCP poisoning, MCP drift, shell egress, nested shell, CI workflow tamper,
  dependency lifecycle scripts, receipt tamper, approval wait, and broker dry-run.
- Microsoft AGT/ACS Python SDK plus local OPA/Rego adapter for allow/deny/ask
  governance decisions.
- OpenShell policy validation plus a live disposable sandbox smoke that creates a
  sandbox, runs `openshell policy set <sandbox> --policy ... --wait`, verifies
  default-denied egress, verifies an allowed GitHub GET, and verifies POST denial.
- Real primitive-boundary action attempts: allowed actions execute; deny/ask
  actions are blocked before execution.
- Approval flow: `ask -> approval_wait -> approval receipt -> resume/deny`.
- Broker dry-run receipts for PR comment, GitHub issue, deploy trigger, and Slack
  notification without committing external side effects.
- Replay fixture for action traces and approval receipts.
- Tamper-negative test that edits a receipt and confirms the verifier fails.
- PR-specific GitHub Actions workflow that regenerates and verifies the receipt
  on `pull_request` / `workflow_dispatch`.

## Current External Limitation

Strict merge-blocking acceptance requires GitHub branch protection or rulesets to
mark `BeforeWire Agent Gate` as a required check. For the current private repo,
GitHub returns HTTP 403 with the plan limitation message, so the strict profile
fails even when every local control passes.

Use these profiles deliberately:

- Strict merge-blocking profile: must pass branch protection/ruleset verification.
- Local PLG profile: can pass all local controls while recording branch protection
  as an external blocker.

## Local Dependencies

Installed/cloned under this directory:

- `.venv/`: Python venv with `beforewire`, `mcp-scan`, and `snyk-agent-scan`.
- `.uv-cache/`: uv cache used for the local install.
- `deps/agent-governance-toolkit/`: public AGT repo clone for reference.
- `deps/OpenShell/`: public OpenShell repo clone for reference and live smoke.
- `deps/opa/`: local OPA binary used by the AGT/ACS Rego dispatcher.

## Run

Bootstrap or refresh dependencies:

```bash
cd examples/agent-readiness-pack
python3 -m venv .venv
.venv/bin/python bin/bootstrap_readiness_pack.py --install-python-deps
```

Generate the readiness receipt and verify it:

```bash
.venv/bin/python bin/run_readiness_pack.py --repo ../..
.venv/bin/python bin/verify_readiness_receipt.py receipts/readiness-receipt.json
```

Run focused controls:

```bash
.venv/bin/python bin/evaluate_agt_govern.py --repo ../..
.venv/bin/python bin/validate_openshell_policy.py
.venv/bin/python bin/verify_github_shadow_gate.py
.venv/bin/python bin/run_live_action_packets.py --repo ../..
.venv/bin/python bin/simulate_approval_flow.py
.venv/bin/python bin/run_broker_dryrun.py
.venv/bin/python bin/run_replay_fixture.py
.venv/bin/python bin/run_tamper_negative.py
.venv/bin/python bin/run_openshell_live_smoke.py
.venv/bin/python bin/verify_branch_protection_gate.py --repo beforewire/forkcell
```

Run strict acceptance and try to configure the required-check ruleset when the GitHub API allows it:

```bash
.venv/bin/python bin/run_acceptance.py --repo ../.. --github-repo beforewire/forkcell --configure-branch-protection
```

Verify the required-check gate without mutating GitHub state:

```bash
.venv/bin/python bin/verify_branch_protection_gate.py --repo beforewire/forkcell
```

Run local PLG acceptance while preserving the strict external blocker in a
separate output file:

```bash
.venv/bin/python bin/run_acceptance.py --repo ../.. --github-repo beforewire/forkcell \
  --allow-external-unavailable \
  --output results/acceptance-local-results.json
```

## Outputs

- `inventory.json`: sanitized repo/Codex/MCP/tool inventory.
- `risk-map.json`: packet-to-risk mapping and recommended gates.
- `policies/agt.rego`: AGT/OPA/Rego policy draft for governance decisions.
- `policies/agt-manifest.yaml`: AGT/ACS manifest binding packet ActionIntent to
  `pre_tool_call`.
- `policies/openshell.yaml`: OpenShell sandbox policy used for validation and
  live smoke.
- `results/agt-govern-results.json`: local AGT govern evaluation output.
- `results/openshell-policy-validation.json`: schema/prover validation output.
- `results/github-shadow-gate-verification.json`: workflow verifier output
  (legacy filename; mode is now PR-specific and blockable when required).
- `results/live-action-packet-results.json`: real primitive-boundary attempts.
- `results/approval-flow-results.json`: approval wait and approval receipt proof.
- `results/broker-dryrun-results.json`: side-effect broker dry-run receipts.
- `results/replay-fixture-results.json`: action trace and approval replay proof.
- `results/tamper-negative-results.json`: receipt tamper-negative proof.
- `results/openshell-live-smoke.json`: live OpenShell sandbox evidence.
- `results/branch-protection-gate.json`: required-check enforcement evidence or
  external blocker evidence.
- `results/acceptance-results.json`: strict merge-blocking acceptance.
- `results/acceptance-local-results.json`: optional local PLG acceptance.
- `receipts/readiness-receipt.json`: CI-verifiable readiness receipt.
- `github/beforewire-agent-gate.yml`: GitHub Actions gate source.
- `.github/workflows/beforewire-agent-gate.yml`: installed PR workflow in the
  repository root.

## CI Gate

The installed workflow is named `beforewire-agent-gate` and its job is named
`BeforeWire Agent Gate`. On every PR it bootstraps the pack, reruns
`bin/run_readiness_pack.py --repo ../..`, verifies the freshly generated receipt,
and uploads the evidence artifacts.

To make it merge-blocking, enable branch protection or a repository ruleset and
require the `BeforeWire Agent Gate` check. The local acceptance runner can attempt
to create/update an additive repository ruleset with `--configure-branch-protection`;
in the current private repo, GitHub's API reports that this feature requires
GitHub Pro or making the repo public.
