import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from plugin.runbox import engine, issues, lifecycle, store, tool  # noqa: E402

SID = "local-abc"


class FakeDocker(engine.Docker):
    """Records docker/colima argv; ``replies`` maps an argv prefix to (rc, stdout, stderr)."""

    def __init__(self, root, *, platform="darwin", up=True):
        super().__init__(root, platform=platform)
        self.calls = []
        self.replies = {}
        self.up = up
        self.containers = {}

    def socket_present(self):
        return self.up

    def _call(self, argv, timeout):
        self.calls.append(argv)
        if argv[:2] == ["colima", "start"]:
            self.up = True
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:2] == ["colima", "stop"]:
            self.up = False
            return subprocess.CompletedProcess(argv, 0, "", "")
        args = argv[1:]
        for prefix, reply in self.replies.items():
            if tuple(args[:len(prefix)]) == prefix:
                if isinstance(reply, Exception):
                    raise reply
                return subprocess.CompletedProcess(argv, *reply)
        if args[:1] == ["version"]:
            return subprocess.CompletedProcess(argv, 0 if self.up else 1, "27.0", "")
        if args[:1] == ["inspect"]:
            state = self.containers.get(args[-1])
            return subprocess.CompletedProcess(argv, 0 if state else 1, state or "", "")
        if args[:2] == ["run", "-d"]:
            self.containers[args[args.index("--name") + 1]] = "running"
        if args[:2] == ["rm", "-f"]:
            self.containers.pop(args[2], None)
        if args[:1] == ["ps"]:
            out = "\n".join(json.dumps({"Names": n, "State": s,
                                        "Labels": f"{engine.LABEL}=1,{engine.LABEL}.session={self.session_of(n)}"})
                            for n, s in self.containers.items())
            return subprocess.CompletedProcess(argv, 0, out, "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def session_of(self, name):
        return SID if name == engine.container_name(SID) else "local-gone"

    def ran(self, *prefix):
        return [c for c in self.calls if tuple(c[1:1 + len(prefix)]) == prefix]


@pytest.fixture
def rt(tmp_path, monkeypatch):
    runtime = tool.Runtime(root=tmp_path / "data")
    runtime.docker = FakeDocker(runtime.root)
    toolchain = runtime.docker.toolchain_dir(runtime.settings.image)
    toolchain.mkdir(parents=True)
    (toolchain / ".runbox-toolchain").write_text(" ".join(engine.TOOLCHAIN))
    monkeypatch.setattr(tool, "runtime", runtime)
    return runtime


def call(**args):
    return json.loads(tool.handle(args, session_id=SID))


def test_container_is_offline_and_hardened(rt):
    rt.docker.replies[("exec",)] = (0, "hi\n", "")
    out = call(action="run", command="echo hi")
    assert out["ok"] and out["stdout"] == "hi\n"
    (run,) = rt.docker.ran("run", "-d")
    joined = " ".join(run)
    assert "--network none" in joined
    for flag in ("--cap-drop ALL", "--security-opt no-new-privileges", "--pids-limit 256", "--init",
                 "--memory 1g", "/opt/runbox/deps:ro", "/opt/runbox/tools:ro"):
        assert flag in joined
    assert "started a new container" in out["notes"]


def test_isolated_docker_env(rt):
    env = rt.docker.env()
    assert env["DOCKER_CONFIG"] == str(rt.root / "docker-config")
    assert json.loads((rt.root / "docker-config" / "config.json").read_text()) == {}
    assert env["DOCKER_HOST"].endswith("/.colima/runbox/docker.sock")
    assert "DOCKER_CONTEXT" not in env


def test_vm_started_on_demand_without_activating(rt):
    rt.docker.up = False
    rt.docker.replies[("exec",)] = (0, "", "")
    out = call(action="run", command="true")
    (start,) = [c for c in rt.docker.calls if c[:2] == ["colima", "start"]]
    assert "--activate=false" in start and start[3] == "runbox"
    assert any(n.startswith("started the runbox VM") for n in out["notes"])


def test_linux_runs_as_host_user(tmp_path):
    d = FakeDocker(tmp_path, platform="linux")
    assert d.host == "" and not d.colima
    if hasattr(__import__("os"), "getuid") and __import__("os").getuid() != 0:
        assert d._user_args()[0] == "--user"


def test_install_requires_approval_and_no_pip_options(rt):
    assert tool.pre_tool_call(tool_name="runbox", args={"action": "run"}) is None
    assert tool.pre_tool_call(tool_name="terminal", args={"action": "install"}) is None
    gate = tool.pre_tool_call(tool_name="runbox", args={"action": "install", "packages": ["requests"]})
    assert gate["action"] == "approve" and "requests" in gate["message"]
    assert "not allowed" in call(action="install", packages=["-i", "http://evil/simple", "x"])["error"]


def test_install_uses_separate_networked_container(rt):
    out = call(action="install", packages=["requests"])
    assert out["ok"]
    (pip,) = [c for c in rt.docker.ran("run", "--rm") if "requests" in c]
    joined = " ".join(pip)
    assert "--network" not in joined and "/workspace" not in joined
    assert "--target /target requests" in joined
    assert not rt.docker.ran("run", "-d")


def test_sync_copies_and_guards_paths(rt, tmp_path):
    src = tmp_path / "proj"
    (src / "pkg").mkdir(parents=True)
    (src / "pkg" / "a.py").write_text("x = 1\n")
    (src / ".git").mkdir()
    (src / ".git" / "HEAD").write_text("ref")
    single = tmp_path / "buf.py"
    single.write_text("print(1)\n")
    out = call(action="sync", paths=[str(src), str(single)], files={"t/test_a.py": "def test(): pass\n"})
    assert out["files"] == ["buf.py", "pkg/a.py", "t/test_a.py"]
    assert call(action="sync", files={"../x.py": "1"})["success"] is False
    assert call(action="sync", paths=["rel/path"])["success"] is False
    assert call(action="sync", clean=True)["files"] == []


def test_check_reports_ruff_issues(rt):
    ruff = [{"filename": "/workspace/broken.py", "location": {"row": 2, "column": 7}, "code": "F821",
             "message": "Undefined name `undefined_name`"},
            {"filename": "/workspace/bad.py", "location": {"row": 1, "column": 5}, "code": None,
             "message": "Expected ':', found newline"}]
    rt.docker.replies[("exec",)] = (0, json.dumps(ruff), "")
    out = call(action="check")
    assert out["ok"] is False
    assert out["report"] == ["bad.py:1:5: syntax Expected ':', found newline",
                             "broken.py:2:7: F821 Undefined name `undefined_name`"]
    argv = rt.docker.ran("exec")[0]
    assert "--isolated" in argv and argv[argv.index("--select") + 1] == tool.CHECK_RULES


JUNIT = """<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest" errors="0"
failures="1" skipped="0" tests="2"><testcase classname="test_calc" name="test_add" file="test_calc.py"
line="2"/><testcase classname="test_calc" name="test_bad" file="test_calc.py" line="5"><failure
message="assert 4 == 5&#10; +  where 4 = add(2, 2)">test_calc.py:7: in test_bad
    assert add(2, 2) == 5
E   assert 4 == 5</failure></testcase></testsuite></testsuites>"""


def test_test_reports_failures_with_location(rt):
    rt.docker.replies[("exec",)] = (1, "F.\n1 failed, 1 passed\n===RUNBOX-JUNIT===\n" + JUNIT, "")
    out = call(action="test")
    assert out["summary"]["passed"] == 1 and out["summary"]["failures"] == 1
    assert out["report"] == ["test_calc.py:7: test-failed test_calc.py::test_bad: assert 4 == 5 + where 4 = add(2, 2)"]
    runs = store.recent_runs(store.find_session(rt.root, SID))
    assert runs[-1]["action"] == "test" and runs[-1]["ok"] is False


def test_run_failure_yields_traceback_issue(rt):
    stderr = ('Traceback (most recent call last):\n  File "/workspace/main.py", line 3, in <module>\n'
              '    main()\n  File "/workspace/main.py", line 2, in main\n    1/0\n'
              'ZeroDivisionError: division by zero\n')
    rt.docker.replies[("exec",)] = (1, "", stderr)
    out = call(action="run", command="python main.py")
    assert out["report"] == ["main.py:2: ZeroDivisionError division by zero"]


def test_timeout_restarts_container(rt):
    rt.docker.replies[("exec",)] = subprocess.TimeoutExpired(["docker"], 1, output=b"partial")
    out = call(action="run", command="sleep 99", timeout=1)
    assert out["timed_out"] and out["stdout"] == "partial"
    assert rt.docker.ran("restart")


def test_reset_wipes_files_and_container(rt):
    call(action="sync", files={"a.py": "1"})
    rt.docker.replies[("exec",)] = (0, "", "")
    call(action="run", command="true")
    assert call(action="reset")["reset"]
    assert rt.docker.ran("rm", "-f")
    assert call(action="status")["files"] == []


def test_idle_sweep_removes_boxes_then_stops_vm(rt):
    rt.docker.replies[("exec",)] = (0, "", "")
    call(action="run", command="true")
    rt.docker.containers[engine.container_name("local-orphan")] = "running"
    now = time.time()
    first = lifecycle.sweep(rt, now=now)
    assert first["removed"] == [engine.container_name("local-orphan")] and not first["vm_stopped"]
    later = now + 16 * 60
    second = lifecycle.sweep(rt, now=later)
    assert second["removed"] == [engine.container_name(SID)] and second["vm_stopped"]
    assert lifecycle.sweep(rt, now=later + 60) == {}


def _db(path, ids):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
    conn.executemany("INSERT INTO sessions VALUES (?)", [(i,) for i in ids])
    conn.commit()
    conn.close()


def test_reconcile_removes_deleted_chat_and_its_container(tmp_path):
    root = tmp_path / "data"
    for sid in ("local-keep", "local-gone"):
        store.session_dir(root, sid)
    db = tmp_path / "state.db"
    _db(db, ["local-keep"])
    removed_boxes = []
    rows = store.reconcile(root, db, grace_seconds=0, on_remove=removed_boxes.append)
    assert rows == [{"session_id": "local-gone", "reason": "deleted"}]
    assert removed_boxes == ["local-gone"]
    assert store.find_session(root, "local-keep") and not store.find_session(root, "local-gone")


def test_reconcile_skips_when_db_matches_nothing(tmp_path):
    root = tmp_path / "data"
    for sid in ("local-a", "local-b"):
        store.session_dir(root, sid)
    db = tmp_path / "state.db"
    _db(db, ["something-else"])
    assert store.reconcile(root, db, grace_seconds=0) == []


def test_pytest_report_handles_empty_and_errors():
    assert issues.pytest_report("") == ({"tests": 0, "failures": 0, "errors": 0, "skipped": 0, "passed": 0}, [])
    xml = ('<testsuite tests="1" errors="1" failures="0" skipped="0"><testcase classname="" name="test_x"'
           ' file="test_x.py"><error message="collection failure">test_x.py:1: in &lt;module&gt;\n'
           'E   ModuleNotFoundError: No module named \'nope\'</error></testcase></testsuite>')
    summary, found = issues.pytest_report(xml)
    assert summary["errors"] == 1 and found[0]["code"] == "test-error" and found[0]["line"] == 1
