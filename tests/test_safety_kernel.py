"""Unit tests for the device-agnostic safety kernel (R0-R4, default deny, completion, fail-safe)."""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from adapter_spi import (
    ABORT_LATENCY_SLA_MS,
    GRBL_SUBSET_CAPABILITIES,
    AdapterConfig,
    AdapterPair,
    InMemoryFakeAdapter,
)
from compiler.ingestor import draft_bundle
from compiler.loader import load_bundle
from core_types import (
    ApiDialect,
    CanonicalMachineState,
    ErrorCode,
    ExecutionMode,
    ExitCode,
    MachineLifecycle,
    ToolPresence,
)
from daemon.registry import AdapterRegistry
from daemon.service import DeviceDaemon
from safety_kernel.domains import DomainExtension, PreconditionContext, register_domains
from safety_kernel.engine import SafetyKernel
from safety_kernel.predicates import evaluate_confirm_if

REPO_ROOT = Path(__file__).resolve().parent.parent
JUBILEE_BUNDLE_DIR = REPO_ROOT / "bundles" / "jubilee"
RADIO_FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "open_radio"


def _make_jubilee_kernel(
    *,
    target: str = "in_memory",
    adapter: InMemoryFakeAdapter | None = None,
    allow_live_hardware: bool = False,
    review_status: str | None = None,
) -> tuple[SafetyKernel, InMemoryFakeAdapter]:
    bundle = load_bundle(JUBILEE_BUNDLE_DIR)
    if review_status is not None:
        bundle = replace(bundle, device=replace(bundle.device, review_status=review_status))
    profile = bundle.device.targets[target]
    fake = adapter or InMemoryFakeAdapter(
        target=target,
        capabilities=profile.capabilities,
        is_real_hardware=profile.is_real_hardware,
    )
    kernel = SafetyKernel(
        bundle=bundle,
        target_name=target,
        supervisor=fake,
        driver=fake,
        allow_live_hardware=allow_live_hardware,
    )
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
    kernel, _ = _make_jubilee_kernel(
        target="duet_real", adapter=fake, allow_live_hardware=True, review_status="verified"
    )
    kernel.invoke("sys.detect")
    assert kernel.invoke("motion.home_all", confirm=True).ok is True

    # On duet_real, R2_MOTION requires --confirm
    env = kernel.invoke("motion.move_safe", args={"x": 100.0, "y": 100.0}, confirm=False)
    assert env.exit_code == ExitCode.CONFIRM_REQUIRED
    assert kernel.invoke("motion.move_safe", args={"x": 100.0, "y": 100.0}, confirm=True).ok is True


@pytest.mark.parametrize(
    ("review_status", "allow_live_hardware", "expected"),
    [
        ("verified", False, ExitCode.SAFETY_DENIED),
        ("draft", True, ExitCode.SAFETY_DENIED),
        ("draft", False, ExitCode.SAFETY_DENIED),
        ("verified", True, ExitCode.OK),
    ],
)
def test_real_hardware_live_unlock(
    review_status: str, allow_live_hardware: bool, expected: ExitCode
) -> None:
    kernel, fake = _make_jubilee_kernel(
        target="duet_real",
        allow_live_hardware=allow_live_hardware,
        review_status=review_status,
    )
    for service in ("sys.detect", "sys.status", "sys.preflight"):
        assert kernel.invoke(service).ok

    plan = kernel.invoke("motion.home_all", dry_run=True)
    assert plan.ok and plan.mode == ExecutionMode.DRY_RUN
    assert plan.data["plan"]["requires_confirm_when_live"]
    assert fake.executed_requests == []

    unconfirmed = kernel.invoke("motion.home_all")
    if expected == ExitCode.OK:
        assert unconfirmed.exit_code == ExitCode.CONFIRM_REQUIRED
    else:
        assert unconfirmed.error is not None and unconfirmed.error.code == ErrorCode.E_LIVE_LOCKED
    assert fake.executed_requests == []

    result = kernel.invoke("motion.home_all", confirm=True)
    assert result.exit_code == expected
    if expected == ExitCode.OK:
        assert [request.service for request in fake.executed_requests] == ["motion.home_all"]
    else:
        assert result.error is not None and result.error.code == ErrorCode.E_LIVE_LOCKED
        assert fake.executed_requests == []
        assert fake.poll_state().homed == frozenset()

    assert kernel.invoke("sys.abort", dry_run=True).ok
    assert fake.abort_count == 0
    aborted = kernel.invoke("sys.abort")
    assert aborted.ok and aborted.state["estop_latched"]
    assert fake.abort_count == 1
    if expected == ExitCode.SAFETY_DENIED:
        reset = kernel.invoke("sys.reset_estop", confirm=True)
        assert reset.error is not None and reset.error.code == ErrorCode.E_LIVE_LOCKED
        assert fake.poll_state().estop_latched


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


@pytest.fixture
def radio_bundle_dir(tmp_path: Path) -> Path:
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
    return bundle_dir


def test_safety_kernel_enforces_rules_on_drafted_open_radio(radio_bundle_dir: Path) -> None:
    bundle = load_bundle(radio_bundle_dir)
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


@pytest.mark.parametrize(
    ("temperature", "allowed"),
    [
        (-10.5, True),
        ("10.25", True),
        (25, True),
        (-10.6, False),
        (25.1, False),
        (None, False),
        ("unavailable", False),
        (True, False),
        (float("nan"), False),
    ],
)
def test_generic_properties_and_confirmation(
    radio_bundle_dir: Path, temperature: object, allowed: bool
) -> None:
    bundle = load_bundle(radio_bundle_dir)
    tune = bundle.catalog.services["radio.tune"]
    service = replace(
        tune,
        preconditions=(*tune.preconditions, "prop_range:temperature_c=-10.5..25"),
        confirm_if=("prop.frequency_mhz != null",),
    )
    bundle.catalog.services[service.name] = service
    fake = InMemoryFakeAdapter(
        capabilities=bundle.device.targets["in_memory"].capabilities,
        initial_properties={"temperature_c": temperature} if temperature is not None else {},
    )
    kernel = SafetyKernel(
        bundle=bundle,
        target_name="in_memory",
        supervisor=fake,
        driver=fake,
        domains=DomainExtension(),
    )
    assert kernel.invoke("sys.detect").ok

    args = {"frequency_mhz": 98.5}
    first_plan = kernel.invoke(service.name, args=args, dry_run=True)
    if not allowed:
        assert first_plan.exit_code == ExitCode.SAFETY_DENIED
        assert first_plan.error is not None
        assert first_plan.error.code == ErrorCode.E_PRECONDITION_FAILED
        assert first_plan.error.step == "precondition:prop_range:temperature_c=-10.5..25"
        assert fake.executed_requests == []
        return

    assert first_plan.ok and not first_plan.data["plan"]["requires_confirm_when_live"]
    assert first_plan.data["plan"]["target_coords"] is None
    assert kernel.invoke(service.name, args=args).ok
    next_plan = kernel.invoke(service.name, args=args, dry_run=True)
    assert next_plan.ok and next_plan.data["plan"]["requires_confirm_when_live"]
    assert kernel.invoke(service.name, args=args).exit_code == ExitCode.CONFIRM_REQUIRED
    assert len(fake.executed_requests) == 1
    assert kernel.invoke(service.name, args=args, confirm=True).ok


@pytest.mark.parametrize(
    ("rule", "matches"),
    [
        ("state.deck != null", False),
        ("state.lifecycle == IDLE", True),
        ("state.detected == true", True),
        ("state.detected == 1", False),
        ("prop.mode == \"remote standby\"", True),
        ("prop.voltage == 3.3", True),
        ("prop.output != null", True),
        ("prop.missing != null", False),
        ("prop.missing == null", True),
    ],
)
def test_confirmation_expression_literals(rule: str, matches: bool) -> None:
    state = CanonicalMachineState(
        detected=True,
        lifecycle=MachineLifecycle.IDLE,
        properties={"mode": "remote standby", "voltage": 3.3, "output": False},
    )
    assert evaluate_confirm_if(rule, state) is matches


def test_declarative_protocol_and_adapter_options_reach_adapter(radio_bundle_dir: Path) -> None:
    device_path = radio_bundle_dir / "device.yaml"
    device_data = yaml.safe_load(device_path.read_text())
    del device_data["review_status"]
    profile = device_data["targets"]["in_memory"]
    profile["is_real_hardware"] = True
    profile["adapter_options"] = {"transport": {"baud": 115200}, "allow_live_hardware": True}
    device_path.write_text(yaml.safe_dump(device_data))

    catalog_path = radio_bundle_dir / "catalog.yaml"
    catalog_data = yaml.safe_load(catalog_path.read_text())
    tune = next(svc for svc in catalog_data["services"] if svc["name"] == "radio.tune")
    protocol = {"command": "FREQ {frequency_mhz}", "reply": {"required": False}}
    tune["protocol"] = protocol
    tune["confirm_if"] = ["prop.frequency_mhz != null", "state.lifecycle == IDLE"]
    catalog_path.write_text(yaml.safe_dump(catalog_data))

    bundle = load_bundle(radio_bundle_dir)
    assert bundle.device.review_status == "draft"
    assert bundle.catalog.services["radio.tune"].confirm_if == tuple(tune["confirm_if"])
    assert bundle.catalog.services["radio.tune"].protocol == protocol
    configs: list[AdapterConfig] = []
    fake = InMemoryFakeAdapter(capabilities=bundle.device.targets["in_memory"].capabilities)

    def factory(cfg: AdapterConfig) -> AdapterPair:
        configs.append(cfg)
        return AdapterPair(target=cfg.target, supervisor=fake, driver=fake)

    registry = AdapterRegistry(load_entry_points=False)
    registry.register("in_memory", factory)
    args = {"frequency_mhz": 98.5}

    daemon = DeviceDaemon(bundle=bundle, registry=registry, allow_live_hardware=True)
    assert daemon.dispatch(target="in_memory", service="sys.detect").ok
    locked = daemon.dispatch(target="in_memory", service="radio.tune", args=args, confirm=True)
    assert locked.error is not None and locked.error.code == ErrorCode.E_LIVE_LOCKED
    daemon.close()

    verified = replace(bundle, device=replace(bundle.device, review_status="verified"))
    daemon = DeviceDaemon(bundle=verified, registry=registry)
    locked = daemon.dispatch(target="in_memory", service="radio.tune", args=args, confirm=True)
    assert locked.error is not None and locked.error.code == ErrorCode.E_LIVE_LOCKED
    assert configs[-1].options["allow_live_hardware"] is False
    assert fake.executed_requests == []
    daemon.close()

    daemon = DeviceDaemon(bundle=verified, registry=registry, allow_live_hardware=True)
    try:
        assert daemon.dispatch(target="in_memory", service="sys.detect").ok
        pending = daemon.dispatch(target="in_memory", service="radio.tune", args=args)
        assert pending.exit_code == ExitCode.CONFIRM_REQUIRED
        confirmed = daemon.dispatch(
            target="in_memory", service="radio.tune", args=args, confirm=True
        )
        assert confirmed.ok
        assert fake.executed_requests[0].protocol == protocol
        assert configs[-1].options["transport"] == {"baud": 115200}
        assert configs[-1].options["allow_live_hardware"] is True
    finally:
        daemon.close()


def test_custom_domain_dispatch(radio_bundle_dir: Path) -> None:
    bundle = load_bundle(radio_bundle_dir)
    tune = bundle.catalog.services["radio.tune"]
    bundle.catalog.services[tune.name] = replace(
        tune, preconditions=("radio_ready",), postconditions=("radio_tuned",)
    )
    fake = InMemoryFakeAdapter(capabilities=bundle.device.targets["in_memory"].capabilities)
    calls: list[str] = []

    def resolve(args: dict[str, Any], state: CanonicalMachineState) -> dict[str, float]:
        assert state.detected
        return {"frequency": args["frequency_mhz"]}

    def ready(ctx: PreconditionContext) -> None:
        assert ctx.target_coords == {"frequency": 98.5}
        calls.append("precondition")

    def tuned(args: dict[str, Any], state: CanonicalMachineState) -> None:
        assert state.properties["frequency_mhz"] == args["frequency_mhz"]
        calls.append("postcondition")

    domains = register_domains(
        DomainExtension(
            preconditions={"radio_ready": ready},
            postconditions={"radio_tuned": tuned},
            coord_resolver={tune.name: resolve},
        )
    )
    kernel = SafetyKernel(
        bundle=bundle, target_name="in_memory", supervisor=fake, driver=fake, domains=domains
    )
    assert kernel.invoke("sys.detect").ok
    assert kernel.invoke(tune.name, args={"frequency_mhz": 98.5}).ok
    assert fake.executed_requests[0].target_coords == {"frequency": 98.5}
    assert calls == ["precondition", "postcondition"]


def test_core_kernel_has_no_deck_or_well_service_dispatch() -> None:
    for module in ("engine.py", "predicates.py"):
        source = (REPO_ROOT / "safety_kernel" / module).read_text()
        assert "deck.load_deck" not in source
        assert "motion.move_well" not in source


def test_unverified_hookup_allows_readonly_preflight_but_blocks_actuation() -> None:
    fake = InMemoryFakeAdapter(target="duet_real", is_real_hardware=True, hookup_verified=False)
    kernel, _ = _make_jubilee_kernel(
        target="duet_real", adapter=fake, allow_live_hardware=True, review_status="verified"
    )

    assert kernel.invoke("sys.detect").ok is True
    assert kernel.invoke("sys.preflight").ok is True

    denied = kernel.invoke("motion.home_all", confirm=True)
    assert denied.ok is False
    assert denied.exit_code == ExitCode.SAFETY_DENIED
    assert denied.error is not None and denied.error.code == ErrorCode.E_NOT_DETECTED
    assert fake.executed_requests == []
