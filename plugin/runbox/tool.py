"""The ``runbox`` tool: schema, handler and the install approval hook."""
from __future__ import annotations

import contextvars
import json
import os
import re
import shutil
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from . import engine, issues, lifecycle, store

ACTIONS = ("sync", "run", "check", "test", "install", "export", "status", "stop", "reset")

SCHEMA = {
    "name": "runbox",
    "description": (
        "Run, check and test Python in this chat's own offline container (no network, no access to "
        "the user's machine beyond its workspace). Put code in with action=sync (host files/folders "
        "via paths, e.g. scratchpad buffers, or inline files), then: run (a shell command in "
        "/workspace), check (ruff lint + syntax), test (pytest). check/test/run failures come back "
        "as a plain issue list ('file:line: CODE message'). install adds PyPI packages (asks the "
        "user first: it needs network). export writes changed files back to the folders they were synced "
        "from (or to dest), through write_file, skipping files changed on the host since the sync "
        "unless overwrite; export only when the user asks for it, and do not copy fixes into the "
        "user's files any other way. status shows the box; stop frees it (files kept; on macOS "
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
            "dest": {"type": "string",
                     "description": "export: absolute or ~ folder to write to instead of where files came from."},
            "overwrite": {"type": "boolean",
                          "description": "export: also replace host files that changed since the sync."},
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
    confirm_export: bool = False
    guard_imports: bool = True


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
_ctx = None


def bind(ctx) -> None:
    """Hermes' plugin context: export writes through its ``write_file``."""
    global _ctx
    _ctx = ctx
_BY = contextvars.ContextVar("runbox_by", default="agent")  # who started a run: agent or desktop
_TASK = contextvars.ContextVar("runbox_task", default="")  # the agent task, for Hermes' file tools
_EXPORTING = contextvars.ContextVar("runbox_exporting", default=False)  # export's own writes pass the guard

_SETTING_TYPES = {"image": str, "idle_minutes": float, "idle_days": float, "memory": str, "cpus": float,
                  "docker_host": str, "hint": bool, "confirm_export": bool,
                  "guard_imports": bool}


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

def pre_tool_call(tool_name=None, args=None, session_id=None, task_id=None, **_kw):
    """Hermes' human-approval gate: always for ``install`` (network); for ``export`` (writes the
    user's files) only with ``confirm_export``, since the user asking for it is the approval; and,
    with ``guard_imports``, for a direct write_file/patch on a file imported into this chat's box
    (fixes are meant to stay in the box until the user asks for them)."""
    if not isinstance(args, dict):
        return None
    if tool_name in ("write_file", "patch"):
        return _guard_direct_edit(tool_name, args, str(session_id or task_id or ""))
    if tool_name != "runbox":
        return None
    action = args.get("action")
    if action == "install":
        packages = args.get("packages") or []
        shown = ", ".join(str(p) for p in packages[:10]) + (" ..." if len(packages) > 10 else "")
        return {"action": "approve", "rule_key": "runbox:install",
                "message": f"runbox install from PyPI (network on, separate installer container): {shown or '(none)'}"}
    if action == "export" and runtime.settings.confirm_export:
        files = args.get("paths") or []
        what = ", ".join(str(p) for p in files[:10]) + (" ..." if len(files) > 10 else "") if files else "all changed files"
        where = f"to {args['dest']}" if args.get("dest") else "back to the folders they were synced from"
        extra = " (overwriting host changes)" if args.get("overwrite") else ""
        return {"action": "approve", "rule_key": "runbox:export",
                "message": f"runbox export {what} {where}{extra}"}
    return None


# --- handler ---------------------------------------------------------------------------------

def handle(args, session_id=None, task_id=None, source="agent", **_kw) -> str:
    token = _BY.set("desktop" if source == "desktop" else "agent")
    task = _TASK.set(str(task_id or ""))
    try:
        return _handle(args or {}, session_id, task_id)
    finally:
        _TASK.reset(task)
        _BY.reset(token)


_V4A_FILE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$")


def _edited_paths(tool_name: str, args: dict) -> list[str]:
    paths = [args["path"]] if isinstance(args.get("path"), str) and args.get("path") else []
    if tool_name == "patch":
        for line in str(args.get("patch") or "").splitlines():
            match = _V4A_FILE.match(line.strip())
            if match:
                paths.append(match.group(1).strip())
    return paths


def _real(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


def _guard_direct_edit(tool_name: str, args: dict, sid: str):
    if not runtime.settings.guard_imports or _EXPORTING.get() or not sid:
        return None
    try:
        entry = store.find_session(runtime.data_root(), sid)
        if entry is None:
            return None
        imported = {_real(o["host"]) for o in store.read_sources(entry)["origins"].values() if o.get("host")}
        hits = [p for p in _edited_paths(tool_name, args) if _real(p) in imported]
    except Exception:
        return None  # the guard must never break Hermes' own file tools
    if not hits:
        return None
    return {"action": "approve", "rule_key": "runbox:direct-edit",
            "message": (f"{', '.join(hits[:3])} was imported into runbox; fixes stay in the box until "
                        "you ask for them (runbox export). Allow this direct edit of the original?")}


def broadcast(event: str, payload: dict) -> None:
    """Tell the Desktop tile what is happening (``plugin.runbox.<event>``). Only inside Hermes:
    importing ``hermes_cli`` from a bare interpreter would re-exec it."""
    if "hermes_cli" not in sys.modules:
        return
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event
        broadcast_plugin_event("runbox", event, payload)
    except Exception:
        pass


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
            event = {"dir": path.name, "action": action, "by": _BY.get(), "command": str(args.get("command") or "")}
            try:
                broadcast("run.started", event)
                return _act(action, sid, root, path, args)
            finally:
                broadcast("runs.updated", event)
    except (engine.RunboxError, store.StoreError, OSError) as exc:
        return _err(exc)


def _act(action: str, sid: str, root: Path, path: Path, args) -> str:
    if action == "sync":
        return _sync(path, args)
    if action == "export":
        return _ok(_export(path, args))
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


def _copy_tree(src: Path, dest: Path, budget: dict, copied: list) -> None:
    for entry in sorted(src.iterdir()):
        if entry.name in SKIP_NAMES or entry.is_symlink():
            continue
        target = dest / entry.name
        if entry.is_dir():
            target.mkdir(exist_ok=True)
            _copy_tree(entry, target, budget, copied)
        elif entry.is_file():
            budget["files"] += 1
            budget["bytes"] += entry.stat().st_size
            if budget["files"] > SYNC_MAX_FILES or budget["bytes"] > SYNC_MAX_BYTES:
                raise store.StoreError(
                    f"sync is limited to {SYNC_MAX_FILES} files / {SYNC_MAX_BYTES // (1024 * 1024)} MB")
            shutil.copy2(entry, target)
            copied.append((target, entry))


def _sync(path: Path, args) -> str:
    workspace = path / "workspace"
    sources = store.read_sources(path)
    if args.get("clean"):
        # Empty the folder, never replace it: a running container's bind mount keeps pointing
        # at the original directory.
        for child in workspace.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
        sources = {"origins": {}, "roots": []}
    budget = {"files": 0, "bytes": 0}
    copied = []
    for raw in args.get("paths") or []:
        src = Path(os.path.expanduser(str(raw)))
        if not src.is_absolute():
            raise store.StoreError(f"sync path {raw!r} must be absolute or start with ~")
        pairs: list = []
        if src.is_dir():
            _copy_tree(src, workspace, budget, pairs)
            if str(src) not in sources["roots"]:
                sources["roots"].append(str(src))
        elif src.is_file():
            budget["files"] += 1
            budget["bytes"] += src.stat().st_size
            shutil.copy2(src, workspace / src.name)
            pairs.append((workspace / src.name, src))
        else:
            raise store.StoreError(f"{src} does not exist")
        for target, origin in pairs:
            # Remember where each file came from (and its content) so export can write it back.
            sources["origins"][target.relative_to(workspace).as_posix()] = {
                "host": str(origin), "sha": store.sha256_file(target)}
        copied.append(str(src))
    if copied:
        store.write_sources(path, sources)
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
    result = {"workspace": str(workspace), "copied": copied, "written": written, "files": _listing(workspace)}
    if copied:
        result["note"] = (f"These are copies. Work on the box's files (under {workspace}, or with runbox run); "
                          "the originals stay untouched until the user asks for an export.")
    return _ok(result)


# --- export ----------------------------------------------------------------------------------

def _export(path: Path, args) -> dict:
    """Write changed workspace files back to where they were synced from (or under ``dest``)."""
    workspace = path / "workspace"
    sources = store.read_sources(path)
    origins, roots = sources["origins"], sources["roots"]
    overwrite = bool(args.get("overwrite"))
    dest_root = None
    if args.get("dest"):
        dest_root = Path(os.path.expanduser(str(args["dest"])))
        if not dest_root.is_absolute():
            raise store.StoreError("export dest must be absolute or start with ~")
    explicit = _rel_paths(args)
    wanted = explicit or [p for p in _listing(workspace, limit=SYNC_MAX_FILES) if p != "..."]
    plan, unchanged, conflicts, skipped = [], [], [], []
    for rel in wanted:
        src = workspace / rel
        if not src.is_file():
            skipped.append(f"{rel}: not in the workspace")
            continue
        sha = store.sha256_file(src)
        origin = origins.get(rel)
        if dest_root is not None:
            target, base = dest_root / rel, None
        elif origin:
            if origin.get("sha") == sha:
                unchanged.append(rel)
                continue
            target, base = Path(origin["host"]), origin.get("sha")
        elif len(roots) == 1:
            target, base = Path(roots[0]) / rel, None  # a new file inside the synced folder
        else:
            if explicit:
                skipped.append(f"{rel}: not synced from a folder; pass dest")
            continue
        if target.exists():
            host_sha = store.sha256_file(target)
            if host_sha == sha:
                unchanged.append(rel)
                continue
            if host_sha != base and not overwrite:
                conflicts.append(f"{rel}: {target} changed on the host since the sync (set overwrite to replace it)")
                continue
        try:
            text = src.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            skipped.append(f"{rel}: not a text file")
            continue
        plan.append((rel, target, text, sha))
    exported, failed = [], []
    if plan and _ctx is None:
        raise engine.RunboxError("export writes through Hermes' write_file; it is only available to the agent")
    task = {"task_id": _TASK.get()} if _TASK.get() else {}
    exporting = _EXPORTING.set(True)
    try:
        _write_plan(plan, task, dest_root, origins, exported, failed)
    finally:
        _EXPORTING.reset(exporting)
    if exported and dest_root is None:
        store.write_sources(path, {"origins": origins, "roots": roots})
    result = {"exported": exported, "unchanged": len(unchanged), "conflicts": conflicts,
              "skipped": skipped, "failed": failed, "ok": not conflicts and not failed}
    lines = [f"{e['file']} -> {e['to']}" for e in exported] + conflicts + skipped + failed
    _record(path, "export", {**result, "output": "\n".join(lines) or "nothing to export"},
            command=str(dest_root or "back to synced folders"))
    return result


def _write_plan(plan, task, dest_root, origins, exported, failed) -> None:
    for rel, target, text, sha in plan:
        if target.exists():
            # write_file only replaces a file this task has read in full. runbox has just checked
            # the file is unchanged since the sync (or overwrite was asked), so read it for real.
            problem = _read_in_full(target, task)
            if problem:
                failed.append(f"{rel}: {problem}")
                continue
        reply = _maybe_json(_ctx.dispatch_tool("write_file", {"path": str(target), "content": text}, **task))
        error = reply.get("error") if isinstance(reply, dict) else None
        if error or (isinstance(reply, dict) and reply.get("success") is False):
            failed.append(f"{rel}: {error or reply}")
            continue
        exported.append({"file": rel, "to": str(target)})
        if dest_root is None:
            origins[rel] = {"host": str(target), "sha": sha}


def _read_in_full(target: Path, task: dict) -> str | None:
    """Read ``target`` page by page through Hermes' read_file; returns an error or ``None``."""
    offset = 1
    for _ in range(10000):
        reply = _maybe_json(_ctx.dispatch_tool("read_file", {"path": str(target), "offset": offset}, **task))
        if not isinstance(reply, dict) or reply.get("error"):
            return f"could not read {target} before writing: {reply.get('error') if isinstance(reply, dict) else reply}"
        if not reply.get("truncated"):
            return None
        nxt = reply.get("next_offset")
        if not isinstance(nxt, int) or nxt <= offset:
            return f"could not page through {target}"
        offset = nxt
    return f"{target} is too long to read in full"


def _maybe_json(raw):
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


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
    notes = []
    if res.exit_code == 5 and not targets:
        # pytest only discovers test_*.py; fall back to any file that defines test functions.
        found_files = _files_with_tests(path / "workspace")
        if found_files:
            notes.append("no test_*.py files; ran the tests defined in: " + ", ".join(found_files))
            res = runtime.engine().exec(engine.container_name(sid), ["sh", "-c", script, "runbox-test", *argv, *found_files],
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
        notes.append(f"timed out after {_timeout(args)}s; the container was restarted")
    if notes:
        result["notes"] = notes
    _record(path, "test", result, command="pytest " + " ".join(extra + targets))
    return result


_TEST_DEF = re.compile(r"^(?:async\s+)?def test_\w*\s*\(|^class Test\w*", re.MULTILINE)


def _files_with_tests(workspace: Path, limit: int = 50) -> list[str]:
    out = []
    for p in sorted(workspace.rglob("*.py")):
        rel = p.relative_to(workspace)
        if any(part in SKIP_NAMES for part in rel.parts) or p.is_symlink():
            continue
        try:
            if _TEST_DEF.search(p.read_text(encoding="utf-8", errors="replace")):
                out.append(rel.as_posix())
        except OSError:
            continue
        if len(out) >= limit:
            break
    return out


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
