"""runbox backend for the Desktop Runbox tab: workspace files, run history, and the same
run / check / test / stop actions the agent's tool performs (install stays agent-only, behind
Hermes' approval prompt)."""
from __future__ import annotations

import importlib
import importlib.util
import json
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException

_PKG = "runbox_desktop_backend"
_DIR = Path(__file__).resolve().parent.parent / "runbox"
if _PKG not in sys.modules:
    _spec = importlib.util.spec_from_file_location(_PKG, _DIR / "__init__.py", submodule_search_locations=[str(_DIR)])
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[_PKG] = _mod
    _spec.loader.exec_module(_mod)
store = importlib.import_module(f"{_PKG}.store")
tool = importlib.import_module(f"{_PKG}.tool")

UI_ACTIONS = ("run", "check", "test", "stop")
router = APIRouter()


def _settings_entry() -> dict:
    try:
        from hermes_cli.config import load_config
        entry = ((load_config() or {}).get("plugins") or {}).get("entries", {}).get("runbox") or {}
        return entry.get("settings") or entry.get("config") or {}
    except Exception:
        return {}


def _runtime():
    settings = tool.apply_settings(tool.Settings(), lambda key, default: _settings_entry().get(key, default))
    if tool.runtime.settings != settings:
        tool.runtime.settings = settings
        tool.runtime.docker = None
    return tool.runtime


def _entry(dir_name: str) -> Path:
    try:
        return store.entry_by_dir(_runtime().data_root(), dir_name)
    except store.StoreError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _state_db() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()) / "state.db"


def _notify(dir_name: str) -> None:
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event
        broadcast_plugin_event("runbox", "runs.updated", {"dir": dir_name})
    except Exception:
        pass


@router.get("/sessions")
def sessions():
    rt = _runtime()
    rows = store.inventory(rt.data_root())
    live = store.db_session_ids(_state_db())
    if live is not None and len(rows) >= 2 and not any(r["session_id"] in live for r in rows):
        live = None  # a read matching none of our chats is a schema change, not mass deletion
    docker = rt.engine()
    running = bool(rows) and docker.reachable()
    states = {b["session_id"]: b["state"] for b in docker.list_boxes()} if running else {}
    return {"sessions": [{**r, "exists": None if live is None else r["session_id"] in live,
                          "container": states.get(r["session_id"], "none")} for r in rows],
            "backend_running": running}


@router.get("/sessions/{dir_name}/files")
def files(dir_name: str):
    return store.workspace_files(_entry(dir_name))


@router.get("/sessions/{dir_name}/file")
def read_file(dir_name: str, path: str):
    try:
        return store.read_file(_entry(dir_name), path)
    except store.StoreError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.put("/sessions/{dir_name}/file")
def write_file(dir_name: str, path: str, body: dict):
    entry = _entry(dir_name)
    try:
        result = store.write_file(entry, path, body.get("content"))
    except store.StoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    store.touch(_runtime().data_root(), store.entry_session_id(entry) or "")
    _notify(dir_name)
    return result


@router.delete("/sessions/{dir_name}/file")
def delete_file(dir_name: str, path: str):
    try:
        result = store.delete_file(_entry(dir_name), path)
    except store.StoreError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _notify(dir_name)
    return result


@router.get("/sessions/{dir_name}/runs")
def runs(dir_name: str, limit: int = 30):
    rows = store.recent_runs(_entry(dir_name), max(1, min(limit, store.RUN_LOG_KEEP)))
    return {"runs": list(reversed(rows))}


@router.post("/sessions/{dir_name}/actions/{action}")
def act(dir_name: str, action: str, body: dict):
    if action not in UI_ACTIONS:
        raise HTTPException(status_code=400, detail=f"action must be one of: {', '.join(UI_ACTIONS)}")
    entry = _entry(dir_name)
    session_id = store.entry_session_id(entry)
    args = {"action": action}
    for key in ("command", "paths", "args", "timeout"):
        if body.get(key) not in (None, "", []):
            args[key] = body[key]
    _runtime()
    result = json.loads(tool.handle(args, session_id=session_id, source="desktop"))
    _notify(dir_name)
    return result
