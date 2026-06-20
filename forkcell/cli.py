from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from forkcell.checkpoint import OpenShellTarFullProvider
from forkcell.native import OpenShellNativeOverlayProvider
from forkcell.overlay import DockerOverlayProvider
from forkcell.volume import DockerVolumeWorkspaceProvider

ROOT = Path.cwd()
STATE_DIR = ROOT / ".forkcell"
STATE_PATH = STATE_DIR / "state.json"
CHECKPOINT_DIR = STATE_DIR / "checkpoints"
RECEIPT_DIR = STATE_DIR / "receipts"
ARTIFACT_DIR = STATE_DIR / "artifacts"
EVENT_DIR = STATE_DIR / "events"
EVENT_STORE_PATH = EVENT_DIR / "events.jsonl"
EVENT_DB_PATH = EVENT_DIR / "events.sqlite3"
OVERLAY_DIR = STATE_DIR / "overlay"
VOLUME_DIR = STATE_DIR / "volume"
NATIVE_DIR = STATE_DIR / "native"
RUNTIME_DIR = STATE_DIR / "runtime"
RUNTIME_LOCK_PATH = STATE_DIR / "runtime-lock.json"
EVIDENCE_DIR = ROOT / "docs" / "evidence"
DEFAULT_WORKSPACE = "/sandbox"

BACKEND_NATIVE_OVERLAY = "native-overlay"
BACKEND_LAYER_CLONE = "layer-clone"
BACKEND_VOLUME_DELTA = "volume-delta"
BACKEND_LOCAL_OVERLAY = "local-overlay"
BACKEND_OPENSHELL_DIRECT = "openshell"

LEGACY_BACKEND_ALIASES = {
    "openshell-native-overlay": BACKEND_NATIVE_OVERLAY,
    "openshell-layer-clone": BACKEND_LAYER_CLONE,
    "openshell-volume": BACKEND_VOLUME_DELTA,
}


def normalize_backend_name(name: str | None) -> str:
    if not name:
        return "auto"
    return LEGACY_BACKEND_ALIASES.get(name, name)


def requested_backend_name(args: argparse.Namespace, default: str) -> str:
    return normalize_backend_name(getattr(args, "requested_backend", getattr(args, "backend", default)))


@dataclass
class CmdResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def metric_lt(value: Any, limit: int | float) -> bool:
    return isinstance(value, (int, float)) and value < limit


def metadata_sha256(payload: dict[str, Any]) -> str:
    return sha256_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def native_checkpoint_sha256(checkpoint: dict[str, Any], *, policy_revision: str | None) -> str:
    return metadata_sha256(
        {
            "checkpoint_id": checkpoint.get("checkpoint_id"),
            "provider": checkpoint.get("provider"),
            "layer": checkpoint.get("layer"),
            "parent": checkpoint.get("parent"),
            "forked_from": checkpoint.get("forked_from"),
            "policy_revision": policy_revision,
        }
    )


def ensure_dirs() -> None:
    for path in (STATE_DIR, CHECKPOINT_DIR, RECEIPT_DIR, ARTIFACT_DIR, EVENT_DIR, OVERLAY_DIR, VOLUME_DIR, NATIVE_DIR, RUNTIME_DIR):
        path.mkdir(parents=True, exist_ok=True)


def load_state() -> dict[str, Any]:
    ensure_dirs()
    if not STATE_PATH.exists():
        return ensure_state_shape({})
    return ensure_state_shape(json.loads(STATE_PATH.read_text()))



def ensure_state_shape(state: dict[str, Any]) -> dict[str, Any]:
    state.setdefault("schema_version", "0.1")
    state.setdefault("cells", {})
    state.setdefault("overlay_cells", {})
    state.setdefault("volume_cells", {})
    state.setdefault("native_cells", {})
    state.setdefault("checkpoints", {})
    state.setdefault("runs", {})
    state.setdefault("policies", {})
    state.setdefault("decisions", {})
    state.setdefault("checkpoint_graph", {"nodes": {}})
    return state


def save_state(state: dict[str, Any]) -> None:
    ensure_state_shape(state)
    ensure_dirs()
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(STATE_PATH)


def run_cmd(args: list[str], *, check: bool = False, input_text: str | None = None) -> CmdResult:
    proc = subprocess.run(args, input=input_text, text=True, capture_output=True)
    result = CmdResult(args=args, returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)
    if check and proc.returncode != 0:
        raise RuntimeError(format_cmd_result(result))
    return result


def format_cmd_result(result: CmdResult) -> str:
    return (
        f"command failed ({result.returncode}): {' '.join(result.args)}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_runtime_lock() -> dict[str, Any] | None:
    if not RUNTIME_LOCK_PATH.exists():
        return None
    try:
        return json.loads(RUNTIME_LOCK_PATH.read_text())
    except json.JSONDecodeError:
        return None


def locked_runtime_binary(name: str) -> Path | None:
    lock = load_runtime_lock()
    if not lock:
        return None
    item = (lock.get("binaries") or {}).get(name) or {}
    path = Path(item.get("path", ""))
    if path.exists() and os.access(path, os.X_OK):
        return path
    return None


def openshell_bin() -> str:
    explicit = os.environ.get("FORKCELL_OPENSHELL_BIN") or os.environ.get("FORKCELL_OPENSHELL")
    if explicit:
        return explicit
    locked = locked_runtime_binary("openshell")
    return str(locked) if locked else "openshell"


def openshell(args: list[str], *, check: bool = False) -> CmdResult:
    return run_cmd([openshell_bin(), *args], check=check)


def require_openshell() -> str:
    binary = openshell_bin()
    if shutil.which(binary) is None and not Path(binary).is_file():
        raise RuntimeError(f"openshell CLI not found: {binary}")
    result = openshell(["--version"], check=True)
    return result.stdout.strip() or result.stderr.strip()


def cell_or_error(state: dict[str, Any], name: str) -> dict[str, Any]:
    try:
        return state["cells"][name]
    except KeyError as exc:
        raise SystemExit(f"unknown cell: {name}") from exc


def parse_sandbox_id(get_output: str) -> str | None:
    match = re.search(r"Id:\s*([0-9a-fA-F-]{36})", strip_ansi(get_output))
    return match.group(1) if match else None


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def effective_openshell_exit_code(result: CmdResult) -> int:
    plain = strip_ansi((result.stdout or "") + "\n" + (result.stderr or ""))
    matches = re.findall(r"exit status:\s*(\d+)", plain)
    if matches:
        return int(matches[-1])
    return result.returncode


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def checkpoint_provider() -> OpenShellTarFullProvider:
    return OpenShellTarFullProvider(openshell=openshell, checkpoint_dir=CHECKPOINT_DIR)


def overlay_provider() -> DockerOverlayProvider:
    return DockerOverlayProvider(root=STATE_DIR)


def volume_provider() -> DockerVolumeWorkspaceProvider:
    return DockerVolumeWorkspaceProvider(root=STATE_DIR)


def native_provider() -> OpenShellNativeOverlayProvider:
    return OpenShellNativeOverlayProvider(root=STATE_DIR)


def graph_node_id(cell_id: str, checkpoint_id: str) -> str:
    return f"{cell_id}:{checkpoint_id}"


def record_checkpoint_graph_node(
    state: dict[str, Any],
    *,
    cell_id: str,
    checkpoint_id: str,
    label: str | None,
    backend: str,
    parent_checkpoint_id: str | None = None,
    parent_node_id: str | None = None,
    forked_from: dict[str, str] | None = None,
) -> dict[str, Any]:
    graph = state.setdefault("checkpoint_graph", {"nodes": {}})
    nodes = graph.setdefault("nodes", {})
    node_id = graph_node_id(cell_id, checkpoint_id)
    if parent_node_id is None and parent_checkpoint_id:
        parent_node_id = graph_node_id(cell_id, parent_checkpoint_id)
    if parent_node_id and parent_node_id not in nodes:
        parent_node_id = None
    node = nodes.get(node_id, {})
    node.update(
        {
            "node_id": node_id,
            "cell_id": cell_id,
            "checkpoint_id": checkpoint_id,
            "label": label,
            "backend": backend,
            "parent_node_id": parent_node_id,
            "forked_from": forked_from,
            "created_at": node.get("created_at") or now_iso(),
            "children": sorted(set(node.get("children", []))),
        }
    )
    nodes[node_id] = node
    if parent_node_id:
        parent = nodes.setdefault(
            parent_node_id,
            {
                "node_id": parent_node_id,
                "children": [],
            },
        )
        parent["children"] = sorted(set(parent.get("children", []) + [node_id]))
    return node


def checkpoint_graph_view(state: dict[str, Any]) -> dict[str, Any]:
    nodes = state.setdefault("checkpoint_graph", {"nodes": {}}).setdefault("nodes", {})
    return {
        "node_count": len(nodes),
        "nodes": dict(sorted(nodes.items())),
        "roots": sorted(node_id for node_id, node in nodes.items() if not node.get("parent_node_id")),
    }


def summarize_logs(logs: str) -> dict[str, Any]:
    plain = strip_ansi(logs)
    return {
        "allowed_events": len(re.findall(r"\bALLOWED\b", plain)),
        "denied_events": len(re.findall(r"\bDENIED\b|policy_denied|HTTP/1\.1 403 Forbidden|CONNECT tunnel failed", plain)),
        "ocsf_events": len(re.findall(r"\[OCSF \]", plain)),
        "net_events": len(re.findall(r"\bNET:", plain)),
        "http_events": len(re.findall(r"\bHTTP:", plain)),
    }


def extract_openshell_events(logs: str, *, limit: int | None = 80) -> list[dict[str, Any]]:
    plain = strip_ansi(logs)
    events: list[dict[str, Any]] = []
    pattern = re.compile(
        r"^\[(?P<timestamp>[^\]]+)\] \[(?P<source>[^\]]+)\] \[OCSF \] "
        r"\[ocsf\] (?P<category>[A-Z_]+):(?P<action>[A-Z_]+) "
        r"\[(?P<severity>[^\]]+)\] ?(?P<message>.*)$"
    )
    for line in plain.splitlines():
        match = pattern.match(line)
        if not match:
            continue
        event = match.groupdict()
        message = event.get("message", "")
        decision = None
        if re.search(r"\bDENIED\b|policy_denied", message):
            decision = "denied"
        elif re.search(r"\bALLOWED\b", message):
            decision = "allowed"
        event["decision"] = decision
        event["raw_sha256"] = sha256_text(line)
        event["preview"] = message[:500]
        events.append(event)
        if limit is not None and len(events) >= limit:
            break
    return events


def extract_policy_signals(logs: str) -> list[dict[str, str]]:
    plain = strip_ansi(logs)
    signals: list[dict[str, str]] = []
    for label, pattern in (
        ("http_403", r"HTTP/1\.1 403 Forbidden"),
        ("connect_tunnel_failed", r"CONNECT tunnel failed"),
        ("policy_denied", r"policy_denied"),
        ("denied", r"\bDENIED\b"),
    ):
        if re.search(pattern, plain):
            signals.append({"type": label, "sha256": sha256_text(label)})
    return signals


def collect_openshell_logs(sandbox_name: str, since: str, *, wait_for_ocsf: bool = True) -> str:
    logs = openshell(["logs", sandbox_name, "-n", "1000", "--since", since], check=False)
    text = (logs.stdout or "") + (logs.stderr or "")
    if wait_for_ocsf and "[OCSF ]" not in strip_ansi(text):
        time.sleep(0.25)
        logs = openshell(["logs", sandbox_name, "-n", "1000", "--since", since], check=False)
        text = (logs.stdout or "") + (logs.stderr or "")
    return text


def write_openshell_event_artifact(run_id: str, event_text: str) -> dict[str, Any]:
    events = extract_openshell_events(event_text, limit=None)
    signals = extract_policy_signals(event_text)
    path = ARTIFACT_DIR / f"{run_id}-openshell-events.jsonl"
    with path.open("w") as f:
        for event in events:
            f.write(json.dumps({"kind": "ocsf", **event}, sort_keys=True) + "\n")
        for signal in signals:
            f.write(json.dumps({"kind": "policy_signal", **signal}, sort_keys=True) + "\n")
    return {
        "path": str(path),
        "ocsf_event_count": len(events),
        "policy_signal_count": len(signals),
        "sha256": sha256_file(path),
    }


def append_event_store(
    *,
    run_id: str,
    receipt_id: str,
    cell_id: str,
    runtime: str,
    event_artifact: dict[str, Any],
) -> dict[str, Any]:
    ensure_dirs()
    source_path = Path(event_artifact["path"])
    appended = 0
    indexed = 0
    stored_at = now_iso()
    enriched_events: list[dict[str, Any]] = []
    with EVENT_STORE_PATH.open("a") as out:
        if source_path.exists():
            for index, line in enumerate(source_path.read_text().splitlines()):
                if not line.strip():
                    continue
                event = json.loads(line)
                event.update(
                    {
                        "cell_id": cell_id,
                        "event_index": index,
                        "receipt_id": receipt_id,
                        "run_id": run_id,
                        "runtime": runtime,
                        "stored_at": stored_at,
                    }
                )
                out.write(json.dumps(event, sort_keys=True) + "\n")
                enriched_events.append(event)
                appended += 1
    if not EVENT_STORE_PATH.exists():
        EVENT_STORE_PATH.touch()
    if enriched_events:
        indexed = index_event_store_rows(enriched_events)
    else:
        init_event_db()
    return {
        "path": str(EVENT_STORE_PATH),
        "sqlite_path": str(EVENT_DB_PATH),
        "format": "forkcell-events-jsonl-sqlite-v1",
        "appended_count": appended,
        "indexed_count": indexed,
        "sqlite_row_count": event_store_db_count(),
        "sha256": sha256_file(EVENT_STORE_PATH),
    }


def init_event_db() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(EVENT_DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            receipt_id TEXT NOT NULL,
            cell_id TEXT NOT NULL,
            runtime TEXT NOT NULL,
            kind TEXT,
            category TEXT,
            decision TEXT,
            event_index INTEGER NOT NULL,
            stored_at TEXT NOT NULL,
            event_json TEXT NOT NULL,
            UNIQUE(run_id, event_index, kind)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_cell ON events(cell_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_receipt ON events(receipt_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_category ON events(category)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_decision ON events(decision)")
    return conn


def event_db_row(event: dict[str, Any]) -> tuple[Any, ...]:
    return (
        event.get("run_id"),
        event.get("receipt_id"),
        event.get("cell_id"),
        event.get("runtime"),
        event.get("kind"),
        event.get("category") or event.get("class_name") or event.get("type"),
        event.get("decision") or event.get("action"),
        event.get("event_index"),
        event.get("stored_at"),
        json.dumps(event, sort_keys=True),
    )


def index_event_store_rows(events: list[dict[str, Any]]) -> int:
    conn = init_event_db()
    try:
        before = conn.total_changes
        conn.executemany(
            """
            INSERT OR IGNORE INTO events
              (run_id, receipt_id, cell_id, runtime, kind, category, decision, event_index, stored_at, event_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [event_db_row(event) for event in events],
        )
        conn.commit()
        return conn.total_changes - before
    finally:
        conn.close()


def event_store_db_count() -> int:
    conn = init_event_db()
    try:
        row = conn.execute("SELECT COUNT(*) FROM events").fetchone()
        return int(row[0] if row else 0)
    finally:
        conn.close()


def new_decision_id() -> str:
    return f"dec_{int(time.time())}_{uuid.uuid4().hex[:8]}"


def decision_artifact_path(decision_id: str) -> Path:
    return ARTIFACT_DIR / f"{decision_id}.json"


def store_decision(state: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    decision_id = decision["decision_id"]
    path = Path(decision.get("artifact") or decision_artifact_path(decision_id))
    decision["artifact"] = str(path)
    path.write_text(json.dumps(decision, indent=2, sort_keys=True) + "\n")
    state["decisions"][decision_id] = decision
    return decision


def checkpoint_receipt_bindings(
    *,
    checkpoint_id: str | None,
    checkpoint_sha256: str | None,
    receipt: dict[str, Any] | None,
) -> dict[str, Any]:
    receipt_checkpoint_id = (receipt or {}).get("checkpoints", {}).get("before")
    receipt_checkpoint_sha = (receipt or {}).get("bindings", {}).get("checkpoint_sha256")
    return {
        "receipt_checkpoint_id": receipt_checkpoint_id,
        "receipt_checkpoint_sha256": receipt_checkpoint_sha,
        "restore_checkpoint_matches_receipt": receipt_checkpoint_id in (None, checkpoint_id),
        "checkpoint_sha256_matches_receipt": receipt_checkpoint_sha in (None, checkpoint_sha256),
    }


def volume_restore_decision_record(
    *,
    decision_id: str,
    cell_id: str,
    run_id: str | None,
    receipt_id: str | None,
    receipt: dict[str, Any] | None,
    checkpoint: dict[str, Any],
    result: str,
    reason: str,
    restore_metrics: dict[str, Any] | None,
    automatic: bool,
) -> dict[str, Any]:
    checkpoint_id = checkpoint.get("checkpoint_id")
    checkpoint_sha = checkpoint.get("sha256")
    return {
        "decision_id": decision_id,
        "cell_id": cell_id,
        "run_id": run_id,
        "receipt_id": receipt_id,
        "receipt_sha256": (receipt or {}).get("hashes", {}).get("receipt_sha256"),
        "checkpoint_id": checkpoint_id,
        "checkpoint_sha256": checkpoint_sha,
        "checkpoint_policy_revision": checkpoint.get("policy_revision"),
        "policy_revision": (receipt or {}).get("policy", {}).get("revision") or checkpoint.get("policy_revision"),
        "result": result,
        "reason": reason,
        "automatic": automatic,
        "restore_metrics": restore_metrics,
        "bindings": checkpoint_receipt_bindings(
            checkpoint_id=checkpoint_id,
            checkpoint_sha256=checkpoint_sha,
            receipt=receipt,
        ),
        "created_at": now_iso(),
    }


def write_markdown_receipt(receipt: dict[str, Any], path: Path) -> None:
    openshell_info = receipt.get("openshell") or {}
    capabilities = receipt.get("capabilities") or {}
    runtime_name = "openshell" if openshell_info else "local"
    checkpoint_metrics = receipt.get("checkpoints", {}).get("before_metrics") or {}
    restore_metrics = receipt.get("checkpoints", {}).get("restore_metrics") or {}
    filesystem = receipt.get("files", {}).get("checkpoint_restore_summary") or {}
    checkpoint_delta = filesystem.get("checkpoint") or {}
    restore_delta = filesystem.get("restore") or {}
    lines = [
        f"# ForkCell Receipt {receipt['receipt_id']}",
        "",
        f"- Cell: `{receipt['cell_id']}`",
        f"- Sandbox: `{openshell_info.get('sandbox_id')}`",
        f"- Run: `{receipt['run']['run_id']}`",
        f"- Backend: `{capabilities.get('runtime')}`",
        f"- Runtime: `{runtime_name}`",
        f"- Command: `{' '.join(receipt['run']['command'])}`",
        f"- Exit code: `{receipt['run']['exit_code']}`",
        f"- Policy revision: `{receipt.get('policy', {}).get('revision')}`",
        f"- Checkpoint before: `{receipt.get('checkpoints', {}).get('before')}`",
        f"- Checkpoint provider: `{checkpoint_metrics.get('provider')}`",
        f"- Checkpoint duration: `{checkpoint_metrics.get('duration_ms')}` ms",
        f"- Checkpoint strict mode: `{checkpoint_metrics.get('strict_mode')}`",
        f"- Checkpoint hashed/reused/new: `{checkpoint_delta.get('hashed_files')}` / `{checkpoint_delta.get('reused_files')}` / `{checkpoint_delta.get('new_objects')}`",
        f"- Restore duration: `{restore_metrics.get('duration_ms')}` ms",
        f"- Restore copied/reused/removed: `{restore_delta.get('copied_files')}` / `{restore_delta.get('reused_files')}` / `{restore_delta.get('removed_paths')}`",
        f"- Decision: `{receipt.get('decision', {}).get('result')}`",
        "",
        "## Checkpoint Metrics",
        "",
        "```json",
        json.dumps(receipt.get("checkpoints", {}), indent=2, sort_keys=True),
        "```",
        "",
        "## Event Summary",
        "",
        "```json",
        json.dumps(receipt.get("policy", {}).get("events", {}), indent=2, sort_keys=True),
        "```",
        "",
        "## Diff Summary",
        "",
        "```json",
        json.dumps(receipt.get("files", {}), indent=2, sort_keys=True),
        "```",
    ]
    path.write_text("\n".join(lines) + "\n")


def command_cell_create(args: argparse.Namespace) -> None:
    version = require_openshell()
    state = load_state()
    if args.name in state["cells"]:
        raise SystemExit(f"cell already exists: {args.name}")

    create = openshell(
        ["sandbox", "create", "--name", args.name, "--no-auto-providers", "--", "echo", "forkcell-cell-ready"],
        check=True,
    )
    get = openshell(["sandbox", "get", args.name], check=True)
    sandbox_id = parse_sandbox_id(get.stdout)
    state["cells"][args.name] = {
        "cell_id": args.name,
        "sandbox_id": sandbox_id,
        "workspace": args.workspace,
        "created_at": now_iso(),
        "openshell_version": version,
        "active_policy_revision": None,
        "last_checkpoint_id": None,
        "last_run_id": None,
        "phase": "ready",
    }
    save_state(state)
    print(create.stdout, end="")
    print(json.dumps(state["cells"][args.name], indent=2, sort_keys=True))


def command_cell_delete(args: argparse.Namespace) -> None:
    state = load_state()
    cell_or_error(state, args.name)
    result = openshell(["sandbox", "delete", args.name], check=False)
    state["cells"].pop(args.name, None)
    save_state(state)
    print(result.stdout or result.stderr, end="")


def command_cell_inspect(args: argparse.Namespace) -> None:
    state = load_state()
    print(json.dumps(cell_or_error(state, args.name), indent=2, sort_keys=True))


def command_policy_apply(args: argparse.Namespace) -> None:
    state = load_state()
    policy_path = Path(args.policy).resolve()
    policy_hash = sha256_file(policy_path)
    policy_revision = f"policy_{policy_hash[:12]}"
    if args.cell in state["cells"]:
        cell = cell_or_error(state, args.cell)
        result = openshell(["policy", "set", args.cell, "--policy", str(policy_path), "--wait"], check=True)
        applied_to = "openshell-cell"
        deferred_to_run = False
        stdout = result.stdout
    elif args.cell in state["volume_cells"]:
        cell = volume_cell_or_error(state, args.cell)
        applied_to = "volume-delta"
        deferred_to_run = True
        stdout = ""
    elif args.cell in state["native_cells"]:
        cell = native_cell_or_error(state, args.cell)
        applied_to = "native-overlay"
        deferred_to_run = True
        stdout = ""
    else:
        raise SystemExit(f"unknown cell: {args.cell}")
    state["policies"][policy_revision] = {
        "policy_revision": policy_revision,
        "path": str(policy_path),
        "sha256": policy_hash,
        "applied_at": now_iso(),
        "cell_id": args.cell,
        "applied_to": applied_to,
        "deferred_to_run": deferred_to_run,
    }
    cell["active_policy_revision"] = policy_revision
    cell["active_policy_path"] = str(policy_path)
    save_state(state)
    print(stdout, end="")
    print(json.dumps(state["policies"][policy_revision], indent=2, sort_keys=True))


def apply_deferred_native_policy(state: dict[str, Any], cell_name: str, policy: str) -> dict[str, Any]:
    cell = native_cell_or_error(state, cell_name)
    policy_path = Path(policy).resolve()
    policy_hash = sha256_file(policy_path)
    policy_revision = f"policy_{policy_hash[:12]}"
    record = {
        "policy_revision": policy_revision,
        "path": str(policy_path),
        "sha256": policy_hash,
        "applied_at": now_iso(),
        "cell_id": cell_name,
        "applied_to": "native-overlay",
        "deferred_to_run": True,
    }
    state["policies"][policy_revision] = record
    cell["active_policy_revision"] = policy_revision
    cell["active_policy_path"] = str(policy_path)
    save_state(state)
    return record


def apply_deferred_volume_policy(state: dict[str, Any], cell_name: str, policy: str) -> dict[str, Any]:
    cell = volume_cell_or_error(state, cell_name)
    policy_path = Path(policy).resolve()
    policy_hash = sha256_file(policy_path)
    policy_revision = f"policy_{policy_hash[:12]}"
    record = {
        "policy_revision": policy_revision,
        "path": str(policy_path),
        "sha256": policy_hash,
        "applied_at": now_iso(),
        "cell_id": cell_name,
        "applied_to": "volume-delta",
        "deferred_to_run": True,
    }
    state["policies"][policy_revision] = record
    cell["active_policy_revision"] = policy_revision
    cell["active_policy_path"] = str(policy_path)
    save_state(state)
    return record


def create_checkpoint(state: dict[str, Any], cell_name: str, label: str | None) -> dict[str, Any]:
    cell = cell_or_error(state, cell_name)
    checkpoint_id = f"chk_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    workspace = cell.get("workspace", DEFAULT_WORKSPACE)
    provider = checkpoint_provider()
    artifact = provider.create(cell_name=cell_name, workspace=workspace, checkpoint_id=checkpoint_id)

    checkpoint = {
        "checkpoint_id": checkpoint_id,
        "cell_id": cell_name,
        "sandbox_id": cell.get("sandbox_id"),
        "workspace": workspace,
        "label": label,
        "created_at": now_iso(),
        "artifact": str(artifact.path),
        "sha256": artifact.sha256,
        "provider": provider.provider_name,
        "metrics": artifact.metrics,
        "policy_revision": cell.get("active_policy_revision"),
    }
    state["checkpoints"][checkpoint_id] = checkpoint
    cell["last_checkpoint_id"] = checkpoint_id
    save_state(state)
    return checkpoint


def command_checkpoint_create(args: argparse.Namespace) -> None:
    state = load_state()
    checkpoint = create_checkpoint(state, args.cell, args.name)
    print(json.dumps(checkpoint, indent=2, sort_keys=True))


def restore_checkpoint(state: dict[str, Any], cell_name: str, checkpoint_id: str) -> dict[str, Any]:
    cell = cell_or_error(state, cell_name)
    checkpoint = state["checkpoints"].get(checkpoint_id)
    if not checkpoint:
        raise SystemExit(f"unknown checkpoint: {checkpoint_id}")
    if checkpoint["cell_id"] != cell_name:
        raise SystemExit(f"checkpoint {checkpoint_id} does not belong to cell {cell_name}")

    local_tar = Path(checkpoint["artifact"])
    if not local_tar.exists():
        raise SystemExit(f"checkpoint artifact missing: {local_tar}")
    workspace = cell.get("workspace", DEFAULT_WORKSPACE)
    metrics = checkpoint_provider().restore(cell_name=cell_name, workspace=workspace, artifact=local_tar)
    event = {"restored_at": now_iso(), "cell_id": cell_name, "checkpoint_id": checkpoint_id, "metrics": metrics}
    cell["last_checkpoint_id"] = checkpoint_id
    save_state(state)
    return event


def command_checkpoint_restore(args: argparse.Namespace) -> None:
    state = load_state()
    checkpoint_id = args.checkpoint or cell_or_error(state, args.cell).get("last_checkpoint_id")
    if not checkpoint_id:
        raise SystemExit("no checkpoint specified and cell has no last checkpoint")
    event = restore_checkpoint(state, args.cell, checkpoint_id)
    print(json.dumps(event, indent=2, sort_keys=True))


def overlay_cell_or_error(state: dict[str, Any], name: str) -> dict[str, Any]:
    try:
        return state["overlay_cells"][name]
    except KeyError as exc:
        raise SystemExit(f"unknown overlay cell: {name}") from exc


def command_overlay_init(args: argparse.Namespace) -> None:
    provider = overlay_provider()
    meta = provider.create(name=args.name, source=Path(args.source))
    state = load_state()
    state["overlay_cells"][args.name] = {
        "cell_id": args.name,
        "backend": "local-overlay",
        "provider": provider.provider_name,
        "volume": meta["volume"],
        "source": meta["source"],
        "created_at_ms": meta["created_at_ms"],
        "last_checkpoint_id": None,
        "last_run_id": None,
        "active_policy_revision": None,
        "active_policy_path": None,
        "base_stats": meta["base_stats"],
    }
    save_state(state)
    print(json.dumps(meta, indent=2, sort_keys=True))


def command_overlay_status(args: argparse.Namespace) -> None:
    print(json.dumps(overlay_provider().status(args.name), indent=2, sort_keys=True))


def command_overlay_delete(args: argparse.Namespace) -> None:
    result = overlay_provider().delete(args.name)
    state = load_state()
    state["overlay_cells"].pop(args.name, None)
    save_state(state)
    print(json.dumps(result, indent=2, sort_keys=True))


def command_overlay_checkpoint(args: argparse.Namespace) -> None:
    provider = overlay_provider()
    checkpoint = provider.checkpoint(args.name, label=args.name_label)
    state = load_state()
    state["checkpoints"][checkpoint["checkpoint_id"]] = {
        **checkpoint,
        "cell_id": args.name,
        "backend": "local-overlay",
        "created_at": now_iso(),
    }
    state["overlay_cells"].setdefault(args.name, {})["last_checkpoint_id"] = checkpoint["checkpoint_id"]
    save_state(state)
    print(json.dumps(checkpoint, indent=2, sort_keys=True))


def command_overlay_restore(args: argparse.Namespace) -> None:
    event = overlay_provider().restore(args.name, args.checkpoint)
    state = load_state()
    state["overlay_cells"].setdefault(args.name, {})["last_checkpoint_id"] = event["checkpoint_id"]
    save_state(state)
    print(json.dumps(event, indent=2, sort_keys=True))


def command_overlay_verify(args: argparse.Namespace) -> None:
    print(json.dumps(overlay_provider().verify(args.name, args.checkpoint), indent=2, sort_keys=True))


def command_overlay_gc(args: argparse.Namespace) -> None:
    print(json.dumps(overlay_provider().gc(args.name), indent=2, sort_keys=True))


def command_overlay_doctor(args: argparse.Namespace) -> None:
    print(json.dumps(overlay_provider().doctor(args.name), indent=2, sort_keys=True))


def command_overlay_run(args: argparse.Namespace) -> None:
    run_local_overlay(args)


def volume_cell_or_error(state: dict[str, Any], name: str) -> dict[str, Any]:
    try:
        return state["volume_cells"][name]
    except KeyError as exc:
        raise SystemExit(f"unknown volume cell: {name}") from exc


def native_cell_or_error(state: dict[str, Any], name: str) -> dict[str, Any]:
    try:
        return state["native_cells"][name]
    except KeyError as exc:
        raise SystemExit(f"unknown native cell: {name}") from exc


def command_native_init(args: argparse.Namespace) -> None:
    provider = native_provider()
    meta = provider.create(name=args.name, source=Path(args.source))
    state = load_state()
    state["native_cells"][args.name] = {
        "cell_id": args.name,
        "backend": "native-overlay",
        "provider": meta["provider"],
        "volume": meta["volume"],
        "workspace": meta["workspace"],
        "backing_path": meta["backing_path"],
        "created_at": now_iso(),
        "last_checkpoint_id": "base",
        "runtime_benchmark_validated": False,
        "active_policy_revision": None,
        "active_policy_path": None,
        "last_decision_id": None,
        "phase": "ready",
    }
    record_checkpoint_graph_node(
        state,
        cell_id=args.name,
        checkpoint_id="base",
        label="base",
        backend="native-overlay",
    )
    save_state(state)
    print(json.dumps(meta, indent=2, sort_keys=True))


def command_native_status(args: argparse.Namespace) -> None:
    print(json.dumps(native_provider().status(args.name), indent=2, sort_keys=True))


def command_native_delete(args: argparse.Namespace) -> None:
    result = native_provider().delete(args.name)
    state = load_state()
    state["native_cells"].pop(args.name, None)
    save_state(state)
    print(json.dumps(result, indent=2, sort_keys=True))


def command_native_checkpoint(args: argparse.Namespace) -> None:
    state = load_state()
    cell = native_cell_or_error(state, args.name)
    policy_revision = cell.get("active_policy_revision")
    policy_record = state["policies"].get(policy_revision or "", {})
    parent_checkpoint_id = cell.get("last_checkpoint_id")
    checkpoint = native_provider().checkpoint(args.name, label=args.name_label)
    checkpoint["policy_revision"] = policy_revision
    checkpoint["policy_sha256"] = policy_record.get("sha256")
    checkpoint["sha256"] = native_checkpoint_sha256(checkpoint, policy_revision=policy_revision)
    checkpoint["sha256_kind"] = "native_metadata_identity"
    checkpoint["cell_id"] = args.name
    checkpoint["backend"] = "native-overlay"
    checkpoint["created_at"] = now_iso()
    state["checkpoints"][checkpoint["checkpoint_id"]] = checkpoint
    state["native_cells"].setdefault(args.name, {})["last_checkpoint_id"] = checkpoint["checkpoint_id"]
    record_checkpoint_graph_node(
        state,
        cell_id=args.name,
        checkpoint_id=checkpoint["checkpoint_id"],
        label=args.name_label,
        backend="native-overlay",
        parent_checkpoint_id=parent_checkpoint_id,
    )
    save_state(state)
    print(json.dumps(checkpoint, indent=2, sort_keys=True))


def command_native_restore(args: argparse.Namespace) -> None:
    event = native_provider().restore(args.name, args.checkpoint)
    state = load_state()
    state["native_cells"].setdefault(args.name, {})["last_checkpoint_id"] = event["checkpoint_id"]
    save_state(state)
    print(json.dumps(event, indent=2, sort_keys=True))


def command_native_gc(args: argparse.Namespace) -> None:
    print(json.dumps(native_provider().gc(args.name, dry_run=args.dry_run), indent=2, sort_keys=True))


def command_native_fork(args: argparse.Namespace) -> None:
    state = load_state()
    native_cell_or_error(state, args.source)
    checkpoint_id = args.checkpoint or state["native_cells"][args.source].get("last_checkpoint_id") or "base"
    meta = native_provider().fork_from_checkpoint(
        source_name=args.source,
        new_name=args.name,
        checkpoint_id=checkpoint_id,
        label=args.label,
    )
    state = load_state()
    state["native_cells"][args.name] = {
        "cell_id": args.name,
        "backend": "native-overlay",
        "provider": meta["provider"],
        "volume": meta["volume"],
        "workspace": meta["workspace"],
        "backing_path": meta["backing_path"],
        "created_at": now_iso(),
        "last_checkpoint_id": "base",
        "runtime_benchmark_validated": False,
        "active_policy_revision": None,
        "active_policy_path": None,
        "last_decision_id": None,
        "phase": "ready",
        "forked_from": {"cell_id": args.source, "checkpoint_id": checkpoint_id},
    }
    record_checkpoint_graph_node(
        state,
        cell_id=args.name,
        checkpoint_id="base",
        label=args.label or f"fork:{args.source}:{checkpoint_id}",
        backend="native-overlay",
        parent_node_id=graph_node_id(args.source, checkpoint_id),
        forked_from={"cell_id": args.source, "checkpoint_id": checkpoint_id},
    )
    save_state(state)
    print(json.dumps(meta, indent=2, sort_keys=True))


def command_native_driver_config(args: argparse.Namespace) -> None:
    config = native_provider().driver_config_json(args.name)
    if args.format == "raw":
        print(config)
    else:
        print(json.dumps(json.loads(config), indent=2, sort_keys=True))


def command_native_run(args: argparse.Namespace) -> None:
    run_openshell_native_overlay(args)


def command_volume_init(args: argparse.Namespace) -> None:
    provider = volume_provider()
    meta = provider.create(name=args.name, source=Path(args.source))
    state = load_state()
    state["volume_cells"][args.name] = {
        "cell_id": args.name,
        "backend": "volume-delta",
        "provider": provider.provider_name,
        "volume": meta["volume"],
        "workspace": meta["workspace"],
        "source": meta["source"],
        "created_at_ms": meta["created_at_ms"],
        "last_checkpoint_id": None,
        "last_run_id": None,
        "active_policy_revision": None,
        "active_policy_path": None,
        "last_decision_id": None,
        "phase": "ready",
        "base_stats": meta["base_stats"],
    }
    save_state(state)
    print(json.dumps(meta, indent=2, sort_keys=True))


def command_volume_status(args: argparse.Namespace) -> None:
    print(json.dumps(volume_provider().status(args.name), indent=2, sort_keys=True))


def command_volume_delete(args: argparse.Namespace) -> None:
    state = load_state()
    stale_record = state["volume_cells"].get(args.name)
    try:
        result = volume_provider().delete(args.name)
    except SystemExit:
        if not stale_record:
            raise
        result = {
            "name": args.name,
            "volume": stale_record.get("volume"),
            "docker_rc": None,
            "stderr": "removed stale state record; provider metadata was missing",
            "stale_state_removed": True,
        }
    state["volume_cells"].pop(args.name, None)
    save_state(state)
    print(json.dumps(result, indent=2, sort_keys=True))


def command_volume_checkpoint(args: argparse.Namespace) -> None:
    provider = volume_provider()
    checkpoint = provider.checkpoint(args.name, label=args.name_label, strict=args.strict)
    state = load_state()
    state["checkpoints"][checkpoint["checkpoint_id"]] = {
        **checkpoint,
        "cell_id": args.name,
        "backend": "volume-delta",
        "created_at": now_iso(),
        "policy_revision": state["volume_cells"].get(args.name, {}).get("active_policy_revision"),
    }
    state["volume_cells"].setdefault(args.name, {})["last_checkpoint_id"] = checkpoint["checkpoint_id"]
    save_state(state)
    print(json.dumps(checkpoint, indent=2, sort_keys=True))


def command_volume_restore(args: argparse.Namespace) -> None:
    state = load_state()
    cell = volume_cell_or_error(state, args.name)
    linked_run = None
    linked_receipt = None
    linked_receipt_path = None
    run_id = args.run or cell.get("last_run_id")
    if run_id:
        linked_run = state["runs"].get(run_id)
        if not linked_run:
            raise SystemExit(f"unknown run: {run_id}")
        if linked_run.get("cell_id") != args.name:
            raise SystemExit(f"run {run_id} does not belong to volume cell {args.name}")
        linked_receipt_path = Path(linked_run["receipt_json"])
        if not linked_receipt_path.exists():
            raise SystemExit(f"receipt artifact missing: {linked_receipt_path}")
        linked_receipt = json.loads(linked_receipt_path.read_text())

    event = volume_provider().restore(args.name, args.checkpoint)
    checkpoint_id = event["checkpoint_id"]
    checkpoint = state["checkpoints"].get(checkpoint_id, {})
    decision = volume_restore_decision_record(
        decision_id=new_decision_id(),
        cell_id=args.name,
        run_id=run_id,
        receipt_id=linked_run.get("receipt_id") if linked_run else None,
        receipt=linked_receipt,
        checkpoint=checkpoint,
        result="restored",
        reason=args.reason or "manual_restore",
        restore_metrics=event.get("metrics"),
        automatic=False,
    )
    decision["policy_revision"] = decision.get("policy_revision") or cell.get("active_policy_revision")
    store_decision(state, decision)
    cell["last_checkpoint_id"] = checkpoint_id
    cell["restored_checkpoint_id"] = checkpoint_id
    cell["last_decision_id"] = decision["decision_id"]
    cell["phase"] = "restored"
    save_state(state)
    event["decision"] = decision
    print(json.dumps(event, indent=2, sort_keys=True))


def command_volume_verify(args: argparse.Namespace) -> None:
    print(json.dumps(volume_provider().verify(args.name), indent=2, sort_keys=True))


def command_volume_run(args: argparse.Namespace) -> None:
    run_openshell_volume(args)


def command_volume_accept(args: argparse.Namespace) -> None:
    state = load_state()
    cell = volume_cell_or_error(state, args.name)
    run_id = args.run or cell.get("last_run_id")
    if not run_id:
        raise SystemExit(f"volume cell has no runs: {args.name}")
    run = state["runs"].get(run_id)
    if not run:
        raise SystemExit(f"unknown run: {run_id}")
    if run.get("cell_id") != args.name:
        raise SystemExit(f"run {run_id} does not belong to volume cell {args.name}")
    receipt_path = Path(run["receipt_json"])
    if not receipt_path.exists():
        raise SystemExit(f"receipt artifact missing: {receipt_path}")
    receipt = json.loads(receipt_path.read_text())
    exit_code = receipt.get("run", {}).get("exit_code")
    if exit_code != 0 and not args.force:
        raise SystemExit(f"refusing to accept non-zero run {run_id}; pass --force to override")
    decision_id = new_decision_id()
    decision = {
        "decision_id": decision_id,
        "cell_id": args.name,
        "run_id": run_id,
        "receipt_id": run["receipt_id"],
        "receipt_sha256": receipt.get("hashes", {}).get("receipt_sha256"),
        "checkpoint_id": receipt.get("checkpoints", {}).get("before"),
        "checkpoint_sha256": receipt.get("bindings", {}).get("checkpoint_sha256"),
        "policy_revision": receipt.get("policy", {}).get("revision"),
        "result": "accepted",
        "reason": args.reason or "explicit_accept",
        "forced": bool(args.force),
        "created_at": now_iso(),
    }
    store_decision(state, decision)
    cell["accepted_run_id"] = run_id
    cell["accepted_receipt_id"] = run["receipt_id"]
    cell["last_decision_id"] = decision_id
    cell["phase"] = "accepted"
    save_state(state)
    print(json.dumps(decision, indent=2, sort_keys=True))


def normalize_remainder(command: list[str]) -> list[str]:
    return command[1:] if command and command[0] == "--" else command


def run_local_overlay(args: argparse.Namespace) -> None:
    state = load_state()
    overlay_cell_or_error(state, args.cell)
    provider = overlay_provider()
    args.command = normalize_remainder(args.command)
    checkpoint = provider.checkpoint(args.cell, label=args.checkpoint_name) if args.checkpoint_before else None
    if checkpoint:
        state = load_state()
        state["checkpoints"][checkpoint["checkpoint_id"]] = {
            **checkpoint,
            "cell_id": args.cell,
            "backend": "local-overlay",
            "created_at": now_iso(),
        }
        state["overlay_cells"][args.cell]["last_checkpoint_id"] = checkpoint["checkpoint_id"]
        save_state(state)

    run_id = f"run_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    started_at = now_iso()
    cmd_result = provider.run(args.cell, args.command)
    finished_at = now_iso()
    verify_stats = provider.verify(args.cell)
    receipt_id = f"rcpt_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    decision = "accepted"
    restored_to = None
    restore_event = None
    if args.restore_on_fail and cmd_result.returncode != 0 and checkpoint:
        restore_event = provider.restore(args.cell, checkpoint["checkpoint_id"])
        decision = "restored"
        restored_to = checkpoint["checkpoint_id"]

    receipt: dict[str, Any] = {
        "schema_version": "0.1",
        "receipt_id": receipt_id,
        "cell_id": args.cell,
        "openshell": None,
        "capabilities": {
            "runtime": "local-overlay",
            "filesystem_checkpoint": True,
            "memory_checkpoint": False,
            "driver_overlay_checkpoint": True,
            "egress_governance": False,
            "credential_governance": False,
            "ocsf_events": False,
            "degraded_policy": "local-overlay backend validates filesystem checkpointing only; OpenShell governance is not attached",
        },
        "run": {
            "run_id": run_id,
            "command": args.command,
            "started_at": started_at,
            "finished_at": finished_at,
            "exit_code": cmd_result.returncode,
            "stdout_sha256": sha256_text(cmd_result.stdout),
            "stderr_sha256": sha256_text(cmd_result.stderr),
            "stdout_preview": cmd_result.stdout[-4000:],
            "stderr_preview": cmd_result.stderr[-4000:],
        },
        "policy": {"revision": None, "events": {}},
        "bindings": {
            "policy_revision": None,
            "checkpoint_policy_revision": None,
            "checkpoint_sha256": checkpoint.get("sha256") if checkpoint else None,
            "receipt_binds_policy_and_checkpoint": True,
        },
        "checkpoints": {
            "before": checkpoint["checkpoint_id"] if checkpoint else None,
            "before_metrics": checkpoint.get("metrics") if checkpoint else None,
            "before_policy_revision": None,
            "after": None,
            "restored_to": restored_to,
            "restore_metrics": restore_event.get("metrics") if restore_event else None,
        },
        "files": {"provider_verify_stats": verify_stats},
        "credentials": [],
        "artifacts": {},
        "decision": {"result": decision, "reason": "restore_on_fail" if decision == "restored" else "not_requested"},
        "created_at": now_iso(),
    }
    receipt_json = json.dumps(receipt, indent=2, sort_keys=True)
    receipt["hashes"] = {"receipt_sha256": sha256_text(receipt_json), "previous_receipt_sha256": None}
    receipt_json = json.dumps(receipt, indent=2, sort_keys=True)

    json_path = RECEIPT_DIR / f"{receipt_id}.json"
    md_path = RECEIPT_DIR / f"{receipt_id}.md"
    json_path.write_text(receipt_json)
    write_markdown_receipt(receipt, md_path)

    state = load_state()
    state["runs"][run_id] = {
        "run_id": run_id,
        "cell_id": args.cell,
        "backend": "local-overlay",
        "receipt_id": receipt_id,
        "receipt_json": str(json_path),
        "receipt_markdown": str(md_path),
        "exit_code": cmd_result.returncode,
        "created_at": receipt["created_at"],
    }
    state["overlay_cells"][args.cell]["last_run_id"] = run_id
    save_state(state)

    print(cmd_result.stdout, end="")
    print(cmd_result.stderr, file=sys.stderr, end="")
    print(json.dumps({"run_id": run_id, "receipt_id": receipt_id, "receipt": str(json_path)}, indent=2, sort_keys=True))
    if args.exit_with_command:
        raise SystemExit(cmd_result.returncode)


def run_openshell_volume(args: argparse.Namespace) -> None:
    state = load_state()
    cell = volume_cell_or_error(state, args.cell)
    provider = volume_provider()
    args.command = normalize_remainder(args.command)
    if getattr(args, "policy", None):
        apply_deferred_volume_policy(state, args.cell, args.policy)
        state = load_state()
        cell = volume_cell_or_error(state, args.cell)
    policy_revision = cell.get("active_policy_revision")
    policy_path = cell.get("active_policy_path")
    strict_checkpoint = bool(getattr(args, "strict_checkpoint", False))
    checkpoint = provider.checkpoint(args.cell, label=args.checkpoint_name, strict=strict_checkpoint) if args.checkpoint_before else None
    if checkpoint:
        checkpoint["policy_revision"] = policy_revision
    if checkpoint:
        state = load_state()
        state["checkpoints"][checkpoint["checkpoint_id"]] = {
            **checkpoint,
            "cell_id": args.cell,
            "backend": "volume-delta",
            "created_at": now_iso(),
            "policy_revision": policy_revision,
        }
        state["volume_cells"][args.cell]["last_checkpoint_id"] = checkpoint["checkpoint_id"]
        save_state(state)

    run_id = f"run_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    sandbox_name = f"fcv-{args.cell}-{uuid.uuid4().hex[:8]}"
    driver_config = provider.driver_config_json(args.cell)
    started_at = now_iso()
    remote = ["sh", "-lc", "cd /sandbox/work && \"$@\"", "forkcell-command", *args.command]
    create_args = [
        "sandbox",
        "create",
        "--name",
        sandbox_name,
        "--no-auto-providers",
        "--driver-config-json",
        driver_config,
    ]
    if policy_path:
        create_args.extend(["--policy", policy_path])
    create_args.extend(["--", *remote])
    cmd_result = openshell(create_args, check=False)
    exit_code = effective_openshell_exit_code(cmd_result)
    finished_at = now_iso()
    log_collection_mode = "sync"
    log_started = time.perf_counter()
    log_text = collect_openshell_logs(sandbox_name, args.logs_since)
    log_collect_ms = int(round((time.perf_counter() - log_started) * 1000))
    sync_logs = True
    delete_result = openshell(["sandbox", "delete", sandbox_name], check=False)
    if not log_text:
        log_text = delete_result.stdout + delete_result.stderr
    log_artifact = ARTIFACT_DIR / f"{run_id}-volume-delta.log"
    log_artifact.write_text(log_text)
    event_text = log_text + cmd_result.stdout + cmd_result.stderr
    event_artifact = write_openshell_event_artifact(run_id, event_text)

    verify_stats = provider.verify(args.cell)
    receipt_id = f"rcpt_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    event_store = append_event_store(
        run_id=run_id,
        receipt_id=receipt_id,
        cell_id=args.cell,
        runtime="volume-delta",
        event_artifact=event_artifact,
    )
    decision = "accepted"
    restored_to = None
    restore_event = None
    if args.restore_on_fail and exit_code != 0 and checkpoint:
        restore_event = provider.restore(args.cell, checkpoint["checkpoint_id"])
        decision = "restored"
        restored_to = checkpoint["checkpoint_id"]
    decision_id = new_decision_id() if decision == "restored" else None
    decision_artifact = str(decision_artifact_path(decision_id)) if decision_id else None

    checkpoint_delta = {
        "provider": checkpoint.get("metrics", {}).get("provider") if checkpoint else None,
        "strict_mode": checkpoint.get("metrics", {}).get("strict_mode") if checkpoint else None,
        "hashed_files": checkpoint.get("metrics", {}).get("hashed_files") if checkpoint else None,
        "reused_files": checkpoint.get("metrics", {}).get("reused_files") if checkpoint else None,
        "new_objects": checkpoint.get("metrics", {}).get("new_objects") if checkpoint else None,
        "copied_object_bytes": checkpoint.get("metrics", {}).get("copied_object_bytes") if checkpoint else None,
        "store_object_count": checkpoint.get("metrics", {}).get("store_object_count") if checkpoint else None,
        "store_bytes": checkpoint.get("metrics", {}).get("store_bytes") if checkpoint else None,
        "manifest_bytes": checkpoint.get("metrics", {}).get("manifest_bytes") if checkpoint else None,
        "manifest_sha256": checkpoint.get("metrics", {}).get("manifest_sha256") if checkpoint else None,
    } if checkpoint else {}
    restore_details = (restore_event or {}).get("metrics", {}).get("details", {}) if restore_event else {}
    restore_delta = {
        "copied_files": restore_details.get("copied_files"),
        "copied_bytes": restore_details.get("copied_bytes"),
        "reused_files": restore_details.get("reused_files"),
        "hashed_files": restore_details.get("hashed_files"),
        "removed_paths": restore_details.get("removed_paths"),
        "replaced_symlinks": restore_details.get("replaced_symlinks"),
    } if restore_event else {}

    receipt: dict[str, Any] = {
        "schema_version": "0.1",
        "receipt_id": receipt_id,
        "cell_id": args.cell,
        "openshell": {
            "sandbox_id": sandbox_name,
            "version": require_openshell(),
            "driver_config_mount": {"type": "volume", "target": cell.get("workspace", "/sandbox/work")},
            "policy_path": policy_path,
        },
        "capabilities": {
            "runtime": "volume-delta",
            "filesystem_checkpoint": True,
            "memory_checkpoint": False,
            "driver_overlay_checkpoint": False,
            "egress_governance": True,
            "credential_governance": True,
            "ocsf_events": True,
            "degraded_policy": "volume workspace is OpenShell-governed; checkpoint is CAS-incremental, while restore still rewrites the workspace rather than using overlay",
        },
        "run": {
            "run_id": run_id,
            "command": args.command,
            "started_at": started_at,
            "finished_at": finished_at,
            "exit_code": exit_code,
            "openshell_cli_exit_code": cmd_result.returncode,
            "stdout_sha256": sha256_text(cmd_result.stdout),
            "stderr_sha256": sha256_text(cmd_result.stderr),
            "stdout_preview": cmd_result.stdout[-4000:],
            "stderr_preview": cmd_result.stderr[-4000:],
        },
        "policy": {
            "revision": policy_revision,
            "path": policy_path,
            "events": summarize_logs(event_text),
            "log_collection": {"mode": log_collection_mode, "blocking_ms": log_collect_ms, "sync_complete": sync_logs},
            "structured_events": extract_openshell_events(event_text),
            "signals": extract_policy_signals(event_text),
        },
        "bindings": {
            "policy_revision": policy_revision,
            "checkpoint_policy_revision": checkpoint.get("policy_revision") if checkpoint else None,
            "checkpoint_sha256": checkpoint.get("sha256") if checkpoint else None,
            "receipt_binds_policy_and_checkpoint": bool(
                checkpoint is None
                or checkpoint.get("policy_revision") == policy_revision
            ),
        },
        "checkpoints": {
            "before": checkpoint["checkpoint_id"] if checkpoint else None,
            "before_metrics": checkpoint.get("metrics") if checkpoint else None,
            "before_policy_revision": checkpoint.get("policy_revision") if checkpoint else None,
            "after": None,
            "restored_to": restored_to,
            "restore_metrics": restore_event.get("metrics") if restore_event else None,
        },
        "files": {
            "provider_verify_stats": verify_stats,
            "checkpoint_restore_summary": {
                "checkpoint": checkpoint_delta,
                "restore": restore_delta,
            },
        },
        "credentials": [],
        "artifacts": {"openshell_log": str(log_artifact), "openshell_events": event_artifact, "event_store": event_store},
        "decision": {
            "result": decision,
            "reason": "restore_on_fail" if decision == "restored" else "not_requested",
            "decision_id": decision_id,
            "artifact": decision_artifact,
        },
        "created_at": now_iso(),
    }
    receipt_json = json.dumps(receipt, indent=2, sort_keys=True)
    receipt["hashes"] = {"receipt_sha256": sha256_text(receipt_json), "previous_receipt_sha256": None}
    receipt_json = json.dumps(receipt, indent=2, sort_keys=True)

    json_path = RECEIPT_DIR / f"{receipt_id}.json"
    md_path = RECEIPT_DIR / f"{receipt_id}.md"
    json_path.write_text(receipt_json)
    write_markdown_receipt(receipt, md_path)

    state = load_state()
    state["runs"][run_id] = {
        "run_id": run_id,
        "cell_id": args.cell,
        "backend": "volume-delta",
        "receipt_id": receipt_id,
        "receipt_json": str(json_path),
        "receipt_markdown": str(md_path),
        "exit_code": exit_code,
        "created_at": receipt["created_at"],
    }
    cell_state = state["volume_cells"][args.cell]
    cell_state["last_run_id"] = run_id
    if decision_id and checkpoint and restore_event:
        decision_record = volume_restore_decision_record(
            decision_id=decision_id,
            cell_id=args.cell,
            run_id=run_id,
            receipt_id=receipt_id,
            receipt=receipt,
            checkpoint=checkpoint,
            result="restored",
            reason="restore_on_fail",
            restore_metrics=restore_event.get("metrics"),
            automatic=True,
        )
        decision_record["policy_revision"] = policy_revision
        decision_record["created_at"] = receipt["created_at"]
        decision_record["artifact"] = decision_artifact
        store_decision(state, decision_record)
        state["runs"][run_id]["decision_id"] = decision_id
        cell_state["last_decision_id"] = decision_id
        cell_state["phase"] = "restored"
        cell_state["restored_checkpoint_id"] = checkpoint["checkpoint_id"]
    save_state(state)

    print(cmd_result.stdout, end="")
    print(cmd_result.stderr, file=sys.stderr, end="")
    print(json.dumps({"run_id": run_id, "receipt_id": receipt_id, "receipt": str(json_path)}, indent=2, sort_keys=True))
    if args.exit_with_command:
        raise SystemExit(exit_code)


def run_openshell_native_overlay(args: argparse.Namespace) -> None:
    state = load_state()
    cell = native_cell_or_error(state, args.cell)
    provider = native_provider()
    args.command = normalize_remainder(args.command)
    if getattr(args, "policy", None):
        apply_deferred_native_policy(state, args.cell, args.policy)
        state = load_state()
        cell = native_cell_or_error(state, args.cell)
    policy_revision = cell.get("active_policy_revision")
    policy_path = cell.get("active_policy_path")
    policy_record = state["policies"].get(policy_revision or "", {})
    parent_checkpoint_id = state.get("native_cells", {}).get(args.cell, {}).get("last_checkpoint_id")
    checkpoint = provider.checkpoint(args.cell, label=args.checkpoint_name) if args.checkpoint_before else None
    if checkpoint:
        checkpoint["policy_revision"] = policy_revision
        checkpoint["policy_sha256"] = policy_record.get("sha256")
        checkpoint["sha256"] = native_checkpoint_sha256(checkpoint, policy_revision=policy_revision)
        checkpoint["sha256_kind"] = "native_metadata_identity"
        state = load_state()
        state["checkpoints"][checkpoint["checkpoint_id"]] = {
            **checkpoint,
            "cell_id": args.cell,
            "backend": "native-overlay",
            "created_at": now_iso(),
            "policy_revision": policy_revision,
        }
        state["native_cells"][args.cell]["last_checkpoint_id"] = checkpoint["checkpoint_id"]
        record_checkpoint_graph_node(
            state,
            cell_id=args.cell,
            checkpoint_id=checkpoint["checkpoint_id"],
            label=args.checkpoint_name,
            backend="native-overlay",
            parent_checkpoint_id=parent_checkpoint_id,
        )
        save_state(state)

    run_id = f"run_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    sandbox_name = f"fcn-{args.cell}-{uuid.uuid4().hex[:8]}"
    driver_config = provider.driver_config_json(args.cell)
    driver_config_sha256 = sha256_text(driver_config)
    started_at = now_iso()
    remote = ["sh", "-lc", "cd /sandbox/work && \"$@\"", "forkcell-command", *args.command]
    create_args = [
        "sandbox",
        "create",
        "--name",
        sandbox_name,
        "--no-auto-providers",
        "--driver-config-json",
        driver_config,
    ]
    if policy_path:
        create_args.extend(["--policy", policy_path])
    create_args.extend(["--", *remote])
    create_started = time.perf_counter()
    cmd_result = openshell(create_args, check=False)
    sandbox_lifecycle_ms = int(round((time.perf_counter() - create_started) * 1000))
    exit_code = effective_openshell_exit_code(cmd_result)
    finished_at = now_iso()
    sync_logs = bool(getattr(args, "sync_logs", False)) or os.environ.get("FORKCELL_SYNC_LOGS") == "1"
    log_collection_mode = "sync" if sync_logs else "best_effort_async"
    log_started = time.perf_counter()
    log_text = collect_openshell_logs(sandbox_name, args.logs_since, wait_for_ocsf=sync_logs)
    log_collect_ms = int(round((time.perf_counter() - log_started) * 1000))
    delete_started = time.perf_counter()
    delete_result = openshell(["sandbox", "delete", sandbox_name], check=False)
    sandbox_delete_ms = int(round((time.perf_counter() - delete_started) * 1000))
    if not log_text:
        log_text = delete_result.stdout + delete_result.stderr
    log_artifact = ARTIFACT_DIR / f"{run_id}-native-overlay.log"
    log_artifact.write_text(log_text)
    event_text = log_text + cmd_result.stdout + cmd_result.stderr
    event_artifact = write_openshell_event_artifact(run_id, event_text)

    decision = "accepted"
    restored_to = None
    restore_event = None
    if args.restore_on_fail and exit_code != 0 and checkpoint:
        restore_started = time.perf_counter()
        restore_event = provider.restore(args.cell, checkpoint["checkpoint_id"])
        restore_call_ms = int(round((time.perf_counter() - restore_started) * 1000))
        restore_metrics = restore_event.setdefault("metrics", {})
        breakdown = restore_metrics.setdefault("breakdown", {})
        overlay_reset_ms = restore_metrics.get("overlay_reset_ms", restore_metrics.get("duration_ms"))
        restore_metrics.setdefault("overlay_reset_ms", overlay_reset_ms)
        restore_metrics["restore_call_ms"] = restore_call_ms
        restore_metrics["sandbox_lifecycle_ms"] = sandbox_lifecycle_ms
        restore_metrics["log_collect_ms"] = log_collect_ms
        restore_metrics["sandbox_delete_ms"] = sandbox_delete_ms
        restore_metrics["total_restore_path_ms"] = (
            (overlay_reset_ms or 0) + sandbox_lifecycle_ms + log_collect_ms + sandbox_delete_ms
        )
        breakdown.update(
            {
                "overlay_reset_ms": overlay_reset_ms,
                "sandbox_lifecycle_ms": sandbox_lifecycle_ms,
                "log_collect_ms": log_collect_ms,
                "log_collection_mode": log_collection_mode,
                "sandbox_delete_ms": sandbox_delete_ms,
                "restore_call_ms": restore_call_ms,
                "total_restore_path_ms": restore_metrics["total_restore_path_ms"],
            }
        )
        decision = "restored"
        restored_to = checkpoint["checkpoint_id"]
    decision_id = new_decision_id() if decision == "restored" else None
    decision_artifact = str(decision_artifact_path(decision_id)) if decision_id else None
    receipt_id = f"rcpt_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    event_store = append_event_store(
        run_id=run_id,
        receipt_id=receipt_id,
        cell_id=args.cell,
        runtime="native-overlay",
        event_artifact=event_artifact,
    )

    checkpoint_delta = {
        "provider": checkpoint.get("metrics", {}).get("provider") if checkpoint else None,
        "metadata_only": checkpoint.get("metrics", {}).get("metadata_only") if checkpoint else None,
        "delta_files": checkpoint.get("metrics", {}).get("delta_files") if checkpoint else None,
        "delta_bytes": checkpoint.get("metrics", {}).get("delta_bytes") if checkpoint else None,
    } if checkpoint else {}
    restore_delta = {
        "metadata_only": restore_event.get("metrics", {}).get("metadata_only"),
        "requires_sandbox_restart": restore_event.get("metrics", {}).get("requires_sandbox_restart"),
    } if restore_event else {}

    receipt: dict[str, Any] = {
        "schema_version": "0.1",
        "receipt_id": receipt_id,
        "cell_id": args.cell,
        "openshell": {
            "sandbox_id": sandbox_name,
            "version": require_openshell(),
            "driver_config_mount": {"type": "forkcell_overlay", "target": cell.get("workspace", "/sandbox/work")},
            "driver_config_sha256": driver_config_sha256,
            "policy_path": policy_path,
        },
        "capabilities": {
            "runtime": "native-overlay",
            "filesystem_checkpoint": True,
            "memory_checkpoint": False,
            "driver_overlay_checkpoint": True,
            "metadata_only_restore": True,
            "egress_governance": True,
            "credential_governance": True,
            "ocsf_events": True,
            "degraded": False,
            "degraded_policy": "native overlay requires a patched OpenShell Docker driver and supervisor; unsupported installed binaries fail before the sandbox run",
            "degradation_reason": None,
        },
        "runtime_selection": {
            "requested_backend": requested_backend_name(args, "native-overlay"),
            "resolved_backend": "native-overlay",
            "production_fast_backend": True,
            "fallback_used": False,
            "fallback_backend": None,
            "degradation_reason": None,
            "patched_runtime_available": patched_openshell_runtime_available(),
        },
        "run": {
            "run_id": run_id,
            "command": args.command,
            "started_at": started_at,
            "finished_at": finished_at,
            "exit_code": exit_code,
            "openshell_cli_exit_code": cmd_result.returncode,
            "stdout_sha256": sha256_text(cmd_result.stdout),
            "stderr_sha256": sha256_text(cmd_result.stderr),
            "stdout_preview": cmd_result.stdout[-4000:],
            "stderr_preview": cmd_result.stderr[-4000:],
        },
        "timings": {
            "sandbox_lifecycle_ms": sandbox_lifecycle_ms,
            "log_collect_ms": log_collect_ms,
            "log_collection_mode": log_collection_mode,
            "sandbox_delete_ms": sandbox_delete_ms,
            "overlay_reset_ms": (restore_event.get("metrics", {}) if restore_event else {}).get("overlay_reset_ms"),
            "restore_call_ms": (restore_event.get("metrics", {}) if restore_event else {}).get("restore_call_ms"),
            "total_restore_path_ms": (restore_event.get("metrics", {}) if restore_event else {}).get("total_restore_path_ms"),
        },
        "policy": {
            "revision": policy_revision,
            "path": policy_path,
            "sha256": policy_record.get("sha256"),
            "events": summarize_logs(event_text),
            "log_collection": {"mode": log_collection_mode, "blocking_ms": log_collect_ms, "sync_complete": sync_logs},
            "structured_events": extract_openshell_events(event_text),
            "signals": extract_policy_signals(event_text),
        },
        "bindings": {
            "policy_revision": policy_revision,
            "policy_sha256": policy_record.get("sha256"),
            "checkpoint_policy_revision": checkpoint.get("policy_revision") if checkpoint else None,
            "checkpoint_sha256": checkpoint.get("sha256") if checkpoint else None,
            "checkpoint_sha256_kind": checkpoint.get("sha256_kind") if checkpoint else None,
            "workspace_config_sha256": driver_config_sha256,
            "receipt_binds_policy_and_checkpoint": bool(
                checkpoint is None
                or checkpoint.get("policy_revision") == policy_revision
            ),
        },
        "workspace_substrate": json.loads(driver_config),
        "checkpoints": {
            "before": checkpoint["checkpoint_id"] if checkpoint else None,
            "before_metrics": checkpoint.get("metrics") if checkpoint else None,
            "before_policy_revision": checkpoint.get("policy_revision") if checkpoint else None,
            "after": None,
            "restored_to": restored_to,
            "restore_metrics": restore_event.get("metrics") if restore_event else None,
        },
        "files": {
            "provider_base_stats": cell.get("base_stats"),
            "checkpoint_restore_summary": {
                "checkpoint": checkpoint_delta,
                "restore": restore_delta,
            },
        },
        "credentials": [],
        "artifacts": {"openshell_log": str(log_artifact), "openshell_events": event_artifact, "event_store": event_store},
        "decision": {
            "result": decision,
            "reason": "restore_on_fail" if decision == "restored" else "not_requested",
            "decision_id": decision_id,
            "artifact": decision_artifact,
        },
        "created_at": now_iso(),
    }
    receipt_json = json.dumps(receipt, indent=2, sort_keys=True)
    receipt["hashes"] = {"receipt_sha256": sha256_text(receipt_json), "previous_receipt_sha256": None}
    receipt_json = json.dumps(receipt, indent=2, sort_keys=True)

    json_path = RECEIPT_DIR / f"{receipt_id}.json"
    md_path = RECEIPT_DIR / f"{receipt_id}.md"
    json_path.write_text(receipt_json)
    write_markdown_receipt(receipt, md_path)

    state = load_state()
    state["runs"][run_id] = {
        "run_id": run_id,
        "cell_id": args.cell,
        "backend": "native-overlay",
        "receipt_id": receipt_id,
        "receipt_json": str(json_path),
        "receipt_markdown": str(md_path),
        "exit_code": exit_code,
        "created_at": receipt["created_at"],
    }
    cell_state = state["native_cells"][args.cell]
    cell_state["last_run_id"] = run_id
    if decision_id and checkpoint and restore_event:
        decision_record = {
            "decision_id": decision_id,
            "cell_id": args.cell,
            "run_id": run_id,
            "receipt_id": receipt_id,
            "receipt_sha256": receipt["hashes"]["receipt_sha256"],
            "checkpoint_id": checkpoint["checkpoint_id"],
            "checkpoint_sha256": checkpoint.get("sha256"),
            "checkpoint_policy_revision": checkpoint.get("policy_revision"),
            "policy_revision": policy_revision,
            "result": "restored",
            "reason": "restore_on_fail",
            "automatic": True,
            "restore_metrics": restore_event.get("metrics"),
            "bindings": checkpoint_receipt_bindings(
                checkpoint_id=checkpoint["checkpoint_id"],
                checkpoint_sha256=checkpoint.get("sha256"),
                receipt=receipt,
            ),
            "artifact": decision_artifact,
            "created_at": receipt["created_at"],
        }
        store_decision(state, decision_record)
        state["runs"][run_id]["decision_id"] = decision_id
        cell_state["last_decision_id"] = decision_id
        cell_state["last_checkpoint_id"] = checkpoint["checkpoint_id"]
        cell_state["phase"] = "restored"
        cell_state["restored_checkpoint_id"] = checkpoint["checkpoint_id"]
    save_state(state)

    print(cmd_result.stdout, end="")
    print(cmd_result.stderr, file=sys.stderr, end="")
    print(json.dumps({"run_id": run_id, "receipt_id": receipt_id, "receipt": str(json_path)}, indent=2, sort_keys=True))
    if args.exit_with_command:
        raise SystemExit(exit_code)



def run_openshell_layer_clone(args: argparse.Namespace) -> None:
    state = load_state()
    cell = native_cell_or_error(state, args.cell)
    provider = native_provider()
    args.command = normalize_remainder(args.command)
    if getattr(args, "policy", None):
        apply_deferred_native_policy(state, args.cell, args.policy)
        state = load_state()
        cell = native_cell_or_error(state, args.cell)
    policy_revision = cell.get("active_policy_revision")
    policy_path = cell.get("active_policy_path")
    policy_record = state["policies"].get(policy_revision or "", {})
    checkpoint = provider.layer_checkpoint(args.cell, label=args.checkpoint_name) if args.checkpoint_before else None
    checkpoint_id = (checkpoint or {}).get("checkpoint_id") or cell.get("last_checkpoint_id") or "base"
    if checkpoint:
        checkpoint["policy_revision"] = policy_revision
        checkpoint["policy_sha256"] = policy_record.get("sha256")
        checkpoint["sha256"] = native_checkpoint_sha256(checkpoint, policy_revision=policy_revision)
        checkpoint["sha256_kind"] = "native_metadata_identity"
        state = load_state()
        state["checkpoints"][checkpoint["checkpoint_id"]] = {
            **checkpoint,
            "cell_id": args.cell,
            "backend": "layer-clone",
            "created_at": now_iso(),
            "policy_revision": policy_revision,
        }
        state["native_cells"][args.cell]["last_checkpoint_id"] = checkpoint["checkpoint_id"]
        save_state(state)

    layer_run = provider.prepare_layer_run(args.cell, checkpoint_id)
    driver_config = provider.volume_mount_driver_config_json(args.cell, layer=layer_run["run_layer"])
    driver_config_sha256 = sha256_text(driver_config)
    run_id = f"run_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    sandbox_name = f"fcl-{args.cell}-{uuid.uuid4().hex[:8]}"
    started_at = now_iso()
    remote = ["sh", "-lc", "cd /sandbox/work && \"$@\"", "forkcell-command", *args.command]
    create_args = [
        "sandbox",
        "create",
        "--name",
        sandbox_name,
        "--no-auto-providers",
        "--driver-config-json",
        driver_config,
    ]
    if policy_path:
        create_args.extend(["--policy", policy_path])
    create_args.extend(["--", *remote])
    cmd_result = openshell(create_args, check=False)
    exit_code = effective_openshell_exit_code(cmd_result)
    finished_at = now_iso()
    log_collection_mode = "sync"
    log_started = time.perf_counter()
    log_text = collect_openshell_logs(sandbox_name, args.logs_since)
    log_collect_ms = int(round((time.perf_counter() - log_started) * 1000))
    sync_logs = True
    delete_result = openshell(["sandbox", "delete", sandbox_name], check=False)
    if not log_text:
        log_text = delete_result.stdout + delete_result.stderr
    log_artifact = ARTIFACT_DIR / f"{run_id}-layer-clone.log"
    log_artifact.write_text(log_text)
    event_text = log_text + cmd_result.stdout + cmd_result.stderr
    event_artifact = write_openshell_event_artifact(run_id, event_text)

    decision = "accepted"
    restored_to = None
    restore_event = None
    accepted_checkpoint = None
    if args.restore_on_fail and exit_code != 0:
        restore_event = provider.restore_layer_run(args.cell, checkpoint_id, layer_run["layer_run_id"])
        decision = "restored"
        restored_to = checkpoint_id
    elif exit_code == 0:
        accepted_checkpoint = provider.accept_layer_run(args.cell, layer_run["layer_run_id"], label="accepted-run")

    decision_id = new_decision_id() if decision == "restored" else None
    decision_artifact = str(decision_artifact_path(decision_id)) if decision_id else None
    receipt_id = f"rcpt_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    event_store = append_event_store(
        run_id=run_id,
        receipt_id=receipt_id,
        cell_id=args.cell,
        runtime="layer-clone",
        event_artifact=event_artifact,
    )

    receipt: dict[str, Any] = {
        "schema_version": "0.1",
        "receipt_id": receipt_id,
        "cell_id": args.cell,
        "openshell": {
            "sandbox_id": sandbox_name,
            "version": require_openshell(),
            "driver_config_mount": {"type": "volume", "target": cell.get("workspace", "/sandbox/work")},
            "driver_config_sha256": driver_config_sha256,
            "policy_path": policy_path,
        },
        "capabilities": {
            "runtime": "layer-clone",
            "filesystem_checkpoint": True,
            "memory_checkpoint": False,
            "driver_overlay_checkpoint": False,
            "metadata_only_restore": True,
            "egress_governance": True,
            "credential_governance": True,
            "ocsf_events": True,
            "degraded": True,
            "degraded_policy": "uses current OpenShell docker volume mounts; run layer creation copies the checkpoint tree, restore switches metadata without rewriting the workspace",
            "degradation_reason": "native-overlay unavailable or explicitly bypassed; using current OpenShell volume-mounted layer clone fallback",
        },
        "runtime_selection": {
            "requested_backend": requested_backend_name(args, "layer-clone"),
            "resolved_backend": "layer-clone",
            "production_fast_backend": False,
            "fallback_used": requested_backend_name(args, "auto") == "auto",
            "fallback_backend": "layer-clone",
            "degradation_reason": "patched OpenShell native overlay runtime is unavailable" if requested_backend_name(args, "auto") == "auto" else "layer-clone explicitly requested",
            "patched_runtime_available": patched_openshell_runtime_available(),
        },
        "run": {
            "run_id": run_id,
            "command": args.command,
            "started_at": started_at,
            "finished_at": finished_at,
            "exit_code": exit_code,
            "openshell_cli_exit_code": cmd_result.returncode,
            "stdout_sha256": sha256_text(cmd_result.stdout),
            "stderr_sha256": sha256_text(cmd_result.stderr),
            "stdout_preview": cmd_result.stdout[-4000:],
            "stderr_preview": cmd_result.stderr[-4000:],
        },
        "policy": {
            "revision": policy_revision,
            "path": policy_path,
            "sha256": policy_record.get("sha256"),
            "events": summarize_logs(event_text),
            "log_collection": {"mode": log_collection_mode, "blocking_ms": log_collect_ms, "sync_complete": sync_logs},
            "structured_events": extract_openshell_events(event_text),
            "signals": extract_policy_signals(event_text),
        },
        "bindings": {
            "policy_revision": policy_revision,
            "policy_sha256": policy_record.get("sha256"),
            "checkpoint_policy_revision": checkpoint.get("policy_revision") if checkpoint else None,
            "checkpoint_sha256": checkpoint.get("sha256") if checkpoint else None,
            "checkpoint_sha256_kind": checkpoint.get("sha256_kind") if checkpoint else None,
            "workspace_config_sha256": driver_config_sha256,
            "receipt_binds_policy_and_checkpoint": bool(
                checkpoint is None
                or checkpoint.get("policy_revision") == policy_revision
            ),
        },
        "workspace_substrate": json.loads(driver_config),
        "checkpoints": {
            "before": checkpoint_id,
            "before_metrics": (checkpoint or {}).get("metrics"),
            "prepare_run_metrics": layer_run.get("metrics"),
            "accepted": (accepted_checkpoint or {}).get("checkpoint_id"),
            "accepted_metrics": (accepted_checkpoint or {}).get("metrics"),
            "restored_to": restored_to,
            "restore_metrics": restore_event.get("metrics") if restore_event else None,
        },
        "files": {"provider_base_stats": cell.get("base_stats"), "layer_run": layer_run},
        "credentials": [],
        "artifacts": {"openshell_log": str(log_artifact), "openshell_events": event_artifact, "event_store": event_store},
        "decision": {"result": decision, "reason": "restore_on_fail" if decision == "restored" else "accepted_run_layer", "decision_id": decision_id, "artifact": decision_artifact},
        "created_at": now_iso(),
    }
    receipt_json = json.dumps(receipt, indent=2, sort_keys=True)
    receipt["hashes"] = {"receipt_sha256": sha256_text(receipt_json), "previous_receipt_sha256": None}
    receipt_json = json.dumps(receipt, indent=2, sort_keys=True)
    json_path = RECEIPT_DIR / f"{receipt_id}.json"
    md_path = RECEIPT_DIR / f"{receipt_id}.md"
    json_path.write_text(receipt_json)
    write_markdown_receipt(receipt, md_path)

    state = load_state()
    state["runs"][run_id] = {"run_id": run_id, "cell_id": args.cell, "backend": "layer-clone", "receipt_id": receipt_id, "receipt_json": str(json_path), "receipt_markdown": str(md_path), "exit_code": exit_code, "created_at": receipt["created_at"]}
    cell_state = state["native_cells"][args.cell]
    cell_state["last_run_id"] = run_id
    if accepted_checkpoint:
        accepted_checkpoint["policy_revision"] = policy_revision
        accepted_checkpoint["policy_sha256"] = policy_record.get("sha256")
        accepted_checkpoint["sha256"] = native_checkpoint_sha256(accepted_checkpoint, policy_revision=policy_revision)
        accepted_checkpoint["sha256_kind"] = "native_metadata_identity"
        state["checkpoints"][accepted_checkpoint["checkpoint_id"]] = {**accepted_checkpoint, "cell_id": args.cell, "backend": "layer-clone", "created_at": now_iso(), "policy_revision": policy_revision}
        cell_state["last_checkpoint_id"] = accepted_checkpoint["checkpoint_id"]
        cell_state["phase"] = "accepted"
    if decision_id and checkpoint and restore_event:
        decision_record = {"decision_id": decision_id, "cell_id": args.cell, "run_id": run_id, "receipt_id": receipt_id, "receipt_sha256": receipt["hashes"]["receipt_sha256"], "checkpoint_id": checkpoint_id, "checkpoint_sha256": checkpoint.get("sha256"), "checkpoint_policy_revision": checkpoint.get("policy_revision"), "policy_revision": policy_revision, "result": "restored", "reason": "restore_on_fail", "automatic": True, "restore_metrics": restore_event.get("metrics"), "bindings": checkpoint_receipt_bindings(checkpoint_id=checkpoint_id, checkpoint_sha256=checkpoint.get("sha256"), receipt=receipt), "artifact": decision_artifact, "created_at": receipt["created_at"]}
        store_decision(state, decision_record)
        state["runs"][run_id]["decision_id"] = decision_id
        cell_state["last_decision_id"] = decision_id
        cell_state["phase"] = "restored"
        cell_state["restored_checkpoint_id"] = checkpoint_id
    save_state(state)

    print(cmd_result.stdout, end="")
    print(cmd_result.stderr, file=sys.stderr, end="")
    print(json.dumps({"run_id": run_id, "receipt_id": receipt_id, "receipt": str(json_path)}, indent=2, sort_keys=True))
    if args.exit_with_command:
        raise SystemExit(exit_code)


def runtime_source_paths(source: Path) -> dict[str, Path]:
    return {
        "openshell": source / "target/debug/openshell",
        "openshell_gateway": source / "target/debug/openshell-gateway",
        "openshell_sandbox": source / "target-linux-docker/debug/openshell-sandbox",
    }


def runtime_version(name: str, path: Path) -> str | None:
    if name == "openshell_sandbox":
        result = run_cmd(
            [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{path.parent}:/runtime:ro",
                "--entrypoint",
                f"/runtime/{path.name}",
                "ghcr.io/nvidia/openshell-community/sandboxes/base:latest",
                "--version",
            ],
            check=False,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip() or result.stderr.strip()
    result = run_cmd([str(path), "--version"], check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or result.stderr.strip()


def latest_patch_sha256() -> str | None:
    found = latest_evidence_summary("forkcell-openshell-patch-bundle-*.md")
    if not found:
        return None
    _, summary = found
    return summary.get("patch_sha256")


def command_runtime_install(args: argparse.Namespace) -> None:
    source = Path(args.source).resolve()
    paths = runtime_source_paths(source)
    missing = [str(path) for path in paths.values() if not path.exists()]
    if missing:
        raise SystemExit(f"missing runtime binaries: {missing}")
    install_dir = (RUNTIME_DIR / "native-overlay").resolve()
    bin_dir = install_dir / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    installed: dict[str, Any] = {}
    for name, src in paths.items():
        dest = bin_dir / src.name
        shutil.copy2(src, dest)
        dest.chmod(dest.stat().st_mode | 0o755)
        version = runtime_version(name, dest)
        if not version:
            raise SystemExit(f"failed to read version from installed runtime binary: {dest}")
        installed[name] = {
            "path": str(dest),
            "source_path": str(src),
            "sha256": sha256_file(dest),
            "size_bytes": dest.stat().st_size,
            "version": version,
            "executable": os.access(dest, os.X_OK),
        }
    lock = {
        "schema_version": "forkcell-runtime-lock-v1",
        "runtime": "native-overlay",
        "installed_at": now_iso(),
        "install_dir": str(install_dir),
        "version_lock": {
            "openshell_version": installed["openshell"]["version"],
            "openshell_gateway_version": installed["openshell_gateway"]["version"],
            "openshell_sandbox_version": installed["openshell_sandbox"]["version"],
            "patch_sha256": latest_patch_sha256(),
        },
        "binaries": installed,
    }
    RUNTIME_LOCK_PATH.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n")
    print(json.dumps(lock, indent=2, sort_keys=True))


def detect_runtime_lock(*, strict: bool = False) -> dict[str, Any]:
    lock = load_runtime_lock()
    problems: list[str] = []
    if not lock:
        return {"available": False, "version_locked": False, "problems": ["runtime lock not found"]}
    binaries = lock.get("binaries") or {}
    detected: dict[str, Any] = {}
    for name in ("openshell", "openshell_gateway", "openshell_sandbox"):
        item = binaries.get(name) or {}
        path = Path(item.get("path", ""))
        current = {
            "path": str(path),
            "exists": path.exists(),
            "executable": path.exists() and os.access(path, os.X_OK),
            "sha256_matches": False,
            "version_matches": False,
            "version": None,
        }
        if path.exists():
            current["sha256"] = sha256_file(path)
            current["sha256_matches"] = current["sha256"] == item.get("sha256")
            if strict or name != "openshell_sandbox":
                current["version"] = runtime_version(name, path)
                current["version_matches"] = current["version"] == item.get("version")
            else:
                current["version_matches"] = True
        for key in ("exists", "executable", "sha256_matches", "version_matches"):
            if not current.get(key):
                problems.append(f"{name}.{key}=false")
        detected[name] = current
    version_lock = lock.get("version_lock") or {}
    patch_locked = bool(version_lock.get("patch_sha256"))
    if not patch_locked:
        problems.append("version_lock.patch_sha256 missing")
    return {
        "available": not problems,
        "version_locked": not problems and patch_locked,
        "runtime": lock.get("runtime"),
        "install_dir": lock.get("install_dir"),
        "version_lock": version_lock,
        "binaries": detected,
        "gateway_endpoint": os.environ.get("OPENSHELL_GATEWAY_ENDPOINT"),
        "strict": strict,
        "problems": problems,
    }


def command_runtime_detect(args: argparse.Namespace) -> None:
    print(json.dumps(detect_runtime_lock(strict=args.strict), indent=2, sort_keys=True))


def command_runtime_env(args: argparse.Namespace) -> None:
    lock = load_runtime_lock()
    if not lock:
        raise SystemExit("runtime lock not found; run `forkcell runtime install` first")
    openshell = (lock.get("binaries") or {}).get("openshell") or {}
    endpoint = args.endpoint or f"http://127.0.0.1:{args.port}"
    print(f"export FORKCELL_OPENSHELL_BIN={json.dumps(openshell.get('path'))}")
    print(f"export OPENSHELL_GATEWAY_ENDPOINT={json.dumps(endpoint)}")


def command_graph_show(args: argparse.Namespace) -> None:
    state = load_state()
    graph = checkpoint_graph_view(state)
    if args.format == "json":
        print(json.dumps(graph, indent=2, sort_keys=True))
        return
    lines = ["# ForkCell Checkpoint Graph", "", f"- Nodes: `{graph['node_count']}`", f"- Roots: `{graph['roots']}`", ""]
    for node_id, node in graph["nodes"].items():
        lines.append(
            f"- `{node_id}`: parent `{node.get('parent_node_id')}`; children `{node.get('children', [])}`; "
            f"forked_from `{node.get('forked_from')}`"
        )
    print("\n".join(lines))


def command_graph_gc(args: argparse.Namespace) -> None:
    state = load_state()
    graph = state.setdefault("checkpoint_graph", {"nodes": {}})
    nodes = graph.setdefault("nodes", {})
    active_cells = set(state.get("native_cells", {}).keys())
    stale = sorted(
        node_id
        for node_id, node in nodes.items()
        if node.get("cell_id") and node.get("cell_id") not in active_cells
    )
    if not args.dry_run:
        for node_id in stale:
            nodes.pop(node_id, None)
        for node in nodes.values():
            node["children"] = [child for child in node.get("children", []) if child in nodes]
            if node.get("parent_node_id") not in nodes:
                node["parent_node_id"] = None
        save_state(state)
    result = {
        "dry_run": args.dry_run,
        "stale_node_count": len(stale),
        "stale_nodes": stale,
        "removed_node_count": 0 if args.dry_run else len(stale),
        "remaining_node_count": len(nodes) if not args.dry_run else len(nodes),
    }
    print(json.dumps(result, indent=2, sort_keys=True))


def patched_openshell_runtime_available() -> bool:
    bin_path = Path(openshell_bin())
    supervisor = locked_runtime_binary("openshell_sandbox") or ROOT / "upstream/openshell/target-linux-docker/debug/openshell-sandbox"
    endpoint = os.environ.get("OPENSHELL_GATEWAY_ENDPOINT", "")
    return bool(
        endpoint.startswith("http://")
        and bin_path.exists()
        and os.access(bin_path, os.X_OK)
        and supervisor.exists()
        and os.access(supervisor, os.X_OK)
    )


def resolve_run_backend(args: argparse.Namespace) -> str:
    requested = normalize_backend_name(getattr(args, "backend", "auto"))
    if requested != "auto":
        return requested
    state = load_state()
    cell = getattr(args, "cell", None)
    if cell in state.get("native_cells", {}):
        if patched_openshell_runtime_available():
            return "native-overlay"
        return "layer-clone"
    if cell in state.get("volume_cells", {}):
        return "volume-delta"
    if cell in state.get("overlay_cells", {}):
        return "local-overlay"
    return "openshell"


def command_run(args: argparse.Namespace) -> None:
    args.command = normalize_remainder(args.command)
    args.requested_backend = getattr(args, "backend", "auto")
    args.backend = resolve_run_backend(args)
    if getattr(args, "backend", "openshell") == BACKEND_LOCAL_OVERLAY:
        run_local_overlay(args)
        return
    if getattr(args, "backend", "openshell") == BACKEND_VOLUME_DELTA:
        run_openshell_volume(args)
        return
    if getattr(args, "backend", "openshell") == BACKEND_NATIVE_OVERLAY:
        run_openshell_native_overlay(args)
        return
    if getattr(args, "backend", "openshell") == BACKEND_LAYER_CLONE:
        run_openshell_layer_clone(args)
        return
    if getattr(args, "backend", "openshell") != BACKEND_OPENSHELL_DIRECT:
        raise SystemExit(f"unknown backend: {args.backend}")
    state = load_state()
    cell = cell_or_error(state, args.cell)
    checkpoint = create_checkpoint(state, args.cell, args.checkpoint_name) if args.checkpoint_before else None
    run_id = f"run_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    started_at = now_iso()
    cmd_result = openshell(["sandbox", "exec", "--name", args.cell, *args.command], check=False)
    finished_at = now_iso()

    # Capture recent OpenShell logs as the first event source for receipts.
    log_collection_mode = "sync"
    log_started = time.perf_counter()
    log_text = collect_openshell_logs(args.cell, args.logs_since)
    log_collect_ms = int(round((time.perf_counter() - log_started) * 1000))
    sync_logs = True
    log_artifact = ARTIFACT_DIR / f"{run_id}-openshell.log"
    log_artifact.write_text(log_text)
    event_text = log_text + cmd_result.stdout + cmd_result.stderr
    event_artifact = write_openshell_event_artifact(run_id, event_text)

    diff_summary = summarize_workspace_diff(args.cell, cell.get("workspace", DEFAULT_WORKSPACE))
    receipt_id = f"rcpt_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    event_store = append_event_store(
        run_id=run_id,
        receipt_id=receipt_id,
        cell_id=args.cell,
        runtime="openshell-derived",
        event_artifact=event_artifact,
    )
    decision = "accepted"
    restored_to = None
    restore_event = None
    if args.restore_on_fail and cmd_result.returncode != 0 and checkpoint:
        restore_event = restore_checkpoint(state, args.cell, checkpoint["checkpoint_id"])
        decision = "restored"
        restored_to = checkpoint["checkpoint_id"]

    receipt: dict[str, Any] = {
        "schema_version": "0.1",
        "receipt_id": receipt_id,
        "cell_id": args.cell,
        "openshell": {
            "sandbox_id": cell.get("sandbox_id"),
            "version": cell.get("openshell_version"),
        },
        "capabilities": {
            "runtime": "openshell-derived",
            "filesystem_checkpoint": True,
            "memory_checkpoint": False,
            "driver_overlay_checkpoint": False,
            "egress_governance": True,
            "credential_governance": True,
            "ocsf_events": True,
        },
        "run": {
            "run_id": run_id,
            "command": args.command,
            "started_at": started_at,
            "finished_at": finished_at,
            "exit_code": cmd_result.returncode,
            "stdout_sha256": sha256_text(cmd_result.stdout),
            "stderr_sha256": sha256_text(cmd_result.stderr),
            "stdout_preview": cmd_result.stdout[-4000:],
            "stderr_preview": cmd_result.stderr[-4000:],
        },
        "policy": {
            "revision": cell.get("active_policy_revision"),
            "events": summarize_logs(event_text),
            "log_collection": {"mode": log_collection_mode, "blocking_ms": log_collect_ms, "sync_complete": sync_logs},
            "structured_events": extract_openshell_events(event_text),
            "signals": extract_policy_signals(event_text),
        },
        "bindings": {
            "policy_revision": cell.get("active_policy_revision"),
            "checkpoint_policy_revision": checkpoint.get("policy_revision") if checkpoint else None,
            "checkpoint_sha256": checkpoint.get("sha256") if checkpoint else None,
            "receipt_binds_policy_and_checkpoint": bool(
                checkpoint is None
                or checkpoint.get("policy_revision") == cell.get("active_policy_revision")
            ),
        },
        "checkpoints": {
            "before": checkpoint["checkpoint_id"] if checkpoint else None,
            "before_metrics": checkpoint.get("metrics") if checkpoint else None,
            "before_policy_revision": checkpoint.get("policy_revision") if checkpoint else None,
            "after": None,
            "restored_to": restored_to,
            "restore_metrics": restore_event.get("metrics") if restore_event else None,
        },
        "files": diff_summary,
        "credentials": [],
        "artifacts": {"openshell_log": str(log_artifact), "openshell_events": event_artifact, "event_store": event_store},
        "decision": {"result": decision, "reason": "restore_on_fail" if decision == "restored" else "not_requested"},
        "created_at": now_iso(),
    }
    receipt_json = json.dumps(receipt, indent=2, sort_keys=True)
    receipt["hashes"] = {"receipt_sha256": sha256_text(receipt_json), "previous_receipt_sha256": None}
    receipt_json = json.dumps(receipt, indent=2, sort_keys=True)

    json_path = RECEIPT_DIR / f"{receipt_id}.json"
    md_path = RECEIPT_DIR / f"{receipt_id}.md"
    json_path.write_text(receipt_json)
    write_markdown_receipt(receipt, md_path)

    state = load_state()
    state["runs"][run_id] = {
        "run_id": run_id,
        "cell_id": args.cell,
        "receipt_id": receipt_id,
        "receipt_json": str(json_path),
        "receipt_markdown": str(md_path),
        "exit_code": cmd_result.returncode,
        "created_at": receipt["created_at"],
    }
    state["cells"][args.cell]["last_run_id"] = run_id
    save_state(state)

    print(cmd_result.stdout, end="")
    print(cmd_result.stderr, file=sys.stderr, end="")
    print(json.dumps({"run_id": run_id, "receipt_id": receipt_id, "receipt": str(json_path)}, indent=2, sort_keys=True))
    if args.exit_with_command:
        raise SystemExit(cmd_result.returncode)


def summarize_workspace_diff(cell_name: str, workspace: str) -> dict[str, Any]:
    script = (
        f"cd {workspace}; "
        "printf 'files=%s\\n' \"$(find . -mindepth 1 -type f | wc -l | tr -d ' ')\"; "
        "find . -mindepth 1 -maxdepth 2 -type f | sort | head -50"
    )
    result = openshell(["sandbox", "exec", "--name", cell_name, "sh", "-lc", script], check=False)
    artifact_id = f"diff_{int(time.time())}_{uuid.uuid4().hex[:8]}.txt"
    artifact_path = ARTIFACT_DIR / artifact_id
    artifact_path.write_text(result.stdout + result.stderr)
    return {
        "summary_artifact": str(artifact_path),
        "preview": strip_ansi(result.stdout)[-2000:],
    }


def command_receipt_show(args: argparse.Namespace) -> None:
    state = load_state()
    receipt_id = args.receipt
    if args.latest:
        if args.cell in state["cells"]:
            cell = state["cells"][args.cell]
        elif args.cell in state["native_cells"]:
            cell = native_cell_or_error(state, args.cell)
        elif args.cell in state["volume_cells"]:
            cell = volume_cell_or_error(state, args.cell)
        else:
            cell = overlay_cell_or_error(state, args.cell)
        run_id = cell.get("last_run_id")
        if not run_id:
            raise SystemExit(f"cell has no runs: {args.cell}")
        receipt_id = state["runs"][run_id]["receipt_id"]
    if not receipt_id:
        raise SystemExit("receipt id required unless --latest is used")
    path = RECEIPT_DIR / f"{receipt_id}.{args.format}"
    if not path.exists():
        raise SystemExit(f"receipt not found: {path}")
    print(path.read_text(), end="")


def command_decisions_show(args: argparse.Namespace) -> None:
    state = load_state()
    decision_id = args.decision
    if args.latest:
        if not args.cell:
            raise SystemExit("--latest requires --cell")
        if args.cell in state["volume_cells"]:
            cell = volume_cell_or_error(state, args.cell)
        elif args.cell in state["native_cells"]:
            cell = native_cell_or_error(state, args.cell)
        elif args.cell in state["cells"]:
            cell = cell_or_error(state, args.cell)
        else:
            cell = overlay_cell_or_error(state, args.cell)
        decision_id = cell.get("last_decision_id")
        if not decision_id:
            raise SystemExit(f"cell has no decisions: {args.cell}")
    if not decision_id:
        raise SystemExit("decision id required unless --latest is used")
    decision = state["decisions"].get(decision_id)
    if not decision:
        raise SystemExit(f"unknown decision: {decision_id}")
    path = Path(decision.get("artifact", ""))
    if args.artifact and path.exists():
        print(path.read_text(), end="")
    else:
        print(json.dumps(decision, indent=2, sort_keys=True))


def command_decisions_list(args: argparse.Namespace) -> None:
    state = load_state()
    decisions = list(state["decisions"].values())
    filters = {
        "cell_id": args.cell,
        "run_id": args.run,
        "receipt_id": args.receipt,
        "result": args.result,
    }
    for key, value in filters.items():
        if value is not None:
            decisions = [decision for decision in decisions if decision.get(key) == value]
    decisions.sort(key=lambda item: item.get("created_at", ""))
    if args.limit is not None:
        decisions = decisions[-args.limit :]
    if args.format == "jsonl":
        for decision in decisions:
            print(json.dumps(decision, sort_keys=True))
    else:
        print(json.dumps(decisions, indent=2, sort_keys=True))


def load_evidence_summary(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    text = path.read_text()
    match = re.search(r"```json\n(.*?)\n```", text, re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def latest_evidence_summary(pattern: str) -> tuple[Path, dict[str, Any]] | None:
    matches = sorted(EVIDENCE_DIR.glob(pattern), key=lambda p: p.stat().st_mtime)
    for path in reversed(matches):
        summary = load_evidence_summary(path)
        if summary is not None:
            return path, summary
    return None


def command_review_status(args: argparse.Namespace) -> None:
    state = load_state()
    event_store_rows = iter_event_store()
    if event_store_rows:
        index_event_store_rows(event_store_rows)
    else:
        init_event_db().close()
    event_store_info = {
        "mode": "jsonl+sqlite-index",
        "jsonl_path": str(EVENT_STORE_PATH),
        "sqlite_path": str(EVENT_DB_PATH),
        "jsonl_rows": len(event_store_rows),
        "sqlite_rows": event_store_db_count(),
        "indexed": event_store_db_count() >= len(event_store_rows),
    }
    benchmarks: dict[str, Any] = {}
    for profile in ("small", "medium", "pruned"):
        found = latest_evidence_summary(f"forkcell-volume-delta-benchmark-*-{profile}.md")
        if not found:
            continue
        path, summary = found
        benchmarks[profile] = {
            "evidence": str(path),
            "source_mib": summary.get("source_mib"),
            "source_files": summary.get("source_files"),
            "clean_checkpoint_ms": summary.get("clean_checkpoint_ms"),
            "run_checkpoint_ms": summary.get("run_checkpoint_ms"),
            "run_restore_ms": summary.get("run_restore_ms"),
            "restore_copied_files": summary.get("restore_copied_files"),
            "restore_copied_bytes": summary.get("restore_copied_bytes"),
            "restore_correct": summary.get("restore_correct"),
        }

    layer_clone_benchmarks: dict[str, Any] = {}
    for profile in ("small", "medium", "pruned"):
        found = latest_evidence_summary(f"forkcell-layer-clone-benchmark-*-{profile}.md")
        if not found:
            continue
        path, summary = found
        layer_clone_benchmarks[profile] = {
            "evidence": str(path),
            "source_mib": summary.get("source_mib"),
            "source_files": summary.get("source_files"),
            "prepare_run_ms": summary.get("prepare_run_ms"),
            "restore_ms": summary.get("restore_ms"),
            "restore_metadata_only": summary.get("restore_metadata_only"),
            "restore_correct": summary.get("restore_correct"),
            "runtime_benchmark_validated": summary.get("runtime_benchmark_validated"),
        }

    native_overlay_benchmarks: dict[str, Any] = {}
    for profile in ("small", "medium", "pruned"):
        found = latest_evidence_summary(f"forkcell-native-overlay-benchmark-*-{profile}.md")
        if not found:
            continue
        path, summary = found
        native_overlay_benchmarks[profile] = {
            "evidence": str(path),
            "source_mib": summary.get("source_mib"),
            "source_files": summary.get("source_files"),
            "import_ms": summary.get("import_ms"),
            "checkpoint_ms": summary.get("checkpoint_ms"),
            "restore_ms": summary.get("restore_ms"),
            "overlay_reset_ms": summary.get("overlay_reset_ms"),
            "restore_sync_ms": summary.get("restore_sync_ms"),
            "generation_switch": summary.get("generation_switch"),
            "gc_pending_count": summary.get("gc_pending_count"),
            "gc_async_ms": summary.get("gc_async_ms"),
            "sandbox_lifecycle_ms": summary.get("sandbox_lifecycle_ms"),
            "log_collect_ms": summary.get("log_collect_ms"),
            "sandbox_delete_ms": summary.get("sandbox_delete_ms"),
            "restore_call_ms": summary.get("restore_call_ms"),
            "total_restore_path_ms": summary.get("total_restore_path_ms"),
            "verify_ms": summary.get("verify_ms"),
            "metadata_only_restore": summary.get("metadata_only_restore"),
            "runtime_supported": summary.get("runtime_supported"),
            "runtime_benchmark_validated": summary.get("runtime_benchmark_validated"),
            "unsupported_reason": summary.get("unsupported_reason"),
            "verify_exit_code": summary.get("verify_exit_code"),
        }

    native_overlay_correctness_matrix = None
    found_native_overlay_matrix = latest_evidence_summary("forkcell-native-overlay-correctness-matrix-*.md")
    if found_native_overlay_matrix:
        path, summary = found_native_overlay_matrix
        native_overlay_correctness_matrix = {
            "evidence": str(path),
            "provider": summary.get("provider"),
            "matrix_passed": summary.get("matrix_passed"),
            "case_count": summary.get("case_count"),
            "passed_count": summary.get("passed_count"),
            "elapsed_ms": summary.get("elapsed_ms"),
            "cases": summary.get("cases"),
        }

    provider_validation = None
    found_provider = latest_evidence_summary("forkcell-volume-delta-provider-validation-*.md")
    if found_provider:
        path, summary = found_provider
        provider_validation = {
            "evidence": str(path),
            "checkpoint_duration_ms": summary.get("checkpoint_duration_ms"),
            "restore_duration_ms": summary.get("restore_duration_ms"),
            "auto_restore_decision_artifact_exists": summary.get("auto_restore_decision_artifact_exists"),
            "manual_restore_decision_artifact_exists": summary.get("manual_restore_decision_artifact_exists"),
            "decisions_list_count": summary.get("decisions_list_count"),
            "structured_event_count": summary.get("structured_event_count"),
            "event_store_appended_count": summary.get("event_store_appended_count"),
            "policy_checkpoint_sha256_matches_manifest": summary.get("policy_checkpoint_sha256_matches_manifest"),
        }

    native_substrate_plan = None
    found_native_plan = latest_evidence_summary("forkcell-native-substrate-plan-*.md")
    if found_native_plan:
        path, summary = found_native_plan
        native_substrate_plan = {
            "evidence": str(path),
            "doc": summary.get("doc"),
            "missing_markers": summary.get("missing"),
            "has_target_architecture": summary.get("has_target_architecture"),
            "has_api_surface": summary.get("has_api_surface"),
            "has_safety_requirements": summary.get("has_safety_requirements"),
            "has_implementation_slices": summary.get("has_implementation_slices"),
            "has_current_readiness": summary.get("has_current_readiness"),
        }

    upstream_workspace_contract = None
    found_workspace_contract = latest_evidence_summary("forkcell-openshell-workspace-contract-*.md")
    if found_workspace_contract:
        path, summary = found_workspace_contract
        upstream_workspace_contract = {
            "evidence": str(path),
            "cargo_test_ok": summary.get("cargo_test_ok"),
            "passed_count": summary.get("passed_count"),
            "workspace_config_struct": summary.get("workspace_config_struct"),
            "workspace_field_in_driver_config": summary.get("workspace_field_in_driver_config"),
            "workspace_validation_fn": summary.get("workspace_validation_fn"),
            "workspace_backing_mount_fn": summary.get("workspace_backing_mount_fn"),
            "duplicate_target_guard": summary.get("duplicate_target_guard"),
            "workspace_accept_test": summary.get("workspace_accept_test"),
            "workspace_supervisor_env_test": summary.get("workspace_supervisor_env_test"),
            "workspace_backing_mount_test": summary.get("workspace_backing_mount_test"),
            "workspace_reject_tests": summary.get("workspace_reject_tests"),
        }

    upstream_supervisor_workspace = None
    found_supervisor_workspace = latest_evidence_summary("forkcell-openshell-supervisor-workspace-*.md")
    if found_supervisor_workspace:
        path, summary = found_supervisor_workspace
        upstream_supervisor_workspace = {
            "evidence": str(path),
            "cargo_test_ok": summary.get("cargo_test_ok"),
            "passed_count": summary.get("passed_count"),
            "workspace_env_constant": summary.get("workspace_env_constant"),
            "workspace_supervisor_only": summary.get("workspace_supervisor_only"),
            "workspace_config_parser": summary.get("workspace_config_parser"),
            "workspace_prepare_from_env": summary.get("workspace_prepare_from_env"),
            "workspace_prepare_called": summary.get("workspace_prepare_called"),
            "workspace_overlay_plan": summary.get("workspace_overlay_plan"),
            "workspace_overlay_mount_fn": summary.get("workspace_overlay_mount_fn"),
            "workspace_overlay_chown_fn": summary.get("workspace_overlay_chown_fn"),
            "workspace_parse_tests": summary.get("workspace_parse_tests"),
        }

    native_provider_validation = None
    found_native_provider = latest_evidence_summary("forkcell-native-overlay-provider-*.md")
    if found_native_provider:
        path, summary = found_native_provider
        native_provider_validation = {
            "evidence": str(path),
            "provider": summary.get("provider"),
            "init_ok": summary.get("init_ok"),
            "driver_config_ok": summary.get("driver_config_ok"),
            "checkpoint_metadata_only": summary.get("checkpoint_metadata_only"),
            "restore_metadata_only": summary.get("restore_metadata_only"),
            "checkpoint_duration_ms": summary.get("checkpoint_duration_ms"),
            "restore_duration_ms": summary.get("restore_duration_ms"),
            "runtime_benchmark_validated": summary.get("runtime_benchmark_validated"),
        }

    native_policy_validation = None
    found_native_policy = latest_evidence_summary("forkcell-native-overlay-policy-*.md")
    if found_native_policy:
        path, summary = found_native_policy
        native_policy_validation = {
            "evidence": str(path),
            "runtime": summary.get("runtime"),
            "policy_apply_applied_to": summary.get("policy_apply_applied_to"),
            "policy_apply_deferred_to_run": summary.get("policy_apply_deferred_to_run"),
            "policy_revision": summary.get("policy_revision"),
            "policy_sha256": summary.get("policy_sha256"),
            "checkpoint_policy_revision": summary.get("checkpoint_policy_revision"),
            "checkpoint_sha256": summary.get("checkpoint_sha256"),
            "checkpoint_sha256_kind": summary.get("checkpoint_sha256_kind"),
            "deny_host_denied_events": summary.get("deny_host_denied_events"),
            "deny_host_policy_signals": summary.get("deny_host_policy_signals"),
            "deny_host_binding_ok": summary.get("deny_host_binding_ok"),
            "allow_get_exit_code": summary.get("allow_get_exit_code"),
            "allow_get_allowed_events": summary.get("allow_get_allowed_events"),
            "allow_get_structured_events": summary.get("allow_get_structured_events"),
            "allow_get_binding_ok": summary.get("allow_get_binding_ok"),
            "deny_l7_denied_events": summary.get("deny_l7_denied_events"),
            "deny_l7_policy_signals": summary.get("deny_l7_policy_signals"),
            "deny_l7_binding_ok": summary.get("deny_l7_binding_ok"),
            "events_query_policy_count": summary.get("events_query_policy_count"),
        }

    native_runtime_benchmark = None
    if native_overlay_benchmarks:
        profile = "pruned" if "pruned" in native_overlay_benchmarks else ("medium" if "medium" in native_overlay_benchmarks else "small")
        summary = native_overlay_benchmarks[profile]
        native_runtime_benchmark = {
            "evidence": summary.get("evidence"),
            "profile": profile,
            "checkpoint_ms": summary.get("checkpoint_ms"),
            "restore_ms": summary.get("restore_ms"),
            "metadata_only_restore": summary.get("metadata_only_restore"),
            "runtime_supported": summary.get("runtime_supported"),
            "runtime_benchmark_validated": summary.get("runtime_benchmark_validated"),
            "unsupported_reason": summary.get("unsupported_reason"),
            "verify_exit_code": summary.get("verify_exit_code"),
        }

    agent_api_validation = None
    found_agent_api = latest_evidence_summary("forkcell-agent-api-validation-*.md")
    if found_agent_api:
        path, summary = found_agent_api
        agent_api_validation = {
            "evidence": str(path),
            "backend": summary.get("backend"),
            "api_facade_validated": summary.get("api_facade_validated"),
            "manual_checkpoint_metadata_only": summary.get("manual_checkpoint_metadata_only"),
            "restore_metadata_only": summary.get("restore_metadata_only"),
            "restore_ms": summary.get("restore_ms"),
            "verify_exit_code": summary.get("verify_exit_code"),
        }

    patch_bundle = None
    found_patch_bundle = latest_evidence_summary("forkcell-openshell-patch-bundle-*.md")
    if found_patch_bundle:
        path, summary = found_patch_bundle
        patch_bundle = {
            "evidence": str(path),
            "patch": summary.get("patch"),
            "patch_bytes": summary.get("patch_bytes"),
            "patch_sha256": summary.get("patch_sha256"),
            "changed_file_count": len(summary.get("changed_files", [])),
            "has_workspace_config_env": summary.get("has_workspace_config_env"),
            "has_docker_workspace_parser": summary.get("has_docker_workspace_parser"),
            "has_supervisor_overlay_mount": summary.get("has_supervisor_overlay_mount"),
            "has_pre_seccomp_workspace_prepare": summary.get("has_pre_seccomp_workspace_prepare"),
        }

    patched_runtime_build = None
    found_patched_runtime_build = latest_evidence_summary("forkcell-patched-runtime-build-*.md")
    if found_patched_runtime_build:
        path, summary = found_patched_runtime_build
        patched_runtime_build = {
            "evidence": str(path),
            "build_validated": summary.get("build_validated"),
            "binaries": summary.get("binaries"),
        }

    runtime_packaging_validation = None
    found_runtime_packaging = latest_evidence_summary("forkcell-runtime-packaging-*.md")
    if found_runtime_packaging:
        path, summary = found_runtime_packaging
        runtime_packaging_validation = {
            "evidence": str(path),
            "runtime": summary.get("runtime"),
            "install_dir": summary.get("install_dir"),
            "binary_count": summary.get("binary_count"),
            "detect_available": summary.get("detect_available"),
            "detect_version_locked": summary.get("detect_version_locked"),
            "detect_after_run_available": summary.get("detect_after_run_available"),
            "packaged_runtime_used": summary.get("packaged_runtime_used"),
            "packaged_runtime_benchmark_validated": summary.get("packaged_runtime_benchmark_validated"),
            "packaged_runtime_restore_ms": summary.get("packaged_runtime_restore_ms"),
            "packaged_runtime_restore_sync_ms": summary.get("packaged_runtime_restore_sync_ms"),
            "version_lock": summary.get("version_lock"),
        }

    checkpoint_graph_validation = None
    found_checkpoint_graph = latest_evidence_summary("forkcell-checkpoint-graph-gc-*.md")
    if found_checkpoint_graph:
        path, summary = found_checkpoint_graph
        checkpoint_graph_validation = {
            "evidence": str(path),
            "source_checkpoint": summary.get("source_checkpoint"),
            "forked_from": summary.get("forked_from"),
            "graph_node_count_before_delete": summary.get("graph_node_count_before_delete"),
            "gc_dry_run_stale_node_count": summary.get("gc_dry_run_stale_node_count"),
            "gc_removed_node_count": summary.get("gc_removed_node_count"),
            "graph_node_count_after_gc": summary.get("graph_node_count_after_gc"),
            "fork_verify_exit_code": summary.get("fork_verify_exit_code"),
            "source_verify_exit_code": summary.get("source_verify_exit_code"),
        }

    patched_runtime_full_validation = None
    found_patched_runtime_full = latest_evidence_summary("forkcell-patched-runtime-full-*.md")
    if found_patched_runtime_full:
        path, summary = found_patched_runtime_full
        patched_runtime_full_validation = {
            "evidence": str(path),
            "native_small_validated": summary.get("native_small_validated"),
            "native_small_restore_ms": summary.get("native_small_restore_ms"),
            "native_medium_validated": summary.get("native_medium_validated"),
            "native_medium_restore_ms": summary.get("native_medium_restore_ms"),
            "native_pruned_validated": summary.get("native_pruned_validated"),
            "native_pruned_restore_ms": summary.get("native_pruned_restore_ms"),
            "native_correctness_matrix_passed": summary.get("native_correctness_matrix_passed"),
            "native_correctness_matrix_cases": summary.get("native_correctness_matrix_cases"),
            "agent_api_facade_validated": summary.get("agent_api_facade_validated"),
            "agent_api_restore_ms": summary.get("agent_api_restore_ms"),
            "review_native_status": summary.get("review_native_status"),
            "sandbox_list": summary.get("sandbox_list"),
            "forkcell_volume_residue": summary.get("forkcell_volume_residue"),
        }


    backend_selection_validation = None
    found_backend_selection = latest_evidence_summary("forkcell-backend-selection-*.md")
    if found_backend_selection:
        path, summary = found_backend_selection
        backend_selection_validation = {
            "evidence": str(path),
            "auto_native_runtime": summary.get("auto_native_runtime"),
            "auto_native_restore_metadata_only": summary.get("auto_native_restore_metadata_only"),
            "auto_native_degraded": summary.get("auto_native_degraded"),
            "auto_native_runtime_selection": summary.get("auto_native_runtime_selection"),
            "explicit_fallback_runtime": summary.get("explicit_fallback_runtime"),
            "explicit_fallback_degraded": summary.get("explicit_fallback_degraded"),
            "explicit_fallback_runtime_selection": summary.get("explicit_fallback_runtime_selection"),
            "auto_volume_runtime": summary.get("auto_volume_runtime"),
            "native_without_patched_runtime_resolves_to": summary.get("native_without_patched_runtime_resolves_to"),
        }

    ci_gate_validation = None
    found_ci_gate = latest_evidence_summary("forkcell-ci-gate-*.md")
    if found_ci_gate:
        path, summary = found_ci_gate
        ci_gate_validation = {
            "evidence": str(path),
            "ci_gate_passed": summary.get("ci_gate_passed"),
            "build_step_ran": summary.get("build_step_ran"),
            "step_count": len(summary.get("steps", [])),
            "listener_17671_residue": summary.get("listener_17671_residue"),
            "docker_container_residue": summary.get("docker_container_residue"),
            "docker_volume_residue": summary.get("docker_volume_residue"),
        }

    native_runtime_validated = bool(
        native_runtime_benchmark and native_runtime_benchmark.get("runtime_benchmark_validated")
    )
    patch_bundle_ready = bool(
        patch_bundle
        and patch_bundle.get("patch")
        and patch_bundle.get("patch_sha256")
        and patch_bundle.get("has_workspace_config_env")
        and patch_bundle.get("has_docker_workspace_parser")
        and patch_bundle.get("has_supervisor_overlay_mount")
        and patch_bundle.get("has_pre_seccomp_workspace_prepare")
    )
    patched_runtime_build_validated = bool(
        patched_runtime_build and patched_runtime_build.get("build_validated")
    )
    runtime_packaging_validated = bool(
        runtime_packaging_validation
        and runtime_packaging_validation.get("binary_count") == 3
        and runtime_packaging_validation.get("detect_available") is True
        and runtime_packaging_validation.get("detect_version_locked") is True
        and runtime_packaging_validation.get("packaged_runtime_used") is True
        and runtime_packaging_validation.get("packaged_runtime_benchmark_validated") is True
        and metric_lt(runtime_packaging_validation.get("packaged_runtime_restore_sync_ms"), 100)
    )
    checkpoint_graph_validated = bool(
        checkpoint_graph_validation
        and checkpoint_graph_validation.get("graph_node_count_before_delete", 0) >= 4
        and checkpoint_graph_validation.get("gc_removed_node_count") == checkpoint_graph_validation.get("gc_dry_run_stale_node_count")
        and checkpoint_graph_validation.get("graph_node_count_after_gc") == 0
        and checkpoint_graph_validation.get("fork_verify_exit_code") == 0
        and checkpoint_graph_validation.get("source_verify_exit_code") == 0
    )
    patched_runtime_full_validated = bool(
        patched_runtime_full_validation
        and patched_runtime_full_validation.get("native_small_validated")
        and patched_runtime_full_validation.get("native_medium_validated")
        and patched_runtime_full_validation.get("native_pruned_validated")
        and patched_runtime_full_validation.get("agent_api_facade_validated")
        and patched_runtime_full_validation.get("review_native_status") == "runtime-validated"
        and patched_runtime_full_validation.get("sandbox_list") == "No sandboxes found."
        and patched_runtime_full_validation.get("forkcell_volume_residue") == []
    )
    backend_selection_validated = bool(
        backend_selection_validation
        and backend_selection_validation.get("auto_native_runtime") == "native-overlay"
        and backend_selection_validation.get("auto_native_restore_metadata_only") is True
        and backend_selection_validation.get("auto_native_degraded") is False
        and backend_selection_validation.get("explicit_fallback_runtime") == "layer-clone"
        and backend_selection_validation.get("explicit_fallback_degraded") is True
        and backend_selection_validation.get("auto_volume_runtime") == "volume-delta"
        and backend_selection_validation.get("native_without_patched_runtime_resolves_to") == "layer-clone"
    )
    native_policy_validated = bool(
        native_policy_validation
        and native_policy_validation.get("runtime") == "native-overlay"
        and native_policy_validation.get("policy_apply_applied_to") == "native-overlay"
        and native_policy_validation.get("policy_apply_deferred_to_run") is True
        and native_policy_validation.get("policy_revision")
        and native_policy_validation.get("policy_sha256")
        and native_policy_validation.get("checkpoint_policy_revision") == native_policy_validation.get("policy_revision")
        and native_policy_validation.get("checkpoint_sha256")
        and native_policy_validation.get("checkpoint_sha256_kind") == "native_metadata_identity"
        and native_policy_validation.get("deny_host_binding_ok") is True
        and native_policy_validation.get("allow_get_exit_code") == 0
        and native_policy_validation.get("allow_get_binding_ok") is True
        and native_policy_validation.get("deny_l7_binding_ok") is True
        and (native_policy_validation.get("deny_host_denied_events") or 0) > 0
        and (native_policy_validation.get("deny_l7_denied_events") or 0) > 0
        and (
            (native_policy_validation.get("allow_get_allowed_events") or 0) > 0
            or (native_policy_validation.get("allow_get_structured_events") or 0) > 0
        )
    )
    ci_gate_validated = bool(
        ci_gate_validation
        and ci_gate_validation.get("ci_gate_passed") is True
        and ci_gate_validation.get("build_step_ran") is True
        and ci_gate_validation.get("listener_17671_residue") in ("", None)
        and ci_gate_validation.get("docker_container_residue") == []
        and ci_gate_validation.get("docker_volume_residue") == []
    )
    native_plan_ready = bool(native_substrate_plan and not native_substrate_plan.get("missing_markers"))
    native_status = "runtime-validated" if native_runtime_validated else ("design-ready" if native_plan_ready else "not-ready")
    remaining_gaps: list[str] = []
    if not event_store_info["indexed"]:
        remaining_gaps.append("event store SQLite index is not caught up with the JSONL event log")
    if not native_runtime_validated:
        remaining_gaps.append(
            "true native-overlay runtime benchmark is not validated until a patched Linux supervisor image is available"
        )

    report = {
        "schema_version": "forkcell-review-v1",
        "generated_at": now_iso(),
        "implementation": {
            "primary_backend": "native-overlay",
            "default_run_backend": "auto",
            "auto_backend_strategy": {
                "native_cell_with_patched_runtime": "native-overlay",
                "native_cell_without_patched_runtime": "layer-clone",
                "volume_cell": "volume-delta",
                "overlay_cell": "local-overlay",
                "openshell_cell": "openshell",
            },
            "checkpoint_provider": "volume-delta",
            "runtime_governance": "OpenShell",
            "openshell_binary": openshell_bin(),
            "openshell_version": require_openshell(),
            "filesystem_checkpoint": True,
            "memory_checkpoint": False,
            "metadata_only_restore": False,
            "native_overlay_provider": bool(native_provider_validation and native_provider_validation.get("driver_config_ok")),
            "production_fast_backend": "native-overlay",
            "fast_restore_backend": "native-overlay" if native_runtime_validated else ("layer-clone" if layer_clone_benchmarks else None),
            "fallback_backend": "layer-clone",
            "openshell_patch_bundle_ready": patch_bundle_ready,
            "patched_runtime_build_validated": patched_runtime_build_validated,
            "runtime_packaging_validated": runtime_packaging_validated,
            "checkpoint_graph_validated": checkpoint_graph_validated,
            "patched_runtime_full_validated": patched_runtime_full_validated,
            "backend_selection_validated": backend_selection_validated,
            "ci_gate_validated": ci_gate_validated,
            "agent_api_facade": bool(agent_api_validation and agent_api_validation.get("api_facade_validated")),
            "decision_artifacts": True,
            "event_store": event_store_info,
        },
        "state_counts": {
            "cells": len(state.get("cells", {})),
            "volume_cells": len(state.get("volume_cells", {})),
            "overlay_cells": len(state.get("overlay_cells", {})),
            "native_cells": len(state.get("native_cells", {})),
            "checkpoints": len(state.get("checkpoints", {})),
            "runs": len(state.get("runs", {})),
            "decisions": len(state.get("decisions", {})),
            "policies": len(state.get("policies", {})),
        },
        "provider_validation": provider_validation,
        "native_substrate_plan": native_substrate_plan,
        "upstream_workspace_contract": upstream_workspace_contract,
        "upstream_supervisor_workspace": upstream_supervisor_workspace,
        "native_provider_validation": native_provider_validation,
        "native_policy_validation": native_policy_validation,
        "native_runtime_benchmark": native_runtime_benchmark,
        "agent_api_validation": agent_api_validation,
        "patch_bundle": patch_bundle,
        "patched_runtime_build": patched_runtime_build,
        "runtime_packaging_validation": runtime_packaging_validation,
        "checkpoint_graph_validation": checkpoint_graph_validation,
        "patched_runtime_full_validation": patched_runtime_full_validation,
        "backend_selection_validation": backend_selection_validation,
        "ci_gate_validation": ci_gate_validation,
        "benchmarks": benchmarks,
        "layer_clone_benchmarks": layer_clone_benchmarks,
        "native_overlay_benchmarks": native_overlay_benchmarks,
        "native_overlay_correctness_matrix": native_overlay_correctness_matrix,
        "practical_targets": {
            "small_restore_lt_1s": benchmarks.get("small", {}).get("run_restore_ms", 10**9) < 1000,
            "medium_restore_lt_5s": benchmarks.get("medium", {}).get("run_restore_ms", 10**9) < 5000,
            "restore_correct_all_reported": all(item.get("restore_correct") is True for item in benchmarks.values()),
            "layer_clone_small_restore_lt_1s": layer_clone_benchmarks.get("small", {}).get("restore_ms", 10**9) < 1000,
            "layer_clone_medium_restore_lt_5s": layer_clone_benchmarks.get("medium", {}).get("restore_ms", 10**9) < 5000,
            "layer_clone_pruned_restore_lt_5s": layer_clone_benchmarks.get("pruned", {}).get("restore_ms", 10**9) < 5000,
            "layer_clone_restore_correct_all_reported": all(
                item.get("restore_correct") is True and item.get("runtime_benchmark_validated") is True
                for item in layer_clone_benchmarks.values()
            ) if layer_clone_benchmarks else False,
            "native_overlay_small_restore_lt_1s": native_overlay_benchmarks.get("small", {}).get("restore_ms", 10**9) < 1000,
            "native_overlay_medium_restore_lt_5s": native_overlay_benchmarks.get("medium", {}).get("restore_ms", 10**9) < 5000,
            "native_overlay_pruned_restore_lt_5s": native_overlay_benchmarks.get("pruned", {}).get("restore_ms", 10**9) < 5000,
            "native_overlay_restore_correct_all_reported": all(
                item.get("metadata_only_restore") is True and item.get("runtime_benchmark_validated") is True
                for item in native_overlay_benchmarks.values()
            ) if native_overlay_benchmarks else False,
            "native_overlay_restore_sync_lt_100ms_all_reported": all(
                metric_lt(item.get("restore_sync_ms"), 100)
                and item.get("generation_switch") is True
                for item in native_overlay_benchmarks.values()
            ) if native_overlay_benchmarks else False,
            "native_overlay_correctness_matrix_passed": bool(
                native_overlay_correctness_matrix
                and native_overlay_correctness_matrix.get("matrix_passed") is True
                and native_overlay_correctness_matrix.get("passed_count") == native_overlay_correctness_matrix.get("case_count")
            ),
            "native_overlay_policy_validated": native_policy_validated,
            "openshell_patch_bundle_ready": patch_bundle_ready,
            "patched_runtime_build_validated": patched_runtime_build_validated,
            "runtime_packaging_validated": runtime_packaging_validated,
            "checkpoint_graph_validated": checkpoint_graph_validated,
            "patched_runtime_full_validated": patched_runtime_full_validated,
            "backend_selection_validated": backend_selection_validated,
            "ci_gate_validated": ci_gate_validated,
            "agent_api_facade_validated": bool(agent_api_validation and agent_api_validation.get("api_facade_validated")),
            "native_substrate_plan_ready": native_plan_ready,
        },
        "native_substrate_readiness": {
            "status": native_status,
            "current_backend": "native-overlay",
            "next_backend": None,
            "fallback_backend": "layer-clone",
            "metadata_only_restore_implemented": bool(
                native_runtime_benchmark
                and native_runtime_benchmark.get("metadata_only_restore")
                and native_runtime_benchmark.get("runtime_benchmark_validated")
            ),
            "generation_switch_restore_implemented": bool(
                native_overlay_benchmarks
                and all(item.get("generation_switch") is True for item in native_overlay_benchmarks.values())
            ),
            "restore_sync_lt_100ms": bool(
                native_overlay_benchmarks
                and all(metric_lt(item.get("restore_sync_ms"), 100) for item in native_overlay_benchmarks.values())
            ),
            "policy_apply_supported": bool(
                native_policy_validation
                and native_policy_validation.get("policy_apply_applied_to") == "native-overlay"
                and native_policy_validation.get("policy_apply_deferred_to_run") is True
            ),
            "policy_smoke_validated": native_policy_validated,
            "warm_helper_required_for_restore": False,
            "warm_helper_decision": "not_required_after_generation_switch_restore",
            "requires_upstream_openshell_patch": True,
            "plan_doc": native_substrate_plan.get("doc") if native_substrate_plan else None,
            "plan_evidence": native_substrate_plan.get("evidence") if native_substrate_plan else None,
            "workspace_contract_parser_implemented": bool(
                upstream_workspace_contract
                and upstream_workspace_contract.get("cargo_test_ok")
                and upstream_workspace_contract.get("workspace_config_struct")
                and upstream_workspace_contract.get("workspace_validation_fn")
                and upstream_workspace_contract.get("workspace_backing_mount_fn")
                and upstream_workspace_contract.get("workspace_accept_test")
                and upstream_workspace_contract.get("workspace_supervisor_env_test")
                and upstream_workspace_contract.get("workspace_backing_mount_test")
                and upstream_workspace_contract.get("workspace_reject_tests")
            ),
            "workspace_contract_evidence": upstream_workspace_contract.get("evidence")
            if upstream_workspace_contract
            else None,
            "supervisor_workspace_parser_implemented": bool(
                upstream_supervisor_workspace
                and upstream_supervisor_workspace.get("cargo_test_ok")
                and upstream_supervisor_workspace.get("workspace_config_parser")
                and upstream_supervisor_workspace.get("workspace_prepare_from_env")
                and upstream_supervisor_workspace.get("workspace_prepare_called")
                and upstream_supervisor_workspace.get("workspace_overlay_plan")
                and upstream_supervisor_workspace.get("workspace_overlay_mount_fn")
                and upstream_supervisor_workspace.get("workspace_overlay_chown_fn")
                and upstream_supervisor_workspace.get("workspace_parse_tests")
            ),
            "supervisor_workspace_evidence": upstream_supervisor_workspace.get("evidence")
            if upstream_supervisor_workspace
            else None,
            "native_provider_implemented": bool(
                native_provider_validation
                and native_provider_validation.get("init_ok")
                and native_provider_validation.get("driver_config_ok")
                and native_provider_validation.get("checkpoint_metadata_only")
                and native_provider_validation.get("restore_metadata_only")
            ),
            "native_provider_evidence": native_provider_validation.get("evidence")
            if native_provider_validation
            else None,
            "native_runtime_benchmark_validated": native_runtime_validated,
            "native_runtime_benchmark_evidence": native_runtime_benchmark.get("evidence")
            if native_runtime_benchmark
            else None,
            "native_runtime_unsupported_reason": native_runtime_benchmark.get("unsupported_reason")
            if native_runtime_benchmark
            else None,
            "patch_bundle_ready": patch_bundle_ready,
            "patch_bundle_evidence": patch_bundle.get("evidence") if patch_bundle else None,
            "patch_sha256": patch_bundle.get("patch_sha256") if patch_bundle else None,
            "patched_runtime_build_validated": patched_runtime_build_validated,
            "patched_runtime_build_evidence": patched_runtime_build.get("evidence")
            if patched_runtime_build
            else None,
            "runtime_packaging_validated": runtime_packaging_validated,
            "runtime_packaging_evidence": runtime_packaging_validation.get("evidence")
            if runtime_packaging_validation
            else None,
            "checkpoint_graph_validated": checkpoint_graph_validated,
            "checkpoint_graph_evidence": checkpoint_graph_validation.get("evidence")
            if checkpoint_graph_validation
            else None,
            "patched_runtime_full_validated": patched_runtime_full_validated,
            "patched_runtime_full_evidence": patched_runtime_full_validation.get("evidence")
            if patched_runtime_full_validation
            else None,
            "backend_selection_validated": backend_selection_validated,
            "backend_selection_evidence": backend_selection_validation.get("evidence")
            if backend_selection_validation
            else None,
            "ci_gate_validated": ci_gate_validated,
            "ci_gate_evidence": ci_gate_validation.get("evidence") if ci_gate_validation else None,
            "missing_markers": native_substrate_plan.get("missing_markers") if native_substrate_plan else None,
        },
        "remaining_gaps": remaining_gaps,
    }
    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
        return

    lines = [
        "# ForkCell Review",
        "",
        f"- Generated: `{report['generated_at']}`",
        f"- Primary backend: `{report['implementation']['primary_backend']}`",
        f"- Checkpoint provider: `{report['implementation']['checkpoint_provider']}`",
            f"- OpenShell patch bundle ready: `{report['implementation']['openshell_patch_bundle_ready']}`",
            f"- Patched runtime build validated: `{report['implementation']['patched_runtime_build_validated']}`",
            f"- Runtime packaging validated: `{report['implementation']['runtime_packaging_validated']}`",
            f"- Checkpoint graph validated: `{report['implementation']['checkpoint_graph_validated']}`",
            f"- Patched runtime full validated: `{report['implementation']['patched_runtime_full_validated']}`",
            f"- Backend selection validated: `{report['implementation']['backend_selection_validated']}`",
            f"- CI gate validated: `{report['implementation']['ci_gate_validated']}`",
            f"- Decision artifacts: `{report['implementation']['decision_artifacts']}`",
            f"- Agent API facade: `{report['implementation']['agent_api_facade']}`",
            f"- State counts: `{json.dumps(report['state_counts'], sort_keys=True)}`",
        "",
        "## Benchmarks",
    ]
    for profile, item in benchmarks.items():
        lines.append(
            f"- `{profile}`: `{item.get('source_mib')}` MiB / `{item.get('source_files')}` files; "
            f"checkpoint `{item.get('run_checkpoint_ms')}` ms; restore `{item.get('run_restore_ms')}` ms; correct `{item.get('restore_correct')}`"
        )
    lines.append("")
    lines.append("## Layer Clone Benchmarks")
    for profile, item in layer_clone_benchmarks.items():
        lines.append(
            f"- `{profile}`: `{item.get('source_mib')}` MiB / `{item.get('source_files')}` files; "
            f"prepare `{item.get('prepare_run_ms')}` ms; restore `{item.get('restore_ms')}` ms; "
            f"metadata-only `{item.get('restore_metadata_only')}`; correct `{item.get('restore_correct')}`"
        )
    lines.append("")
    lines.append("## Native Overlay Benchmarks")
    for profile, item in native_overlay_benchmarks.items():
        lines.append(
            f"- `{profile}`: `{item.get('source_mib')}` MiB / `{item.get('source_files')}` files; "
            f"import `{item.get('import_ms')}` ms; checkpoint `{item.get('checkpoint_ms')}` ms; "
            f"restore `{item.get('restore_ms')}` ms; overlay reset `{item.get('overlay_reset_ms')}` ms; "
            f"restore sync `{item.get('restore_sync_ms')}` ms; sandbox lifecycle `{item.get('sandbox_lifecycle_ms')}` ms; "
            f"verify `{item.get('verify_ms')}` ms; gc pending `{item.get('gc_pending_count')}`; "
            f"metadata-only `{item.get('metadata_only_restore')}`; "
            f"validated `{item.get('runtime_benchmark_validated')}`"
        )
    if native_overlay_correctness_matrix:
        case_names = [
            item.get("case")
            for item in (native_overlay_correctness_matrix.get("cases") or [])
            if isinstance(item, dict)
        ]
        lines.extend(
            [
                "",
                "## Native Overlay Correctness Matrix",
                (
                    f"- Passed `{native_overlay_correctness_matrix.get('passed_count')}` / "
                    f"`{native_overlay_correctness_matrix.get('case_count')}`; "
                    f"matrix passed `{native_overlay_correctness_matrix.get('matrix_passed')}`; "
                    f"cases `{case_names}`"
                ),
                f"- Evidence: `{native_overlay_correctness_matrix.get('evidence')}`",
            ]
        )
    lines.extend(
        [
            "",
            "## Targets",
            "",
            f"- Small restore <1s: `{report['practical_targets']['small_restore_lt_1s']}`",
            f"- Medium restore <5s: `{report['practical_targets']['medium_restore_lt_5s']}`",
            f"- Restore correctness for reported benchmarks: `{report['practical_targets']['restore_correct_all_reported']}`",
            f"- Layer clone small restore <1s: `{report['practical_targets']['layer_clone_small_restore_lt_1s']}`",
            f"- Layer clone medium restore <5s: `{report['practical_targets']['layer_clone_medium_restore_lt_5s']}`",
            f"- Layer clone pruned restore <5s: `{report['practical_targets']['layer_clone_pruned_restore_lt_5s']}`",
            f"- Layer clone runtime correctness: `{report['practical_targets']['layer_clone_restore_correct_all_reported']}`",
            f"- Native overlay small restore <1s: `{report['practical_targets']['native_overlay_small_restore_lt_1s']}`",
            f"- Native overlay medium restore <5s: `{report['practical_targets']['native_overlay_medium_restore_lt_5s']}`",
            f"- Native overlay pruned restore <5s: `{report['practical_targets']['native_overlay_pruned_restore_lt_5s']}`",
            f"- Native overlay runtime correctness: `{report['practical_targets']['native_overlay_restore_correct_all_reported']}`",
            f"- Native overlay restore sync <100ms: `{report['practical_targets']['native_overlay_restore_sync_lt_100ms_all_reported']}`",
            f"- Native overlay correctness matrix: `{report['practical_targets']['native_overlay_correctness_matrix_passed']}`",
            f"- Native overlay policy validated: `{report['practical_targets']['native_overlay_policy_validated']}`",
            f"- OpenShell patch bundle ready: `{report['practical_targets']['openshell_patch_bundle_ready']}`",
            f"- Patched runtime build validated: `{report['practical_targets']['patched_runtime_build_validated']}`",
            f"- Runtime packaging validated: `{report['practical_targets']['runtime_packaging_validated']}`",
            f"- Checkpoint graph validated: `{report['practical_targets']['checkpoint_graph_validated']}`",
            f"- Patched runtime full validated: `{report['practical_targets']['patched_runtime_full_validated']}`",
            f"- Backend selection validated: `{report['practical_targets']['backend_selection_validated']}`",
            f"- CI gate validated: `{report['practical_targets']['ci_gate_validated']}`",
            f"- Agent API facade validated: `{report['practical_targets']['agent_api_facade_validated']}`",
            f"- Native substrate plan ready: `{report['practical_targets']['native_substrate_plan_ready']}`",
            "",
            "## Native Substrate Readiness",
            "",
            f"- Status: `{report['native_substrate_readiness']['status']}`",
            f"- Current backend: `{report['native_substrate_readiness']['current_backend']}`",
            f"- Next backend: `{report['native_substrate_readiness']['next_backend']}`",
            f"- Metadata-only restore implemented: `{report['native_substrate_readiness']['metadata_only_restore_implemented']}`",
            f"- Generation-switch restore implemented: `{report['native_substrate_readiness']['generation_switch_restore_implemented']}`",
            f"- Restore sync <100ms: `{report['native_substrate_readiness']['restore_sync_lt_100ms']}`",
            f"- Policy apply supported: `{report['native_substrate_readiness']['policy_apply_supported']}`",
            f"- Policy smoke validated: `{report['native_substrate_readiness']['policy_smoke_validated']}`",
            f"- Warm helper required for restore: `{report['native_substrate_readiness']['warm_helper_required_for_restore']}`",
            f"- Warm helper decision: `{report['native_substrate_readiness']['warm_helper_decision']}`",
            f"- Requires upstream OpenShell patch: `{report['native_substrate_readiness']['requires_upstream_openshell_patch']}`",
            f"- Plan doc: `{report['native_substrate_readiness']['plan_doc']}`",
            f"- Plan evidence: `{report['native_substrate_readiness']['plan_evidence']}`",
            f"- Workspace contract parser implemented: `{report['native_substrate_readiness']['workspace_contract_parser_implemented']}`",
            f"- Workspace contract evidence: `{report['native_substrate_readiness']['workspace_contract_evidence']}`",
            f"- Supervisor workspace parser implemented: `{report['native_substrate_readiness']['supervisor_workspace_parser_implemented']}`",
            f"- Supervisor workspace evidence: `{report['native_substrate_readiness']['supervisor_workspace_evidence']}`",
            f"- Native provider implemented: `{report['native_substrate_readiness']['native_provider_implemented']}`",
            f"- Native provider evidence: `{report['native_substrate_readiness']['native_provider_evidence']}`",
            f"- Native runtime benchmark validated: `{report['native_substrate_readiness']['native_runtime_benchmark_validated']}`",
            f"- Native runtime benchmark evidence: `{report['native_substrate_readiness']['native_runtime_benchmark_evidence']}`",
            f"- Native runtime unsupported reason: `{report['native_substrate_readiness']['native_runtime_unsupported_reason']}`",
            f"- Patch bundle ready: `{report['native_substrate_readiness']['patch_bundle_ready']}`",
            f"- Patch bundle evidence: `{report['native_substrate_readiness']['patch_bundle_evidence']}`",
            f"- Patch sha256: `{report['native_substrate_readiness']['patch_sha256']}`",
            f"- Patched runtime build validated: `{report['native_substrate_readiness']['patched_runtime_build_validated']}`",
            f"- Patched runtime build evidence: `{report['native_substrate_readiness']['patched_runtime_build_evidence']}`",
            f"- Runtime packaging validated: `{report['native_substrate_readiness']['runtime_packaging_validated']}`",
            f"- Runtime packaging evidence: `{report['native_substrate_readiness']['runtime_packaging_evidence']}`",
            f"- Checkpoint graph validated: `{report['native_substrate_readiness']['checkpoint_graph_validated']}`",
            f"- Checkpoint graph evidence: `{report['native_substrate_readiness']['checkpoint_graph_evidence']}`",
            f"- Patched runtime full validated: `{report['native_substrate_readiness']['patched_runtime_full_validated']}`",
            f"- Patched runtime full evidence: `{report['native_substrate_readiness']['patched_runtime_full_evidence']}`",
            f"- Backend selection validated: `{report['native_substrate_readiness']['backend_selection_validated']}`",
            f"- Backend selection evidence: `{report['native_substrate_readiness']['backend_selection_evidence']}`",
            f"- CI gate validated: `{report['native_substrate_readiness']['ci_gate_validated']}`",
            f"- CI gate evidence: `{report['native_substrate_readiness']['ci_gate_evidence']}`",
            "",
            "## Remaining Gaps",
        ]
    )
    lines.extend(f"- {gap}" for gap in report["remaining_gaps"])
    print("\n".join(lines))


def iter_event_store() -> list[dict[str, Any]]:
    ensure_dirs()
    if not EVENT_STORE_PATH.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in EVENT_STORE_PATH.read_text().splitlines():
        if not line.strip():
            continue
        events.append(json.loads(line))
    return events


def sync_event_db_from_jsonl() -> None:
    events = iter_event_store()
    if events:
        index_event_store_rows(events)
    else:
        init_event_db().close()


def query_event_store(filters: dict[str, str | None], limit: int | None) -> list[dict[str, Any]]:
    sync_event_db_from_jsonl()
    where: list[str] = []
    values: list[Any] = []
    for key, value in filters.items():
        if value is None:
            continue
        where.append(f"{key} = ?")
        values.append(value)
    sql = "SELECT event_json FROM events"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id ASC"
    if limit is not None:
        sql += " LIMIT ?"
        values.append(limit)
    conn = init_event_db()
    try:
        rows = conn.execute(sql, values).fetchall()
    finally:
        conn.close()
    return [json.loads(row[0]) for row in rows]


def command_events_query(args: argparse.Namespace) -> None:
    filters = {
        "cell_id": args.cell,
        "run_id": args.run,
        "receipt_id": args.receipt,
        "kind": args.kind,
        "category": args.category,
        "decision": args.decision,
    }
    events = query_event_store(filters, args.limit)
    if args.format == "jsonl":
        for event in events:
            print(json.dumps(event, sort_keys=True))
    else:
        print(json.dumps(events, indent=2, sort_keys=True))



def parse_run_manual(tokens: list[str]) -> argparse.Namespace:
    values: dict[str, Any] = {
        "command": "run",
        "cell": None,
        "checkpoint_before": False,
        "checkpoint_name": None,
        "restore_on_fail": False,
        "logs_since": "5m",
        "exit_with_command": False,
        "strict_checkpoint": False,
        "policy": None,
        "backend": "auto",
        "func": command_run,
    }
    remote: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == "--":
            remote = tokens[i + 1 :]
            break
        if token == "--checkpoint-before":
            values["checkpoint_before"] = True
            i += 1
            continue
        if token == "--strict-checkpoint":
            values["strict_checkpoint"] = True
            i += 1
            continue
        if token == "--backend":
            values["backend"] = tokens[i + 1]
            i += 2
            continue
        if token == "--policy":
            values["policy"] = tokens[i + 1]
            i += 2
            continue
        if token == "--restore-on-fail":
            values["restore_on_fail"] = True
            i += 1
            continue
        if token == "--exit-with-command":
            values["exit_with_command"] = True
            i += 1
            continue
        if token == "--checkpoint-name":
            values["checkpoint_name"] = tokens[i + 1]
            i += 2
            continue
        if token == "--logs-since":
            values["logs_since"] = tokens[i + 1]
            i += 2
            continue
        if values["cell"] is None:
            values["cell"] = token
            i += 1
            continue
        remote = tokens[i:]
        break
    if values["cell"] is None:
        raise SystemExit("run requires a cell name")
    if not remote:
        raise SystemExit("run requires a command after --")
    values["command"] = remote
    return argparse.Namespace(**values)

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="forkcell", description="ForkCell MVP CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    cell = sub.add_parser("cell")
    cell_sub = cell.add_subparsers(dest="cell_command", required=True)
    c_create = cell_sub.add_parser("create")
    c_create.add_argument("--name", required=True)
    c_create.add_argument("--workspace", default=DEFAULT_WORKSPACE)
    c_create.set_defaults(func=command_cell_create)
    c_delete = cell_sub.add_parser("delete")
    c_delete.add_argument("name")
    c_delete.set_defaults(func=command_cell_delete)
    c_inspect = cell_sub.add_parser("inspect")
    c_inspect.add_argument("name")
    c_inspect.set_defaults(func=command_cell_inspect)

    policy = sub.add_parser("policy")
    policy_sub = policy.add_subparsers(dest="policy_command", required=True)
    p_apply = policy_sub.add_parser("apply")
    p_apply.add_argument("cell")
    p_apply.add_argument("policy")
    p_apply.set_defaults(func=command_policy_apply)

    checkpoint = sub.add_parser("checkpoint")
    checkpoint_sub = checkpoint.add_subparsers(dest="checkpoint_command", required=True)
    ch_create = checkpoint_sub.add_parser("create")
    ch_create.add_argument("cell")
    ch_create.add_argument("--name")
    ch_create.set_defaults(func=command_checkpoint_create)
    ch_restore = checkpoint_sub.add_parser("restore")
    ch_restore.add_argument("cell")
    ch_restore.add_argument("checkpoint", nargs="?")
    ch_restore.set_defaults(func=command_checkpoint_restore)

    overlay = sub.add_parser("overlay")
    overlay_sub = overlay.add_subparsers(dest="overlay_command", required=True)
    ov_init = overlay_sub.add_parser("init")
    ov_init.add_argument("name")
    ov_init.add_argument("--from", dest="source", required=True)
    ov_init.set_defaults(func=command_overlay_init)
    ov_status = overlay_sub.add_parser("status")
    ov_status.add_argument("name")
    ov_status.set_defaults(func=command_overlay_status)
    ov_delete = overlay_sub.add_parser("delete")
    ov_delete.add_argument("name")
    ov_delete.set_defaults(func=command_overlay_delete)
    ov_checkpoint = overlay_sub.add_parser("checkpoint")
    ov_checkpoint.add_argument("name")
    ov_checkpoint.add_argument("--name", dest="name_label")
    ov_checkpoint.set_defaults(func=command_overlay_checkpoint)
    ov_restore = overlay_sub.add_parser("restore")
    ov_restore.add_argument("name")
    ov_restore.add_argument("checkpoint", nargs="?")
    ov_restore.set_defaults(func=command_overlay_restore)
    ov_verify = overlay_sub.add_parser("verify")
    ov_verify.add_argument("name")
    ov_verify.add_argument("checkpoint", nargs="?")
    ov_verify.set_defaults(func=command_overlay_verify)
    ov_gc = overlay_sub.add_parser("gc")
    ov_gc.add_argument("name")
    ov_gc.set_defaults(func=command_overlay_gc)
    ov_doctor = overlay_sub.add_parser("doctor")
    ov_doctor.add_argument("name")
    ov_doctor.set_defaults(func=command_overlay_doctor)
    ov_run = overlay_sub.add_parser("run")
    ov_run.add_argument("cell")
    ov_run.add_argument("--checkpoint-before", action="store_true")
    ov_run.add_argument("--checkpoint-name")
    ov_run.add_argument("--restore-on-fail", action="store_true")
    ov_run.add_argument("--exit-with-command", action="store_true")
    ov_run.add_argument("command", nargs=argparse.REMAINDER)
    ov_run.set_defaults(func=command_overlay_run, backend="local-overlay", logs_since="0m")

    runtime = sub.add_parser("runtime")
    runtime_sub = runtime.add_subparsers(dest="runtime_command", required=True)
    runtime_install = runtime_sub.add_parser("install")
    runtime_install.add_argument("--from", dest="source", default="upstream/openshell")
    runtime_install.set_defaults(func=command_runtime_install)
    runtime_detect = runtime_sub.add_parser("detect")
    runtime_detect.add_argument("--strict", action="store_true")
    runtime_detect.set_defaults(func=command_runtime_detect)
    runtime_env = runtime_sub.add_parser("env")
    runtime_env.add_argument("--endpoint")
    runtime_env.add_argument("--port", default=os.environ.get("OPENSHELL_PATCHED_GATEWAY_PORT", "17671"))
    runtime_env.set_defaults(func=command_runtime_env)

    graph = sub.add_parser("graph")
    graph_sub = graph.add_subparsers(dest="graph_command", required=True)
    graph_show = graph_sub.add_parser("show")
    graph_show.add_argument("--format", choices=["json", "md"], default="json")
    graph_show.set_defaults(func=command_graph_show)
    graph_gc = graph_sub.add_parser("gc")
    graph_gc.add_argument("--dry-run", action="store_true")
    graph_gc.set_defaults(func=command_graph_gc)

    native = sub.add_parser("native")
    native_sub = native.add_subparsers(dest="native_command", required=True)
    native_init = native_sub.add_parser("init")
    native_init.add_argument("name")
    native_init.add_argument("--from", dest="source", required=True)
    native_init.set_defaults(func=command_native_init)
    native_status = native_sub.add_parser("status")
    native_status.add_argument("name")
    native_status.set_defaults(func=command_native_status)
    native_delete = native_sub.add_parser("delete")
    native_delete.add_argument("name")
    native_delete.set_defaults(func=command_native_delete)
    native_checkpoint = native_sub.add_parser("checkpoint")
    native_checkpoint.add_argument("name")
    native_checkpoint.add_argument("--name", dest="name_label")
    native_checkpoint.set_defaults(func=command_native_checkpoint)
    native_restore = native_sub.add_parser("restore")
    native_restore.add_argument("name")
    native_restore.add_argument("checkpoint", nargs="?")
    native_restore.set_defaults(func=command_native_restore)
    native_gc = native_sub.add_parser("gc")
    native_gc.add_argument("name")
    native_gc.add_argument("--dry-run", action="store_true")
    native_gc.set_defaults(func=command_native_gc)
    native_fork = native_sub.add_parser("fork")
    native_fork.add_argument("source")
    native_fork.add_argument("--checkpoint")
    native_fork.add_argument("--name", required=True)
    native_fork.add_argument("--label")
    native_fork.set_defaults(func=command_native_fork)
    native_driver_config = native_sub.add_parser("driver-config")
    native_driver_config.add_argument("name")
    native_driver_config.add_argument("--format", choices=["json", "raw"], default="json")
    native_driver_config.set_defaults(func=command_native_driver_config)
    native_run = native_sub.add_parser("run")
    native_run.add_argument("cell")
    native_run.add_argument("--checkpoint-before", action="store_true")
    native_run.add_argument("--checkpoint-name")
    native_run.add_argument("--restore-on-fail", action="store_true")
    native_run.add_argument("--policy", help="policy YAML to pass to OpenShell for this run")
    native_run.add_argument("--logs-since", default="5m")
    native_run.add_argument("--sync-logs", action="store_true", help="wait briefly for OCSF/log events before writing the receipt")
    native_run.add_argument("--exit-with-command", action="store_true")
    native_run.add_argument("command", nargs=argparse.REMAINDER)
    native_run.set_defaults(func=command_native_run, backend="native-overlay")
    native_layer_run = native_sub.add_parser("run-layer")
    native_layer_run.add_argument("cell")
    native_layer_run.add_argument("--checkpoint-before", action="store_true")
    native_layer_run.add_argument("--checkpoint-name")
    native_layer_run.add_argument("--restore-on-fail", action="store_true")
    native_layer_run.add_argument("--policy", help="policy YAML to pass to OpenShell for this run")
    native_layer_run.add_argument("--logs-since", default="5m")
    native_layer_run.add_argument("--exit-with-command", action="store_true")
    native_layer_run.add_argument("command", nargs=argparse.REMAINDER)
    native_layer_run.set_defaults(func=command_run, backend="layer-clone")

    volume = sub.add_parser("volume")
    volume_sub = volume.add_subparsers(dest="volume_command", required=True)
    vol_init = volume_sub.add_parser("init")
    vol_init.add_argument("name")
    vol_init.add_argument("--from", dest="source", required=True)
    vol_init.set_defaults(func=command_volume_init)
    vol_status = volume_sub.add_parser("status")
    vol_status.add_argument("name")
    vol_status.set_defaults(func=command_volume_status)
    vol_delete = volume_sub.add_parser("delete")
    vol_delete.add_argument("name")
    vol_delete.set_defaults(func=command_volume_delete)
    vol_checkpoint = volume_sub.add_parser("checkpoint")
    vol_checkpoint.add_argument("name")
    vol_checkpoint.add_argument("--name", dest="name_label")
    vol_checkpoint.add_argument("--strict", action="store_true", help="hash every file instead of using the metadata cache")
    vol_checkpoint.set_defaults(func=command_volume_checkpoint)
    vol_restore = volume_sub.add_parser("restore")
    vol_restore.add_argument("name")
    vol_restore.add_argument("checkpoint", nargs="?")
    vol_restore.add_argument("--run", help="run to bind this manual restore decision to; defaults to the cell's last run")
    vol_restore.add_argument("--reason")
    vol_restore.set_defaults(func=command_volume_restore)
    vol_verify = volume_sub.add_parser("verify")
    vol_verify.add_argument("name")
    vol_verify.set_defaults(func=command_volume_verify)
    vol_run = volume_sub.add_parser("run")
    vol_run.add_argument("cell")
    vol_run.add_argument("--checkpoint-before", action="store_true")
    vol_run.add_argument("--checkpoint-name")
    vol_run.add_argument("--restore-on-fail", action="store_true")
    vol_run.add_argument("--strict-checkpoint", action="store_true", help="use strict mode for --checkpoint-before")
    vol_run.add_argument("--policy", help="policy YAML to apply to this run and remember for later volume-delta runs")
    vol_run.add_argument("--logs-since", default="5m")
    vol_run.add_argument("--exit-with-command", action="store_true")
    vol_run.add_argument("command", nargs=argparse.REMAINDER)
    vol_run.set_defaults(func=command_volume_run, backend="volume-delta")
    vol_accept = volume_sub.add_parser("accept")
    vol_accept.add_argument("name")
    vol_accept.add_argument("--run")
    vol_accept.add_argument("--reason")
    vol_accept.add_argument("--force", action="store_true")
    vol_accept.set_defaults(func=command_volume_accept)

    run = sub.add_parser("run")
    run.add_argument("cell")
    run.add_argument(
        "--backend",
        default="auto",
        metavar="{auto,native-overlay,layer-clone,volume-delta,local-overlay}",
        help="ForkCell backend strategy",
    )
    run.add_argument("--checkpoint-before", action="store_true")
    run.add_argument("--checkpoint-name")
    run.add_argument("--restore-on-fail", action="store_true")
    run.add_argument("--strict-checkpoint", action="store_true", help="use strict mode for --checkpoint-before on supported backends")
    run.add_argument("--policy", help="policy YAML to apply on supported backends")
    run.add_argument("--logs-since", default="5m")
    run.add_argument("--sync-logs", action="store_true", help="wait briefly for OCSF/log events on native-overlay runs")
    run.add_argument("--exit-with-command", action="store_true")
    run.add_argument("command", nargs=argparse.REMAINDER)
    run.set_defaults(func=command_run)

    receipt = sub.add_parser("receipt")
    receipt_sub = receipt.add_subparsers(dest="receipt_command", required=True)
    r_show = receipt_sub.add_parser("show")
    r_show.add_argument("receipt", nargs="?")
    r_show.add_argument("--cell")
    r_show.add_argument("--latest", action="store_true")
    r_show.add_argument("--format", choices=["json", "md"], default="json")
    r_show.set_defaults(func=command_receipt_show)

    decisions = sub.add_parser("decisions")
    decisions_sub = decisions.add_subparsers(dest="decisions_command", required=True)
    d_show = decisions_sub.add_parser("show")
    d_show.add_argument("decision", nargs="?")
    d_show.add_argument("--cell")
    d_show.add_argument("--latest", action="store_true")
    d_show.add_argument("--artifact", action="store_true", help="print the decision artifact file contents")
    d_show.set_defaults(func=command_decisions_show)
    d_list = decisions_sub.add_parser("list")
    d_list.add_argument("--cell")
    d_list.add_argument("--run")
    d_list.add_argument("--receipt")
    d_list.add_argument("--result", choices=["accepted", "restored"])
    d_list.add_argument("--limit", type=int, default=50)
    d_list.add_argument("--format", choices=["json", "jsonl"], default="json")
    d_list.set_defaults(func=command_decisions_list)

    review = sub.add_parser("review")
    review_sub = review.add_subparsers(dest="review_command", required=True)
    review_status = review_sub.add_parser("status")
    review_status.add_argument("--format", choices=["json", "md"], default="json")
    review_status.set_defaults(func=command_review_status)

    events = sub.add_parser("events")
    events_sub = events.add_subparsers(dest="events_command", required=True)
    ev_query = events_sub.add_parser("query")
    ev_query.add_argument("--cell")
    ev_query.add_argument("--run")
    ev_query.add_argument("--receipt")
    ev_query.add_argument("--kind", choices=["ocsf", "policy_signal"])
    ev_query.add_argument("--category")
    ev_query.add_argument("--decision", choices=["allowed", "denied"])
    ev_query.add_argument("--limit", type=int, default=50)
    ev_query.add_argument("--format", choices=["json", "jsonl"], default="json")
    ev_query.set_defaults(func=command_events_query)
    return parser


def main(argv: list[str] | None = None) -> None:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] == "run" and not (len(raw) > 1 and raw[1] in {"-h", "--help"}):
        args = parse_run_manual(raw[1:])
    else:
        parser = build_parser()
        args = parser.parse_args(raw)
    try:
        args.func(args)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
