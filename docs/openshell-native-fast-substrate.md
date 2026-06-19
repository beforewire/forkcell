# OpenShell Native Fast Substrate

ForkCell's native fast path depends on a narrow OpenShell runtime patch carried in the pinned `beforewire/openshell` submodule.

## Patch Scope

The patch adds:

- a typed Docker driver workspace config (`docker.workspace`);
- validation for `forkcell_overlay` workspace shape;
- a private workspace backing mount path;
- supervisor-only workspace config env plumbing;
- supervisor overlay runtime directory prepare/chown before privilege drop;
- unit coverage for valid config, rejected paths, duplicate targets, and supervisor parsing.

The patch does not rewrite OpenShell policy, egress, credential, or OCSF paths.

## Runtime Contract

ForkCell passes a driver config like:

```json
{
  "docker": {
    "workspace": {
      "type": "forkcell_overlay",
      "volume": "forkcell-native-demo",
      "target": "/sandbox/work",
      "backing_path": "/var/lib/openshell/workspace",
      "lower_subpath": "layers/base",
      "upper_subpath": "layers/run-upper-1",
      "work_subpath": "layers/run-work-1",
      "merged_subpath": "layers/merged-1",
      "checkpoint_id": "chk_..."
    }
  }
}
```

## Provenance

- Runtime source: `upstream/openshell` submodule.
- Runtime fork: `https://github.com/beforewire/openshell`.
- Runtime commit: `8717d17a1cff50204cdd139fa4bf1c262cbf5f85`.
- Review patch: `patches/openshell-workspace-substrate-2026-06-19.patch`.
- Patch sha256: `646ad71866eeaa36598d0f91cf7ec69ed708e370893c709474b74b77e3f2d42d`.
