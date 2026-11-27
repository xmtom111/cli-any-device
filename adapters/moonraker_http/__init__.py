"""Klipper supervisor and action driver using Moonraker's HTTP API."""

from __future__ import annotations

import math
import time
from dataclasses import replace
from decimal import Decimal
from typing import Any

from adapters._generic import ConfiguredAdapter, invalid_protocol, render, time_left
from adapters._http import JsonHttpClient, json_path

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

_OBJECTS_PATH = "/printer/objects/query"
_POLL_PATH = _OBJECTS_PATH + "?toolhead&gcode_move&idle_timeout"
_SETTLED_PATH = _OBJECTS_PATH + "?idle_timeout&toolhead&gcode_move"
_MOVES = frozenset({"motion.move_safe", "motion.move_to", "motion.move_rel"})
_KLIPPER_LIFECYCLES = {
    "shutdown": MachineLifecycle.HALTED,
    "error": MachineLifecycle.FAULT,
    "startup": MachineLifecycle.UNKNOWN,
    "disconnected": MachineLifecycle.DISCONNECTED,
}
_IDLE_LIFECYCLES = {
    "Ready": MachineLifecycle.IDLE,
    "Idle": MachineLifecycle.IDLE,
    "Printing": MachineLifecycle.MOVING,
}


def _xyz(value: Any) -> dict[str, float]:
    if not isinstance(value, list) or len(value) < 3:
        return {}
    if not all(
        isinstance(coord, (int, float)) and math.isfinite(coord) for coord in value[:3]
    ):
        return {}
    return dict(zip("XYZ", map(float, value[:3]), strict=True))


def _number(value: Any, step: str) -> float:
    try:
        number = float(value)
        if math.isfinite(number):
            return number
    except (TypeError, ValueError, OverflowError):
        pass
    raise invalid_protocol("G-code coordinates and feed must be finite numbers.", step)


def _decimal(number: float) -> str:
    # Klipper interprets an exponent's E as a separate extruder argument.
    return format(Decimal(str(number)), "f")


class MoonrakerAdapter(ConfiguredAdapter):
    def __init__(self, config: AdapterConfig) -> None:
        super().__init__(config, ApiDialect.HTTP_REST, "http_rest", "axes_xyz", "abort_oob")
        self._http = JsonHttpClient(self.endpoint, config.options)
        self._safe_z: float | None = config.options.get("safe_z_mm")
        self._homing_valid = True
        self._gcode_offset: dict[str, float] = {}

    def detect(self) -> Identity:
        with self._actions:
            deadline = time.monotonic() + self._timeout
            server = self._http.request("GET", "/server/info", deadline, "detect")
            printer = self._http.request("GET", "/printer/info", deadline, "detect")
            firmware = json_path(printer, "result.software_version")
            hostname = json_path(printer, "result.hostname")
            klippy_state = json_path(server, "result.klippy_state")
            if not isinstance(firmware, str) or not firmware:
                raise AdapterTransportError(
                    "Moonraker did not report a firmware version.", step="detect"
                )
            return self._identify(
                firmware, hostname if isinstance(hostname, str) else None,
                klippy_state=klippy_state if isinstance(klippy_state, str) else None,
            )

    def _current_state(self) -> CanonicalMachineState:
        return replace(self._state(), safe_z_mm=self._safe_z)

    def _poll(self, path: str, deadline: float, step: str) -> CanonicalMachineState:
        if self._halted:
            return self._current_state()
        server = self._http.request("GET", "/server/info", deadline, step)
        klippy_state = json_path(server, "result.klippy_state")
        if self._halted:
            return self._current_state()
        if isinstance(klippy_state, str) and klippy_state in _KLIPPER_LIFECYCLES:
            lifecycle = _KLIPPER_LIFECYCLES[klippy_state]
            self._homing_valid = False
            state = self._state(lifecycle)
            return replace(
                state, safe_z_mm=self._safe_z,
                estop_latched=state.estop_latched or lifecycle == MachineLifecycle.HALTED,
            )

        payload = self._http.request("GET", path, deadline, step)
        idle_state = json_path(payload, "result.status.idle_timeout.state")
        idle_state = idle_state if isinstance(idle_state, str) else ""
        lifecycle = _IDLE_LIFECYCLES.get(idle_state, MachineLifecycle.UNKNOWN)
        state = self._state(lifecycle)
        if state.estop_latched:
            return replace(state, safe_z_mm=self._safe_z)
        position = _xyz(json_path(payload, "result.status.toolhead.position"))
        axes = json_path(payload, "result.status.toolhead.homed_axes")
        axes = axes.upper() if isinstance(axes, str) else None
        if axes == "":
            self._homing_valid = True
        homed = frozenset(axes or "") if self._homing_valid else frozenset()

        commanded = _xyz(json_path(payload, "result.status.gcode_move.position"))
        gcode = _xyz(json_path(payload, "result.status.gcode_move.gcode_position"))
        self._gcode_offset = {
            axis: gcode[axis] - commanded[axis] for axis in commanded.keys() & gcode.keys()
        }
        return replace(
            state, position=Position(position.get("X"), position.get("Y"), position.get("Z")),
            homed=homed, safe_z_mm=self._safe_z,
            properties={"idle_timeout": idle_state} if idle_state else {},
        )

    def poll_state(self) -> CanonicalMachineState:
        state = self._current_state()
        if state.estop_latched or not state.detected:
            return state
        with self._actions:
            return self._poll(_POLL_PATH, time.monotonic() + self._timeout, "poll_state")

    def preflight(self) -> PreflightReport:
        return self._preflight(self.poll_state())

    def _move_script(self, request: ActionRequest, state: CanonicalMachineState) -> str:
        step, args = request.service, request.args
        relative = step == "motion.move_rel"
        prefix = "d" if relative else ""
        coords = {
            axis: _number(args[prefix + axis.lower()], step)
            for axis in "XYZ" if prefix + axis.lower() in args
        }
        if not relative and request.target_coords is not None:
            coords = {
                axis: _number(request.target_coords[axis], step)
                for axis in "XYZ" if axis in request.target_coords
            }
        if not coords:
            raise invalid_protocol("A Moonraker move requires at least one axis.", step)
        speed = _number(args["speed"], step) if "speed" in args else None
        if speed is not None and speed <= 0:
            raise invalid_protocol("G-code feed must be positive.", step)
        feed = f" F{_decimal(speed)}" if speed is not None else ""

        def line(axes: dict[str, float]) -> str:
            words = [
                f"{axis}{_decimal(_number(value + self._gcode_offset.get(axis, 0.0), step))}"
                if not relative else f"{axis}{_decimal(value)}"
                for axis, value in axes.items()
            ]
            return f"G1 {' '.join(words)}{feed}"

        safe_z = request.safe_z_mm if request.safe_z_mm is not None else self._safe_z
        if step == "motion.move_safe" and safe_z is not None and coords.keys() & {"X", "Y"}:
            current_z = state.position.z
            if current_z is None:
                raise HardwareFaultError("Safe travel requires a known Z position.", step=step)
            clearance = max(current_z, _number(safe_z, step))
            frames = ["G90"]
            if clearance > current_z:
                frames.append(line({"Z": clearance}))
            frames.append(line({axis: value for axis, value in coords.items() if axis != "Z"}))
            final_z = coords.get("Z", current_z)
            if final_z != clearance:
                frames.append(line({"Z": final_z}))
            return "\n".join(frames)
        move = line(coords)
        return f"G91\n{move}\nG90" if relative else f"G90\n{move}"

    def _script(self, script: str, deadline: float, step: str) -> Any:
        self._check_running(step)
        try:
            payload = self._http.request(
                "POST", "/printer/gcode/script", deadline, step, body={"script": script}
            )
        except (AdapterTimeoutError, AdapterTransportError):
            self._check_running(step)
            raise
        self._check_running(step)
        return payload

    def execute(self, request: ActionRequest) -> ActionTelemetry:
        step = request.service
        template = request.protocol.get("gcode")
        with self._actions:
            deadline = time.monotonic() + min(request.timeout_s, self._timeout)
            if template is not None:
                if not isinstance(template, str):
                    raise invalid_protocol("G-code templates must be strings.", step)
                script = render(template, request.args, step, forbidden="\r\n")
                if not script.strip():
                    raise invalid_protocol("G-code script must not be empty.", step)
            elif step == "motion.home_all":
                script = "G28"
            elif step in _MOVES:
                state = self._poll(_POLL_PATH, deadline, step)
                if state.lifecycle != MachineLifecycle.IDLE or set("XYZ") - state.homed:
                    raise HardwareFaultError(
                        "Motion requires an idle printer with XYZ homed.", step=step
                    )
                script = self._move_script(request, state)
            else:
                raise UnsupportedCapabilityError(f"{step} has no Moonraker G-code.", step=step)
            payload = self._script(script, deadline, step)
            if step == "motion.home_all":
                self._homing_valid = True
        return ActionTelemetry(step, True, {"response": payload})

    def await_settled(self, timeout_s: float) -> CanonicalMachineState:
        step = "await_settled"
        deadline = time.monotonic() + timeout_s
        with self._actions:
            try:
                self._script("M400", deadline, step)
                while True:
                    self._check_running(step)
                    state = self._poll(
                        _SETTLED_PATH, min(deadline, time.monotonic() + self._timeout), step
                    )
                    if state.lifecycle in {MachineLifecycle.HALTED, MachineLifecycle.FAULT}:
                        raise HardwareFaultError(f"Klipper is {state.lifecycle.value}.", step=step)
                    if state.properties.get("idle_timeout") == "Ready":
                        self._check_running(step)
                        return state
                    time.sleep(min(0.02, time_left(deadline, step)))
            except (AdapterTimeoutError, AdapterTransportError):
                self._check_running(step)
                raise

    def abort_now(self) -> AbortResult:
        return self._abort(
            "moonraker_http",
            lambda deadline: self._http.request(
                "POST", "/printer/emergency_stop", deadline, "abort_now", abort=True
            ),
        )

    def reset_estop(self) -> CanonicalMachineState:
        def reset() -> None:
            self._http.request(
                "POST", "/printer/firmware_restart", time.monotonic() + self._timeout,
                "reset_estop",
            )
            self._homing_valid = False

        with self._actions:
            self._reset(reset)
            return self._current_state()

    def apply_fail_safe(self, policy: str, reason: str) -> str:
        if policy == "none":
            return policy
        if policy not in {"adapter_abort", "abort_m112", "retract_safe_z_or_halt"}:
            raise UnsupportedCapabilityError(f"Moonraker cannot apply fail-safe '{policy}'.")
        if self._halted:
            return "Klipper emergency stop already latched"
        result = self.abort_now()
        if not result.sent:
            raise HardwareFaultError(result.note, step="fail_safe")
        return "Klipper emergency stop sent"


def create_adapter(config: AdapterConfig) -> AdapterPair:
    adapter = MoonrakerAdapter(config)
    return AdapterPair(config.target, adapter, adapter)
