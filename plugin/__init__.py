"""runbox: build, run and test Python in an offline per-chat container."""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from .runbox import engine, guidance, lifecycle, store, tool

logger = logging.getLogger(__name__)

SWEEP_POLL_S = 60
RECONCILE_INTERVAL_S = 3600
RECONCILE_POLL_S = 600
_threads_started = False


def _state_db() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()) / "state.db"


def _remove_container(session_id: str) -> None:
    docker = tool.runtime.engine()
    if docker.socket_present():
        docker.remove(engine.container_name(session_id))


def _reconcile_once() -> None:
    root = tool.runtime.data_root()
    stamp = root / ".last_reconcile"
    try:
        if stamp.exists() and time.time() - stamp.stat().st_mtime < RECONCILE_INTERVAL_S:
            return
        stamp.touch()
        removed = store.reconcile(root, _state_db(), idle_days=tool.runtime.settings.idle_days,
                                  on_remove=_remove_container)
        for row in removed:
            logger.info("runbox: removed workspace of session %s (%s)", row["session_id"], row["reason"])
    except Exception:  # cleanup is best-effort; never take a Hermes process down
        logger.warning("runbox reconcile failed", exc_info=True)


def _loop() -> None:
    last_reconcile_poll = 0.0
    while True:
        try:
            done = lifecycle.sweep(tool.runtime)
            if done.get("removed") or done.get("vm_stopped"):
                logger.info("runbox idle stop: %s", done)
        except Exception:
            logger.warning("runbox idle sweep failed", exc_info=True)
        if time.monotonic() - last_reconcile_poll >= RECONCILE_POLL_S:
            last_reconcile_poll = time.monotonic()
            _reconcile_once()
        time.sleep(SWEEP_POLL_S)


def _setting(ctx, key, cast, default):
    try:
        value = ctx.get_config(key, default=default)
        if value is None:
            return default
        if cast is bool and isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return cast(value)
    except Exception:
        return default


def register(ctx):
    global _threads_started
    s = tool.runtime.settings
    s.image = _setting(ctx, "image", str, s.image)
    s.idle_minutes = _setting(ctx, "idle_minutes", float, s.idle_minutes)
    s.idle_days = _setting(ctx, "idle_days", float, s.idle_days)
    s.memory = _setting(ctx, "memory", str, s.memory)
    s.cpus = _setting(ctx, "cpus", float, s.cpus)
    s.docker_host = _setting(ctx, "docker_host", str, s.docker_host)
    s.hint = _setting(ctx, "hint", bool, s.hint)
    ctx.register_tool(name="runbox", toolset="runbox", schema=tool.SCHEMA, handler=tool.handle)
    ctx.register_hook("pre_tool_call", tool.pre_tool_call)
    ctx.register_hook("pre_llm_call", lambda **kw: guidance.reminder(s.hint, **kw))
    if not _threads_started:
        _threads_started = True
        threading.Thread(target=_loop, name="runbox-lifecycle", daemon=True).start()
