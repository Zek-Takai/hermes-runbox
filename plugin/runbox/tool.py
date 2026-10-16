"""The ``runbox`` tool: schema, handler and the install approval hook."""
from __future__ import annotations

import contextvars
import json
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from . import engine, issues, lifecycle, store

ACTIONS = ("sync", "run", "check", "test", "install", "status", "stop", "reset")

SCHEMA = {
    "name": "runbox",
    "description": (
        "Run, check and test Python in this chat's own offline container (no network, no access to "
        "the user's machine beyond its workspace). Put code in with action=sync (host files/folders "
        "via paths, e.g. scratchpad buffers, or inline files), then: run (a shell command in "
        "/workspace), check (ruff lint + syntax), test (pytest). check/test/run failures come back "
        "as a plain issue list ('file:line: CODE message'). install adds PyPI packages (asks the "
        "user first: it needs network). status shows the box; stop frees it (files kept; on macOS "
        "it also stops the runbox VM when no other chat is using it); reset wipes the workspace "
        "and packages. The container starts on demand and stops when idle."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": list(ACTIONS)},
            "paths": {"type": "array", "items": {"type": "string"},
                      "description": ("sync: absolute or ~ host files/folders to copy in (a folder's "
                                      "contents land at the workspace root). check/test: workspace-"
                                      "relative paths to limit to.")},
            "files": {"type": "object", "additionalProperties": {"type": "string"},
                      "description": "sync: workspace-relative path -> file content, written directly."},
            "clean": {"type": "boolean", "description": "sync: empty the workspace first."},
            "command": {"type": "string", "description": "run: shell command, run in /workspace."},
            "args": {"type": "array", "items": {"type": "string"},
                     "description": "test: extra pytest arguments, e.g. ['-k', 'parser']."},
            "packages": {"type": "array", "items": {"type": "string"},
                         "description": "install: PyPI requirement specs, e.g. ['requests', 'numpy<3']."},
            "timeout": {"type": "integer", "description": "run/test: seconds (default 120, max 600)."},
        },
        "required": ["action"],
    },
}

DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 600
MAX_OUTPUT = 12000
SYNC_MAX_FILES = 5000
SYNC_MAX_BYTES = 200 * 1024 * 1024
SKIP_NAMES = {".git", ".hg", ".svn", "__pycache__", ".venv", "venv", "node_modules", ".mypy_cache",
              ".pytest_cache", ".ruff_cache", ".tox", ".DS_Store"}
_JUNIT_MARK = "===RUNBOX-JUNIT==="
# Errors only (syntax, undefined names, unused/shadowed imports, bad comparisons): no style rules.
CHECK_RULES = "E4,E7,E9,F"


@dataclass
class Settings:
    image: str = engine.DEFAULT_IMAGE
    idle_minutes: float = 15
    idle_days: float = store.IDLE_DAYS
    memory: str = "1g"
    cpus: float = 2
    docker_host: str = ""
    hint: bool = True


@dataclass
class Runtime:
    settings: Settings = field(default_factory=Settings)
    root: Path | None = None
    docker: engine.Docker | None = None
    _setup_lock: threading.Lock = field(default_factory=threading.Lock)
    _session_locks: dict = field(default_factory=dict)

    def data_root(self) -> Path:
        if self.root is None:
            self.root = store.data_root()
        return self.root

    def engine(self) -> engine.Docker:
        if self.docker is None:
            self.docker = engine.Docker(self.data_root(), docker_host=self.settings.docker_host)
        return self.docker

    def lock_for(self, session_id: str) -> threading.Lock:
        with self._setup_lock:
            return self._session_locks.setdefault(session_id, threading.Lock())


runtime = Runtime()
_BY = contextvars.ContextVar("runbox_by", default="agent")  # who started a run: agent or desktop

_SETTING_TYPES = {"image": str, "idle_minutes": float, "idle_days": float, "memory": str, "cpus": float,
                  "docker_host": str, "hint": bool}


def apply_settings(settings: Settings, get) -> Settings:
    """Fill ``settings`` from ``get(key, default)`` (``ctx.get_config`` or a config dict lookup)."""
    for key, cast in _SETTING_TYPES.items():
        default = getattr(settings, key)
        try:
            value = get(key, default)
            if value is None:
                continue
            if cast is bool and isinstance(value, str):
                value = value.strip().lower() in ("1", "true", "yes", "on")
            setattr(settings, key, cast(value))
        except Exception:
            setattr(settings, key, default)
    return settings


def _ok(data) -> str:
    return json.dumps({"success": True, **data}, ensure_ascii=False)


def _err(message, **extra) -> str:
    return json.dumps({"success": False, "error": str(message), **extra}, ensure_ascii=False)


def _clip(text: str, limit: int = MAX_OUTPUT) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return f"[... {len(text) - limit} earlier characters cut]\n" + text[-limit:]


def _timeout(args) -> int:
    try:
        value = int(args.get("timeout") or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        value = DEFAULT_TIMEOUT
    return max(1, min(MAX_TIMEOUT, value))


def mark_used(root: Path) -> None:
    (root / ".last_used").touch()


# --- hooks -----------------------------------------------------------------------------------

def pre_tool_call(tool_name=None, args=None, **_kw):
    """``install`` reaches the network, so it goes through Hermes' human-approval gate."""
    if tool_name != "runbox" or not isinstance(args, dict) or args.get("action") != "install":
        return None
    packages = args.get("packages") or []
    shown = ", ".join(str(p) for p in packages[:10]) + (" ..." if len(packages) > 10 else "")
    return {"action": "approve", "rule_key": "runbox:install",
            "message": f"runbox install from PyPI (network on, separate installer container): {shown or '(none)'}"}


# --- handler ---------------------------------------------------------------------------------

def handle(args, session_id=None, task_id=None, source="agent", **_kw) -> str:
    token = _BY.set("desktop" if source == "desktop" else "agent")
    try:
        return _handle(args or {}, session_id, task_id)
    finally:
        _BY.reset(token)


def _handle(args, session_id, task_id) -> str:
    action = (args.get("action") or "").strip()
    sid = str(session_id or task_id or "")
    if action not in ACTIONS:
        return _err("action must be one of: " + ", ".join(ACTIONS))
    if not sid:
        return _err("no session is bound to this call")
    root = runtime.data_root()
    try:
        with runtime.lock_for(sid):
            if action == "status":
                return _status(root, sid)
            path = store.session_dir(root, sid)
            store.touch(root, sid)
            mark_used(root)
            if action == "sync":
                return _sync(path, args)
            if action == "stop":
                return _ok(_stop(sid))
            if action == "reset":
                runtime.engine().remove(engine.container_name(sid))
                for sub in ("workspace", "deps"):
                    shutil.rmtree(path / sub, ignore_errors=True)
                store.session_dir(root, sid)
                return _ok({"reset": True, "workspace": str(path / "workspace")})
            notes = _prepare(sid, path, container=action != "install")
            result = {"run": _run, "check": _check, "test": _test, "install": _install}[action](sid, path, args)
            result.setdefault("notes", [])
            result["notes"] = notes + result["notes"]
            store.touch(root, sid, force=True)  # a long run must not count as idle time
            return json.dumps(result, ensure_ascii=False)
    except (engine.RunboxError, store.StoreError, OSError) as exc:
        return _err(exc)


def _stop(sid: str) -> dict:
    """Remove this chat's container; on macOS also stop the VM when no other box is left."""
    docker = runtime.engine()
    if not docker.reachable():
        return {"stopped": True, "vm_stopped": False, "note": "the backend was not running"}
    stopped = docker.remove(engine.container_name(sid))
    vm_stopped = False
    if docker.colima and not docker.list_boxes():
        vm_stopped = docker.stop_vm()
        if vm_stopped:
            lifecycle.clear_active(runtime.data_root())
    return {"stopped": stopped, "vm_stopped": vm_stopped}


def _prepare(sid: str, path: Path, *, container: bool) -> list[str]:
    docker = runtime.engine()
    image = runtime.settings.image
    notes = []
    with runtime._setup_lock, store.locked(runtime.data_root(), ".setup.lock"):
        for step in (docker.ensure_up, lambda: docker.ensure_image(image), lambda: docker.ensure_toolchain(image)):
            note = step()
            if note:
                notes.append(note)
        lifecycle.mark_active(runtime.data_root())
    if container:
        note = docker.ensure_container(engine.container_name(sid), sid, image=image,
                                       workspace=path / "workspace", deps=path / "deps",
                                       memory=runtime.settings.memory, cpus=runtime.settings.cpus)
        if note:
            notes.append(note)
    return notes


def _record(path: Path, action: str, result: dict, *, command: str = "") -> None:
    store.log_run(path, {
        "ts": time.time(), "action": action, "command": command, "by": _BY.get(),
        "exit_code": result.get("exit_code"), "duration_ms": result.get("duration_ms"),
        "ok": result.get("ok"), "issues": result.get("issues", [])[:50],
        "output": _clip(result.get("output", ""), 4000),
    })


# --- sync ------------------------------------------------------------------------------------

def _safe_rel(rel: str) -> PurePosixPath:
    p = PurePosixPath(str(rel).replace("\\", "/"))
    if not rel or p.is_absolute() or any(part in ("", "..") for part in p.parts):
        raise store.StoreError(f"invalid workspace path {rel!r}: use a relative path inside the workspace")
    return p


def _copy_tree(src: Path, dest: Path, budget: dict) -> None:
    for entry in sorted(src.iterdir()):
        if entry.name in SKIP_NAMES or entry.is_symlink():
            continue
        target = dest / entry.name
        if entry.is_dir():
            target.mkdir(exist_ok=True)
            _copy_tree(entry, target, budget)
        elif entry.is_file():
            budget["files"] += 1
            budget["bytes"] += entry.stat().st_size
            if budget["files"] > SYNC_MAX_FILES or budget["bytes"] > SYNC_MAX_BYTES:
                raise store.StoreError(
                    f"sync is limited to {SYNC_MAX_FILES} files / {SYNC_MAX_BYTES // (1024 * 1024)} MB")
            shutil.copy2(entry, target)


def _sync(path: Path, args) -> str:
    workspace = path / "workspace"
    if args.get("clean"):
        shutil.rmtree(workspace, ignore_errors=True)
        workspace.mkdir()
    budget = {"files": 0, "bytes": 0}
    copied = []
    for raw in args.get("paths") or []:
        src = Path(os.path.expanduser(str(raw)))
        if not src.is_absolute():
            raise store.StoreError(f"sync path {raw!r} must be absolute or start with ~")
        if src.is_dir():
            _copy_tree(src, workspace, budget)
        elif src.is_file():
            budget["files"] += 1
            budget["bytes"] += src.stat().st_size
            shutil.copy2(src, workspace / src.name)
        else:
            raise store.StoreError(f"{src} does not exist")
        copied.append(str(src))
    written = []
    files = args.get("files") or {}
    if not isinstance(files, dict):
        raise store.StoreError("files must be an object of path -> content")
    for rel, content in files.items():
        target = workspace / _safe_rel(rel)
        if not isinstance(content, str):
            raise store.StoreError(f"content for {rel!r} must be a string")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        written.append(str(_safe_rel(rel)))
    return _ok({"workspace": str(workspace), "copied": copied, "written": written,
                "files": _listing(workspace)})


def _listing(workspace: Path, limit: int = 200) -> list[str]:
    out = []
    for p in sorted(workspace.rglob("*")):
        if p.is_file():
            out.append(str(p.relative_to(workspace)))
            if len(out) >= limit:
                out.append("...")
                break
    return out


# --- run / check / test / install ------------------------------------------------------------

def _run(sid: str, path: Path, args) -> dict:
    command = args.get("command")
    if not isinstance(command, str) or not command.strip():
        raise store.StoreError("run needs command")
    res = runtime.engine().exec(engine.container_name(sid), ["sh", "-c", command], timeout=_timeout(args))
    found = [] if res.exit_code == 0 else [i for i in [issues.traceback_issue(res.stderr)] if i]
    result = {
        "success": True, "ok": res.exit_code == 0 and not res.timed_out, "exit_code": res.exit_code,
        "timed_out": res.timed_out, "duration_ms": res.duration_ms,
        "stdout": _clip(res.stdout), "stderr": _clip(res.stderr),
        "issues": found, "report": [issues.render(i) for i in found],
    }
    if res.timed_out:
        result["notes"] = [f"timed out after {_timeout(args)}s; the container was restarted"]
    _record(path, "run", {**result, "output": res.stdout + res.stderr}, command=command)
    return result


def _rel_paths(args) -> list[str]:
    return [str(_safe_rel(p)) for p in (args.get("paths") or [])]


def _check(sid: str, path: Path, args) -> dict:
    targets = _rel_paths(args) or ["."]
    res = runtime.engine().exec(
        engine.container_name(sid),
        ["ruff", "check", "--isolated", "--no-cache", "--select", CHECK_RULES, "--output-format", "json",
         "--exit-zero", *targets],
        timeout=DEFAULT_TIMEOUT)
    if res.exit_code != 0:
        raise engine.RunboxError("ruff failed: " + _clip(res.stderr or res.stdout, 800))
    found = issues.ruff_issues(res.stdout)
    result = {"success": True, "ok": not found, "duration_ms": res.duration_ms, "issues": found,
              "report": [issues.render(i) for i in found] or ["no issues"]}
    _record(path, "check", result, command="ruff check " + " ".join(targets))
    return result


def _test(sid: str, path: Path, args) -> dict:
    extra = [str(a) for a in (args.get("args") or [])]
    targets = _rel_paths(args)
    argv = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=short",
            "-o", "junit_family=xunit1", "--junitxml=/tmp/runbox-junit.xml", *extra, *targets]
    script = ('rm -f /tmp/runbox-junit.xml; "$@"; rc=$?; echo "' + _JUNIT_MARK + '"; '
              'cat /tmp/runbox-junit.xml 2>/dev/null; exit $rc')
    res = runtime.engine().exec(engine.container_name(sid), ["sh", "-c", script, "runbox-test", *argv],
                                timeout=_timeout(args))
    out, _, junit = res.stdout.partition(_JUNIT_MARK)
    summary, found = issues.pytest_report(junit)
    if res.exit_code == 5:
        summary["note"] = "no tests were collected"
    result = {
        "success": True, "ok": res.exit_code == 0 and not res.timed_out, "exit_code": res.exit_code,
        "timed_out": res.timed_out, "duration_ms": res.duration_ms, "summary": summary,
        "issues": found, "report": [issues.render(i) for i in found] or
        [summary.get("note") or f"{summary['passed']} passed"],
        "output": _clip(out.strip() + ("\n" + res.stderr.strip() if res.stderr.strip() else ""), 6000),
    }
    if res.timed_out:
        result["notes"] = [f"timed out after {_timeout(args)}s; the container was restarted"]
    _record(path, "test", result, command="pytest " + " ".join(extra + targets))
    return result


def _install(sid: str, path: Path, args) -> dict:
    packages = [str(p).strip() for p in (args.get("packages") or []) if str(p).strip()]
    if not packages:
        raise store.StoreError("install needs packages")
    bad = [p for p in packages if p.startswith("-")]
    if bad:
        raise store.StoreError(f"pip options are not allowed: {', '.join(bad)}")
    res = runtime.engine().pip_install(runtime.settings.image, path / "deps", packages,
                                       memory=runtime.settings.memory, cpus=runtime.settings.cpus)
    result = {"success": True, "ok": res.exit_code == 0 and not res.timed_out, "exit_code": res.exit_code,
              "duration_ms": res.duration_ms, "packages": packages, "installed": _installed(path / "deps"),
              "output": _clip((res.stdout + res.stderr).strip(), 4000)}
    _record(path, "install", result, command="pip install " + " ".join(packages))
    return result


def _installed(deps: Path) -> list[str]:
    if not deps.is_dir():
        return []
    return sorted(p.name[:-len(".dist-info")] for p in deps.glob("*.dist-info"))


# --- status ----------------------------------------------------------------------------------

def _status(root: Path, sid: str) -> str:
    docker = runtime.engine()
    path = store.find_session(root, sid)
    up = docker.reachable()
    state = docker.container_state(engine.container_name(sid)) if up else None
    data = {
        "backend": ("colima VM 'runbox'" if docker.colima else (docker.host or "default Docker daemon")),
        "backend_running": up,
        "container": state or "none",
        "image": runtime.settings.image,
        "idle_stop_minutes": runtime.settings.idle_minutes,
    }
    if path is not None:
        data.update({
            "workspace": str(path / "workspace"),
            "files": _listing(path / "workspace"),
            "packages": _installed(path / "deps"),
            "recent_runs": [{k: r.get(k) for k in ("action", "command", "ok", "exit_code")}
                            for r in store.recent_runs(path, 5)],
        })
    return _ok(data)
