from __future__ import annotations

import hashlib
import json
import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class OpenShellRunner(Protocol):
    def __call__(self, args: list[str], *, check: bool = False) -> Any:
        ...


@dataclass(frozen=True)
class CheckpointArtifact:
    path: Path
    sha256: str
    metrics: dict[str, Any]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def artifact_name(checkpoint_id: str) -> str:
    return f"{checkpoint_id}.tgz"


def remote_path(workspace: str, name: str) -> str:
    return f"{workspace.rstrip('/')}/{name}"


class CheckpointProvider(Protocol):
    provider_name: str

    def create(self, *, cell_name: str, workspace: str, checkpoint_id: str) -> CheckpointArtifact:
        ...

    def restore(self, *, cell_name: str, workspace: str, artifact: Path) -> dict[str, Any]:
        ...


class OpenShellTarFullProvider:
    """Portable baseline provider: full workspace tar over OpenShell upload/download."""

    provider_name = "openshell-tar-full"

    def __init__(self, *, openshell: OpenShellRunner, checkpoint_dir: Path) -> None:
        self._openshell = openshell
        self._checkpoint_dir = checkpoint_dir

    def create(self, *, cell_name: str, workspace: str, checkpoint_id: str) -> CheckpointArtifact:
        started = time.perf_counter()
        before_stats = self._workspace_stats(cell_name, workspace)
        remote_name = f".forkcell_{artifact_name(checkpoint_id)}"
        remote_tar = remote_path(workspace, remote_name)
        local_tar = self._checkpoint_dir / artifact_name(checkpoint_id)
        local_tar.parent.mkdir(parents=True, exist_ok=True)

        workspace_q = shlex.quote(workspace)
        remote_name_q = shlex.quote(remote_name)
        remote_tar_q = shlex.quote(remote_tar)
        # GNU tar may return 1 when files change while archiving; keep that as degraded success.
        tar_script = (
            f"tar -C {workspace_q} --exclude {remote_name_q} -czf {remote_tar_q} .; "
            "rc=$?; [ $rc -le 1 ] || exit $rc"
        )
        self._openshell(["sandbox", "exec", "--name", cell_name, "sh", "-lc", tar_script], check=True)
        try:
            self._openshell(["sandbox", "download", cell_name, remote_tar, str(local_tar)], check=True)
        finally:
            self._openshell(["sandbox", "exec", "--name", cell_name, "rm", "-f", remote_tar], check=False)

        duration_ms = int(round((time.perf_counter() - started) * 1000))
        metrics: dict[str, Any] = {
            "provider": self.provider_name,
            "operation": "checkpoint_create",
            "duration_ms": duration_ms,
            "workspace": workspace,
            "workspace_file_count": before_stats.get("file_count"),
            "workspace_bytes": before_stats.get("bytes"),
            "artifact_bytes": local_tar.stat().st_size,
            "local_artifact": str(local_tar),
            "full_archive": True,
        }
        if before_stats.get("error"):
            metrics["workspace_stats_error"] = before_stats["error"]
        return CheckpointArtifact(path=local_tar, sha256=sha256_file(local_tar), metrics=metrics)

    def restore(self, *, cell_name: str, workspace: str, artifact: Path) -> dict[str, Any]:
        started = time.perf_counter()
        if not artifact.exists():
            raise FileNotFoundError(str(artifact))

        remote_name = f".forkcell_restore_{artifact.name}"
        remote_tar = remote_path(workspace, remote_name)
        self._openshell(["sandbox", "upload", cell_name, str(artifact), remote_tar], check=True)

        workspace_q = shlex.quote(workspace)
        remote_name_q = shlex.quote(remote_name)
        remote_tar_q = shlex.quote(remote_tar)
        cleanup_script = (
            f"set -e; mkdir -p {workspace_q}; "
            f"find {workspace_q} -mindepth 1 -maxdepth 1 ! -name {remote_name_q} -exec rm -rf {{}} +; "
            f"tar -C {workspace_q} -xzf {remote_tar_q}; rm -f {remote_tar_q}"
        )
        self._openshell(["sandbox", "exec", "--name", cell_name, "sh", "-lc", cleanup_script], check=True)

        after_stats = self._workspace_stats(cell_name, workspace)
        duration_ms = int(round((time.perf_counter() - started) * 1000))
        metrics: dict[str, Any] = {
            "provider": self.provider_name,
            "operation": "checkpoint_restore",
            "duration_ms": duration_ms,
            "workspace": workspace,
            "workspace_file_count_after": after_stats.get("file_count"),
            "workspace_bytes_after": after_stats.get("bytes"),
            "artifact_bytes": artifact.stat().st_size,
            "local_artifact": str(artifact),
            "full_archive": True,
        }
        if after_stats.get("error"):
            metrics["workspace_stats_error"] = after_stats["error"]
        return metrics

    def _workspace_stats(self, cell_name: str, workspace: str) -> dict[str, Any]:
        workspace_q = shlex.quote(workspace)
        script = (
            f"cd {workspace_q}; "
            "files=$(find . -mindepth 1 -type f | wc -l | tr -d ' '); "
            "bytes=$(find . -mindepth 1 -type f -exec wc -c {} + 2>/dev/null "
            "| awk '{s += $1} END {print s + 0}'); "
            "printf '{\"file_count\":%s,\"bytes\":%s}\\n' \"$files\" \"$bytes\""
        )
        result = self._openshell(["sandbox", "exec", "--name", cell_name, "sh", "-lc", script], check=False)
        if result.returncode != 0:
            return {"file_count": None, "bytes": None, "error": (result.stderr or result.stdout)[-500:]}
        try:
            return json.loads(result.stdout.strip().splitlines()[-1])
        except Exception as exc:
            return {"file_count": None, "bytes": None, "error": f"parse_failed: {exc}"}
