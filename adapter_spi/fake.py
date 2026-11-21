"""Generic in-memory fake adapter implementing SupervisorChannel and ActionDriver.

Supports ANY device bundle:
- Arbitrary domain services (e.g. radio.tune, arm.set_joint, psu.set_voltage) update
  ``state.properties`` cleanly or delegate to optional custom handlers.
- Motion/tool/deck services (used by Jubilee and 3-axis platform fixtures) simulate
  axes, safe Z, labware wells, tool pickup/park, and honest capability rejection.
- ``abort_now()`` is strictly lock-free and completes in < 100 ms even when
  ``ActionDriver.execute()`` is intentionally deadlocked.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from adapter_spi.protocols import (
    AbortResult,
    ActionRequest,
    ActionTelemetry,
    AdapterConfig,
    AdapterPair,
    PreflightReport,
)
from core_types import (
    AdapterTimeoutError,
    AdapterTransportError,
    ApiDialect,
    CanonicalMachineState,
    DeckState,
    HardwareFaultError,
    Identity,
    LabwareState,
    MachineLifecycle,
    Position,
    ToolPresence,
    UnsupportedCapabilityError,
)

DEFAULT_FULL_CAPABILITIES: frozenset[str] = frozenset(
    {
        "axes_xyz",
        "axes_xyzu",
        "tool_lock_u",
        "rrf_object_model",
        "deck_labware",
        "abort_oob",
        "expert_gcode",
    }
)

GRBL_SUBSET_CAPABILITIES: frozenset[str] = frozenset(
    {
        "axes_xyz",
        "serial_gcode",
        "abort_oob",
    }
)

CustomHandler = Callable[[ActionRequest, dict[str, Any]], dict[str, Any]]


def _build_default_96_well_map(
    slot_origin_x: float, slot_origin_y: float, top_z: float
) -> dict[str, tuple[float, float, float]]:
    wells: dict[str, tuple[float, float, float]] = {}
    rows = "ABCDEFGH"
    for r_idx, row in enumerate(rows):
        for col in range(1, 13):
            well_name = f"{row}{col}"
            wells[well_name] = (
                round(slot_origin_x + (col - 1) * 9.0, 3),
                round(slot_origin_y + r_idx * 9.0, 3),
                top_z,
            )
    return wells


class InMemoryFakeAdapter:
    """Thread-safe generic in-memory fake implementing SupervisorChannel + ActionDriver."""

    def __init__(
        self,
        *,
        target: str = "in_memory",
        endpoint: str = "memory://device-fake",
        capabilities: frozenset[str] = DEFAULT_FULL_CAPABILITIES,
        is_real_hardware: bool = False,
        api_mode: ApiDialect = ApiDialect.IN_MEMORY_FAKE,
        firmware: str = "InMemoryFake 1.0",
        detect_fails: bool = False,
        action_delay_s: float = 0.0,
        settle_delay_s: float = 0.0,
        initial_tool_presence: ToolPresence = ToolPresence.UNKNOWN,
        verify_tool_on_detect: bool = True,
        initial_properties: dict[str, Any] | None = None,
        initial_safe_z_mm: float | None = 120.0,
        fail_postcondition_on_service: str | None = None,
        deadlock_event: threading.Event | None = None,
        custom_handlers: dict[str, CustomHandler] | None = None,
    ) -> None:
        self.target = target
        self.endpoint = endpoint
        self._capabilities = capabilities
        self._is_real_hardware = is_real_hardware
        self._api_mode = api_mode
        self._firmware = firmware
        self.detect_fails = detect_fails
        self.action_delay_s = action_delay_s
        self.settle_delay_s = settle_delay_s
        self._verify_tool_on_detect = verify_tool_on_detect
        self.fail_postcondition_on_service = fail_postcondition_on_service
        self.deadlock_event = deadlock_event
        self._custom_handlers = dict(custom_handlers or {})

        # Separate locks: _action_lock serializes execute(); _abort_event is lock-free.
        self._action_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._abort_event = threading.Event()
        self._settled_event = threading.Event()
        self._settled_event.set()

        self.executed_requests: list[ActionRequest] = []
        self.fail_safe_calls: list[tuple[str, str]] = []
        self.abort_count: int = 0

        has_motion = "axes_xyz" in capabilities or "axes_xyzu" in capabilities
        initial_pos = (
            Position(x=0.0, y=0.0, z=0.0, u=0.0 if "axes_xyzu" in capabilities else None)
            if has_motion
            else Position()
        )
        self._state = CanonicalMachineState(
            lifecycle=MachineLifecycle.UNKNOWN,
            detected=False,
            estop_latched=False,
            identity=None,
            capabilities=capabilities,
            state_seq=0,
            properties=dict(initial_properties or {}),
            verified_flags=frozenset(),
            homed=frozenset(),
            position=initial_pos,
            active_tool=None,
            tool_presence=initial_tool_presence
            if "tool_lock_u" in capabilities
            else ToolPresence.NONE,
            tool_locked=False if "tool_lock_u" in capabilities else None,
            safe_z_mm=initial_safe_z_mm if has_motion else None,
            deck=None,
            collision_count=0,
        )

    def capabilities(self) -> frozenset[str]:
        return self._capabilities

    def detect(self) -> Identity:
        if self.detect_fails:
            raise AdapterTransportError(
                f"Cannot reach {self.endpoint} (in-memory simulated disconnect). "
                "See HOOKUP.md troubleshooting section.",
                step="sys.detect",
                hookup_ref="HOOKUP.md#sec-troubleshooting",
            )
        identity = Identity(
            target=self.target,
            endpoint=self.endpoint,
            api_mode=self._api_mode,
            firmware=self._firmware,
            board="InMemoryFakeBoard",
            capabilities=self._capabilities,
            is_real_hardware=self._is_real_hardware,
            hookup_verified=True,
            details={"completion_barrier": "settled+idle"},
        )
        with self._state_lock:
            tool_presence = self._state.tool_presence
            if self._verify_tool_on_detect and tool_presence == ToolPresence.UNKNOWN:
                tool_presence = (
                    ToolPresence.LOADED
                    if self._state.active_tool is not None
                    else ToolPresence.NONE
                )
            lifecycle = (
                MachineLifecycle.HALTED
                if self._abort_event.is_set() or self._state.estop_latched
                else MachineLifecycle.IDLE
            )
            self._state = CanonicalMachineState(
                lifecycle=lifecycle,
                detected=True,
                estop_latched=self._state.estop_latched or self._abort_event.is_set(),
                identity=identity,
                capabilities=self._capabilities,
                state_seq=(self._state.state_seq or 0) + 1,
                properties=dict(self._state.properties),
                verified_flags=self._state.verified_flags,
                homed=self._state.homed,
                position=self._state.position,
                active_tool=self._state.active_tool,
                tool_presence=tool_presence,
                tool_locked=self._state.tool_locked,
                safe_z_mm=self._state.safe_z_mm,
                deck=self._state.deck,
                collision_count=self._state.collision_count,
            )
        return identity

    def poll_state(self) -> CanonicalMachineState:
        if self._abort_event.is_set():
            with self._state_lock:
                if (
                    not self._state.estop_latched
                    or self._state.lifecycle != MachineLifecycle.HALTED
                ):
                    self._state = CanonicalMachineState(
                        lifecycle=MachineLifecycle.HALTED,
                        detected=self._state.detected,
                        estop_latched=True,
                        identity=self._state.identity,
                        capabilities=self._capabilities,
                        state_seq=(self._state.state_seq or 0) + 1,
                        properties=dict(self._state.properties),
                        verified_flags=frozenset(),
                        homed=frozenset(),
                        position=self._state.position,
                        active_tool=self._state.active_tool,
                        tool_presence=self._state.tool_presence,
                        tool_locked=self._state.tool_locked,
                        safe_z_mm=self._state.safe_z_mm,
                        deck=self._state.deck,
                        collision_count=self._state.collision_count,
                    )
                return self._state
        with self._state_lock:
            return self._state

    def preflight(self) -> PreflightReport:
        state = self.poll_state()
        checks: dict[str, bool] = {
            "detected": state.detected,
            "not_estop_latched": not state.estop_latched,
            "lifecycle_ready": state.lifecycle in (MachineLifecycle.IDLE, MachineLifecycle.UNKNOWN),
            "physical_estop_required": True,
        }
        blocking: list[str] = []
        warnings: list[str] = []
        if "tool_lock_u" in self._capabilities:
            checks["tool_presence_known"] = state.tool_presence != ToolPresence.UNKNOWN
            if not checks["tool_presence_known"]:
                blocking.append("Tool presence is UNKNOWN; sensor verification required.")
        if not checks["detected"]:
            blocking.append("Device not detected yet; run sys.detect first.")
        if not checks["not_estop_latched"]:
            blocking.append("Emergency stop is latched; inspect hardware and run sys.reset-estop.")
        if (
            "axes_xyz" in self._capabilities or "axes_xyzu" in self._capabilities
        ) and not state.homed:
            warnings.append("Axes are not homed; motion commands remain blocked.")
        if self._is_real_hardware:
            warnings.append(
                "Real hardware target: verify physical emergency stop / power cut before actuation."
            )
        return PreflightReport(
            passed=len(blocking) == 0,
            checks=checks,
            warnings=tuple(warnings),
            blocking_reasons=tuple(blocking),
            details={
                "target": self.target,
                "api_mode": self._api_mode.value,
                "capabilities": sorted(self._capabilities),
            },
        )

    def await_settled(self, timeout_s: float) -> CanonicalMachineState:
        deadline = time.monotonic() + timeout_s
        while True:
            if self._abort_event.is_set():
                self.poll_state()
                raise HardwareFaultError(
                    "Device halted by out-of-band emergency stop while awaiting completion.",
                    step="await_settled",
                )
            if self._settled_event.is_set():
                state = self.poll_state()
                if state.lifecycle in (MachineLifecycle.HALTED, MachineLifecycle.FAULT):
                    raise HardwareFaultError(
                        f"Device entered {state.lifecycle.value} while settling.",
                        step="await_settled",
                    )
                return state
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AdapterTimeoutError(
                    f"Timed out after {timeout_s:.2f}s waiting for device settle/idle.",
                    step="await_settled",
                )
            time.sleep(min(0.01, remaining))

    def abort_now(self) -> AbortResult:
        """Lock-free out-of-band abort: sets _abort_event without waiting on _action_lock."""
        t0 = time.perf_counter()
        self._abort_event.set()
        self.abort_count += 1
        self._settled_event.set()
        if self.deadlock_event is not None:
            self.deadlock_event.set()
        latency_ms = (time.perf_counter() - t0) * 1000.0
        return AbortResult(
            sent=True,
            latency_ms=latency_ms,
            channel="in_memory_oob_abort",
            note="Lock-free in-memory abort latched; homed/verified state invalidated.",
        )

    def reset_estop(self) -> CanonicalMachineState:
        self._abort_event.clear()
        with self._state_lock:
            self._state = CanonicalMachineState(
                lifecycle=MachineLifecycle.IDLE
                if self._state.detected
                else MachineLifecycle.UNKNOWN,
                detected=self._state.detected,
                estop_latched=False,
                identity=self._state.identity,
                capabilities=self._capabilities,
                state_seq=(self._state.state_seq or 0) + 1,
                properties=dict(self._state.properties),
                verified_flags=frozenset(),
                homed=frozenset(),
                position=self._state.position,
                active_tool=self._state.active_tool,
                tool_presence=self._state.tool_presence,
                tool_locked=self._state.tool_locked,
                safe_z_mm=self._state.safe_z_mm,
                deck=self._state.deck,
                collision_count=self._state.collision_count,
            )
            return self._state

    def execute(self, request: ActionRequest) -> ActionTelemetry:
        if (
            request.service
            in (
                "tool.pickup",
                "tool.park",
                "expert.tool_lock",
                "expert.tool_unlock",
            )
            and "tool_lock_u" not in self._capabilities
        ):
            raise UnsupportedCapabilityError(
                f"Target '{self.target}' does not support capability 'tool_lock_u' "
                f"required by '{request.service}'.",
                step=request.service,
            )
        if request.service == "motion.move_well" and "deck_labware" not in self._capabilities:
            raise UnsupportedCapabilityError(
                f"Target '{self.target}' does not support capability 'deck_labware'.",
                step=request.service,
            )

        with self._action_lock:
            self.executed_requests.append(request)
            if self.deadlock_event is not None:
                self.deadlock_event.wait(timeout=request.timeout_s)
                if self._abort_event.is_set():
                    raise HardwareFaultError("Aborted during blocked action.", step=request.service)

            if self.action_delay_s > 0:
                end = time.monotonic() + self.action_delay_s
                while time.monotonic() < end:
                    if self._abort_event.is_set():
                        raise HardwareFaultError("Aborted during action.", step=request.service)
                    time.sleep(0.005)

            if self._abort_event.is_set():
                raise HardwareFaultError("Device is in e-stop.", step=request.service)

            self._settled_event.clear()
            try:
                raw = self._apply_action(request)
                if self.settle_delay_s > 0:
                    timer = threading.Timer(self.settle_delay_s, self._settled_event.set)
                    timer.daemon = True
                    timer.start()
                else:
                    self._settled_event.set()
                return ActionTelemetry(service=request.service, accepted=True, raw=raw)
            except Exception:
                self._settled_event.set()
                raise

    def apply_fail_safe(self, policy: str, reason: str) -> str:
        self.fail_safe_calls.append((policy, reason))
        if policy in ("abort_m112", "abort_now", "cut_output"):
            self.abort_now()
            return f"{policy} applied ({reason})"
        with self._state_lock:
            safe_z = self._state.safe_z_mm or 120.0
            pos = self._state.position
            props = dict(self._state.properties)
            if policy == "retract_safe_z_or_halt" and pos.z is not None and pos.z < safe_z:
                pos = Position(x=pos.x, y=pos.y, z=safe_z, u=pos.u, extra=dict(pos.extra))
            elif policy == "disable_output":
                props["output_enabled"] = False
                props["tx_enabled"] = False
            self._state = CanonicalMachineState(
                lifecycle=self._state.lifecycle,
                detected=self._state.detected,
                estop_latched=self._state.estop_latched,
                identity=self._state.identity,
                capabilities=self._capabilities,
                state_seq=(self._state.state_seq or 0) + 1,
                properties=props,
                verified_flags=self._state.verified_flags,
                homed=self._state.homed,
                position=pos,
                active_tool=self._state.active_tool,
                tool_presence=self._state.tool_presence,
                tool_locked=self._state.tool_locked,
                safe_z_mm=self._state.safe_z_mm,
                deck=self._state.deck,
                collision_count=self._state.collision_count,
            )
        return f"{policy} applied ({reason})"

    def _apply_action(self, request: ActionRequest) -> dict[str, Any]:
        svc = request.service
        args = request.args
        with self._state_lock:
            st = self._state
            pos = st.position
            homed = set(st.homed)
            active_tool = st.active_tool
            tool_presence = st.tool_presence
            tool_locked = st.tool_locked
            safe_z = st.safe_z_mm
            deck = st.deck
            props = dict(st.properties)
            raw: dict[str, Any] = {"service": svc}

            if self.fail_postcondition_on_service == svc:
                return {"service": svc, "simulated_postcondition_mismatch": True}

            if svc in self._custom_handlers:
                raw = self._custom_handlers[svc](request, props)
            elif svc == "deck.load_deck":
                spec = str(args["spec"])
                slots: tuple[str, ...] = ("0", "1", "2", "3", "4", "5")
                deck = DeckState(spec=spec, slots=slots, labware={})
                raw = {"slots": list(slots), "safe_z": safe_z}
            elif svc == "deck.load_labware":
                slot = str(args["slot"])
                spec = str(args["spec"])
                top_z = float(args.get("top_z_mm", 20.0))
                slot_idx = int(slot) if slot.isdigit() else 0
                origin_x = 40.0 + (slot_idx % 3) * 100.0
                origin_y = 40.0 + (slot_idx // 3) * 100.0
                lw = LabwareState(
                    slot=slot,
                    spec=spec,
                    top_z_mm=top_z,
                    well_map=_build_default_96_well_map(origin_x, origin_y, top_z),
                )
                existing_lw = dict(deck.labware) if deck is not None else {}
                existing_lw[slot] = lw
                deck_slots: tuple[str, ...] = (
                    deck.slots if deck is not None else ("0", "1", "2", "3", "4", "5")
                )
                deck_spec = deck.spec if deck is not None else "default_deck"
                deck = DeckState(spec=deck_spec, slots=deck_slots, labware=existing_lw)
                raw = {"slot": slot, "spec": spec, "top_z": top_z, "well_count": len(lw.well_map)}
            elif svc == "deck.safe_z":
                safe_z = float(args["z_mm"])
                raw = {"safe_z": safe_z}
            elif svc == "motion.home_all":
                axes = {"X", "Y", "Z"}
                if "axes_xyzu" in self._capabilities:
                    axes.add("U")
                homed |= axes
                pos = Position(
                    x=0.0,
                    y=0.0,
                    z=safe_z if safe_z is not None else 120.0,
                    u=0.0 if "U" in axes else pos.u,
                )
                raw = {"homed": sorted(homed)}
            elif svc == "motion.home_xyu":
                homed |= {"X", "Y", "U"}
                pos = Position(x=0.0, y=0.0, z=pos.z, u=0.0)
                raw = {"homed": sorted(homed)}
            elif svc == "motion.home_z":
                homed.add("Z")
                pos = Position(x=pos.x, y=pos.y, z=safe_z if safe_z is not None else 120.0, u=pos.u)
                raw = {"homed": sorted(homed)}
            elif svc in ("motion.move_safe", "motion.move_to", "motion.move_well"):
                coords = request.target_coords or {}
                nx = float(coords.get("X", args.get("x", pos.x or 0.0)))
                ny = float(coords.get("Y", args.get("y", pos.y or 0.0)))
                nz = float(
                    coords.get(
                        "Z",
                        args.get("z", pos.z if pos.z is not None else (safe_z or 120.0)),
                    )
                )
                pos = Position(x=nx, y=ny, z=nz, u=pos.u)
                raw = {"target": {"X": nx, "Y": ny, "Z": nz}}
            elif svc == "motion.move_rel":
                nx = (pos.x or 0.0) + float(args.get("dx", 0.0))
                ny = (pos.y or 0.0) + float(args.get("dy", 0.0))
                nz = (pos.z or 0.0) + float(args.get("dz", 0.0))
                pos = Position(x=nx, y=ny, z=nz, u=pos.u)
                raw = {"target": {"X": nx, "Y": ny, "Z": nz}}
            elif svc == "tool.pickup":
                tool_id = int(args["tool_id"])
                active_tool = tool_id
                tool_presence = ToolPresence.LOADED
                tool_locked = True
                raw = {"active_tool": tool_id}
            elif svc == "tool.park":
                active_tool = None
                tool_presence = ToolPresence.NONE
                tool_locked = False
                raw = {"active_tool": None}
            elif svc == "expert.tool_lock":
                tool_locked = True
                raw = {"tool_locked": True}
            elif svc == "expert.tool_unlock":
                tool_locked = False
                raw = {"tool_locked": False}
            elif svc == "expert.home_u":
                homed.add("U")
                pos = Position(x=pos.x, y=pos.y, z=pos.z, u=0.0)
                raw = {"homed": sorted(homed)}
            elif svc == "expert.gcode":
                raw = {"gcode": str(args["command"]), "reply": "ok"}
            else:
                # Generic device service fallback: record arguments into properties
                for key, value in args.items():
                    props[key] = value
                props["last_service"] = svc
                raw = {"service": svc, "applied_args": dict(args), "properties": dict(props)}

            self._state = CanonicalMachineState(
                lifecycle=MachineLifecycle.IDLE,
                detected=st.detected,
                estop_latched=st.estop_latched,
                identity=st.identity,
                capabilities=self._capabilities,
                state_seq=(st.state_seq or 0) + 1,
                properties=props,
                verified_flags=st.verified_flags,
                homed=frozenset(homed),
                position=pos,
                active_tool=active_tool,
                tool_presence=tool_presence,
                tool_locked=tool_locked,
                safe_z_mm=safe_z,
                deck=deck,
                collision_count=st.collision_count,
            )
            return raw


def create_in_memory_adapter(config: AdapterConfig) -> AdapterPair:
    """Factory creating a generic InMemoryFakeAdapter paired as Supervisor + Driver."""
    caps_opt = config.options.get("capabilities")
    capabilities = (
        frozenset(caps_opt)
        if isinstance(caps_opt, (list, tuple, set, frozenset))
        else DEFAULT_FULL_CAPABILITIES
    )
    adapter = InMemoryFakeAdapter(
        target=config.target,
        endpoint=config.endpoint or f"memory://{config.target}",
        capabilities=capabilities,
        is_real_hardware=bool(config.options.get("is_real_hardware", False)),
        detect_fails=bool(config.options.get("detect_fails", False)),
        initial_properties=dict(config.options.get("initial_properties", {})),
    )
    return AdapterPair(target=config.target, supervisor=adapter, driver=adapter)
