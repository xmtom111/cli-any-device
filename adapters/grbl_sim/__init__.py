"""In-process AutoDNA GRBL emulator with no physical transport."""

import _imp
import math
import os
import sys
import threading
import time
from importlib import import_module
from pathlib import Path

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
    ToolPresence,
    UnsupportedCapabilityError,
)


class _DiscardLog:
    def emit(self, channel, **event):
        pass


def _load_autodna(root):
    # The upstream modules use top-level imports. Limit the search-path change
    # to bootstrap and disable writing bytecode in the source checkout.
    root = Path(root).resolve()
    _imp.acquire_lock()
    write_setting = sys.dont_write_bytecode
    sys.path.insert(0, str(root))
    sys.dont_write_bytecode = True
    try:
        modules = []
        for name in ("common", "grbl_emulator.controller"):
            module = import_module(name)
            if not Path(module.__file__).resolve().is_relative_to(root):
                raise ImportError(f"{name} is already resolved outside AutoDNA: {root}")
            modules.append(module)
        common, controller = modules
        return common.config, controller.Controller
    finally:
        sys.dont_write_bytecode = write_setting
        sys.path.pop(0)
        _imp.release_lock()


class GrblAdapter:
    _capabilities = frozenset({"axes_xyz", "serial_gcode", "abort_oob"})
    _moves = frozenset({"motion.move_to", "motion.move_rel", "motion.move_safe"})
    _axes = frozenset("XYZ")

    def __init__(self, *, autodna_root="/workspace/autodna_sim_a", time_scale=1.0):
        config, Controller = _load_autodna(autodna_root)

        if not math.isfinite(time_scale) or time_scale <= 0:
            raise ValueError("time_scale must be finite and positive")
        self._settings = config("platform")
        self._controller = Controller(self._settings, _DiscardLog(), time_scale=time_scale)
        self._actions = threading.Lock()
        self._aborted = threading.Event()
        self._identity = None
        self._safe_z = float(self._settings["rack"]["safeZ"])

    def close(self):
        self._controller.close()

    def capabilities(self) -> frozenset[str]:
        return self._capabilities

    def detect(self) -> Identity:
        self._identity = Identity(
            target="grbl_sim",
            endpoint="memory://autodna-grbl",
            api_mode=ApiDialect.GRBL_SERIAL_TCP,
            firmware="AutoDNA GRBL 1.1h emulator",
            capabilities=self.capabilities(),
            details={"transport": "in-process realtime GRBL frames", "tool_interface": "absent"},
        )
        return self._identity

    def preflight(self) -> PreflightReport:
        state = self.poll_state()
        checks = {
            "detected": state.detected,
            "no_alarm_or_estop": state.lifecycle
            not in {MachineLifecycle.FAULT, MachineLifecycle.HALTED},
        }
        return PreflightReport(
            passed=all(checks.values()),
            checks=checks,
            warnings=("GRBL simulator provides XYZ motion only.",),
            blocking_reasons=tuple(name for name, passed in checks.items() if not passed),
        )

    def poll_state(self) -> CanonicalMachineState:
        controller = self._controller
        with controller.condition:
            position, _ = controller.current()
            halted = self._aborted.is_set()
            if halted:
                lifecycle = MachineLifecycle.HALTED
            elif controller.alarm or controller.closed:
                lifecycle = MachineLifecycle.FAULT
            elif controller.homing or controller.hold:
                lifecycle = MachineLifecycle.BUSY
            elif controller.active or controller.blocks:
                lifecycle = MachineLifecycle.MOVING
            else:
                lifecycle = MachineLifecycle.IDLE
            return CanonicalMachineState(
                lifecycle=lifecycle,
                position=Position(*position),
                homed=self._axes if controller.homed and not halted else frozenset(),
                # This in-process controller has no changeable tool head.
                # No tool number, lock indication, or sensor reading is inferred.
                tool_presence=ToolPresence.NONE,
                estop_latched=halted,
                detected=self._identity is not None,
                identity=self._identity,
                capabilities=self.capabilities(),
                safe_z_mm=self._safe_z,
            )

    def abort_now(self) -> AbortResult:
        start = time.monotonic()
        with self._controller.condition:
            self._aborted.set()
            self._controller.realtime("\x18", lambda _: None)
        return AbortResult(True, (time.monotonic() - start) * 1000, "grbl.realtime_reset")

    def reset_estop(self) -> CanonicalMachineState:
        with self._actions, self._controller.condition:
            if self._aborted.is_set():
                self._controller.realtime("\x18", lambda _: None)
                self._controller.submit("$X", lambda _: None)
                self._aborted.clear()
            return self.poll_state()

    def await_settled(self, timeout_s: float) -> CanonicalMachineState:
        deadline = time.monotonic() + timeout_s
        with self._controller.condition:
            while True:
                state = self.poll_state()
                if state.lifecycle == MachineLifecycle.IDLE:
                    return state
                if state.lifecycle in {MachineLifecycle.FAULT, MachineLifecycle.HALTED}:
                    raise HardwareFaultError(f"GRBL {state.lifecycle.value}", step="await_settled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AdapterTimeoutError("GRBL did not reach Idle")
                self._controller.condition.wait(remaining)

    def _check_position(self, position):
        if not all(
            lo <= value <= hi
            for value, (lo, hi) in zip(position, self._settings["motion"]["limits_mm"], strict=True)
        ):
            raise ValueError("position outside GRBL bounds")

    def _movement_frames(self, request, state):
        if state.homed != self._axes:
            raise HardwareFaultError("home XYZ before movement", step=request.service)
        origin = tuple(state.position.to_dict()[axis] for axis in "XYZ")
        args = request.args
        if request.target_coords is not None:
            target = tuple(float(request.target_coords[axis]) for axis in "XYZ")
        elif request.service == "motion.move_rel":
            target = tuple(
                value + float(args[f"d{axis}"]) for axis, value in zip("xyz", origin, strict=True)
            )
        else:
            z = origin[2] if args.get("z") is None else float(args["z"])
            target = (float(args["x"]), float(args["y"]), z)
        feed = float(
            self._settings["rack"]["xyFeed"] if args.get("speed") is None else args["speed"]
        )
        if not math.isfinite(feed) or feed < 0.000001:
            raise ValueError("GRBL feed must be finite and positive")
        safe = self._safe_z if request.safe_z_mm is None else max(self._safe_z, request.safe_z_mm)
        if request.service == "motion.move_safe":
            waypoints = [(*origin[:2], max(origin[2], safe)), (*target[:2], safe), target]
        else:
            if target[:2] != origin[:2] and min(origin[2], target[2]) < safe:
                raise ValueError("GRBL XY travel requires safe Z")
            waypoints = [target]
        frames = []
        for waypoint in waypoints:
            self._check_position(waypoint)
            axes = " ".join(
                f"{axis}{value:.6f}" for axis, value in zip("XYZ", waypoint, strict=True)
            )
            frames.append(f"G21 G90 G1 {axes} F{feed:.6f}")
        return frames

    def execute(self, request: ActionRequest) -> ActionTelemetry:
        verb = request.service
        if verb == "motion.stop":
            self.abort_now()
            return ActionTelemetry(verb, True, {"realtime": "reset"})
        if verb not in self._moves | {"motion.home_all", "deck.safe_z"}:
            raise UnsupportedCapabilityError(
                f"GRBL motion subset does not support {verb}", step=verb
            )
        deadline = time.monotonic() + request.timeout_s
        with self._actions:
            state = self.poll_state()
            if state.estop_latched:
                raise HardwareFaultError("GRBL halted", step=verb)
            if verb == "deck.safe_z":
                safe = float(request.args["z_mm"])
                self._check_position((0, 0, safe))
                if safe < self._settings["rack"]["safeZ"]:
                    raise ValueError("safe Z below the emulator configuration")
                self._safe_z = safe
                return ActionTelemetry(verb, True, {"safe_z": safe})
            frames = ["$H"] if verb == "motion.home_all" else self._movement_frames(request, state)
            return ActionTelemetry(
                verb,
                True,
                {"frames": frames, "replies": [self._submit(frame, deadline) for frame in frames]},
            )

    def _submit(self, frame, deadline):
        replies = []
        controller = self._controller
        with controller.condition:
            if self._aborted.is_set() or controller.closed:
                raise HardwareFaultError("GRBL halted or closed")
            if time.monotonic() >= deadline:
                raise AdapterTimeoutError("GRBL submission deadline expired", step="execute")
            controller.submit(frame, replies.append)
            while not replies and not controller.alarm and not controller.closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AdapterTimeoutError("GRBL response timed out", step="execute")
                controller.condition.wait(remaining)
            if self._aborted.is_set() or replies != ["ok"]:
                raise HardwareFaultError(f"GRBL rejected or interrupted {frame}: {replies}")
        return replies[0]

    def apply_fail_safe(self, policy: str, reason: str) -> str:
        if policy == "none":
            return "none"
        if policy not in {"adapter_abort", "abort_m112", "retract_safe_z_or_halt"}:
            raise UnsupportedCapabilityError(f"GRBL cannot apply fail-safe {policy}")
        self.abort_now()
        return "GRBL realtime reset sent"


def create_adapter(config: AdapterConfig) -> AdapterPair:
    root = config.options.get(
        "autodna_root", os.getenv("CLI_ANY_DEVICE_AUTODNA_ROOT", "/workspace/autodna_sim_a")
    )
    try:
        adapter = GrblAdapter(autodna_root=root, time_scale=config.options.get("time_scale", 1.0))
    except (OSError, ImportError) as exc:
        raise AdapterTransportError(
            f"Cannot load AutoDNA GRBL assets: {exc}", step="detect"
        ) from exc
    return AdapterPair(config.target, adapter, adapter, adapter.close)
