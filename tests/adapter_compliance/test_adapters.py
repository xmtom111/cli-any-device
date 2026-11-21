import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from adapters.duet_rrf import create_adapter as duet_factory
from adapters.grbl_sim import create_adapter as grbl_factory
from adapters.mujoco_twin import create_adapter as twin_factory

from adapter_spi import (
    ABORT_LATENCY_SLA_MS,
    ActionDriver,
    ActionRequest,
    ActionTelemetry,
    AdapterConfig,
    SupervisorChannel,
)
from core_types import (
    AdapterTimeoutError,
    ApiDialect,
    ErrorCode,
    HardwareFaultError,
    MachineLifecycle,
    ToolPresence,
    UnsupportedCapabilityError,
)


def dispatch(driver, service, **args):
    telemetry = driver.execute(ActionRequest(service, args, timeout_s=2))
    assert isinstance(telemetry, ActionTelemetry)
    assert telemetry.service == service
    assert telemetry.accepted
    return telemetry


@pytest.fixture
def grbl():
    pair = grbl_factory(AdapterConfig("grbl_sim", options={"time_scale": 30}))
    try:
        yield pair
    finally:
        pair.close()


@pytest.mark.parametrize(
    ("target", "factory"),
    [
        ("duet_real", duet_factory),
        ("mujoco_twin", twin_factory),
        ("grbl_sim", grbl_factory),
    ],
)
def test_shared_spi(target, factory):
    pair = factory(AdapterConfig(target))
    try:
        assert pair.target == target
        assert isinstance(pair.driver, ActionDriver)
        assert isinstance(pair.supervisor, SupervisorChannel)
        identity = pair.supervisor.detect()
        assert identity.target == target
        assert identity.capabilities == pair.driver.capabilities()
        assert isinstance(pair.supervisor.preflight().passed, bool)
    finally:
        if pair.close is not None:
            pair.close()


def test_grbl_is_not_jubilee(grbl, monkeypatch):
    supervisor, driver = grbl.supervisor, grbl.driver
    identity = supervisor.detect()
    assert identity.api_mode == ApiDialect.GRBL_SERIAL_TCP
    assert "emulator" in identity.firmware
    assert identity.is_real_hardware is False
    assert identity.capabilities == {"axes_xyz", "serial_gcode", "abort_oob"}
    assert supervisor.preflight().passed
    dispatch(driver, "motion.home_all")
    assert supervisor.await_settled(2).homed == set("XYZ")
    dispatch(driver, "motion.move_to", x=3, y=7, z=5)
    assert supervisor.await_settled(2).position.to_dict() == {"X": 3, "Y": 7, "Z": 5}

    def unexpected_frame(*args):
        pytest.fail("GRBL received a frame for an unsupported action")

    monkeypatch.setattr(driver._controller, "submit", unexpected_frame)
    before = supervisor.poll_state()
    for verb, args in (
        ("tool.pickup", {"tool_id": 0}),
        ("tool.park", {}),
        ("expert.tool_lock", {}),
        ("expert.tool_unlock", {}),
        ("expert.gcode", {"command": "T0"}),
        ("T-1", {}),
    ):
        with pytest.raises(UnsupportedCapabilityError) as exc:
            dispatch(driver, verb, **args)
        assert exc.value.code == ErrorCode.E_UNSUPPORTED_CAPABILITY
    assert supervisor.poll_state() == before
    assert before.active_tool is None
    assert before.tool_presence == ToolPresence.NONE
    assert before.tool_locked is None


def test_twin_workflow():
    twin = twin_factory(AdapterConfig("mujoco_twin"))
    driver, supervisor = twin.driver, twin.supervisor
    assert supervisor.detect().details["tool_observation"] == "logical_simulation"
    before = supervisor.poll_state()
    assert supervisor.preflight().checks["model_loaded"]
    assert supervisor.poll_state() == before
    with pytest.raises(HardwareFaultError, match="home"):
        dispatch(driver, "motion.move_well", slot="1", well="A1")
    dispatch(driver, "deck.load_deck", spec="autodna_deck")
    assert not supervisor.poll_state().deck.labware
    dispatch(driver, "deck.load_labware", slot="1", spec="autodna_rack")
    assert len(supervisor.poll_state().deck.labware["1"].well_map) == 96
    dispatch(driver, "motion.home_all")
    assert supervisor.await_settled(1).homed == set("XYZU")
    dispatch(driver, "tool.pickup", tool_id=0)
    picked = supervisor.await_settled(1)
    assert picked.tool_presence == ToolPresence.LOADED
    assert picked.active_tool == 0
    assert picked.tool_locked is True
    with pytest.raises(HardwareFaultError, match="park"):
        dispatch(driver, "tool.pickup", tool_id=0)
    with pytest.raises(ValueError, match="bounds"):
        dispatch(driver, "motion.move_well", slot="1", well="A1", dz=-500)
    dispatch(driver, "motion.move_well", slot="1", well="A1", dz=5)
    settled = supervisor.await_settled(1)
    assert settled.collision_count == 0
    assert settled.position.to_dict() == {"X": 35, "Y": 24, "Z": 7}
    assert supervisor.joints_mm() == pytest.approx((35, 24, 7))
    dispatch(driver, "tool.park")
    parked = supervisor.await_settled(1)
    assert parked.active_tool is None
    assert parked.tool_presence == ToolPresence.NONE
    assert parked.tool_locked is False


def test_grbl_abort_while_home_action_waits():
    pair = grbl_factory(AdapterConfig("grbl_sim", options={"time_scale": 0.01}))
    driver, supervisor = pair.driver, pair.supervisor
    try:
        with ThreadPoolExecutor() as worker:
            action = worker.submit(dispatch, driver, "motion.home_all")
            deadline = time.monotonic() + 1
            try:
                while supervisor.poll_state().lifecycle != MachineLifecycle.BUSY:
                    assert time.monotonic() < deadline
                    time.sleep(0.001)
                result = supervisor.abort_now()
                assert result.sent and result.latency_ms < ABORT_LATENCY_SLA_MS
            finally:
                supervisor.abort_now()
            with pytest.raises(HardwareFaultError):
                action.result(timeout=1)
        assert supervisor.poll_state().estop_latched
        assert not supervisor.poll_state().homed
        reset = supervisor.reset_estop()
        assert not reset.estop_latched and not reset.homed
        with pytest.raises(HardwareFaultError, match="home"):
            dispatch(driver, "motion.move_to", x=3, y=7, z=5)
    finally:
        pair.close()


def test_grbl_ack_is_not_completion_and_abort_stops_diagonal(grbl):
    driver, supervisor = grbl.driver, grbl.supervisor
    dispatch(driver, "motion.home_all")
    telemetry = dispatch(driver, "motion.move_to", x=90, y=90, z=5, speed=20)
    assert telemetry.raw["replies"] == ["ok"]
    with pytest.raises(AdapterTimeoutError):
        supervisor.await_settled(0.02)
    assert supervisor.poll_state().lifecycle == MachineLifecycle.MOVING
    assert supervisor.abort_now().sent
    stopped = supervisor.poll_state()
    assert stopped.lifecycle == MachineLifecycle.HALTED
    assert stopped.position.x < 90
    assert stopped.position.x == stopped.position.y
    with pytest.raises(HardwareFaultError):
        supervisor.await_settled(1)
    assert supervisor.poll_state().position == stopped.position


def test_twin_abort_while_geometry_is_blocked():
    twin = twin_factory(AdapterConfig("mujoco_twin"))
    driver, supervisor = twin.driver, twin.supervisor
    dispatch(driver, "motion.home_all")
    entered = threading.Event()
    release = threading.Event()
    pose = driver._model_pose

    def blocked_pose(*args):
        entered.set()
        assert release.wait(2)
        pose(*args)

    driver._model_pose = blocked_pose
    with ThreadPoolExecutor() as worker:
        action = worker.submit(dispatch, driver, "motion.move_to", x=60, y=40, z=5)
        try:
            assert entered.wait(1)
            result = supervisor.abort_now()
            assert result.sent and result.latency_ms < ABORT_LATENCY_SLA_MS
            assert not action.done()
        finally:
            release.set()
        with pytest.raises(HardwareFaultError, match="halted"):
            action.result(timeout=1)
    assert supervisor.poll_state().lifecycle == MachineLifecycle.HALTED
    assert supervisor.poll_state().position.to_dict() == {"X": 0, "Y": 0, "Z": 5}
    assert supervisor.joints_mm() == pytest.approx((0, 0, 5))
    assert not supervisor.reset_estop().homed
