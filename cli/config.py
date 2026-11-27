"""Private CLI defaults and the managed daemon lifecycle."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from daemon import DaemonIPCClient

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

MANAGED_LISTEN = "tcp://127.0.0.1:0"


def config_dir() -> Path:
    override = os.environ.get("CLI_DEVICE_CONFIG_DIR")
    if override:
        return Path(override).expanduser().absolute()
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "cli-any-device"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as source:
            value = json.load(source)
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}.")
    return value


def read_config() -> dict[str, Any]:
    return _read_json(config_dir() / "config.json")


def read_daemon() -> dict[str, Any]:
    return _read_json(config_dir() / "daemon.json")


def _save_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(value, out, indent=2)
            out.write("\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def save_config(bundle: str, target: str, listen: str | None) -> None:
    _save_json(config_dir() / "config.json", {"bundle": bundle, "target": target, "listen": listen})


@contextmanager
def _management_lock(directory: Path) -> Iterator[None]:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(directory / "daemon.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if sys.platform == "win32":
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _is_serving(record: dict[str, Any]) -> bool:
    try:
        response = DaemonIPCClient(
            listen=record["listen"], token=record["token"], timeout_s=0.3
        ).call(op="lease_status")
    except (KeyError, ValueError):
        return False
    pid = response.get("daemon_pid")
    return isinstance(pid, int) and pid == record.get("pid")


def _process_alive(pid: int) -> bool:
    try:
        if sys.platform == "linux":
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[0]
            return state not in ("Z", "X")
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except FileNotFoundError:
        return False
    return True


def _run_directory(directory: Path, record: dict[str, Any]) -> Path:
    run_id = record["run_id"]
    if not isinstance(run_id, str) or not run_id.startswith("run-") or Path(run_id).name != run_id:
        raise ValueError("Invalid managed daemon run directory.")
    return directory / run_id


def _remove_record(directory: Path, record: dict[str, Any]) -> None:
    (directory / "daemon.json").unlink(missing_ok=True)
    shutil.rmtree(_run_directory(directory, record), ignore_errors=True)


def start_daemon(
    bundle: str,
    listen: str,
    confirm_mode: str,
    allow_live_hardware: bool,
    token: str | None,
    *,
    reuse_running: bool = False,
) -> dict[str, Any]:
    directory = config_dir()
    with _management_lock(directory):
        current = read_daemon()
        if current and _is_serving(current):
            if (
                current["bundle"] != bundle
                or listen not in (current["requested_listen"], current["listen"])
                or (
                    not reuse_running
                    and (
                        current["confirm_mode"] != confirm_mode
                        or current["allow_live_hardware"] != allow_live_hardware
                        or (token is not None and current["token"] != token)
                    )
                )
            ):
                raise ValueError(
                    "A different managed daemon is running. Run cli-device down first."
                )
            return current
        if current:
            _remove_record(directory, current)

        run_dir = Path(tempfile.mkdtemp(prefix="run-", dir=directory))
        command = [
            sys.executable, "-m", "daemon.main", "--bundle", bundle, "--listen", listen,
            "--confirm-mode", confirm_mode, "--run-dir", str(run_dir),
        ]
        if allow_live_hardware:
            command.append("--allow-live-hardware")
        env = os.environ.copy()
        if token is not None:
            env["CLI_DEVICE_IPC_TOKEN"] = token
        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS

        process: subprocess.Popen[bytes] | None = None
        try:
            log = run_dir / "daemon.log"
            fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as output:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    env=env,
                    start_new_session=sys.platform != "win32",
                    creationflags=creationflags,
                )
            deadline = time.monotonic() + 5
            while process.poll() is None and time.monotonic() < deadline:
                try:
                    ready = _read_json(run_dir / "ready")
                except json.JSONDecodeError:
                    ready = {}
                if ready:
                    record = {
                        **ready,
                        "pid": process.pid,
                        "bundle": bundle,
                        "requested_listen": listen,
                        "confirm_mode": confirm_mode,
                        "allow_live_hardware": allow_live_hardware,
                        "run_id": run_dir.name,
                    }
                    if _is_serving(record):
                        _save_json(directory / "daemon.json", record)
                        return record
                time.sleep(0.02)
            detail = log.read_text(encoding="utf-8", errors="replace").strip()
            raise ValueError(detail or "cli-device-d did not become ready within 5 seconds.")
        except BaseException:
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
            shutil.rmtree(run_dir, ignore_errors=True)
            raise


def stop_daemon() -> bool:
    directory = config_dir()
    with _management_lock(directory):
        record = read_daemon()
        if not record:
            return False
        run_dir = _run_directory(directory, record)
        pid = record["pid"]
        if _is_serving(record):
            if sys.platform == "win32":
                (run_dir / "stop").touch()
            else:
                with suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGTERM)

            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if sys.platform == "win32":
                    if (run_dir / "stopped").exists():
                        break
                elif not _process_alive(pid):
                    break
                time.sleep(0.02)
            else:
                raise ValueError(f"Managed daemon {pid} did not exit within 5 seconds.")
        _remove_record(directory, record)
        return True
