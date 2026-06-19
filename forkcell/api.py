from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


class ForkCellCommandError(RuntimeError):
    def __init__(self, args: list[str], returncode: int, stdout: str, stderr: str) -> None:
        self.args_list = args
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(
            f"forkcell command failed ({returncode}): {' '.join(args)}\nstdout:\n{stdout}\nstderr:\n{stderr}"
        )


@dataclass(frozen=True)
class CommandResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str
    json: dict[str, Any]


def _extract_json_object(text: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and not text[index + end :].strip():
            return value
    raise ValueError("missing JSON object in command output")


def _normalize_command(command: str | Iterable[str]) -> list[str]:
    if isinstance(command, str):
        return ["sh", "-lc", command]
    return [str(part) for part in command]


LEGACY_BACKEND_ALIASES = {
    "openshell-native-overlay": "native-overlay",
    "openshell-layer-clone": "layer-clone",
    "openshell-volume": "volume-delta",
}


def _normalize_backend(backend: str) -> str:
    return LEGACY_BACKEND_ALIASES.get(backend, backend)


class ForkCellClient:
    """Small Python facade for agent-style ForkCell workflows.

    The facade intentionally shells out to `python -m forkcell.cli` so the API
    and CLI share the same state, receipts, and review artifacts during the
    current governed-runtime integration.
    """

    def __init__(
        self,
        *,
        root: str | Path | None = None,
        python: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.root = Path(root or Path.cwd()).resolve()
        self.python = python or sys.executable
        self.env = dict(os.environ)
        if env:
            self.env.update(env)

    def cli(self, args: list[str], *, check: bool = True) -> CommandResult:
        full_args = [self.python, "-m", "forkcell.cli", *args]
        proc = subprocess.run(full_args, cwd=self.root, env=self.env, text=True, capture_output=True)
        parsed: dict[str, Any] = {}
        output = proc.stdout.strip()
        if output:
            try:
                parsed = _extract_json_object(output)
            except ValueError:
                parsed = {}
        if check and proc.returncode != 0:
            raise ForkCellCommandError(full_args, proc.returncode, proc.stdout, proc.stderr)
        return CommandResult(
            args=full_args,
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            json=parsed,
        )

    def create_native_cell(
        self,
        *,
        source: str | Path,
        name: str | None = None,
        backend: str = "native-overlay",
    ) -> "ForkCellSandbox":
        cell = name or f"fc-api-{uuid.uuid4().hex[:8]}"
        self.cli(["native", "init", cell, "--from", str(Path(source).resolve())])
        return ForkCellSandbox(client=self, name=cell, backend=_normalize_backend(backend))

    def native_cell(self, name: str, *, backend: str = "native-overlay") -> "ForkCellSandbox":
        return ForkCellSandbox(client=self, name=name, backend=_normalize_backend(backend))

    def review_status(self) -> dict[str, Any]:
        return self.cli(["review", "status", "--format", "json"]).json


@dataclass
class ForkCellSandbox:
    client: ForkCellClient
    name: str
    backend: str = "native-overlay"
    auto_delete: bool = True

    def __post_init__(self) -> None:
        self.backend = _normalize_backend(self.backend)

    def __enter__(self) -> "ForkCellSandbox":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self.auto_delete:
            self.delete(check=False)

    def status(self) -> dict[str, Any]:
        return self.client.cli(["native", "status", self.name]).json

    def checkpoint(self, *, name: str | None = None) -> dict[str, Any]:
        args = ["native", "checkpoint", self.name]
        if name:
            args.extend(["--name", name])
        return self.client.cli(args).json

    def restore(self, checkpoint: str | None = None) -> dict[str, Any]:
        args = ["native", "restore", self.name]
        if checkpoint:
            args.append(checkpoint)
        return self.client.cli(args).json

    def run(
        self,
        command: str | Iterable[str],
        *,
        checkpoint_before: bool = False,
        checkpoint_name: str | None = None,
        restore_on_fail: bool = False,
        policy: str | Path | None = None,
        logs_since: str = "5m",
    ) -> dict[str, Any]:
        if self.backend == "native-overlay":
            args = ["native", "run"]
        elif self.backend == "layer-clone":
            args = ["native", "run-layer"]
        else:
            args = ["run", self.name, "--backend", self.backend]
        if checkpoint_before:
            args.append("--checkpoint-before")
        if checkpoint_name:
            args.extend(["--checkpoint-name", checkpoint_name])
        if restore_on_fail:
            args.append("--restore-on-fail")
        if policy:
            args.extend(["--policy", str(policy)])
        if logs_since:
            args.extend(["--logs-since", logs_since])
        if self.backend in {"native-overlay", "layer-clone"}:
            args.append(self.name)
        args.extend(["--", *_normalize_command(command)])
        run = self.client.cli(args).json
        receipt = Path(run.get("receipt", ""))
        if receipt.exists():
            return json.loads(receipt.read_text())
        return run

    def delete(self, *, check: bool = True) -> dict[str, Any]:
        return self.client.cli(["native", "delete", self.name], check=check).json
