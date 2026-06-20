from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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


class DockerOverlayProvider:
    provider_name = "linux-overlayfs-docker-volume"
    image = "python:3.13-slim"

    def __init__(self, *, root: Path) -> None:
        self.root = root
        self.cells_dir = root / "overlay" / "cells"
        self.cells_dir.mkdir(parents=True, exist_ok=True)

    def _cell_dir(self, name: str) -> Path:
        return self.cells_dir / name

    def _metadata_path(self, name: str) -> Path:
        return self._cell_dir(name) / "metadata.json"

    def load(self, name: str) -> dict[str, Any]:
        path = self._metadata_path(name)
        if not path.exists():
            raise SystemExit(f"unknown overlay cell: {name}")
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
            raise SystemExit(f"overlay cell already exists: {name}")
        source = source.resolve()
        if not source.is_dir():
            raise SystemExit(f"source directory not found: {source}")
        volume = f"forkcell-overlay-{name}-{uuid.uuid4().hex[:8]}"
        run_cmd(["docker", "volume", "create", volume], check=True)
        started = time.perf_counter()
        script = """
set -eu
mkdir -p /demo/base /demo/layers /demo/active/upper /demo/active/work /demo/active/merged
(cd /src && tar --exclude='./.env' --exclude='./.env.*' --exclude='*/.env' --exclude='*/.env.*' --exclude='./.ssh' --exclude='./.aws' --exclude='*.pem' --exclude='*.key' -cf - .) | tar -xf - -C /demo/base
python - <<'PY2'
import json, os
root='/demo/base'
files=dirs=bytes_=0
for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
    dirs += 1
    for f in filenames:
        p=os.path.join(dirpath, f)
        try: st=os.lstat(p)
        except FileNotFoundError: continue
        if os.path.isfile(p):
            files += 1; bytes_ += st.st_size
print(json.dumps({'files': files, 'dirs': dirs, 'bytes': bytes_}, sort_keys=True))
PY2
""".strip()
        try:
            result = self._docker(volume, ["-v", f"{source}:/src:ro"], script, check=True)
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
            "created_at_ms": now_ms(),
            "base_stats": stats,
            "lower_chain": [],
            "active_upper": "active/upper",
            "checkpoints": {},
            "last_checkpoint_id": None,
            "last_run_id": None,
            "import_ms": import_ms,
        }
        self.save(name, meta)
        return meta

    def delete(self, name: str) -> dict[str, Any]:
        meta = self.load(name)
        result = run_cmd(["docker", "volume", "rm", meta["volume"]], check=False)
        metadata = self._metadata_path(name)
        if metadata.exists():
            metadata.unlink()
        cell_dir = self._cell_dir(name)
        try:
            cell_dir.rmdir()
        except OSError:
            pass
        return {"name": name, "volume": meta["volume"], "docker_rc": result.returncode, "stderr": result.stderr.strip()}

    def status(self, name: str) -> dict[str, Any]:
        meta = self.load(name)
        volume_result = run_cmd(["docker", "volume", "inspect", meta["volume"]], check=False)
        meta = dict(meta)
        meta["docker_volume_exists"] = volume_result.returncode == 0
        meta["checkpoint_count"] = len(meta.get("checkpoints", {}))
        meta["lower_chain_depth"] = len(meta.get("lower_chain", []))
        return meta

    def checkpoint(self, name: str, *, label: str | None = None) -> dict[str, Any]:
        meta = self.load(name)
        checkpoint_id = f"chk_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        lower_chain = list(meta.get("lower_chain", []))
        script = f"""
set -eu
mkdir -p /demo/base /demo/layers /demo/active/upper /demo/active/work /demo/active/merged
python - <<'PY2' >/tmp/start
import time; print(time.time())
PY2
if [ -d /demo/layers/{shlex.quote(checkpoint_id)} ]; then
  echo 'checkpoint layer already exists' >&2
  exit 9
fi
mv /demo/active/upper /demo/layers/{shlex.quote(checkpoint_id)}
mkdir -p /demo/active/upper /demo/active/work /demo/active/merged
python - <<'PY2'
import json, os, time
root='/demo/layers/{checkpoint_id}'
files=dirs=bytes_=0
for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
    dirs += 1
    for f in filenames:
        p=os.path.join(dirpath, f)
        try: st=os.lstat(p)
        except FileNotFoundError: continue
        if os.path.isfile(p):
            files += 1; bytes_ += st.st_size
start=float(open('/tmp/start').read().strip())
print(json.dumps({{'files': files, 'dirs': dirs, 'bytes': bytes_, 'inner_duration_ms': int(round((time.time()-start)*1000))}}, sort_keys=True))
PY2
""".strip()
        started = time.perf_counter()
        result = self._docker(meta["volume"], [], script, check=True)
        total_ms = int(round((time.perf_counter() - started) * 1000))
        layer_stats = json.loads(result.stdout.strip().splitlines()[-1])
        new_chain = [checkpoint_id, *lower_chain]
        checkpoint = {
            "checkpoint_id": checkpoint_id,
            "label": label,
            "provider": self.provider_name,
            "layer": f"layers/{checkpoint_id}",
            "lower_chain": new_chain,
            "created_at_ms": now_ms(),
            "metrics": {
                "provider": self.provider_name,
                "operation": "checkpoint_create",
                "duration_ms": total_ms,
                "inner_duration_ms": layer_stats.get("inner_duration_ms"),
                "delta_files": layer_stats.get("files"),
                "delta_dirs": layer_stats.get("dirs"),
                "delta_bytes": layer_stats.get("bytes"),
                "lower_chain_depth": len(new_chain),
                "metadata_only": True,
                "process_checkpoint": False,
                "requires_sandbox_restart": True,
            },
        }
        meta["checkpoints"][checkpoint_id] = checkpoint
        meta["lower_chain"] = new_chain
        meta["last_checkpoint_id"] = checkpoint_id
        self.save(name, meta)
        return checkpoint

    def restore(self, name: str, checkpoint_id: str | None = None) -> dict[str, Any]:
        meta = self.load(name)
        checkpoint_id = checkpoint_id or meta.get("last_checkpoint_id")
        if not checkpoint_id:
            raise SystemExit("no checkpoint specified and overlay cell has no last checkpoint")
        checkpoint = meta.get("checkpoints", {}).get(checkpoint_id)
        if not checkpoint:
            raise SystemExit(f"unknown overlay checkpoint: {checkpoint_id}")
        script = """
set -eu
rm -rf /demo/active/upper /demo/active/work
mkdir -p /demo/active/upper /demo/active/work /demo/active/merged
python - <<'PY2'
import json, os
root='/demo/active/upper'
print(json.dumps({'active_upper_reset': os.path.isdir(root)}, sort_keys=True))
PY2
""".strip()
        started = time.perf_counter()
        result = self._docker(meta["volume"], [], script, check=True)
        total_ms = int(round((time.perf_counter() - started) * 1000))
        meta["lower_chain"] = list(checkpoint["lower_chain"])
        self.save(name, meta)
        return {
            "restored_at_ms": now_ms(),
            "cell_id": name,
            "checkpoint_id": checkpoint_id,
            "metrics": {
                "provider": self.provider_name,
                "operation": "checkpoint_restore",
                "duration_ms": total_ms,
                "lower_chain_depth": len(meta["lower_chain"]),
                "metadata_only": True,
                "process_checkpoint": False,
                "requires_sandbox_restart": True,
                "details": json.loads(result.stdout.strip().splitlines()[-1]),
            },
        }

    def run(self, name: str, command: list[str]) -> CommandResult:
        if not command:
            raise SystemExit("local-overlay run requires a command")
        meta = self.load(name)
        lowerdir = self._lowerdir(meta.get("lower_chain", []))
        script = f"""
set -eu
mkdir -p /demo/base /demo/layers /demo/active/upper /demo/active/work /demo/active/merged
mount -t overlay overlay -o lowerdir={shlex.quote(lowerdir)},upperdir=/demo/active/upper,workdir=/demo/active/work /demo/active/merged
set +e
cd /demo/active/merged
"$@"
rc=$?
cd /
umount /demo/active/merged
exit $rc
""".strip()
        return self._docker(meta["volume"], [], script, args=command, check=False)

    def verify(self, name: str, checkpoint_id: str | None = None) -> dict[str, Any]:
        meta = self.load(name)
        checkpoint_id = checkpoint_id or meta.get("last_checkpoint_id")
        chain = meta.get("checkpoints", {}).get(checkpoint_id, {}).get("lower_chain", meta.get("lower_chain", [])) if checkpoint_id else meta.get("lower_chain", [])
        lowerdir = self._lowerdir(chain)
        script = f"""
set -eu
mkdir -p /demo/active/merged
mount -t overlay overlay -o lowerdir={shlex.quote(lowerdir)},upperdir=/demo/active/upper,workdir=/demo/active/work /demo/active/merged
python - <<'PY2'
import json, os, subprocess
root='/demo/active/merged'
files=dirs=bytes_=0
for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
    dirs += 1
    for f in filenames:
        p=os.path.join(dirpath, f)
        try: st=os.lstat(p)
        except FileNotFoundError: continue
        if os.path.isfile(p):
            files += 1; bytes_ += st.st_size
print(json.dumps({{'files': files, 'dirs': dirs, 'bytes': bytes_}}, sort_keys=True))
PY2
umount /demo/active/merged
""".strip()
        result = self._docker(meta["volume"], [], script, check=True)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def gc(self, name: str) -> dict[str, Any]:
        meta = self.load(name)
        referenced = set()
        for checkpoint in meta.get("checkpoints", {}).values():
            referenced.update(checkpoint.get("lower_chain", []))
        script = """
set -eu
mkdir -p /demo/layers
python - <<'PY2'
import json, os
layers='/demo/layers'
print(json.dumps({'layers': sorted(os.listdir(layers))}, sort_keys=True))
PY2
""".strip()
        result = self._docker(meta["volume"], [], script, check=True)
        existing = set(json.loads(result.stdout.strip().splitlines()[-1]).get("layers", []))
        removable = sorted(existing - referenced)
        if removable:
            rm_script = "set -eu\n" + "\n".join(f"rm -rf /demo/layers/{shlex.quote(layer)}" for layer in removable)
            self._docker(meta["volume"], [], rm_script, check=True)
        return {"cell_id": name, "referenced_layers": sorted(referenced), "removed_layers": removable}

    def doctor(self, name: str) -> dict[str, Any]:
        meta = self.load(name)
        volume = run_cmd(["docker", "volume", "inspect", meta["volume"]], check=False)
        script = """
set -eu
mkdir -p /demo/base /demo/layers /demo/active/upper /demo/active/work /demo/active/merged
python - <<'PY2'
import json, os
print(json.dumps({
  'base_exists': os.path.isdir('/demo/base'),
  'active_upper_exists': os.path.isdir('/demo/active/upper'),
  'active_work_exists': os.path.isdir('/demo/active/work'),
  'active_merged_exists': os.path.isdir('/demo/active/merged'),
  'layers': sorted(os.listdir('/demo/layers')) if os.path.isdir('/demo/layers') else [],
}, sort_keys=True))
PY2
""".strip()
        check = self._docker(meta["volume"], [], script, check=False)
        details = json.loads(check.stdout.strip().splitlines()[-1]) if check.returncode == 0 and check.stdout.strip() else {}
        return {"cell_id": name, "volume_exists": volume.returncode == 0, "helper_rc": check.returncode, "details": details}

    def _lowerdir(self, chain: list[str]) -> str:
        layers = [f"/demo/layers/{item}" for item in chain]
        layers.append("/demo/base")
        return ":".join(layers)

    def _docker(
        self,
        volume: str,
        extra_mounts: list[str],
        script: str,
        *,
        args: list[str] | None = None,
        check: bool = False,
    ) -> CommandResult:
        docker_args = ["docker", "run", "--rm", "--privileged", "-v", f"{volume}:/demo", *extra_mounts, self.image, "sh", "-lc", script, "forkcell-helper"]
        if args:
            docker_args.extend(args)
        return run_cmd(docker_args, check=check)
