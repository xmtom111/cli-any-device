"""Jubilee discovery and supervision over direct RRF HTTP."""

import math
import os
import threading
import time
from dataclasses import replace

from adapter_spi import (
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
    HardwareFaultError,
    Identity,
    MachineLifecycle,
    Position,
    UnsupportedCapabilityError,
)

from .supervisor import RrfSupervisor

_HOOKUP = "HOOKUP.md#sec-troubleshooting"
_LIFECYCLE = {
    "idle": MachineLifecycle.IDLE,
    "moving": MachineLifecycle.MOVING,
    "halted": MachineLifecycle.HALTED,
    "off": MachineLifecycle.FAULT,
}
_MALFORMED_HTTP = (OSError, ValueError, KeyError, TypeError, AttributeError)
_ABORT_FAILURE = (*_MALFORMED_HTTP, RuntimeError)


class DuetAdapter:
    def __init__(self, *, endpoint=None, password=None, vendor_rehearsal=False):
        if endpoint is not None and password is None:
            raise ValueError("configure the Duet HTTP password explicitly")
        if endpoint is not None and vendor_rehearsal:
            raise ValueError("vendor rehearsal requires offline mode")
        self._supervisor = RrfSupervisor(endpoint, password) if endpoint is not None else None
        self._vendor_rehearsal = vendor_rehearsal
        self._aborted = threading.Event()
        self._identity = None

    def capabilities(self) -> frozenset[str]:
        if self._supervisor is None:
            return frozenset()
        return frozenset({"rrf_object_model", "abort_m112", "abort_oob"})

    def detect(self) -> Identity:
        if self._supervisor is None:
            self._identity = Identity(
                "duet_real",
                "offline",
                ApiDialect.UNKNOWN,
                "unavailable",
                hookup_verified=False,
                details={
                    "read_only": True,
                    "offline": True,
                    "vendor_rehearsal": self._vendor_rehearsal,
                },
            )
        else:
            try:
                raw = self._supervisor.detect()
            except _MALFORMED_HTTP as exc:
                raise AdapterTransportError(
                    str(exc),
                    step="detect",
                    hookup_ref=_HOOKUP,
                ) from exc
            dialect = {
                "sbc": ApiDialect.RRF_SBC,
                "standalone": ApiDialect.RRF_STANDALONE,
            }[raw["api_mode"]]
            self._identity = Identity(
                "duet_real",
                raw["endpoint"],
                dialect,
                raw["firmware"] or "unknown",
                board=raw["board"],
                capabilities=self.capabilities(),
                is_real_hardware=True,
                hookup_verified=False,
                details={"read_only": True},
            )
        return self._identity

    def _canonical(self, raw):
        coords = raw["position"].copy()
        return CanonicalMachineState(
            lifecycle=_LIFECYCLE.get(raw["status"], MachineLifecycle.BUSY),
            position=Position(*(coords.pop(axis, None) for axis in "XYZU"), extra=coords),
            homed=frozenset(raw["homed"]),
            active_tool=raw["active_tool"],
            # State.currentTool identifies firmware selection. Occupancy and lock
            # require evidence from verified, machine-specific sensors.
            estop_latched=raw["status"] == "halted",
            detected=self._identity is not None,
            identity=self._identity,
            capabilities=self.capabilities(),
        )

    def poll_state(self) -> CanonicalMachineState:
        if self._supervisor is None:
            state = CanonicalMachineState(
                lifecycle=MachineLifecycle.DISCONNECTED,
                identity=self._identity,
            )
        else:
            try:
                state = self._canonical(self._supervisor.poll_state())
            except _MALFORMED_HTTP as exc:
                raise AdapterTransportError(
                    "Unable to read Duet object model",
                    step="poll_state",
                    hookup_ref=_HOOKUP,
                ) from exc
        if self._aborted.is_set():
            return replace(
                state, lifecycle=MachineLifecycle.HALTED, homed=frozenset(), estop_latched=True
            )
        return state

    def preflight(self) -> PreflightReport:
        state = self.poll_state()
        details = {"motion_enabled": False, "state": state.to_summary_dict()}
        warnings = ()
        if self._supervisor is not None:
            try:
                details.update(self._supervisor.read_preflight_details())
            except _MALFORMED_HTTP:
                warnings = ("Optional endstop and power telemetry could not be read.",)
        return PreflightReport(
            passed=False,
            checks={
                "object_model_reachable": self._supervisor is not None,
                "homed_xyz": frozenset("XYZ") <= state.homed,
                "onsite_verification": False,
            },
            blocking_reasons=("Physical checks and supervised motion authorization are required.",),
            details=details,
            warnings=warnings,
        )

    def await_settled(self, timeout_s: float) -> CanonicalMachineState:
        if self._aborted.is_set():
            raise HardwareFaultError("Duet abort latched", step="await_settled")
        if self._supervisor is None:
            raise AdapterTransportError(
                "No Duet configured for a completion barrier", step="await_settled"
            )
        try:
            state = self._canonical(self._supervisor.await_settled(timeout_s))
        except TimeoutError as exc:
            raise AdapterTimeoutError(str(exc)) from exc
        except _MALFORMED_HTTP as exc:
            raise AdapterTransportError(
                "Duet completion telemetry unavailable",
                step="await_settled",
                hookup_ref=_HOOKUP,
            ) from exc
        except RuntimeError as exc:
            raise HardwareFaultError(str(exc), step="await_settled") from exc
        if self._aborted.is_set():
            raise HardwareFaultError("Duet abort latched", step="await_settled")
        return state

    def abort_now(self) -> AbortResult:
        start = time.monotonic()
        self._aborted.set()
        sent = False
        note = "No Duet endpoint configured."
        if self._supervisor is not None:
            try:
                self._supervisor.abort_now()
            except _ABORT_FAILURE:
                note = "M112 delivery could not be confirmed."
            else:
                sent = True
                note = "M112 submitted independently. Confirm the physical stop."
        return AbortResult(sent, (time.monotonic() - start) * 1000, "duet.http_m112", note)

    def reset_estop(self) -> CanonicalMachineState:
        raise UnsupportedCapabilityError(
            "Duet reset requires onsite inspection and manual controller reset",
            step="sys.reset_estop",
        )

    def execute(self, request: ActionRequest) -> ActionTelemetry:
        if self._vendor_rehearsal and request.service == "motion.move_to":
            if self._aborted.is_set():
                raise HardwareFaultError("Duet rehearsal halted", step=request.service)
            return self._rehearse_motion(request)
        raise UnsupportedCapabilityError(
            f"Duet motion dispatch is disabled in this read-only adapter ({request.service})",
            step=request.service,
        )

    def apply_fail_safe(self, policy: str, reason: str) -> str:
        if policy == "none":
            return "none"
        if policy not in {"abort_m112", "retract_safe_z_or_halt"}:
            raise UnsupportedCapabilityError(f"Duet cannot apply fail-safe {policy}")
        result = self.abort_now()
        if not result.sent:
            raise HardwareFaultError(result.note, step="fail_safe")
        return result.note

    @staticmethod
    def _rehearse_motion(request: ActionRequest) -> ActionTelemetry:
        """Run the pinned science-jubilee XYZ method with a capture-only G-code sink."""
        from science_jubilee.Machine import Machine

        coordinates = {axis: float(request.args[axis]) for axis in "xyz"}
        speed = float(request.args.get("speed") or 6000)
        if not all(math.isfinite(value) for value in (*coordinates.values(), speed)) or speed <= 0:
            raise ValueError("XYZ and feed must be finite and feed must be positive")
        frames = []

        class OfflineMachine(Machine):
            def gcode(self, cmd="", timeout=None, response_wait=60):
                frames.append(cmd)

        machine = OfflineMachine(address=Machine.LOCALHOST, simulated=True)
        frames.clear()  # Constructor positioning belongs to setup, not this action.
        machine.move_to(**coordinates, s=speed, wait=False)
        return ActionTelemetry(request.service, False, {"rehearsal": True, "frames": frames})


def create_adapter(config: AdapterConfig) -> AdapterPair:
    password = config.options.get("password", os.getenv("CLI_ANY_DEVICE_DUET_PASSWORD"))
    endpoint = config.endpoint if password is not None else None
    adapter = DuetAdapter(
        endpoint=endpoint,
        password=password,
        vendor_rehearsal=config.options.get("vendor_rehearsal", False),
    )
    return AdapterPair(config.target, adapter, adapter)
