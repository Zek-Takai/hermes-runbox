"""Per-chat runbox folders on the host, their stamps, the run log, and cleanup.

``<HERMES_HOME>/plugin-data/runbox/sessions/<dir>/``
    ``.session``      the raw session id
    ``.stamp.json``   our own record: created / last_active
    ``workspace/``    mounted at /workspace
    ``deps/``         packages from ``install``, mounted read-only
    ``runs.jsonl``    recent runs (the Desktop tab reads it)

Cleanup (``reconcile``) mirrors devkit: a folder whose chat is gone from ``state.db`` is removed
(fail-safe on unreadable or non-matching reads), and a folder idle longer than ``idle_days`` is
removed whatever the database says.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import re
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Callable

IDLE_DAYS = 90
GRACE_SECONDS = 3600
TOUCH_EVERY_SECONDS = 60
RUN_LOG_KEEP = 50
_SESSION_FILE = ".session"
_STAMP_FILE = ".stamp.json"
_RUN_LOG = "runs.jsonl"
_SAFE_DIR = re.compile(r"^[A-Za-z0-9_-]{1,96}$")


class StoreError(ValueError):
    pass


def data_root() -> Path:
    from plugins.plugin_storage import plugin_data_dir
    return plugin_data_dir("runbox")


def sessions_root(root: Path) -> Path:
    return root / "sessions"


def _dir_name(session_id: str) -> str:
    if _SAFE_DIR.match(session_id):
        return session_id
    return "s-" + hashlib.sha256(session_id.encode()).hexdigest()[:32]


def _read_stamp(path: Path) -> dict:
    try:
        data = json.loads((path / _STAMP_FILE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_stamp(path: Path, stamp: dict) -> None:
    tmp = path / (_STAMP_FILE + ".tmp")
    tmp.write_text(json.dumps(stamp), encoding="utf-8")
    tmp.replace(path / _STAMP_FILE)


def session_dir(root: Path, session_id: str, *, create: bool = True) -> Path:
    if not session_id:
        raise StoreError("no session is bound to this call")
    path = sessions_root(root) / _dir_name(session_id)
    if create and not path.exists():
        path.mkdir(parents=True, exist_ok=True)
        (path / _SESSION_FILE).write_text(session_id, encoding="utf-8")
        now = time.time()
        _write_stamp(path, {"session_id": session_id, "created": now, "last_active": now})
    if create:
        (path / "workspace").mkdir(exist_ok=True)
        (path / "deps").mkdir(exist_ok=True)
    return path


def touch(root: Path, session_id: str, *, now: float | None = None, force: bool = False) -> bool:
    """Record activity for a chat that already has a folder; never creates one."""
    if not session_id:
        return False
    path = sessions_root(root) / _dir_name(session_id)
    if not path.is_dir():
        return False
    now = time.time() if now is None else now
    stamp = _read_stamp(path)
    if not force and now - float(stamp.get("last_active") or 0) < TOUCH_EVERY_SECONDS:
        return False
    stamp.setdefault("session_id", session_id)
    stamp.setdefault("created", now)
    stamp["last_active"] = now
    _write_stamp(path, stamp)
    return True


def last_active(path: Path) -> float:
    stamped = _read_stamp(path).get("last_active")
    if isinstance(stamped, (int, float)) and stamped > 0:
        return float(stamped)
    return path.stat().st_mtime


def entry_session_id(entry: Path) -> str | None:
    try:
        return (entry / _SESSION_FILE).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def session_entries(root: Path) -> list[Path]:
    base = sessions_root(root)
    if not base.is_dir():
        return []
    return [e for e in sorted(base.iterdir()) if e.is_dir() and entry_session_id(e)]


def find_session(root: Path, session_id: str) -> Path | None:
    path = sessions_root(root) / _dir_name(session_id)
    return path if path.is_dir() else None


# --- run log ---------------------------------------------------------------------------------

def log_run(path: Path, record: dict) -> None:
    log = path / _RUN_LOG
    with log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    lines = log.read_text(encoding="utf-8").splitlines()
    if len(lines) > RUN_LOG_KEEP * 2:
        tmp = log.with_suffix(".tmp")
        tmp.write_text("\n".join(lines[-RUN_LOG_KEEP:]) + "\n", encoding="utf-8")
        tmp.replace(log)


def recent_runs(path: Path, limit: int = RUN_LOG_KEEP) -> list[dict]:
    try:
        lines = (path / _RUN_LOG).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows = []
    for line in lines[-limit:]:
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


# --- automatic cleanup -----------------------------------------------------------------------

def db_session_ids(state_db: Path) -> set[str] | None:
    if not state_db.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True, timeout=5)
        try:
            return {row[0] for row in conn.execute("SELECT id FROM sessions")}
        finally:
            conn.close()
    except sqlite3.Error:
        return None


@contextlib.contextmanager
def exclusive(root: Path, name: str):
    """One holder at a time across Hermes processes; yields False when another holds it."""
    root.mkdir(parents=True, exist_ok=True)
    handle = open(root / name, "a+")
    try:
        try:
            import fcntl
        except ImportError:  # Windows: no advisory lock; callers are idempotent anyway
            yield True
            return
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        handle.close()


def remove_entry(entry: Path) -> None:
    shutil.rmtree(entry, ignore_errors=True)


def reconcile(root: Path, state_db: Path, *, idle_days: float = IDLE_DAYS,
              grace_seconds: float = GRACE_SECONDS, now: float | None = None,
              on_remove: Callable[[str], None] | None = None) -> list[dict]:
    """Remove folders of deleted or long-idle chats; returns ``{session_id, reason}`` rows.
    ``on_remove(session_id)`` runs first for each (the plugin removes the container there)."""
    if not sessions_root(root).is_dir():
        return []
    now = time.time() if now is None else now
    with exclusive(root, ".reconcile.lock") as held:
        if not held:
            return []
        entries = [(e, entry_session_id(e)) for e in session_entries(root)]
        live = db_session_ids(state_db)
        by_db, by_idle = [], []
        for entry, session_id in entries:
            idle = now - last_active(entry)
            if idle < grace_seconds:
                continue
            if live is not None and session_id not in live:
                by_db.append((entry, session_id))
            elif idle > idle_days * 86400:
                by_idle.append((entry, session_id))
        overlap = live is not None and any(sid in live for _, sid in entries)
        if by_db and not overlap and len(by_db) >= 2:
            # The database matches none of our chats: most likely a schema change, not a mass
            # delete. Leave these to the idle rule.
            by_idle.extend((e, s) for e, s in by_db if now - last_active(e) > idle_days * 86400)
            by_db = []
        removed = []
        for reason, group in (("deleted", by_db), ("idle", by_idle)):
            for entry, session_id in group:
                if on_remove is not None:
                    on_remove(session_id)
                remove_entry(entry)
                removed.append({"session_id": session_id, "reason": reason})
        return removed
