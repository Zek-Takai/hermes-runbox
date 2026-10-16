"""Docker backend: one hardened, offline container per chat.

The plugin drives the ``docker`` CLI with its own ``DOCKER_CONFIG`` (an empty config under the
plugin's data dir) and, on macOS, its own colima profile reached through ``DOCKER_HOST``, so the
user's Docker settings and active context are never read or changed.

Inside a container:
- ``/workspace``          the chat's workspace (bind mount, read-write)
- ``/opt/runbox/deps``    packages from ``install`` (bind mount, read-only)
- ``/opt/runbox/tools``   the checker toolchain, ruff + pytest (bind mount, read-only)
The work container runs with ``--network none``. Packages are installed by a short-lived
installer container that has network access and mounts only the deps directory.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_IMAGE = "python:3.12-slim"
TOOLCHAIN = ("ruff==0.17.1", "pytest==9.1.1")
LABEL = "io.hermes.runbox"
COLIMA_PROFILE = "runbox"
COLIMA_CREATE_ARGS = ("--cpu", "2", "--memory", "2", "--disk", "20", "--vm-type", "vz", "--runtime", "docker")
HARDENING = (
    "--init",
    "--cap-drop", "ALL",
    "--cap-add", "DAC_OVERRIDE", "--cap-add", "CHOWN", "--cap-add", "FOWNER",
    "--security-opt", "no-new-privileges",
    "--pids-limit", "256",
    "--tmpfs", "/tmp:rw,nosuid,size=512m",
    "--tmpfs", "/run:rw,noexec,nosuid,size=64m",
)
CONTAINER_ENV = {
    "HOME": "/tmp",
    "PYTHONPATH": "/opt/runbox/deps:/opt/runbox/tools",
    "PATH": "/opt/runbox/deps/bin:/opt/runbox/tools/bin:/usr/local/bin:/usr/bin:/bin",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTEST_ADDOPTS": "-p no:cacheprovider",  # no .pytest_cache in the workspace, whoever runs pytest
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
}
_EXTRA_BIN = ("/opt/homebrew/bin", "/usr/local/bin")


class RunboxError(RuntimeError):
    pass


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False


def container_name(session_id: str) -> str:
    return "runbox-" + hashlib.sha256(session_id.encode()).hexdigest()[:16]


def _image_key(image: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", image)


class Docker:
    """Thin wrapper over the docker (and, on macOS, colima) CLIs with runbox's isolated env."""

    def __init__(self, data_root: Path, *, docker_host: str = "", platform: str = sys.platform):
        self.data_root = Path(data_root)
        self.colima = not docker_host and platform == "darwin"
        if docker_host:
            self.host = docker_host
        elif self.colima:
            self.host = f"unix://{Path.home()}/.colima/{COLIMA_PROFILE}/docker.sock"
        else:
            self.host = ""
        self.linux = platform.startswith("linux")
        self.config_dir = self.data_root / "docker-config"

    # --- process plumbing ------------------------------------------------------------------

    def env(self) -> dict:
        env = os.environ.copy()
        parts = env.get("PATH", "").split(os.pathsep)
        env["PATH"] = os.pathsep.join(parts + [p for p in _EXTRA_BIN if p not in parts])
        self.config_dir.mkdir(parents=True, exist_ok=True)
        cfg = self.config_dir / "config.json"
        if not cfg.exists():
            cfg.write_text("{}\n", encoding="utf-8")
        env["DOCKER_CONFIG"] = str(self.config_dir)
        env.pop("DOCKER_CONTEXT", None)
        if self.host:
            env["DOCKER_HOST"] = self.host
        return env

    def _call(self, argv: list[str], timeout: float) -> subprocess.CompletedProcess:
        env = self.env()
        exe = shutil.which(argv[0], path=env["PATH"])
        if exe is None:
            hint = " (brew install colima docker)" if self.colima else ""
            raise RunboxError(f"`{argv[0]}` was not found on PATH{hint}")
        return subprocess.run([exe, *argv[1:]], capture_output=True, text=True, timeout=timeout, env=env)

    def docker(self, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
        return self._call(["docker", *args], timeout)

    # --- daemon / VM -----------------------------------------------------------------------

    def socket_present(self) -> bool:
        if self.host.startswith("unix://"):
            return Path(self.host[len("unix://"):]).exists()
        return True

    def reachable(self) -> bool:
        if not self.socket_present():
            return False
        try:
            return self.docker("version", "--format", "{{.Server.Version}}", timeout=20).returncode == 0
        except (subprocess.TimeoutExpired, RunboxError):
            return False

    def ensure_up(self) -> str | None:
        """Make sure the daemon answers; on macOS start the runbox colima VM if needed."""
        if self.reachable():
            return None
        if not self.colima:
            where = self.host or "the default Docker daemon"
            raise RunboxError(f"Docker is not reachable at {where}; start Docker or set runbox's docker_host")
        profile_cfg = Path.home() / ".colima" / COLIMA_PROFILE / "colima.yaml"
        argv = ["colima", "start", "-p", COLIMA_PROFILE, "--activate=false"]
        if not profile_cfg.exists():
            argv[4:4] = list(COLIMA_CREATE_ARGS)
        started = time.monotonic()
        try:
            proc = self._call(argv, timeout=900)
        except subprocess.TimeoutExpired as exc:
            raise RunboxError("the runbox VM did not start within 15 minutes") from exc
        if proc.returncode != 0 or not self.reachable():
            raise RunboxError("could not start the runbox VM: " + _tail(proc.stderr or proc.stdout, 800))
        self._wait_for_file_sharing()
        return f"started the runbox VM in {time.monotonic() - started:.0f}s"

    def _wait_for_file_sharing(self, timeout: float = 60) -> None:
        """Docker can answer before the VM has mounted the home folder; a container created in
        that window gets empty stand-in folders. Wait until the VM sees the plugin's data folder."""
        self.data_root.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                probe = self._call(["colima", "ssh", "-p", COLIMA_PROFILE, "--", "test", "-d", str(self.data_root)],
                                   timeout=20)
                if probe.returncode == 0:
                    return
            except (subprocess.TimeoutExpired, RunboxError):
                pass
            time.sleep(1)
        raise RunboxError(f"the runbox VM cannot see {self.data_root}; is it under your home folder?")

    def stop_vm(self) -> bool:
        if not self.colima:
            return False
        try:
            return self._call(["colima", "stop", "-p", COLIMA_PROFILE], timeout=180).returncode == 0
        except (subprocess.TimeoutExpired, RunboxError):
            return False

    # --- images and toolchain --------------------------------------------------------------

    def ensure_image(self, image: str) -> str | None:
        if self.docker("image", "inspect", image, timeout=30).returncode == 0:
            return None
        proc = self.docker("pull", "-q", image, timeout=900)
        if proc.returncode != 0:
            raise RunboxError(f"could not pull {image}: " + _tail(proc.stderr, 800))
        return f"pulled {image}"

    def toolchain_dir(self, image: str) -> Path:
        return self.data_root / "toolchain" / _image_key(image)

    def ensure_toolchain(self, image: str) -> str | None:
        target = self.toolchain_dir(image)
        marker = target / ".runbox-toolchain"
        spec = " ".join(TOOLCHAIN)
        try:
            if marker.read_text(encoding="utf-8").strip() == spec:
                return None
        except OSError:
            pass
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        result = self.pip_install(image, target, list(TOOLCHAIN))
        landed = (target / "bin" / "ruff").exists() and (target / "pytest").is_dir()
        if result.exit_code != 0 or not landed:
            shutil.rmtree(target, ignore_errors=True)
            why = (result.stderr or result.stdout) if result.exit_code != 0 else "the files did not reach the host folder"
            raise RunboxError("could not install the checker toolchain: " + _tail(why, 800))
        marker.write_text(spec + "\n", encoding="utf-8")
        return "installed the checker toolchain (" + ", ".join(TOOLCHAIN) + ")"

    # --- containers ------------------------------------------------------------------------

    def _user_args(self) -> list[str]:
        # Native Linux daemons write bind-mount files as the container user; match the host user so
        # the workspace stays owned (and removable) by them. colima maps ownership itself.
        if self.linux and hasattr(os, "getuid") and os.getuid() != 0:
            return ["--user", f"{os.getuid()}:{os.getgid()}"]
        return []

    @staticmethod
    def _env_args() -> list[str]:
        args: list[str] = []
        for key, value in CONTAINER_ENV.items():
            args += ["-e", f"{key}={value}"]
        return args

    def pip_install(self, image: str, target: Path, packages: list[str], *,
                    memory: str = "1g", cpus: float = 2, timeout: float = 600) -> ExecResult:
        """Run pip in a throwaway, networked container that mounts only ``target``."""
        argv = ["run", "--rm", *HARDENING, "--memory", memory, "--cpus", str(cpus), *self._user_args(),
                "-e", "HOME=/tmp", "-e", "PIP_DISABLE_PIP_VERSION_CHECK=1",
                "-v", f"{target}:/target", image,
                "python", "-m", "pip", "install", "--no-cache-dir", "--root-user-action=ignore",
                "--upgrade", "--target", "/target", *packages]
        return self._timed(argv, timeout)

    def container_state(self, name: str) -> str | None:
        proc = self.docker("inspect", "-f", "{{.State.Status}}", name, timeout=30)
        return proc.stdout.strip() if proc.returncode == 0 else None

    MOUNT_MARKER = ".runbox-mount"

    def mounts_visible(self, name: str, image: str) -> bool:
        """The container sees this chat's folders and the toolchain (marker files written host-side)."""
        probe = self.docker("exec", name, "sh", "-c",
                            f"test -f /opt/runbox/deps/{self.MOUNT_MARKER} && test -f /opt/runbox/tools/.runbox-toolchain",
                            timeout=30)
        return probe.returncode == 0

    def ensure_container(self, name: str, session_id: str, *, image: str, workspace: Path, deps: Path,
                         memory: str, cpus: float, attempts: int = 5) -> str | None:
        (deps / self.MOUNT_MARKER).write_text("runbox\n", encoding="utf-8")
        recreated = False
        for attempt in range(attempts):
            note = self._start_container(name, session_id, image=image, workspace=workspace, deps=deps,
                                         memory=memory, cpus=cpus)
            if self.mounts_visible(name, image):
                if recreated:
                    return "the container could not see its files; started a fresh one"
                return note
            # Stale or not-yet-ready mount (VM file sharing, or a replaced folder): start over.
            self.remove(name)
            recreated = True
            time.sleep(min(2 * (attempt + 1), 6))
        raise RunboxError("the container cannot see its workspace (VM file sharing is not ready); try again shortly")

    def _start_container(self, name: str, session_id: str, *, image: str, workspace: Path, deps: Path,
                         memory: str, cpus: float) -> str | None:
        state = self.container_state(name)
        if state == "running":
            return None
        if state is not None:
            proc = self.docker("start", name, timeout=60)
            if proc.returncode == 0:
                return None
            self.remove(name)
        argv = ["run", "-d", "--name", name,
                "--label", f"{LABEL}=1", "--label", f"{LABEL}.session={session_id}",
                "--network", "none", *HARDENING, "--memory", memory, "--cpus", str(cpus), *self._user_args(),
                *self._env_args(),
                "-v", f"{workspace}:/workspace",
                "-v", f"{deps}:/opt/runbox/deps:ro",
                "-v", f"{self.toolchain_dir(image)}:/opt/runbox/tools:ro",
                "-w", "/workspace", image, "sleep", "infinity"]
        proc = self.docker(*argv, timeout=120)
        if proc.returncode != 0:
            if "already in use" in (proc.stderr or "") and self.container_state(name) == "running":
                return None  # another caller (the Desktop tab or the agent) created it first
            raise RunboxError("could not start the container: " + _tail(proc.stderr, 800))
        return "started a new container"

    def remove(self, name: str) -> bool:
        try:
            return self.docker("rm", "-f", name, timeout=60).returncode == 0
        except (subprocess.TimeoutExpired, RunboxError):
            return False

    def list_boxes(self) -> list[dict]:
        proc = self.docker("ps", "-a", "--filter", f"label={LABEL}=1",
                           "--format", "{{json .}}", timeout=30)
        if proc.returncode != 0:
            return []
        rows = []
        for line in proc.stdout.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            labels = dict(kv.split("=", 1) for kv in (row.get("Labels") or "").split(",") if "=" in kv)
            rows.append({"name": row.get("Names", ""), "state": row.get("State", ""),
                         "session_id": labels.get(f"{LABEL}.session", "")})
        return rows

    def exec(self, name: str, argv: list[str], *, timeout: float) -> ExecResult:
        result = self._timed(["exec", "-w", "/workspace", name, *argv], timeout)
        if result.timed_out:
            # The exec client is gone but the process may still run inside; restart the box.
            self.docker("restart", "-t", "1", name, timeout=60)
        return result

    def _timed(self, docker_args: list[str], timeout: float) -> ExecResult:
        started = time.monotonic()
        try:
            proc = self.docker(*docker_args, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            return ExecResult(-1, _text(exc.stdout), _text(exc.stderr), int((time.monotonic() - started) * 1000), True)
        return ExecResult(proc.returncode, proc.stdout, proc.stderr, int((time.monotonic() - started) * 1000))


def _text(value) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value


def _tail(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else "..." + text[-limit:]
