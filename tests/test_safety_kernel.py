"""Unit tests for the device-agnostic safety kernel (R0-R4, default deny, completion, fail-safe)."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from adapter_spi import (
    ABORT_LATENCY_SLA_MS,
    GRBL_SUBSET_CAPABILITIES,
    InMemoryFakeAdapter,
)
from compiler.ingestor import draft_bundle
from compiler.loader import load_bundle
from core_types import (
    ApiDialect,
    ErrorCode,
    ExecutionMode,
    ExitCode,
    ToolPresence,
)
from safety_kernel.engine import SafetyKernel

REPO_ROOT = Path(__file__).resolve().parent.parent
JUBILEE_BUNDLE_DIR = REPO_ROOT / "bundles" / "jubilee"
RADIO_FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "open_radio"


def _make_jubilee_kernel(
    *,
    target: str = "in_memory",
    adapter: InMemoryFakeAdapter | None = None,
) -> tuple[SafetyKernel, InMemoryFakeAdapter]:
    bundle = load_bundle(JUBILEE_BUNDLE_DIR)
    profile = bundle.device.targets[target]
    fake = adapter or InMemoryFakeAdapter(
        target=target,
        capabilities=profile.capabilities,
        is_real_hardware=profile.is_real_hardware,
    )
    kernel = SafetyKernel(bundle=bundle, target_name=target, supervisor=fake, driver=fake)
    return kernel, fake


def test_full_jubilee_v1_workflow_and_dry_run_zero_side_effects() -> None:
    kernel, fake = _make_jubilee_kernel()

    # 1. detect -> preflight
    env = kernel.invoke("sys.detect")
    assert env.ok is True
    assert env.exit_code == ExitCode.OK

    env = kernel.invoke("sys.preflight")
    assert env.ok is True
    assert env.data["readiness_report"]["passed"] is True

    # 2. dry-run home_all: exit 0, ZERO driver execution, ZERO state change
    env = kernel.invoke("motion.home_all", dry_run=True)
    assert env.ok is True
    assert env.exit_code == ExitCode.OK
    assert env.mode == ExecutionMode.DRY_RUN
    assert len(fake.executed_requests) == 0
    assert fake.poll_state().homed == frozenset()

    # 3. home_all without --confirm -> exit code 4 (CONFIRM_REQUIRED)
    env = kernel.invoke("motion.home_all", confirm=False)
    assert env.ok is False
    assert env.exit_code == ExitCode.CONFIRM_REQUIRED
    assert env.error is not None
    assert env.error.code == ErrorCode.E_CONFIRM_REQUIRED
    assert len(fake.executed_requests) == 0

    # 4. home_all with --confirm -> succeeds
    env = kernel.invoke("motion.home_all", confirm=True)
    assert env.ok is True
    assert env.exit_code == ExitCode.OK
    assert set(env.state["homed"]) == {"X", "Y", "Z", "U"}

    # 5. load deck + labware
    assert kernel.invoke("deck.load_deck", args={"spec": "jubilee_deck.json"}).ok is True
    env_lw = kernel.invoke(
        "deck.load_labware",
        args={"slot": "1", "spec": "corning_96_wellplate.json", "top_z_mm": 20.0},
    )
    assert env_lw.ok is True

    # 6. pickup tool 0 (requires --confirm)
    assert kernel.invoke("tool.pickup", args={"tool_id": 0}).exit_code == ExitCode.CONFIRM_REQUIRED
    env_pick = kernel.invoke("tool.pickup", args={"tool_id": 0}, confirm=True)
    assert env_pick.ok is True
    assert env_pick.state["active_tool"] == 0

    # 7. move-well A1 with dz=5
    env_well = kernel.invoke("motion.move_well", args={"slot": "1", "well": "A1", "dz": 5.0})
    assert env_well.ok is True
    assert env_well.state["pos"]["Z"] == 25.0

    # 8. park tool (requires --confirm)
    env_park = kernel.invoke("tool.park", confirm=True)
    assert env_park.ok is True
    assert env_park.state["active_tool"] is None


def test_safety_denial_cases_emit_zero_motion() -> None:
    kernel, fake = _make_jubilee_kernel()

    # Un-detected motion denied
    env = kernel.invoke("motion.move_safe", args={"x": 50.0, "y": 50.0})
    assert env.exit_code == ExitCode.SAFETY_DENIED
    assert env.error is not None and env.error.code == ErrorCode.E_NOT_DETECTED
    assert len(fake.executed_requests) == 0

    kernel.invoke("sys.detect")
    kernel.invoke("deck.load_deck", args={"spec": "deck.json"})
    kernel.invoke("deck.load_labware", args={"slot": "1", "spec": "plate.json", "top_z_mm": 25.0})

    # Unhomed move-well denied BEFORE driver is called
    env = kernel.invoke("motion.move_well", args={"slot": "1", "well": "A1", "dz": 5.0})
    assert env.exit_code == ExitCode.SAFETY_DENIED
    assert env.error is not None and env.error.code == ErrorCode.E_NOT_HOMED
    assert len(fake.executed_requests) == 2  # only deck + labware

    # Home all
    assert kernel.invoke("motion.home_all", confirm=True).ok is True

    # Out-of-bounds Z denied (exit code 3)
    env = kernel.invoke("motion.move_safe", args={"x": 50.0, "y": 50.0, "z": 500.0})
    assert env.exit_code == ExitCode.SAFETY_DENIED
    assert env.error is not None and env.error.code == ErrorCode.E_OUT_OF_BOUNDS

    # Forbidden zone (rear tool strip low Z) denied
    env = kernel.invoke("motion.move_safe", args={"x": 50.0, "y": 290.0, "z": 20.0})
    assert env.exit_code == ExitCode.SAFETY_DENIED
    assert env.error is not None and env.error.code == ErrorCode.E_OUT_OF_BOUNDS

    # Pickup tool 0, then attempt second pickup while tool already loaded -> denied!
    assert kernel.invoke("tool.pickup", args={"tool_id": 0}, confirm=True).ok is True
    env = kernel.invoke("tool.pickup", args={"tool_id": 1}, confirm=True)
    assert env.exit_code == ExitCode.SAFETY_DENIED
    assert env.error is not None and env.error.code == ErrorCode.E_TOOL_ALREADY_LOADED

    # Lowering safe_z below tallest labware (25) + tool length (40) + margin (10) = 75 -> denied!
    env = kernel.invoke("deck.safe_z", args={"z_mm": 50.0})
    assert env.exit_code == ExitCode.SAFETY_DENIED
    assert env.error is not None and env.error.code == ErrorCode.E_SAFE_Z_TOO_LOW

    # Crossing XY below safe Z with move_to -> denied!
    assert kernel.invoke("motion.move_safe", args={"x": 50.0, "y": 50.0, "z": 30.0}).ok is True
    env = kernel.invoke("motion.move_to", args={"x": 100.0, "y": 100.0, "z": 30.0})
    assert env.exit_code == ExitCode.SAFETY_DENIED
    assert env.error is not None and env.error.code == ErrorCode.E_UNSAFE_Z

    # Overwriting loaded deck requires --confirm (Section 6.4)
    env = kernel.invoke("deck.load_deck", args={"spec": "other_deck.json"}, confirm=False)
    assert env.exit_code == ExitCode.CONFIRM_REQUIRED


def test_real_hardware_target_requires_confirm_for_r2_motion() -> None:
    """Policy driven strictly by TargetProfile.require_confirm_for_risks (no if target ==)."""
    fake = InMemoryFakeAdapter(
        target="duet_real",
        capabilities=load_bundle(JUBILEE_BUNDLE_DIR).device.targets["duet_real"].capabilities,
        is_real_hardware=True,
    )
    kernel, _ = _make_jubilee_kernel(target="duet_real", adapter=fake)
    kernel.invoke("sys.detect")
    assert kernel.invoke("motion.home_all", confirm=True).ok is True

    # On duet_real, R2_MOTION requires --confirm
    env = kernel.invoke("motion.move_safe", args={"x": 100.0, "y": 100.0}, confirm=False)
    assert env.exit_code == ExitCode.CONFIRM_REQUIRED
    assert kernel.invoke("motion.move_safe", args={"x": 100.0, "y": 100.0}, confirm=True).ok is True


def test_grbl_sim_capability_honesty() -> None:
    fake = InMemoryFakeAdapter(
        target="grbl_sim",
        api_mode=ApiDialect.GRBL_SERIAL_TCP,
        capabilities=GRBL_SUBSET_CAPABILITIES,
    )
    kernel, _ = _make_jubilee_kernel(target="grbl_sim", adapter=fake)
    assert kernel.invoke("sys.detect").ok is True
    assert kernel.invoke("motion.home_all", confirm=True).ok is True
    assert kernel.invoke("motion.move_safe", args={"x": 50.0, "y": 50.0, "z": 120.0}).ok is True

    # tool.pickup immediately fails with E_UNSUPPORTED_CAPABILITY (exit code 2) before driver call
    calls_before = len(fake.executed_requests)
    env = kernel.invoke("tool.pickup", args={"tool_id": 0}, confirm=True)
    assert env.ok is False
    assert env.exit_code == ExitCode.INVALID_USAGE
    assert env.error is not None and env.error.code == ErrorCode.E_UNSUPPORTED_CAPABILITY
    assert len(fake.executed_requests) == calls_before


def test_unknown_tool_presence_blocks_homing() -> None:
    fake = InMemoryFakeAdapter(
        initial_tool_presence=ToolPresence.UNKNOWN,
        verify_tool_on_detect=False,
    )
    kernel, _ = _make_jubilee_kernel(adapter=fake)
    kernel.invoke("sys.detect")
    env = kernel.invoke("motion.home_all", confirm=True)
    assert env.exit_code == ExitCode.SAFETY_DENIED
    assert env.error is not None and env.error.code == ErrorCode.E_UNKNOWN_STATE


def test_postcondition_failure_triggers_fail_safe() -> None:
    fake = InMemoryFakeAdapter(fail_postcondition_on_service="motion.home_all")
    kernel, _ = _make_jubilee_kernel(adapter=fake)
    kernel.invoke("sys.detect")
    env = kernel.invoke("motion.home_all", confirm=True)
    assert env.ok is False
    assert env.exit_code == ExitCode.EXECUTION_ERROR
    assert env.error is not None
    assert env.error.code == ErrorCode.E_POSTCONDITION_FAILED
    assert env.error.fail_safe_applied is not None
    assert fake.fail_safe_calls
    assert fake.poll_state().estop_latched is True


def test_out_of_band_abort_latches_estop_and_reset_invalidates_homed() -> None:
    deadlock = threading.Event()
    fake = InMemoryFakeAdapter(deadlock_event=deadlock)
    kernel, _ = _make_jubilee_kernel(adapter=fake)
    kernel.invoke("sys.detect")

    result_box: list[object] = []

    def _run_slow() -> None:
        result_box.append(kernel.invoke("motion.home_all", confirm=True))

    worker = threading.Thread(target=_run_slow, daemon=True)
    worker.start()
    time.sleep(0.03)

    t0 = time.perf_counter()
    abort_env = kernel.invoke("sys.abort")
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    assert abort_env.ok is True
    assert elapsed_ms < ABORT_LATENCY_SLA_MS

    worker.join(timeout=1.0)
    assert not worker.is_alive()

    # Subsequent motion rejected with exit code 99 (ESTOP_LATCHED)
    blocked = kernel.invoke("motion.move_safe", args={"x": 10.0, "y": 10.0})
    assert blocked.exit_code == ExitCode.ESTOP_LATCHED
    assert blocked.error is not None and blocked.error.code == ErrorCode.E_ESTOP_LATCHED

    # Explicit confirmed reset clears latch but requires re-homing
    assert kernel.invoke("sys.reset_estop", confirm=False).exit_code == ExitCode.CONFIRM_REQUIRED
    reset_env = kernel.invoke("sys.reset_estop", confirm=True)
    assert reset_env.ok is True
    assert reset_env.state["estop_latched"] is False
    assert reset_env.state["homed"] == []


def test_safety_kernel_enforces_rules_on_drafted_open_radio(tmp_path: Path) -> None:
    """Same safety_kernel governs a drafted non-motion open-source radio without modification."""
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
    profile = bundle.device.targets["in_memory"]
    fake = InMemoryFakeAdapter(
        target="in_memory",
        endpoint="memory://open_radio",
        api_mode=ApiDialect.SERIAL_TEXT,
        capabilities=profile.capabilities,
        initial_safe_z_mm=None,
    )
    kernel = SafetyKernel(bundle=bundle, target_name="in_memory", supervisor=fake, driver=fake)

    assert kernel.invoke("sys.detect").ok is True
    assert kernel.invoke("radio.tune", args={"frequency_mhz": 98.5}).ok is True
    assert fake.poll_state().properties["frequency_mhz"] == 98.5

    # Expert raw AT command requires --confirm
    assert (
        kernel.invoke("expert.send_raw_at", args={"command": "AT+INFO"}).exit_code
        == ExitCode.CONFIRM_REQUIRED
    )
    assert (
        kernel.invoke("expert.send_raw_at", args={"command": "AT+INFO"}, confirm=True).ok is True
    )
