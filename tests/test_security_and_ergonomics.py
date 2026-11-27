"""Exercise daemon approvals, leases, authenticated IPC, and CLI defaults."""

from __future__ import annotations

import json
import os
import select
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from adapter_spi import (
    AbortResult,
    ActionRequest,
    ActionTelemetry,
    AdapterPair,
    InMemoryFakeAdapter,
)
from compiler import load_bundle
from core_types import ConfirmMode, ErrorCode, ExitCode, RiskClass
from daemon import AdapterRegistry, DaemonIPCClient, DaemonTCPServer, DeviceDaemon

REPO_ROOT = Path(__file__).resolve().parents[1]
BUNDLE = REPO_ROOT / "bundles" / "jubilee"
TARGET = "in_memory"
HOME_ALL = ("motion", "home-all")


def _entrypoint(name: str) -> str:
    command = shutil.which(name, path=str(Path(sys.executable).parent)) or shutil.which(name)
    assert command is not None, f"Install the project to provide {name}."
    return command


def _cli(*args: str) -> list[str]:
    return [_entrypoint("cli-device"), "--json", *args]


def _response(result: subprocess.CompletedProcess[str], code: ExitCode) -> dict[str, Any]:
    assert result.returncode == code, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is (code == ExitCode.OK)
    return payload


def _agent(*args: str, code: ExitCode = ExitCode.OK) -> dict[str, Any]:
    result = subprocess.run(
        _cli(*args), stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10,
        check=False,
    )
    return _response(result, code)


def _human(
    *args: str, answer: str | None = None, code: ExitCode = ExitCode.OK,
) -> tuple[dict[str, Any], str]:
    if os.name != "posix":
        pytest.skip("Requires a POSIX pseudoterminal.")
    import pty
    import tty

    master, slave = pty.openpty()
    try:
        tty.setraw(slave)
        with subprocess.Popen(
            _cli(*args), stdin=slave, stdout=slave, stderr=subprocess.PIPE, text=True,
        ) as process:
            if answer is not None:
                os.write(master, f"{answer}\n".encode())
            try:
                _, stderr = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                raise
            stdout = bytearray()
            while select.select([master], [], [], 0)[0]:
                stdout.extend(os.read(master, 4096))
        result = subprocess.CompletedProcess(
            process.args, process.returncode, stdout.decode(), stderr,
        )
        return _response(result, code), stderr
    finally:
        os.close(master)
        os.close(slave)


@pytest.fixture(autouse=True)
def isolated_cli_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    config_dir = tmp_path / "config"
    monkeypatch.setenv("CLI_DEVICE_CONFIG_DIR", str(config_dir))
    for name in (
        "CLI_DEVICE_BUNDLE", "CLI_DEVICE_TARGET", "CLI_DEVICE_SOCKET", "CLI_DEVICE_IPC_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    yield config_dir
    if (config_dir / "daemon.json").exists():
        _agent("down")


@contextmanager
def _real_daemon(tmp_path: Path, *options: str) -> Iterator[tuple[str, ...]]:
    run_dir = tmp_path / "daemon"
    run_dir.mkdir()
    with subprocess.Popen(
        [
            _entrypoint("cli-device-d"), "--bundle", str(BUNDLE), "--listen", "tcp://127.0.0.1:0",
            "--run-dir", str(run_dir), *options,
        ],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ) as process:
        try:
            deadline = time.monotonic() + 5
            ready_path = run_dir / "ready"
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    stdout, stderr = process.communicate()
                    pytest.fail(f"Daemon exited before readiness: {stdout}{stderr}")
                try:
                    ready = json.loads(ready_path.read_text(encoding="utf-8"))
                except (FileNotFoundError, json.JSONDecodeError):
                    time.sleep(0.01)
                    continue
                break
            else:
                pytest.fail("Daemon did not become ready.")

            token_file = run_dir / "token"
            token_file.write_text(ready["token"] + "\n", encoding="utf-8")
            yield (
                "--bundle", str(BUNDLE), "--target", TARGET, "--listen", ready["listen"],
                "--token-file", str(token_file),
            )
        finally:
            if process.poll() is None:
                (run_dir / "stop").touch()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    raise
                assert process.returncode == 0


def _dry_run_home(base: tuple[str, ...]) -> dict[str, Any]:
    preview = _agent(*base, "--dry-run", *HOME_ALL)
    assert preview["mode"] == "dry_run"
    assert preview["data"]["plan"]["requires_confirm_when_live"] is True
    return preview


@pytest.mark.parametrize("confirm_mode", [ConfirmMode.TTY, ConfirmMode.TOKEN])
def test_non_tty_confirm_cannot_actuate_a_real_daemon(
    tmp_path: Path, confirm_mode: ConfirmMode,
) -> None:
    options = ("--confirm-mode", confirm_mode.value) if confirm_mode == ConfirmMode.TOKEN else ()
    with _real_daemon(tmp_path, *options) as base:
        before = _agent(*base, "sys", "detect")
        assert before["confirm_mode"] == confirm_mode.value
        attack = _agent(*base, "--confirm", *HOME_ALL, code=ExitCode.CONFIRM_REQUIRED)
        assert attack["error"]["code"] == ErrorCode.E_CONFIRM_REQUIRED
        assert attack["error"]["step"] == "confirm_gate"
        after = _agent(*base, "sys", "status")
        assert after["state"] == before["state"]
        assert after["state"]["homed"] == []


def test_token_plan_approval_is_tty_only_single_use_and_bound_to_state(tmp_path: Path) -> None:
    with _real_daemon(tmp_path, "--confirm-mode", "token") as base:
        before = _agent(*base, "sys", "detect")
        preview = _dry_run_home(base)
        assert preview["state"] == before["state"]
        plan_id = preview["data"]["plan"]["plan_id"]

        refused = _agent(*base, "approve", plan_id, code=ExitCode.CONFIRM_REQUIRED)
        assert refused["error"]["code"] == ErrorCode.E_CONFIRM_REQUIRED
        unapproved = _agent(*base, "--plan-id", plan_id, *HOME_ALL, code=ExitCode.CONFIRM_REQUIRED)
        assert unapproved["error"]["step"] == "confirm_gate"
        approved, _ = _human(*base, "approve", plan_id)
        assert approved["data"]["plan_id"] == plan_id
        homed = _agent(*base, "--plan-id", plan_id, *HOME_ALL)
        assert {"X", "Y", "Z"} <= set(homed["state"]["homed"])
        replay = _agent(*base, "--plan-id", plan_id, *HOME_ALL, code=ExitCode.CONFIRM_REQUIRED)
        assert replay["error"]["code"] == ErrorCode.E_CONFIRM_REQUIRED
        assert replay["state"] == homed["state"]

        preview = _dry_run_home(base)
        stale_id = preview["data"]["plan"]["plan_id"]
        _human(*base, "approve", stale_id)
        # In-memory detect advances state_seq and preserves the observable machine state.
        changed = _agent(*base, "sys", "detect")
        assert changed["state"] == preview["state"]
        stale = _agent(*base, "--plan-id", stale_id, *HOME_ALL, code=ExitCode.CONFIRM_REQUIRED)
        assert stale["error"]["code"] == ErrorCode.E_CONFIRM_REQUIRED
        assert stale["state"] == changed["state"]


def test_interactive_confirm_previews_and_honors_the_answer(tmp_path: Path) -> None:
    with _real_daemon(tmp_path) as base:
        before = _agent(*base, "sys", "detect")
        declined, prompt = _human(
            *base, "--confirm", *HOME_ALL, answer="n", code=ExitCode.CONFIRM_REQUIRED,
        )
        assert "motion.home_all on in_memory [R3_PHYSICAL_INIT]" in prompt
        assert "Approve this plan and execute it?" in prompt
        assert declined["error"]["code"] == ErrorCode.E_CONFIRM_REQUIRED
        assert _agent(*base, "sys", "status")["state"] == before["state"]

        confirmed, _ = _human(*base, "--confirm", *HOME_ALL, answer="y")
        assert {"X", "Y", "Z"} <= set(confirmed["state"]["homed"])
        assert confirmed["mode"] == "live"


def test_session_lease_limits_risk_and_services_and_abort_revokes_it(tmp_path: Path) -> None:
    grant = (
        "lease", "grant", "--target", TARGET, "--ttl", "30",
        "--max-risk", RiskClass.R2_MOTION.value, "--allow-service", "motion.move_safe",
        "--allow-service", "motion.home_all", "--allow-service", "expert.gcode",
    )
    move_safe = ("motion", "move-safe", "--x", "10", "--y", "20")
    with _real_daemon(tmp_path, "--confirm-mode", "token") as base:
        _agent(*base, "sys", "detect")
        _human(*base, "--confirm", *HOME_ALL, answer="y")
        refused = _agent(*base, *grant, code=ExitCode.CONFIRM_REQUIRED)
        assert refused["error"]["code"] == ErrorCode.E_CONFIRM_REQUIRED
        assert _agent(*base, "lease", "status")["data"]["lease"] is None
        before_grant = _agent(*base, *move_safe, code=ExitCode.CONFIRM_REQUIRED)
        assert before_grant["error"]["code"] == ErrorCode.E_CONFIRM_REQUIRED

        granted, _ = _human(*base, *grant)
        assert granted["data"]["lease"]["max_risk"] == RiskClass.R2_MOTION
        assert 0 < granted["data"]["lease"]["expires_in_s"] <= 30
        moved = _agent(*base, *move_safe)
        assert moved["state"]["pos"]["X"] == 10
        assert moved["state"]["pos"]["Y"] == 20
        assert _agent(*base, "lease", "status")["data"]["lease"] is not None

        for denied in (
            HOME_ALL,
            ("expert", "gcode", "--command", "M114"),
            ("motion", "move-to", "--x", "10", "--y", "20", "--z", "150"),
        ):
            result = _agent(*base, *denied, code=ExitCode.CONFIRM_REQUIRED)
            assert result["error"]["code"] == ErrorCode.E_CONFIRM_REQUIRED
            assert result["state"] == moved["state"]

        _agent(*base, "lease", "revoke", "--target", TARGET)
        assert _agent(*base, "lease", "status", "--target", TARGET)["data"]["lease"] is None
        _human(*base, *grant)
        assert _agent(*base, "sys", "abort")["state"]["estop_latched"] is True
        assert _agent(*base, "lease", "status")["data"]["lease"] is None


def test_tcp_bearer_auth_and_second_cli_process_unblocks_action(tmp_path: Path) -> None:
    action_started = threading.Event()
    deadlocked = threading.Event()
    abort_times: list[float] = []
    unblock_times: list[float] = []

    class BlockedAdapter(InMemoryFakeAdapter):
        def execute(self, request: ActionRequest) -> ActionTelemetry:
            try:
                return super().execute(request)
            finally:
                unblock_times.append(time.perf_counter())

        def abort_now(self) -> AbortResult:
            abort_times.append(time.perf_counter())
            return super().abort_now()

    fake = BlockedAdapter(
        target=TARGET, deadlock_event=deadlocked, action_started_event=action_started,
    )
    registry = AdapterRegistry(load_entry_points=False)
    registry.register(TARGET, lambda cfg: AdapterPair(cfg.target, fake, fake))
    daemon = DeviceDaemon(
        bundle=load_bundle(BUNDLE), registry=registry, confirm_mode=ConfirmMode.TOKEN,
    )
    with pytest.raises(ValueError, match="TCP IPC requires"):
        DaemonTCPServer(listen="tcp://0.0.0.0:0", daemon=daemon)

    token = "test-bearer-token"
    server = DaemonTCPServer(listen="tcp://127.0.0.1:0", daemon=daemon, auth_token=token)
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    worker.start()
    base = ("--bundle", str(BUNDLE), "--target", TARGET, "--listen", server.listen_uri)
    try:
        wrong_token = tmp_path / "wrong-token"
        wrong_token.write_text("wrong-token\n", encoding="utf-8")
        for options in ((), ("--token-file", str(wrong_token))):
            rejected = _agent(*base, *options, "sys", "detect", code=ExitCode.SAFETY_DENIED)
            assert rejected["error"]["code"] == ErrorCode.E_PRECONDITION_FAILED
            assert rejected["error"]["step"] == "ipc_auth"
        assert fake.poll_state().detected is False

        client = DaemonIPCClient(listen=server.listen_uri, token=token, timeout_s=1)
        assert client.call(target=TARGET, service="sys.detect")["ok"]
        preview = client.call(target=TARGET, service="motion.home_all", dry_run=True)
        plan_id = preview["data"]["plan"]["plan_id"]
        assert client.call(op="approve_plan", plan_id=plan_id)["ok"]
        env = {**os.environ, "CLI_DEVICE_IPC_TOKEN": token}
        with subprocess.Popen(
            _cli(*base, "--plan-id", plan_id, *HOME_ALL), env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ) as motion:
            try:
                assert action_started.wait(timeout=5)
                assert motion.poll() is None
                aborted = _response(subprocess.run(
                    _cli(*base, "sys", "abort"), env=env, stdin=subprocess.DEVNULL,
                    capture_output=True, text=True, timeout=5, check=False,
                ), ExitCode.OK)
                assert aborted["state"]["estop_latched"]
                assert aborted["duration_ms"] < 100
                assert aborted["data"]["abort"]["latency_ms"] < 100

                stdout, stderr = motion.communicate(timeout=5)
                result = subprocess.CompletedProcess(motion.args, motion.returncode, stdout, stderr)
                failed = _response(result, ExitCode.EXECUTION_ERROR)
                assert failed["error"]["code"] == ErrorCode.E_HARDWARE_FAULT
                assert 0 <= (unblock_times[0] - abort_times[0]) * 1000 < 100
            finally:
                if motion.poll() is None:
                    motion.kill()
                    motion.communicate()
    finally:
        deadlocked.set()
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_use_up_down_and_implicit_auto_start(isolated_cli_config: Path, tmp_path: Path) -> None:
    record_path = isolated_cli_config / "daemon.json"
    saved = _agent("use", "jubilee", "--target", TARGET, "--listen", "tcp://127.0.0.1:0")
    assert saved["target"] == TARGET
    assert not record_path.exists()

    token_file = tmp_path / "token"
    token_file.write_text("managed-test-token\n", encoding="utf-8")
    started = _agent("up", "--confirm-mode", "token", "--token-file", str(token_file))
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["token"] == "managed-test-token"
    assert "token" not in started
    if os.name == "posix":
        assert stat.S_IMODE(record_path.stat().st_mode) == 0o600
    detected = _agent("sys", "detect")
    assert detected["daemon_pid"] == started["pid"] == record["pid"]
    assert detected["target"] == TARGET
    assert detected["confirm_mode"] == ConfirmMode.TOKEN

    assert _agent("down")["stopped"] is True
    assert not record_path.exists()
    stopped = DaemonIPCClient(listen=record["listen"], token=record["token"], timeout_s=1).call(
        target=TARGET, service="sys.status",
    )
    assert stopped["error"]["code"] == ErrorCode.E_DAEMON_UNAVAILABLE
    if sys.platform == "linux":
        proc_stat = Path(f"/proc/{record['pid']}/stat")
        if proc_stat.exists():
            fields = proc_stat.read_text().rsplit(") ", 1)[1].split()
            assert fields[0] == "Z"
            assert int(fields[-1]) == 0

    custom_endpoints = [("--socket", str(tmp_path / "missing.sock"))]
    with socket.socket() as unused:
        unused.bind(("127.0.0.1", 0))
        custom_endpoints.append(("--listen", f"tcp://127.0.0.1:{unused.getsockname()[1]}"))
        for options in (("--no-auto-start",), *custom_endpoints):
            unavailable = _agent(*options, "sys", "detect", code=ExitCode.TIMEOUT)
            assert unavailable["error"]["code"] == ErrorCode.E_DAEMON_UNAVAILABLE
            assert not record_path.exists()

    auto_started = _agent("sys", "detect")
    assert auto_started["confirm_mode"] == ConfirmMode.TTY
    assert auto_started["target"] == TARGET
    assert record_path.exists()


def test_explicit_auto_start_without_saved_defaults(isolated_cli_config: Path) -> None:
    detected = _agent(
        "--auto-start", "--bundle", str(BUNDLE), "--target", TARGET, "sys", "detect",
    )
    record = json.loads((isolated_cli_config / "daemon.json").read_text(encoding="utf-8"))
    assert detected["daemon_pid"] == record["pid"]
    assert not (isolated_cli_config / "config.json").exists()
