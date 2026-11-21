"""End-to-end closed-loop tests: daemon (IPC) + schema-driven CLI for Jubilee and OpenRadio."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from click.testing import CliRunner

from adapter_spi import AdapterConfig, AdapterPair, InMemoryFakeAdapter
from cli.main import build_cli_app
from compiler.loader import load_bundle
from core_types import ExitCode
from daemon.ipc import DaemonIPCServer
from daemon.registry import AdapterRegistry
from daemon.service import DeviceDaemon

REPO_ROOT = Path(__file__).resolve().parent.parent
JUBILEE_BUNDLE_DIR = REPO_ROOT / "bundles" / "jubilee"
RADIO_FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "open_radio"


def _run_cli(args: list[str]) -> tuple[int, dict[str, object]]:
    runner = CliRunner()
    app = build_cli_app()
    result = runner.invoke(app, args, catch_exceptions=False)
    payload = json.loads(result.output)
    return result.exit_code, payload


def test_daemon_ipc_and_cli_closed_loop_jubilee_workflow(tmp_path: Path) -> None:
    socket_path = tmp_path / "jubilee.sock"
    bundle = load_bundle(JUBILEE_BUNDLE_DIR)
    daemon = DeviceDaemon(bundle=bundle)
    server = DaemonIPCServer(socket_path=socket_path, daemon=daemon)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    base = [
        "--bundle",
        str(JUBILEE_BUNDLE_DIR),
        "--target",
        "in_memory",
        "--socket",
        str(socket_path),
        "--json",
    ]
    try:
        code, payload = _run_cli([*base, "sys", "detect"])
        assert code == ExitCode.OK
        assert payload["ok"] is True

        code, payload = _run_cli([*base, "sys", "preflight"])
        assert code == ExitCode.OK

        # Dry-run home-all -> exit 0, zero motion
        code, payload = _run_cli([*base, "--dry-run", "motion", "home-all"])
        assert code == ExitCode.OK
        assert payload["mode"] == "dry_run"

        # Missing confirm -> exit 4
        code, payload = _run_cli([*base, "motion", "home-all"])
        assert code == ExitCode.CONFIRM_REQUIRED

        # Confirmed home-all -> exit 0
        code, payload = _run_cli([*base, "--confirm", "motion", "home-all"])
        assert code == ExitCode.OK

        # Load deck & labware -> pickup -> move-well -> park
        assert _run_cli([*base, "deck", "load-deck", "--spec", "jubilee_deck.json"])[0] == 0
        assert (
            _run_cli(
                [
                    *base,
                    "deck",
                    "load-labware",
                    "--slot",
                    "1",
                    "--spec",
                    "corning_96_wellplate.json",
                ]
            )[0]
            == 0
        )
        assert _run_cli([*base, "--confirm", "tool", "pickup", "--tool-id", "0"])[0] == 0

        code, payload = _run_cli(
            [*base, "motion", "move-well", "--slot", "1", "--well", "A1", "--dz", "5"]
        )
        assert code == ExitCode.OK
        assert payload["ok"] is True

        code, payload = _run_cli([*base, "--confirm", "tool", "park"])
        assert code == ExitCode.OK
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=1.0)


def test_concurrent_ipc_abort_unblocks_deadlocked_action(tmp_path: Path) -> None:
    socket_path = tmp_path / "abort.sock"
    deadlock = threading.Event()
    fake = InMemoryFakeAdapter(target="in_memory", deadlock_event=deadlock)

    registry = AdapterRegistry()
    registry.register(
        "in_memory",
        lambda cfg: AdapterPair(target=cfg.target, supervisor=fake, driver=fake),
    )
    bundle = load_bundle(JUBILEE_BUNDLE_DIR)
    daemon = DeviceDaemon(bundle=bundle, registry=registry)
    server = DaemonIPCServer(socket_path=socket_path, daemon=daemon)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    base = [
        "--bundle",
        str(JUBILEE_BUNDLE_DIR),
        "--target",
        "in_memory",
        "--socket",
        str(socket_path),
        "--json",
    ]
    try:
        assert _run_cli([*base, "sys", "detect"])[0] == 0

        motion_result: list[tuple[int, dict[str, object]]] = []

        def _blocked_home() -> None:
            motion_result.append(_run_cli([*base, "--confirm", "motion", "home-all"]))

        worker = threading.Thread(target=_blocked_home, daemon=True)
        worker.start()
        time.sleep(0.05)

        # Second client issues out-of-band sys abort while first client is stuck
        code, payload = _run_cli([*base, "sys", "abort"])
        assert code == ExitCode.OK
        assert payload["ok"] is True

        worker.join(timeout=2.0)
        assert not worker.is_alive()
        assert motion_result and motion_result[0][0] == ExitCode.EXECUTION_ERROR
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=1.0)


def test_generic_toolchain_end_to_end_on_open_radio(tmp_path: Path) -> None:
    """Full generic pipeline: bundle draft -> bundle build -> daemon + dynamic CLI on OpenRadio."""
    bundle_dir = tmp_path / "open_radio"
    gen_dir = tmp_path / "generated" / "open_radio"
    socket_path = tmp_path / "radio.sock"

    runner = CliRunner()
    app = build_cli_app()

    # 1. Draft bundle from interface + manual + host API via CLI
    draft_res = runner.invoke(
        app,
        [
            "bundle",
            "draft",
            "--name",
            "open_radio",
            "--display-name",
            "OpenRadio-100",
            "--kind",
            "radio",
            "--interface",
            "usb_serial",
            "--endpoint",
            "serial:///dev/ttyUSB0",
            "--manual",
            str(RADIO_FIXTURE_DIR / "manual.md"),
            "--host-api",
            str(RADIO_FIXTURE_DIR / "radio_host.py"),
            "--output",
            str(bundle_dir),
        ],
        catch_exceptions=False,
    )
    assert draft_res.exit_code == 0

    # 2. Build artifacts + verify --check via CLI
    build_res = runner.invoke(
        app,
        ["bundle", "build", str(bundle_dir), "--output-dir", str(gen_dir)],
        catch_exceptions=False,
    )
    assert build_res.exit_code == 0
    check_res = runner.invoke(
        app,
        ["bundle", "build", str(bundle_dir), "--output-dir", str(gen_dir), "--check"],
        catch_exceptions=False,
    )
    assert check_res.exit_code == 0

    # 3. Boot generic daemon with drafted OpenRadio bundle & run dynamic CLI commands
    bundle = load_bundle(bundle_dir)
    registry = AdapterRegistry()

    def _radio_factory(cfg: AdapterConfig) -> AdapterPair:
        fake = InMemoryFakeAdapter(
            target=cfg.target,
            endpoint=cfg.endpoint or "serial:///dev/ttyUSB0",
            capabilities=bundle.device.targets["in_memory"].capabilities,
            initial_safe_z_mm=None,
        )
        return AdapterPair(target=cfg.target, supervisor=fake, driver=fake)

    registry.register("in_memory", _radio_factory)
    daemon = DeviceDaemon(bundle=bundle, registry=registry)
    server = DaemonIPCServer(socket_path=socket_path, daemon=daemon)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    base = [
        "--bundle",
        str(bundle_dir),
        "--target",
        "in_memory",
        "--socket",
        str(socket_path),
        "--json",
    ]
    try:
        assert _run_cli([*base, "sys", "detect"])[0] == ExitCode.OK
        code, payload = _run_cli([*base, "radio", "tune", "--frequency-mhz", "101.5"])
        assert code == ExitCode.OK
        assert payload["ok"] is True

        # Expert raw AT command rejected without --confirm
        code, _ = _run_cli([*base, "expert", "send-raw-at", "--command", "AT+VER"])
        assert code == ExitCode.CONFIRM_REQUIRED
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=1.0)
