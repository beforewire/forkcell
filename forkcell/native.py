from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SANDBOX_UID = 998
SANDBOX_GID = 998
WORKSPACE_TARGET = "/sandbox/work"
WORKSPACE_BACKING_PATH = "/var/lib/openshell/workspace"


@dataclass(frozen=True)
class CommandResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str


def run_cmd(args: list[str], *, check: bool = False) -> CommandResult:
    timeout = float(os.environ.get("FORKCELL_DOCKER_TIMEOUT_SECONDS", "30")) if args and args[0] == "docker" else None
    try:
        proc = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
        result = CommandResult(args=args, returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        result = CommandResult(
            args=args,
            returncode=124,
            stdout=stdout,
            stderr=stderr + f"\ncommand timed out after {timeout}s",
        )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(args)}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def now_ms() -> int:
    return int(round(time.time() * 1000))


class OpenShellNativeOverlayProvider:
    """ForkCell provider for the OpenShell-native workspace substrate contract.

    This manages the Docker named backing volume and emits the `docker.workspace`
    config consumed by the patched local OpenShell driver/supervisor. Runtime
    benchmark validation is separate because the installed OpenShell binary may
    not yet include the local fork patch.
    """

    provider_name = "native-overlay"
    image = "python:3.13-slim"

    def __init__(self, *, root: Path) -> None:
        self.root = root
        self.cells_dir = root / "native" / "cells"
        self.cells_dir.mkdir(parents=True, exist_ok=True)

    def _cell_dir(self, name: str) -> Path:
        return self.cells_dir / name

    def _metadata_path(self, name: str) -> Path:
        return self._cell_dir(name) / "metadata.json"

    def load(self, name: str) -> dict[str, Any]:
        path = self._metadata_path(name)
        if not path.exists():
            raise SystemExit(f"unknown native cell: {name}")
        return json.loads(path.read_text())

    def save(self, name: str, meta: dict[str, Any]) -> None:
        cell_dir = self._cell_dir(name)
        cell_dir.mkdir(parents=True, exist_ok=True)
        path = self._metadata_path(name)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
        tmp.replace(path)

    def create(self, *, name: str, source: Path) -> dict[str, Any]:
        if shutil.which("docker") is None:
            raise RuntimeError("docker CLI not found")
        if self._metadata_path(name).exists():
            raise SystemExit(f"native cell already exists: {name}")
        source = source.resolve()
        if not source.is_dir():
            raise SystemExit(f"source directory not found: {source}")
        volume = f"forkcell-native-{name}-{uuid.uuid4().hex[:8]}"
        run_cmd(["docker", "volume", "create", volume], check=True)
        script = f"""
set -eu
mkdir -p /workspace/layers/base /workspace/layers/run-upper /workspace/layers/run-work /workspace/layers/merged
cp -a /src/. /workspace/layers/base/
chown -R -h {SANDBOX_UID}:{SANDBOX_GID} /workspace/layers
python - <<'PY2'
import json, os, stat
root='/workspace/layers/base'
files=dirs=bytes_=0
for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
    dirs += 1
    for f in filenames:
        p=os.path.join(dirpath, f)
        try: st=os.lstat(p)
        except FileNotFoundError: continue
        if stat.S_ISREG(st.st_mode):
            files += 1
            bytes_ += st.st_size
print(json.dumps({{'files': files, 'dirs': dirs, 'bytes': bytes_}}, sort_keys=True))
PY2
""".strip()
        started = time.perf_counter()
        try:
            result = self._docker(volume, script, extra_mounts=["-v", f"{source}:/src:ro"], check=True)
        except Exception:
            run_cmd(["docker", "volume", "rm", volume], check=False)
            raise
        import_ms = int(round((time.perf_counter() - started) * 1000))
        stats = json.loads(result.stdout.strip().splitlines()[-1])
        meta = {
            "name": name,
            "provider": self.provider_name,
            "volume": volume,
            "source": str(source),
            "workspace": WORKSPACE_TARGET,
            "backing_path": WORKSPACE_BACKING_PATH,
            "lower_subpath": "layers/base",
            "run_generation": 0,
            "upper_subpath": "layers/run-upper-0",
            "work_subpath": "layers/run-work-0",
            "merged_subpath": "layers/merged-0",
            "checkpoint_id": "base",
            "created_at_ms": now_ms(),
            "base_stats": stats,
            "checkpoints": {
                "base": {
                    "checkpoint_id": "base",
                    "label": "base",
                    "provider": self.provider_name,
                    "layer": "layers/base",
                    "created_at_ms": now_ms(),
                    "metrics": {
                        "provider": self.provider_name,
                        "operation": "base_import",
                        "duration_ms": import_ms,
                        "metadata_only": False,
                        **stats,
                    },
                }
            },
            "last_checkpoint_id": "base",
            "active_layer": "layers/base",
            "clone_runs": {},
            "stale_layers": [],
            "import_ms": import_ms,
            "runtime_benchmark_validated": False,
        }
        self.save(name, meta)
        return meta

    def delete(self, name: str) -> dict[str, Any]:
        meta = self.load(name)
        result = run_cmd(["docker", "volume", "rm", meta["volume"]], check=False)
        metadata = self._metadata_path(name)
        if metadata.exists():
            metadata.unlink()
        try:
            self._cell_dir(name).rmdir()
        except OSError:
            pass
        return {"name": name, "volume": meta["volume"], "docker_rc": result.returncode, "stderr": result.stderr.strip()}

    def fork_from_checkpoint(
        self,
        *,
        source_name: str,
        new_name: str,
        checkpoint_id: str,
        label: str | None = None,
    ) -> dict[str, Any]:
        if self._metadata_path(new_name).exists():
            raise SystemExit(f"native cell already exists: {new_name}")
        source_meta = self.load(source_name)
        checkpoint = source_meta.get("checkpoints", {}).get(checkpoint_id)
        if not checkpoint:
            raise SystemExit(f"unknown native checkpoint on {source_name}: {checkpoint_id}")
        volume = f"forkcell-native-{new_name}-{uuid.uuid4().hex[:8]}"
        run_cmd(["docker", "volume", "create", volume], check=True)
        source_layer = checkpoint["layer"]
        script = f"""
set -eu
mkdir -p /workspace/layers/base /workspace/layers/run-upper /workspace/layers/run-work /workspace/layers/merged
cp -a /source/{source_layer}/. /workspace/layers/base/
chown -R -h {SANDBOX_UID}:{SANDBOX_GID} /workspace/layers
python - <<'PY2'
import json, os, stat
root='/workspace/layers/base'
files=dirs=bytes_=0
for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
    dirs += 1
    for f in filenames:
        p=os.path.join(dirpath, f)
        try: st=os.lstat(p)
        except FileNotFoundError: continue
        if stat.S_ISREG(st.st_mode):
            files += 1
            bytes_ += st.st_size
print(json.dumps({{'files': files, 'dirs': dirs, 'bytes': bytes_}}, sort_keys=True))
PY2
""".strip()
        started = time.perf_counter()
        try:
            result = run_cmd(
                [
                    "docker",
                    "run",
                    "--rm",
                    "-v",
                    f"{source_meta['volume']}:/source:ro",
                    "-v",
                    f"{volume}:/workspace",
                    self.image,
                    "sh",
                    "-lc",
                    script,
                ],
                check=True,
            )
        except Exception:
            run_cmd(["docker", "volume", "rm", volume], check=False)
            raise
        fork_ms = int(round((time.perf_counter() - started) * 1000))
        stats = json.loads(result.stdout.strip().splitlines()[-1])
        now = now_ms()
        base_checkpoint = {
            "checkpoint_id": "base",
            "label": label or f"fork:{source_name}:{checkpoint_id}",
            "provider": self.provider_name,
            "layer": "layers/base",
            "created_at_ms": now,
            "forked_from": {"cell_id": source_name, "checkpoint_id": checkpoint_id},
            "metrics": {
                "provider": self.provider_name,
                "operation": "fork_from_checkpoint",
                "duration_ms": fork_ms,
                "metadata_only": False,
                **stats,
            },
        }
        meta = {
            "name": new_name,
            "provider": self.provider_name,
            "volume": volume,
            "source": f"fork:{source_name}:{checkpoint_id}",
            "workspace": WORKSPACE_TARGET,
            "backing_path": WORKSPACE_BACKING_PATH,
            "lower_subpath": "layers/base",
            "run_generation": 0,
            "upper_subpath": "layers/run-upper-0",
            "work_subpath": "layers/run-work-0",
            "merged_subpath": "layers/merged-0",
            "checkpoint_id": "base",
            "created_at_ms": now,
            "base_stats": stats,
            "checkpoints": {"base": base_checkpoint},
            "last_checkpoint_id": "base",
            "active_layer": "layers/base",
            "clone_runs": {},
            "stale_layers": [],
            "import_ms": fork_ms,
            "runtime_benchmark_validated": False,
            "forked_from": {"cell_id": source_name, "checkpoint_id": checkpoint_id},
        }
        self.save(new_name, meta)
        return meta

    def status(self, name: str) -> dict[str, Any]:
        meta = self.load(name)
        volume_result = run_cmd(["docker", "volume", "inspect", meta["volume"]], check=False)
        meta = dict(meta)
        meta["docker_volume_exists"] = volume_result.returncode == 0
        meta["checkpoint_count"] = len(meta.get("checkpoints", {}))
        meta["driver_config"] = json.loads(self.driver_config_json(name))
        return meta

    def checkpoint(self, name: str, *, label: str | None = None) -> dict[str, Any]:
        meta = self.load(name)
        checkpoint_id = f"chk_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        started = time.perf_counter()
        duration_ms = int(round((time.perf_counter() - started) * 1000))
        active_layer = meta.get("lower_subpath", "layers/base")
        checkpoint = {
            "checkpoint_id": checkpoint_id,
            "label": label,
            "provider": self.provider_name,
            "layer": active_layer,
            "created_at_ms": now_ms(),
            "metrics": {
                "provider": self.provider_name,
                "operation": "checkpoint_mark",
                "duration_ms": duration_ms,
                "metadata_only": True,
                "process_checkpoint": False,
                "requires_sandbox_restart": True,
                "delta_files": 0,
                "delta_dirs": 0,
                "delta_bytes": 0,
                "active_layer": active_layer,
            },
        }
        meta["checkpoints"][checkpoint_id] = checkpoint
        meta["last_checkpoint_id"] = checkpoint_id
        meta["checkpoint_id"] = checkpoint_id
        self.save(name, meta)
        return checkpoint

    def restore(self, name: str, checkpoint_id: str | None = None) -> dict[str, Any]:
        meta = self.load(name)
        checkpoint_id = checkpoint_id or meta.get("last_checkpoint_id")
        if not checkpoint_id:
            raise SystemExit("no checkpoint specified and native cell has no last checkpoint")
        checkpoint = meta.get("checkpoints", {}).get(checkpoint_id)
        if not checkpoint:
            raise SystemExit(f"unknown native checkpoint: {checkpoint_id}")
        started = time.perf_counter()
        old_generation = int(meta.get("run_generation", 0))
        old_paths = {
            "upper_subpath": meta.get("upper_subpath", "layers/run-upper"),
            "work_subpath": meta.get("work_subpath", "layers/run-work"),
            "merged_subpath": meta.get("merged_subpath", "layers/merged"),
        }
        new_generation = old_generation + 1
        new_paths = {
            "upper_subpath": f"layers/run-upper-{new_generation}",
            "work_subpath": f"layers/run-work-{new_generation}",
            "merged_subpath": f"layers/merged-{new_generation}",
        }
        stale_layers = list(meta.get("stale_layers", []))
        if any(old_paths.values()):
            stale_layers.append(
                {
                    "generation": old_generation,
                    "paths": old_paths,
                    "queued_at_ms": now_ms(),
                    "reason": "generation_switch_restore",
                    "checkpoint_id": checkpoint_id,
                }
            )
        duration_ms = int(round((time.perf_counter() - started) * 1000))
        meta["last_checkpoint_id"] = checkpoint_id
        meta["checkpoint_id"] = checkpoint_id
        meta["lower_subpath"] = checkpoint["layer"]
        meta["run_generation"] = new_generation
        meta.update(new_paths)
        meta["stale_layers"] = stale_layers
        self.save(name, meta)
        return {
            "restored_at_ms": now_ms(),
            "cell_id": name,
            "checkpoint_id": checkpoint_id,
            "metrics": {
                "provider": self.provider_name,
                "operation": "checkpoint_restore",
                "duration_ms": duration_ms,
                "overlay_reset_ms": duration_ms,
                "restore_sync_ms": duration_ms,
                "metadata_only": True,
                "process_checkpoint": False,
                "requires_sandbox_restart": True,
                "generation_switch": True,
                "old_generation": old_generation,
                "new_generation": new_generation,
                "gc_pending_count": len(stale_layers),
                "gc_async_ms": 0,
                "old_paths": old_paths,
                "new_paths": new_paths,
                "details": {
                    "overlay_reset_ms": duration_ms,
                    "restore_sync_ms": duration_ms,
                    "generation_switch": True,
                    "old_generation": old_generation,
                    "new_generation": new_generation,
                },
                "breakdown": {
                    "overlay_reset_ms": duration_ms,
                    "restore_sync_ms": duration_ms,
                    "provider_call_ms": duration_ms,
                    "gc_async_ms": 0,
                },
            },
        }

    def gc(self, name: str, *, dry_run: bool = False) -> dict[str, Any]:
        meta = self.load(name)
        stale_layers = list(meta.get("stale_layers", []))
        paths: list[str] = []
        for item in stale_layers:
            for path in (item.get("paths") or {}).values():
                if path and path not in paths:
                    paths.append(path)
        started = time.perf_counter()
        if paths and not dry_run:
            quoted_paths = " ".join(f"/workspace/{path}" for path in paths)
            script = f"""
set -eu
rm -rf {quoted_paths}
python - <<'PY2'
import json
print(json.dumps({{'removed_paths': {paths!r}}}, sort_keys=True))
PY2
""".strip()
            self._docker(meta["volume"], script, check=True)
            meta["stale_layers"] = []
            self.save(name, meta)
        duration_ms = int(round((time.perf_counter() - started) * 1000))
        return {
            "cell_id": name,
            "dry_run": dry_run,
            "stale_generation_count": len(stale_layers),
            "stale_path_count": len(paths),
            "removed_path_count": 0 if dry_run else len(paths),
            "gc_async_ms": duration_ms,
            "paths": paths,
        }

    def driver_config_json(self, name: str) -> str:
        meta = self.load(name)
        return json.dumps(
            {
                "docker": {
                    "workspace": {
                        "type": "forkcell_overlay",
                        "volume": meta["volume"],
                        "target": meta.get("workspace", WORKSPACE_TARGET),
                        "lower_subpath": meta.get("lower_subpath", "layers/base"),
                        "upper_subpath": meta.get("upper_subpath", "layers/run-upper-0"),
                        "work_subpath": meta.get("work_subpath", "layers/run-work-0"),
                        "merged_subpath": meta.get("merged_subpath", "layers/merged-0"),
                        "checkpoint_id": meta.get("checkpoint_id", "base"),
                    }
                }
            },
            separators=(",", ":"),
        )

    def volume_mount_driver_config_json(self, name: str, *, layer: str | None = None) -> str:
        meta = self.load(name)
        subpath = layer or meta.get("active_layer", "layers/base")
        return json.dumps(
            {
                "docker": {
                    "mounts": [
                        {
                            "type": "volume",
                            "source": meta["volume"],
                            "target": meta.get("workspace", WORKSPACE_TARGET),
                            "subpath": subpath,
                            "read_only": False,
                        }
                    ]
                }
            },
            separators=(",", ":"),
        )

    def layer_checkpoint(self, name: str, *, label: str | None = None) -> dict[str, Any]:
        meta = self.load(name)
        checkpoint_id = f"chk_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        active_layer = meta.get("active_layer", "layers/base")
        started = time.perf_counter()
        checkpoint = {
            "checkpoint_id": checkpoint_id,
            "label": label,
            "provider": "layer-clone",
            "layer": active_layer,
            "created_at_ms": now_ms(),
            "metrics": {
                "provider": "layer-clone",
                "operation": "checkpoint_mark",
                "duration_ms": int(round((time.perf_counter() - started) * 1000)),
                "metadata_only": True,
                "process_checkpoint": False,
                "requires_sandbox_restart": True,
                "active_layer": active_layer,
            },
        }
        meta.setdefault("checkpoints", {})[checkpoint_id] = checkpoint
        meta["last_checkpoint_id"] = checkpoint_id
        self.save(name, meta)
        return checkpoint

    def prepare_layer_run(self, name: str, checkpoint_id: str) -> dict[str, Any]:
        meta = self.load(name)
        checkpoint = meta.get("checkpoints", {}).get(checkpoint_id)
        if not checkpoint:
            raise SystemExit(f"unknown native checkpoint: {checkpoint_id}")
        source_layer = checkpoint["layer"]
        run_layer = f"layers/run-{int(time.time())}-{uuid.uuid4().hex[:8]}"
        script = f"""
set -eu
mkdir -p /workspace/layers
if [ -e /workspace/{run_layer} ]; then exit 9; fi
cp -a /workspace/{source_layer} /workspace/{run_layer}
chown -R -h {SANDBOX_UID}:{SANDBOX_GID} /workspace/{run_layer}
python - <<'PY2'
import json, os, stat
root='/workspace/{run_layer}'
files=dirs=bytes_=0
for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
    dirs += 1
    for f in filenames:
        p=os.path.join(dirpath, f)
        try: st=os.lstat(p)
        except FileNotFoundError: continue
        if stat.S_ISREG(st.st_mode):
            files += 1; bytes_ += st.st_size
print(json.dumps({{'files': files, 'dirs': dirs, 'bytes': bytes_}}, sort_keys=True))
PY2
""".strip()
        started = time.perf_counter()
        result = self._docker(meta["volume"], script, check=True)
        duration_ms = int(round((time.perf_counter() - started) * 1000))
        stats = json.loads(result.stdout.strip().splitlines()[-1])
        run_id = f"layer_run_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        run = {
            "layer_run_id": run_id,
            "checkpoint_id": checkpoint_id,
            "source_layer": source_layer,
            "run_layer": run_layer,
            "created_at_ms": now_ms(),
            "metrics": {
                "provider": "layer-clone",
                "operation": "prepare_run_layer",
                "duration_ms": duration_ms,
                "metadata_only": False,
                **stats,
            },
        }
        meta.setdefault("clone_runs", {})[run_id] = run
        self.save(name, meta)
        return run

    def restore_layer_run(self, name: str, checkpoint_id: str, layer_run_id: str | None = None) -> dict[str, Any]:
        meta = self.load(name)
        checkpoint = meta.get("checkpoints", {}).get(checkpoint_id)
        if not checkpoint:
            raise SystemExit(f"unknown native checkpoint: {checkpoint_id}")
        started = time.perf_counter()
        meta["active_layer"] = checkpoint["layer"]
        meta["last_checkpoint_id"] = checkpoint_id
        if layer_run_id and layer_run_id in meta.get("clone_runs", {}):
            meta["clone_runs"][layer_run_id]["status"] = "abandoned"
        self.save(name, meta)
        return {
            "restored_at_ms": now_ms(),
            "cell_id": name,
            "checkpoint_id": checkpoint_id,
            "metrics": {
                "provider": "layer-clone",
                "operation": "checkpoint_restore",
                "duration_ms": int(round((time.perf_counter() - started) * 1000)),
                "metadata_only": True,
                "process_checkpoint": False,
                "requires_sandbox_restart": True,
                "details": {"active_layer": checkpoint["layer"], "abandoned_layer_run_id": layer_run_id},
            },
        }

    def accept_layer_run(self, name: str, layer_run_id: str, *, label: str | None = None) -> dict[str, Any]:
        meta = self.load(name)
        run = meta.get("clone_runs", {}).get(layer_run_id)
        if not run:
            raise SystemExit(f"unknown layer run: {layer_run_id}")
        checkpoint_id = f"chk_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        checkpoint = {
            "checkpoint_id": checkpoint_id,
            "label": label or "accepted-run",
            "provider": "layer-clone",
            "layer": run["run_layer"],
            "created_at_ms": now_ms(),
            "parent_checkpoint_id": run["checkpoint_id"],
            "metrics": {
                "provider": "layer-clone",
                "operation": "accept_run_layer",
                "duration_ms": 0,
                "metadata_only": True,
                "process_checkpoint": False,
                "requires_sandbox_restart": True,
            },
        }
        run["status"] = "accepted"
        meta.setdefault("checkpoints", {})[checkpoint_id] = checkpoint
        meta["last_checkpoint_id"] = checkpoint_id
        meta["active_layer"] = run["run_layer"]
        self.save(name, meta)
        return checkpoint

    def _docker(
        self,
        volume: str,
        script: str,
        *,
        extra_mounts: list[str] | None = None,
        check: bool = False,
    ) -> CommandResult:
        mounts = ["-v", f"{volume}:/workspace"]
        if extra_mounts:
            mounts.extend(extra_mounts)
        return run_cmd(["docker", "run", "--rm", *mounts, self.image, "sh", "-lc", script], check=check)
