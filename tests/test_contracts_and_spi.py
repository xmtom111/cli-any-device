"""Contract tests for core_types and adapter_spi (including <100ms deadlock abort SLA)."""

from __future__ import annotations

import threading
import time

import pytest

from adapter_spi import (
    ABORT_LATENCY_SLA_MS,
    GRBL_SUBSET_CAPABILITIES,
    ActionDriver,
    ActionRequest,
    InMemoryFakeAdapter,
    SupervisorChannel,
)
from core_types import (
    ApiDialect,
    CanonicalMachineState,
    ErrorCode,
    ExitCode,
    HardwareFaultError,
    MachineLifecycle,
    RiskClass,
    UnsupportedCapabilityError,
)


def test_in_memory_adapter_satisfies_spi_protocols() -> None:
    adapter = InMemoryFakeAdapter()
    assert isinstance(adapter, SupervisorChannel)
    assert isinstance(adapter, ActionDriver)


def test_abort_now_completes_under_100ms_during_action_driver_deadlock() -> None:
    """Compliance invariant: SupervisorChannel.abort_now() is lock-free and <100ms."""
    deadlock_gate = threading.Event()
    adapter = InMemoryFakeAdapter(deadlock_event=deadlock_gate)
    adapter.detect()

    action_error: list[BaseException] = []
    started = threading.Event()

    def _blocked_driver() -> None:
        started.set()
        try:
            adapter.execute(ActionRequest(service="motion.home_all", args={}, timeout_s=10.0))
        except BaseException as exc:
            action_error.append(exc)

    worker = threading.Thread(target=_blocked_driver, daemon=True)
    worker.start()
    assert started.wait(timeout=1.0)
    time.sleep(0.02)  # Ensure worker holds _action_lock and blocks on deadlock_gate

    t0 = time.perf_counter()
    abort_result = adapter.abort_now()
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    assert abort_result.sent is True
    assert elapsed_ms < ABORT_LATENCY_SLA_MS
    assert abort_result.latency_ms < ABORT_LATENCY_SLA_MS

    worker.join(timeout=1.0)
    assert not worker.is_alive()
    assert action_error and isinstance(action_error[0], HardwareFaultError)

    state = adapter.poll_state()
    assert state.estop_latched is True
    assert state.lifecycle == MachineLifecycle.HALTED
    assert state.homed == frozenset()


def test_grbl_subset_rejects_tool_pickup_honestly() -> None:
    """GRBL subset must never fabricate tool state; must raise UnsupportedCapabilityError."""
    grbl_fake = InMemoryFakeAdapter(
        target="grbl_sim",
        api_mode=ApiDialect.GRBL_SERIAL_TCP,
        capabilities=GRBL_SUBSET_CAPABILITIES,
    )
    grbl_fake.detect()
    with pytest.raises(UnsupportedCapabilityError) as exc_info:
        grbl_fake.execute(ActionRequest(service="tool.pickup", args={"tool_id": 0}, timeout_s=5.0))
    assert exc_info.value.code == ErrorCode.E_UNSUPPORTED_CAPABILITY
    assert exc_info.value.exit_code == ExitCode.INVALID_USAGE


def test_generic_non_motion_device_state_uses_properties() -> None:
    """Verify core_types and fake adapter cleanly represent a non-motion open-source radio."""
    radio = InMemoryFakeAdapter(
        target="open_radio_sim",
        endpoint="serial:///dev/ttyUSB0",
        api_mode=ApiDialect.SERIAL_TEXT,
        capabilities=frozenset({"rf_rx", "rf_tx", "audio_out", "abort_oob"}),
        initial_properties={"frequency_mhz": 98.5, "tx_enabled": False},
    )
    radio.detect()
    radio.execute(ActionRequest(service="radio.tune", args={"frequency_mhz": 101.1}, timeout_s=5.0))
    state: CanonicalMachineState = radio.await_settled(timeout_s=2.0)
    assert state.lifecycle == MachineLifecycle.IDLE
    assert state.properties["frequency_mhz"] == 101.1
    assert state.safe_z_mm is None
    assert state.position.to_dict() == {}
    assert RiskClass.R0_READ.value == "R0_READ"
