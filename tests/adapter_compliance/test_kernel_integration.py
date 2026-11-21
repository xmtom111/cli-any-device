from pathlib import Path

from adapters.grbl_sim import create_adapter as grbl_factory
from adapters.mujoco_twin import create_adapter as twin_factory

from adapter_spi import AdapterConfig
from compiler.loader import load_bundle
from core_types import ErrorCode, ExecutionMode
from safety_kernel.engine import SafetyKernel

_BUNDLE = Path(__file__).resolve().parents[2] / "bundles" / "jubilee"


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
