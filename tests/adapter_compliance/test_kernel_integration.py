from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from adapters.grbl_sim import create_adapter as grbl_factory
from adapters.mujoco_twin import create_adapter as twin_factory

from adapter_spi import AdapterConfig
from compiler.ingestor import draft_bundle
from compiler.loader import load_bundle, validate_bundle
from core_types import ErrorCode, ExecutionMode
from daemon.registry import AdapterRegistry
from daemon.service import DeviceDaemon
from safety_kernel.engine import SafetyKernel

_BUNDLE = Path(__file__).resolve().parents[2] / "bundles" / "jubilee"


@pytest.mark.usefixtures("require_mujoco")
def test_twin_kernel_workflow_dry_run_and_rejections(monkeypatch):
    pair = twin_factory(AdapterConfig("mujoco_twin"))
    kernel = SafetyKernel(
        bundle=load_bundle(_BUNDLE),
        target_name=pair.target,
        supervisor=pair.supervisor,
        driver=pair.driver,
    )
    called = []
    execute = pair.driver.execute

    def record(request):
        called.append(request.service)
        return execute(request)

    monkeypatch.setattr(pair.driver, "execute", record)
    assert kernel.invoke("sys.detect").ok
    assert kernel.invoke("sys.preflight").ok
    before = pair.supervisor.poll_state()
    dry = kernel.invoke("motion.home_all", dry_run=True)
    assert dry.ok and dry.mode == ExecutionMode.DRY_RUN
    assert pair.supervisor.poll_state() == before
    rejected = kernel.invoke("motion.move_well", args={"slot": "1", "well": "A1", "dz": 5})
    assert rejected.error.code == ErrorCode.E_NOT_HOMED
    assert called == []

    assert kernel.invoke("motion.home_all", confirm=True).ok
    assert kernel.invoke("tool.pickup", args={"tool_id": 0}, confirm=True).ok
    rejected = kernel.invoke("tool.pickup", args={"tool_id": 0}, confirm=True)
    assert rejected.error.code == ErrorCode.E_TOOL_ALREADY_LOADED
    rejected = kernel.invoke("motion.move_safe", args={"x": 35, "y": 24, "z": 500})
    assert rejected.error.code == ErrorCode.E_OUT_OF_BOUNDS
    assert called == ["motion.home_all", "tool.pickup"]

    assert kernel.invoke("motion.move_well", args={"slot": "1", "well": "A1", "dz": 5}).ok
    assert pair.supervisor.poll_state().collision_count == 0
    assert kernel.invoke("tool.park", confirm=True).ok
    assert pair.supervisor.poll_state().active_tool is None


@pytest.mark.usefixtures("require_autodna")
def test_grbl_kernel_homing_and_capability_gate(monkeypatch):
    pair = grbl_factory(AdapterConfig("grbl_sim", options={"time_scale": 30}))
    try:
        kernel = SafetyKernel(
            bundle=load_bundle(_BUNDLE),
            target_name=pair.target,
            supervisor=pair.supervisor,
            driver=pair.driver,
        )
        assert kernel.invoke("sys.detect").ok
        assert kernel.invoke("sys.preflight").ok
        assert kernel.invoke("motion.home_all", confirm=True).ok
        assert kernel.invoke("motion.move_to", args={"x": 10, "y": 20, "z": 5}).ok

        def unexpected_action(*args):
            raise AssertionError("unsupported tool action reached GRBL driver")

        monkeypatch.setattr(pair.driver, "execute", unexpected_action)
        before = pair.supervisor.poll_state()
        failed = kernel.invoke("tool.pickup", args={"tool_id": 0}, confirm=True)
        assert failed.error.code == ErrorCode.E_UNSUPPORTED_CAPABILITY
        assert pair.supervisor.poll_state() == before
    finally:
        pair.close()


def test_root_install_discovers_builtin_adapters(monkeypatch, autodna_targets):
    monkeypatch.delenv("CLI_ANY_DEVICE_DUET_PASSWORD", raising=False)
    daemon = DeviceDaemon(bundle=load_bundle(_BUNDLE))
    try:
        for target in (*autodna_targets, "duet_real"):
            detected = daemon.dispatch(target=target, service="sys.detect")
            assert detected.ok, detected.error
            assert detected.state["detected"]
        assert detected.data["identity"]["details"]["offline"]
        assert detected.state["lifecycle"] == "DISCONNECTED"
        rejected = daemon.dispatch(target="duet_real", service="motion.home_all", confirm=True)
        assert rejected.error.code == ErrorCode.E_UNSUPPORTED_CAPABILITY
    finally:
        daemon.close()


@pytest.mark.usefixtures("require_autodna")
def test_engraver_draft_dispatches_through_grbl_emulator(tmp_path, monkeypatch):
    fixture = _BUNDLE.parents[1] / "tests" / "fixtures" / "grbl_engraver"
    draft_bundle(
        device_id="grbl_engraver",
        display_name="OpenCNC-3018",
        device_kind="engraver",
        interface_type="usb_serial",
        endpoint="serial:///dev/ttyUSB0",
        manual_paths=[fixture / "manual.md"],
        host_api_paths=[fixture / "engraver_host.py"],
        output_dir=tmp_path,
    )

    # Bind the reviewed XYZ subset to the emulator. Keep unsupported draft
    # commands visible so the ordinary capability gate can reject them.
    device_path = tmp_path / "device.yaml"
    device = yaml.safe_load(device_path.read_text())
    device["targets"] = {
        "grbl_sim": {
            "adapter_id": "grbl_sim",
            "capabilities": ["axes_xyz", "serial_gcode", "abort_oob"],
        },
    }
    device_path.write_text(yaml.safe_dump(device))
    catalog_path = tmp_path / "catalog.yaml"
    catalog = yaml.safe_load(catalog_path.read_text())
    home, move = "engraver.home_axes", "engraver.move_xyz"
    for service in catalog["services"]:
        if service["name"] in {home, move}:
            service["requires_capabilities"] = ["axes_xyz"]
    catalog_path.write_text(yaml.safe_dump(catalog))
    bundle = load_bundle(tmp_path)
    validate_bundle(bundle)

    pair = grbl_factory(AdapterConfig("grbl_sim", options={"time_scale": 30}))
    execute = pair.driver.execute

    def dispatch(request):
        args = request.args
        if request.service == home:
            native = replace(request, service="motion.home_all", args={})
        else:
            native = replace(
                request,
                service="motion.move_to",
                args={
                    "x": args["x_mm"],
                    "y": args["y_mm"],
                    "z": args["z_mm"],
                    "speed": args["feed_rate"],
                },
            )
        return replace(execute(native), service=request.service)

    monkeypatch.setattr(pair.driver, "execute", dispatch)
    registry = AdapterRegistry(load_entry_points=False)
    registry.register("grbl_sim", lambda _: pair)
    daemon = DeviceDaemon(bundle=bundle, registry=registry)

    def run(service, **kwargs):
        return daemon.dispatch(target="grbl_sim", service=service, **kwargs)

    try:
        assert run("sys.detect").ok
        assert run(home).error.code == ErrorCode.E_CONFIRM_REQUIRED
        assert run(home, confirm=True).ok
        result = run(move, args={"x_mm": 6, "y_mm": 7, "z_mm": 5})
        assert result.ok, result.error
        assert result.state["pos"] == {"X": 6.0, "Y": 7.0, "Z": 5.0}
        assert (
            run(
                "engraver.set_spindle",
                args={
                    "spindle_rpm": 1000,
                },
            ).error.code
            == ErrorCode.E_UNSUPPORTED_CAPABILITY
        )
        rejected = run(move, args={"x_mm": 450, "y_mm": 7, "z_mm": 5})
        assert rejected.error.code == ErrorCode.E_OUT_OF_BOUNDS
        failed = run(move, args={"x_mm": 8, "y_mm": 9, "z_mm": 1})
        assert failed.error.fail_safe_applied == "GRBL realtime reset sent"
        assert failed.state["estop_latched"]
    finally:
        daemon.close()
