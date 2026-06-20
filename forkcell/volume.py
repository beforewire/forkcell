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


class DockerVolumeWorkspaceProvider:
    provider_name = "volume-delta"
    image = "python:3.13-slim"

    def __init__(self, *, root: Path) -> None:
        self.root = root
        self.cells_dir = root / "volume" / "cells"
        self.checkpoint_dir = root / "volume" / "checkpoints"
        self.cells_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)

    def _cell_dir(self, name: str) -> Path:
        return self.cells_dir / name

    def _metadata_path(self, name: str) -> Path:
        return self._cell_dir(name) / "metadata.json"

    def load(self, name: str) -> dict[str, Any]:
        path = self._metadata_path(name)
        if not path.exists():
            raise SystemExit(f"unknown volume cell: {name}")
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
            raise SystemExit(f"volume cell already exists: {name}")
        source = source.resolve()
        if not source.is_dir():
            raise SystemExit(f"source directory not found: {source}")
        volume = f"forkcell-work-{name}-{uuid.uuid4().hex[:8]}"
        run_cmd(["docker", "volume", "create", volume], check=True)
        script = f"""
set -eu
mkdir -p /data/work /data/store/objects /data/checkpoints
(cd /src && tar --exclude='./.env' --exclude='./.env.*' --exclude='*/.env' --exclude='*/.env.*' --exclude='./.ssh' --exclude='./.aws' --exclude='*.pem' --exclude='*.key' -cf - .) | tar -xf - -C /data/work
chown -R -h {SANDBOX_UID}:{SANDBOX_GID} /data/work
python - <<'PY2'
import json, os, stat
root='/data/work'
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
            "workspace": "/sandbox/work",
            "workspace_subpath": "work",
            "store_subpath": "store",
            "checkpoint_subpath": "checkpoints",
            "created_at_ms": now_ms(),
            "base_stats": stats,
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
        try:
            self._cell_dir(name).rmdir()
        except OSError:
            pass
        return {"name": name, "volume": meta["volume"], "docker_rc": result.returncode, "stderr": result.stderr.strip()}

    def status(self, name: str) -> dict[str, Any]:
        meta = self.load(name)
        volume_result = run_cmd(["docker", "volume", "inspect", meta["volume"]], check=False)
        meta = dict(meta)
        meta["docker_volume_exists"] = volume_result.returncode == 0
        meta["checkpoint_count"] = len(meta.get("checkpoints", {}))
        return meta

    def checkpoint(self, name: str, *, label: str | None = None, strict: bool = False) -> dict[str, Any]:
        meta = self.load(name)
        checkpoint_id = f"chk_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        script = self._cas_checkpoint_script(checkpoint_id, strict=strict)
        started = time.perf_counter()
        result = self._docker(meta["volume"], script, check=True)
        duration_ms = int(round((time.perf_counter() - started) * 1000))
        stats = json.loads(result.stdout.strip().splitlines()[-1])
        checkpoint = {
            "checkpoint_id": checkpoint_id,
            "label": label,
            "provider": self.provider_name,
            "manifest": f"checkpoints/{checkpoint_id}/manifest.json",
            "sha256": stats.get("manifest_sha256"),
            "created_at_ms": now_ms(),
            "metrics": {
                "provider": self.provider_name,
                "operation": "checkpoint_create",
                "duration_ms": duration_ms,
                "files": stats.get("files"),
                "dirs": stats.get("dirs"),
                "bytes": stats.get("bytes"),
                "symlinks": stats.get("symlinks"),
                "manifest_bytes": stats.get("manifest_bytes"),
                "manifest_sha256": stats.get("manifest_sha256"),
                "store_object_count": stats.get("store_object_count"),
                "store_bytes": stats.get("store_bytes"),
                "hashed_files": stats.get("hashed_files"),
                "reused_files": stats.get("reused_files"),
                "new_objects": stats.get("new_objects"),
                "copied_object_bytes": stats.get("copied_object_bytes"),
                "artifact_bytes": stats.get("store_bytes"),
                "strict_mode": strict,
                "metadata_only": False,
                "process_checkpoint": False,
                "requires_sandbox_restart": True,
            },
        }
        meta["checkpoints"][checkpoint_id] = checkpoint
        meta["last_checkpoint_id"] = checkpoint_id
        self.save(name, meta)
        return checkpoint

    def restore(self, name: str, checkpoint_id: str | None = None) -> dict[str, Any]:
        meta = self.load(name)
        checkpoint_id = checkpoint_id or meta.get("last_checkpoint_id")
        if not checkpoint_id:
            raise SystemExit("no checkpoint specified and volume cell has no last checkpoint")
        checkpoint = meta.get("checkpoints", {}).get(checkpoint_id)
        if not checkpoint:
            raise SystemExit(f"unknown volume checkpoint: {checkpoint_id}")
        script = self._cas_restore_script(checkpoint_id)
        started = time.perf_counter()
        result = self._docker(meta["volume"], script, check=True)
        duration_ms = int(round((time.perf_counter() - started) * 1000))
        details = json.loads(result.stdout.strip().splitlines()[-1])
        meta["last_checkpoint_id"] = checkpoint_id
        self.save(name, meta)
        return {
            "restored_at_ms": now_ms(),
            "cell_id": name,
            "checkpoint_id": checkpoint_id,
            "metrics": {
                "provider": self.provider_name,
                "operation": "checkpoint_restore",
                "duration_ms": duration_ms,
                "metadata_only": False,
                "process_checkpoint": False,
                "requires_sandbox_restart": True,
                "details": details,
            },
        }

    def verify(self, name: str) -> dict[str, Any]:
        meta = self.load(name)
        script = """
set -eu
python - <<'PY2'
import json, os, stat
root='/data/work'
files=dirs=bytes_=0
sample=[]
for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
    dirs += 1
    for f in filenames:
        p=os.path.join(dirpath, f)
        rel=os.path.relpath(p, root)
        if len(sample) < 50:
            sample.append(rel)
        try: st=os.lstat(p)
        except FileNotFoundError: continue
        if stat.S_ISREG(st.st_mode):
            files += 1
            bytes_ += st.st_size
print(json.dumps({'files': files, 'dirs': dirs, 'bytes': bytes_, 'sample': sorted(sample)}, sort_keys=True))
PY2
""".strip()
        result = self._docker(meta["volume"], script, check=True)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def driver_config_json(self, name: str) -> str:
        meta = self.load(name)
        return json.dumps(
            {
                "docker": {
                    "mounts": [
                        {
                            "type": "volume",
                            "source": meta["volume"],
                            "target": meta["workspace"],
                            "read_only": False,
                            "subpath": meta.get("workspace_subpath", "work"),
                        }
                    ]
                }
            },
            separators=(",", ":"),
        )

    def _docker(
        self,
        volume: str,
        script: str,
        *,
        extra_mounts: list[str] | None = None,
        check: bool = False,
    ) -> CommandResult:
        mounts = ["-v", f"{volume}:/data"]
        if extra_mounts:
            mounts.extend(extra_mounts)
        return run_cmd(["docker", "run", "--rm", *mounts, self.image, "sh", "-lc", script], check=check)

    def _cas_checkpoint_script(self, checkpoint_id: str, *, strict: bool) -> str:
        strict_literal = "True" if strict else "False"
        return f"""
set -eu
mkdir -p /data/work /data/store/objects /data/checkpoints/{checkpoint_id}
python - <<'PY2'
import hashlib, json, os, shutil, stat
from pathlib import Path

uid={SANDBOX_UID}
gid={SANDBOX_GID}
root=Path('/data/work')
store=Path('/data/store')
objects=store/'objects'
cache_path=store/'cache.json'
checkpoint=Path('/data/checkpoints/{checkpoint_id}')
manifest_path=checkpoint/'manifest.json'
strict_mode={strict_literal}

try:
    cache=json.loads(cache_path.read_text())
except FileNotFoundError:
    cache={{}}

entries=[]
new_cache={{}}
stats={{'files': 0, 'dirs': 0, 'symlinks': 0, 'bytes': 0, 'hashed_files': 0, 'reused_files': 0, 'new_objects': 0, 'copied_object_bytes': 0}}

def object_path(sha):
    return objects/sha[:2]/sha

def hash_file(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()

for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
    current=Path(dirpath)
    rel_dir=current.relative_to(root)
    if rel_dir != Path('.'):
        st=os.lstat(current)
        entries.append({{'type': 'dir', 'path': rel_dir.as_posix(), 'mode': stat.S_IMODE(st.st_mode)}})
        stats['dirs'] += 1
    names=list(dirnames) + list(filenames)
    for name in names:
        path=current/name
        try:
            st=os.lstat(path)
        except FileNotFoundError:
            continue
        rel=path.relative_to(root).as_posix()
        mode=stat.S_IMODE(st.st_mode)
        if stat.S_ISLNK(st.st_mode):
            entries.append({{'type': 'symlink', 'path': rel, 'target': os.readlink(path)}})
            stats['symlinks'] += 1
        elif stat.S_ISREG(st.st_mode):
            cache_entry=cache.get(rel)
            if (
                not strict_mode
                and
                cache_entry
                and cache_entry.get('size') == st.st_size
                and cache_entry.get('mtime_ns') == st.st_mtime_ns
                and cache_entry.get('ctime_ns') == st.st_ctime_ns
                and cache_entry.get('mode') == mode
                and object_path(cache_entry.get('sha256', '')).exists()
            ):
                sha=cache_entry['sha256']
                stats['reused_files'] += 1
            else:
                sha=hash_file(path)
                stats['hashed_files'] += 1
                obj=object_path(sha)
                if not obj.exists():
                    obj.parent.mkdir(parents=True, exist_ok=True)
                    tmp=obj.with_suffix('.tmp')
                    shutil.copyfile(path, tmp)
                    os.chmod(tmp, 0o444)
                    os.replace(tmp, obj)
                    stats['new_objects'] += 1
                    stats['copied_object_bytes'] += st.st_size
            entries.append({{'type': 'file', 'path': rel, 'sha256': sha, 'size': st.st_size, 'mode': mode}})
            new_cache[rel]={{'sha256': sha, 'size': st.st_size, 'mtime_ns': st.st_mtime_ns, 'ctime_ns': st.st_ctime_ns, 'mode': mode}}
            stats['files'] += 1
            stats['bytes'] += st.st_size

manifest={{'checkpoint_id': '{checkpoint_id}', 'entries': sorted(entries, key=lambda x: (x['path'], x['type']))}}
checkpoint.mkdir(parents=True, exist_ok=True)
manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(',', ':')))
cache_path.write_text(json.dumps(new_cache, sort_keys=True, separators=(',', ':')))

store_bytes=0
store_count=0
for dirpath, dirnames, filenames in os.walk(objects, followlinks=False):
    for name in filenames:
        p=Path(dirpath)/name
        try:
            store_bytes += p.stat().st_size
            store_count += 1
        except FileNotFoundError:
            pass
stats['dirs'] += 1  # workspace root
stats['manifest_bytes']=manifest_path.stat().st_size
stats['manifest_sha256']=hashlib.sha256(manifest_path.read_bytes()).hexdigest()
stats['store_bytes']=store_bytes
stats['store_object_count']=store_count
print(json.dumps(stats, sort_keys=True))
PY2
""".strip()

    def _cas_restore_script(self, checkpoint_id: str) -> str:
        return f"""
set -eu
python - <<'PY2'
import hashlib, json, os, shutil, stat
from pathlib import Path

uid={SANDBOX_UID}
gid={SANDBOX_GID}
root=Path('/data/work')
objects=Path('/data/store/objects')
cache_path=Path('/data/store/cache.json')
manifest_path=Path('/data/checkpoints/{checkpoint_id}/manifest.json')
if not manifest_path.exists():
    raise SystemExit(f'manifest not found: {{manifest_path}}')
manifest=json.loads(manifest_path.read_text())

root.mkdir(parents=True, exist_ok=True)

entries=manifest['entries']
dirs={{e['path']: e for e in entries if e['type'] == 'dir'}}
files={{e['path']: e for e in entries if e['type'] == 'file'}}
symlinks={{e['path']: e for e in entries if e['type'] == 'symlink'}}
desired=set(dirs) | set(files) | set(symlinks)

try:
    old_cache=json.loads(cache_path.read_text())
except FileNotFoundError:
    old_cache={{}}

removed_paths=0
current_paths=[]
for dirpath, dirnames, filenames in os.walk(root, topdown=False, followlinks=False):
    current=Path(dirpath)
    for name in filenames:
        current_paths.append(current/name)
    for name in dirnames:
        current_paths.append(current/name)

for path in current_paths:
    try:
        rel=path.relative_to(root).as_posix()
        st=os.lstat(path)
    except FileNotFoundError:
        continue
    if rel in desired:
        continue
    if stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode):
        shutil.rmtree(path)
    else:
        os.unlink(path)
    removed_paths += 1

for entry in sorted(dirs.values(), key=lambda e: e['path'].count('/')):
    path=root/entry['path']
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, entry.get('mode', 0o755))
    os.chown(path, uid, gid)

def object_path(sha):
    return objects/sha[:2]/sha

def hash_file(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()

def current_file_matches(path, entry):
    try:
        st=os.lstat(path)
    except FileNotFoundError:
        return False, None, False
    mode=stat.S_IMODE(st.st_mode)
    if not stat.S_ISREG(st.st_mode) or mode != entry.get('mode', 0o644) or st.st_size != entry.get('size'):
        return False, st, False
    cached=old_cache.get(entry['path'])
    if (
        cached
        and cached.get('sha256') == entry['sha256']
        and cached.get('size') == st.st_size
        and cached.get('mtime_ns') == st.st_mtime_ns
        and cached.get('ctime_ns') == st.st_ctime_ns
        and cached.get('mode') == mode
    ):
        return True, st, False
    return hash_file(path) == entry['sha256'], st, True

cache={{}}
copied_bytes=0
copied_files=0
reused_files=0
hashed_files=0
for entry in files.values():
    path=root/entry['path']
    path.parent.mkdir(parents=True, exist_ok=True)
    sha=entry['sha256']
    matches, st, hashed = current_file_matches(path, entry)
    if hashed:
        hashed_files += 1
    if matches:
        reused_files += 1
    else:
        obj=object_path(sha)
        if not obj.exists():
            raise SystemExit(f'object missing: {{sha}}')
        if path.exists() or path.is_symlink():
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                os.unlink(path)
        shutil.copyfile(obj, path)
        os.chmod(path, entry.get('mode', 0o644))
        os.chown(path, uid, gid)
        copied_files += 1
        copied_bytes += entry.get('size', 0)
    st=os.lstat(path)
    cache[entry['path']]={{'sha256': sha, 'size': st.st_size, 'mtime_ns': st.st_mtime_ns, 'ctime_ns': st.st_ctime_ns, 'mode': stat.S_IMODE(st.st_mode)}}

replaced_symlinks=0
for entry in symlinks.values():
    path=root/entry['path']
    path.parent.mkdir(parents=True, exist_ok=True)
    replace=True
    if path.is_symlink():
        try:
            replace=os.readlink(path) != entry['target']
        except OSError:
            replace=True
    if replace:
        if path.exists() or path.is_symlink():
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                os.unlink(path)
        os.symlink(entry['target'], path)
        replaced_symlinks += 1
    try:
        os.lchown(path, uid, gid)
    except AttributeError:
        pass

os.chown(root, uid, gid)
cache_path.write_text(json.dumps(cache, sort_keys=True, separators=(',', ':')))

files_count=len(files)
dirs_count=len(dirs) + 1
symlink_count=len(symlinks)
bytes_=sum(entry.get('size', 0) for entry in files.values())
print(json.dumps({{
    'files': files_count,
    'dirs': dirs_count,
    'symlinks': symlink_count,
    'bytes': bytes_,
    'copied_files': copied_files,
    'copied_bytes': copied_bytes,
    'reused_files': reused_files,
    'hashed_files': hashed_files,
    'removed_paths': removed_paths,
    'replaced_symlinks': replaced_symlinks,
}}, sort_keys=True))
PY2
""".strip()
