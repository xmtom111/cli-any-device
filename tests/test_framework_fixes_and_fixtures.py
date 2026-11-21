"""Regression tests for framework fixes #1-#6 and new generic open-source hardware fixtures."""

from __future__ import annotations

import json
import threading
from importlib import metadata
from pathlib import Path

from click.testing import CliRunner

from adapter_spi import (
    ADAPTER_ENTRY_POINT_GROUP,
    AdapterConfig,
    AdapterPair,
    InMemoryFakeAdapter,
)
from cli.main import build_cli_app
from compiler import build_bundle, draft_bundle, load_bundle, validate_bundle
from core_types import ApiDialect, ErrorCode, ExecutionMode, ExitCode
from daemon import AdapterRegistry, DaemonIPCServer, DeviceDaemon
from safety_kernel import SafetyKernel

REPO_ROOT = Path(__file__).resolve().parent.parent
JUBILEE_BUNDLE_DIR = REPO_ROOT / "bundles" / "jubilee"
RADIO_FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "open_radio"
GRBL_ENGRAVER_FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "grbl_engraver"
SCPI_PSU_FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "scpi_psu"


def _run_cli_json(args: list[str]) -> tuple[int, dict[str, object]]:
    runner = CliRunner()
    res = runner.invoke(build_cli_app(), args, catch_exceptions=False)
    return res.exit_code, json.loads(res.output)


# --- Defect #1: --dry-run sys abort must NOT execute or latch e-stop ---


def test_dry_run_sys_abort_returns_plan_without_latching_estop() -> None:
    bundle = load_bundle(JUBILEE_BUNDLE_DIR)
    profile = bundle.device.targets["in_memory"]
    fake = InMemoryFakeAdapter(target="in_memory", capabilities=profile.capabilities)
    kernel = SafetyKernel(bundle=bundle, target_name="in_memory", supervisor=fake, driver=fake)

    assert kernel.invoke("sys.detect").ok is True
    assert kernel.invoke("motion.home_all", confirm=True).ok is True

    dry_abort = kernel.invoke("sys.abort", dry_run=True)
    assert dry_abort.ok is True
    assert dry_abort.exit_code == ExitCode.OK
    assert dry_abort.mode == ExecutionMode.DRY_RUN
    assert "plan" in dry_abort.data
    assert fake.abort_count == 0
    assert dry_abort.state["estop_latched"] is False

    status = kernel.invoke("sys.status")
    assert status.ok is True
    assert status.state["estop_latched"] is False
    assert set(status.state["homed"]) == {"X", "Y", "Z", "U"}

    # Live abort behavior remains unchanged: latches e-stop and clears homed
    live_abort = kernel.invoke("sys.abort", dry_run=False)
    assert live_abort.ok is True
    assert live_abort.mode == ExecutionMode.LIVE
    assert fake.abort_count == 1
    assert live_abort.state["estop_latched"] is True
    assert kernel.invoke("sys.status").state["homed"] == []


# --- Defect #2 & #5: Manual limits bound to runtime arg names + no RRF fail_safe leak ---


def test_open_radio_limits_bind_to_runtime_args_and_reject_out_of_bounds(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "open_radio"
    draft_bundle(
        device_id="open_radio",
        display_name="OpenRadio-100",
        device_kind="radio",
        interface_type="usb_serial",
        endpoint="serial:///dev/ttyUSB0",
        manual_paths=[RADIO_FIXTURE_DIR / "manual.md"],
        host_api_paths=[RADIO_FIXTURE_DIR / "radio_host.py"],
        output_dir=bundle_dir,
    )
    bundle = load_bundle(bundle_dir)
    validate_bundle(bundle)

    # Bound directly to runtime parameter names
    assert "frequency_mhz" in bundle.device.safety_limits
    assert bundle.device.safety_limits["frequency_mhz"].min_value == 87.5
    assert bundle.device.safety_limits["frequency_mhz"].max_value == 108.0
    assert "power_w" in bundle.device.safety_limits
    assert bundle.device.safety_limits["power_w"].max_value == 5.0
    assert "volume_db" in bundle.device.safety_limits
    assert bundle.device.safety_limits["volume_db"].min_value == -40.0

    # Non-RRF device must never leak abort_m112 into catalog.yaml
    for svc in bundle.catalog.services.values():
        assert svc.fail_safe != "abort_m112", f"{svc.name} leaked RRF fail_safe abort_m112"

    # Runtime enforcement: out-of-bounds frequency & TX power fail with exit 3 BEFORE driver call
    profile = bundle.device.targets["in_memory"]
    fake = InMemoryFakeAdapter(
        target="in_memory",
        api_mode=ApiDialect.SERIAL_TEXT,
        capabilities=profile.capabilities,
        initial_safe_z_mm=None,
    )
    kernel = SafetyKernel(bundle=bundle, target_name="in_memory", supervisor=fake, driver=fake)
    assert kernel.invoke("sys.detect").ok is True

    bad_freq = kernel.invoke("radio.tune", args={"frequency_mhz": 150.0})
    assert bad_freq.ok is False
    assert bad_freq.exit_code == ExitCode.SAFETY_DENIED
    assert bad_freq.error is not None and bad_freq.error.code == ErrorCode.E_OUT_OF_BOUNDS

    bad_tx = kernel.invoke("radio.enable_tx", args={"power_w": 12.0}, confirm=True)
    assert bad_tx.ok is False
    assert bad_tx.exit_code == ExitCode.SAFETY_DENIED
    assert bad_tx.error is not None and bad_tx.error.code == ErrorCode.E_OUT_OF_BOUNDS
    assert fake.executed_requests == []

    # Valid in-range calls succeed
    assert kernel.invoke("radio.tune", args={"frequency_mhz": 99.5}).ok is True
    assert kernel.invoke("radio.enable_tx", args={"power_w": 2.5}, confirm=True).ok is True


# --- Defect #4: Generated SKILL.md prerequisites & honest completion wording ---


def test_generated_skill_documents_prerequisites_and_distinguishes_in_memory(
    tmp_path: Path,
) -> None:
    out_dir = tmp_path / "jubilee_gen"
    build_bundle(JUBILEE_BUNDLE_DIR, out_dir, check=False)
    skill = (out_dir / "SKILL.md").read_text(encoding="utf-8")

    assert "--bundle jubilee" in skill
    assert "cli-device-d --bundle jubilee" in skill
    assert "cli_any_device.adapters" in skill
    assert "in_memory" in skill
    assert "offline in-memory fake" in skill

    # In-memory-only bundle must NOT claim physical completion
    bundle_dir = tmp_path / "fake_only"
    draft_bundle(
        device_id="fake_only",
        display_name="FakeOnly Bench",
        device_kind="instrument",
        interface_type="usb_serial",
        endpoint="serial:///dev/null",
        output_dir=bundle_dir,
    )
    dev_yaml = (bundle_dir / "device.yaml").read_text(encoding="utf-8")
    # Remove hardware_real target so only in_memory remains
    trimmed = dev_yaml.split("  hardware_real:")[0]
    (bundle_dir / "device.yaml").write_text(trimmed, encoding="utf-8")
    fake_out = tmp_path / "fake_only_gen"
    build_bundle(bundle_dir, fake_out, check=False)
    fake_skill = (fake_out / "SKILL.md").read_text(encoding="utf-8")
    assert "no physical hardware attached" in fake_skill
    assert "Commands block until physical completion" not in fake_skill


# --- Defect #6: Root packaging exposes adapter entry points in project .venv ---


def test_root_package_exposes_adapter_entry_points_and_daemon_resolves_targets() -> None:
    eps = {ep.name: ep.value for ep in metadata.entry_points(group=ADAPTER_ENTRY_POINT_GROUP)}
    assert {"duet_rrf", "mujoco_twin", "grbl_sim"} <= set(eps)

    registry = AdapterRegistry()
    assert {"duet_rrf", "grbl_sim", "in_memory", "mujoco_twin"} <= set(
        registry.available_adapters()
    )

    daemon = DeviceDaemon(bundle=load_bundle(JUBILEE_BUNDLE_DIR), registry=registry)
    try:
        for target in ("mujoco_twin", "grbl_sim", "duet_real"):
            env = daemon.dispatch(target=target, service="sys.status")
            assert env.error is None or env.error.code != ErrorCode.E_UNKNOWN_TARGET
            assert env.ok is True
    finally:
        daemon.close()


# --- New Fixture 1: 3-axis GRBL desktop engraver (USB serial + manual + host API) ---


def test_grbl_engraver_fixture_draft_build_and_cli_closed_loop(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "grbl_engraver"
    gen_dir = tmp_path / "gen_grbl_engraver"
    socket_path = tmp_path / "engraver.sock"

    runner = CliRunner()
    app = build_cli_app()
    res = runner.invoke(
        app,
        [
            "bundle",
            "draft",
            "--name",
            "grbl_engraver",
            "--display-name",
            "OpenCNC-3018 Engraver",
            "--kind",
            "engraver",
            "--interface",
            "usb_serial",
            "--endpoint",
            "serial:///dev/ttyUSB0",
            "--manual",
            str(GRBL_ENGRAVER_FIXTURE_DIR / "manual.md"),
            "--host-api",
            str(GRBL_ENGRAVER_FIXTURE_DIR / "engraver_host.py"),
            "--output",
            str(bundle_dir),
        ],
        catch_exceptions=False,
    )
    assert res.exit_code == 0
    assert (
        runner.invoke(
            app,
            ["bundle", "build", str(bundle_dir), "--output-dir", str(gen_dir)],
            catch_exceptions=False,
        ).exit_code
        == 0
    )
    assert (
        runner.invoke(
            app,
            ["bundle", "build", str(bundle_dir), "--output-dir", str(gen_dir), "--check"],
            catch_exceptions=False,
        ).exit_code
        == 0
    )
    val_res = runner.invoke(app, ["bundle", "validate", str(bundle_dir)], catch_exceptions=False)
    assert val_res.exit_code == 0

    bundle = load_bundle(bundle_dir)
    # Non-RRF engraver has no abort_m112 and binds X/Y/Z/spindle/feed limits
    for svc in bundle.catalog.services.values():
        assert svc.fail_safe != "abort_m112"
    assert {"x_mm", "y_mm", "z_mm", "spindle_rpm", "feed_rate"} <= set(bundle.device.safety_limits)

    registry = AdapterRegistry(load_entry_points=False)

    def _engraver_factory(cfg: AdapterConfig) -> AdapterPair:
        fake = InMemoryFakeAdapter(
            target=cfg.target,
            endpoint=cfg.endpoint or "serial:///dev/ttyUSB0",
            api_mode=ApiDialect.SERIAL_TEXT,
            capabilities=bundle.device.targets["in_memory"].capabilities,
            initial_safe_z_mm=None,
        )
        return AdapterPair(target=cfg.target, supervisor=fake, driver=fake)

    registry.register("in_memory", _engraver_factory)
    daemon = DeviceDaemon(bundle=bundle, registry=registry)
    server = DaemonIPCServer(socket_path=socket_path, daemon=daemon)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

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
        assert _run_cli_json([*base, "sys", "detect"])[0] == ExitCode.OK
        assert _run_cli_json([*base, "sys", "preflight"])[0] == ExitCode.OK

        # Home requires --confirm
        assert _run_cli_json([*base, "engraver", "home-axes"])[0] == ExitCode.CONFIRM_REQUIRED
        assert _run_cli_json([*base, "--confirm", "engraver", "home-axes"])[0] == ExitCode.OK

        # Valid keyword-only XYZ move & spindle speed
        code, payload = _run_cli_json(
            [
                *base,
                "engraver",
                "move-xyz",
                "--x-mm",
                "120",
                "--y-mm",
                "80",
                "--z-mm",
                "10",
                "--feed-rate",
                "800",
            ]
        )
        assert code == ExitCode.OK and payload["ok"] is True

        # Out-of-bounds X & spindle RPM rejected with exit 3
        code_x, err_x = _run_cli_json(
            [*base, "engraver", "move-xyz", "--x-mm", "450", "--y-mm", "50", "--z-mm", "10"]
        )
        assert code_x == ExitCode.SAFETY_DENIED
        assert isinstance(err_x["error"], dict) and err_x["error"]["code"] == "E_OUT_OF_BOUNDS"

        code_rpm, _ = _run_cli_json([*base, "engraver", "set-spindle", "--spindle-rpm", "25000"])
        assert code_rpm == ExitCode.SAFETY_DENIED

        # Dry-run abort does NOT latch e-stop
        dry_code, dry_payload = _run_cli_json([*base, "--dry-run", "sys", "abort"])
        assert dry_code == ExitCode.OK and dry_payload["mode"] == "dry_run"
        _, st = _run_cli_json([*base, "sys", "status"])
        assert isinstance(st["state"], dict) and st["state"]["estop_latched"] is False
    finally:
        server.shutdown()
        server.server_close()
        t.join(timeout=2.0)


# --- New Fixture 2: SCPI PSU & Temp Probe (kwonly args, limit binding, non-motion) ---


def test_scpi_psu_fixture_kwonly_args_limits_and_cli_closed_loop(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "scpi_psu"
    gen_dir = tmp_path / "gen_scpi_psu"
    socket_path = tmp_path / "psu.sock"

    runner = CliRunner()
    app = build_cli_app()
    assert (
        runner.invoke(
            app,
            [
                "bundle",
                "draft",
                "--name",
                "scpi_psu",
                "--display-name",
                "OpenPSU-305 Benchtop Supply",
                "--kind",
                "instrument",
                "--interface",
                "tcp_scpi",
                "--endpoint",
                "tcp://192.168.10.50:5025",
                "--manual",
                str(SCPI_PSU_FIXTURE_DIR / "manual.md"),
                "--host-api",
                str(SCPI_PSU_FIXTURE_DIR / "psu_host.py"),
                "--output",
                str(bundle_dir),
            ],
            catch_exceptions=False,
        ).exit_code
        == 0
    )
    assert (
        runner.invoke(
            app,
            ["bundle", "build", str(bundle_dir), "--output-dir", str(gen_dir)],
            catch_exceptions=False,
        ).exit_code
        == 0
    )
    assert (
        runner.invoke(
            app,
            ["bundle", "build", str(bundle_dir), "--output-dir", str(gen_dir), "--check"],
            catch_exceptions=False,
        ).exit_code
        == 0
    )

    bundle = load_bundle(bundle_dir)
    validate_bundle(bundle)

    # Defect #3 regression check: kwonly `def read_probe(*, channel: int)` MUST have arg `channel`
    read_probe = bundle.catalog.services["instrument.read_probe"]
    assert [a.name for a in read_probe.args] == ["channel"]
    assert read_probe.args[0].arg_type == "int"
    assert read_probe.args[0].required is True

    # Defect #2 & #5: limits bound to voltage_v, current_a, channel; zero abort_m112
    assert {"voltage_v", "current_a", "channel"} <= set(bundle.device.safety_limits)
    for svc in bundle.catalog.services.values():
        assert svc.fail_safe != "abort_m112"

    # SKILL.md omits expert SCPI command & includes prerequisites
    skill = (gen_dir / "SKILL.md").read_text(encoding="utf-8")
    assert "instrument.read_probe(channel)" in skill
    assert "send_raw_scpi" not in skill
    assert "--bundle scpi_psu" in skill
    assert "cli-device-d --bundle scpi_psu" in skill

    registry = AdapterRegistry(load_entry_points=False)

    def _psu_factory(cfg: AdapterConfig) -> AdapterPair:
        fake = InMemoryFakeAdapter(
            target=cfg.target,
            endpoint=cfg.endpoint or "tcp://192.168.10.50:5025",
            api_mode=ApiDialect.TCP_SCPI,
            capabilities=bundle.device.targets["in_memory"].capabilities,
            initial_safe_z_mm=None,
        )
        return AdapterPair(target=cfg.target, supervisor=fake, driver=fake)

    registry.register("in_memory", _psu_factory)
    daemon = DeviceDaemon(bundle=bundle, registry=registry)
    server = DaemonIPCServer(socket_path=socket_path, daemon=daemon)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

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
        assert _run_cli_json([*base, "sys", "detect"])[0] == ExitCode.OK

        # Keyword-only --channel works on CLI and enforces 1..4 limit
        code, payload = _run_cli_json([*base, "instrument", "read-probe", "--channel", "2"])
        assert code == ExitCode.OK and payload["ok"] is True

        code_bad_ch, err_ch = _run_cli_json([*base, "instrument", "read-probe", "--channel", "9"])
        assert code_bad_ch == ExitCode.SAFETY_DENIED
        assert isinstance(err_ch["error"], dict) and err_ch["error"]["code"] == "E_OUT_OF_BOUNDS"

        # Bound voltage_v (0..30 V) and current_a (0..5 A) enforced
        assert (
            _run_cli_json(
                [*base, "instrument", "set-voltage", "--channel", "1", "--voltage-v", "12.5"]
            )[0]
            == ExitCode.OK
        )
        assert (
            _run_cli_json(
                [*base, "instrument", "set-voltage", "--channel", "1", "--voltage-v", "42.0"]
            )[0]
            == ExitCode.SAFETY_DENIED
        )
        assert (
            _run_cli_json(
                [*base, "instrument", "set-current-limit", "--channel", "1", "--current-a", "8.0"]
            )[0]
            == ExitCode.SAFETY_DENIED
        )

        # enable-output & expert send-raw-scpi require --confirm
        assert (
            _run_cli_json(
                [
                    *base,
                    "instrument",
                    "enable-output",
                    "--channel",
                    "1",
                    "--voltage-v",
                    "5.0",
                    "--current-a",
                    "1.0",
                ]
            )[0]
            == ExitCode.CONFIRM_REQUIRED
        )
        assert (
            _run_cli_json(
                [
                    *base,
                    "--confirm",
                    "instrument",
                    "enable-output",
                    "--channel",
                    "1",
                    "--voltage-v",
                    "5.0",
                    "--current-a",
                    "1.0",
                ]
            )[0]
            == ExitCode.OK
        )
        assert (
            _run_cli_json([*base, "expert", "send-raw-scpi", "--command", "*IDN?"])[0]
            == ExitCode.CONFIRM_REQUIRED
        )
    finally:
        server.shutdown()
        server.server_close()
        t.join(timeout=2.0)
