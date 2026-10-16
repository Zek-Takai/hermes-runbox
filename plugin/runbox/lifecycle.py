"""Idle stop: remove containers of chats idle past ``idle_minutes``; on macOS stop the VM too."""
from __future__ import annotations

import time

from . import store

ACTIVE_MARKER = ".active"


def mark_active(root) -> None:
    (root / ACTIVE_MARKER).touch()


def clear_active(root) -> None:
    (root / ACTIVE_MARKER).unlink(missing_ok=True)


def sweep(rt, *, now: float | None = None) -> dict:
    """One idle pass. Cheap no-op unless runbox started something since the last full stop."""
    root = rt.data_root()
    marker = root / ACTIVE_MARKER
    if not marker.exists():
        return {}
    now = time.time() if now is None else now
    docker = rt.engine()
    with store.exclusive(root, ".sweep.lock") as held:
        if not held:
            return {}
        if not docker.reachable():
            marker.unlink(missing_ok=True)
            return {"backend": "down"}
        idle_s = rt.settings.idle_minutes * 60
        boxes = docker.list_boxes()
        removed = []
        for box in boxes:
            entry = store.find_session(root, box["session_id"]) if box["session_id"] else None
            if entry is None or now - store.last_active(entry) > idle_s:
                if docker.remove(box["name"]):
                    removed.append(box["name"])
        vm_stopped = False
        if len(removed) == len(boxes):
            try:
                last_used = (root / ".last_used").stat().st_mtime
            except OSError:
                last_used = 0
            if docker.colima:
                if now - last_used > idle_s:
                    vm_stopped = docker.stop_vm()
                    if vm_stopped:
                        marker.unlink(missing_ok=True)
            else:
                marker.unlink(missing_ok=True)
        return {"removed": removed, "vm_stopped": vm_stopped}
