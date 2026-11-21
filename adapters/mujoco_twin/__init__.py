"""Wrap the existing AutoDNA CAD model and add logical Jubilee tool state."""

import _imp
import math
import os
import sys
import threading
import time
from importlib import import_module
from pathlib import Path
from typing import ClassVar

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
    DeckState,
    HardwareFaultError,
    Identity,
    LabwareState,
    MachineLifecycle,
    Position,
    ToolPresence,
    UnsupportedCapabilityError,
)


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
        for name in ("common", "sim.model", "sim.twin"):
            module = import_module(name)
            if not Path(module.__file__).resolve().is_relative_to(root):
                raise ImportError(f"{name} is already resolved outside AutoDNA: {root}")
            modules.append(module)
        common, model, twin = modules
        return common.config, common.tube_xy, model.pose, twin.DigitalTwin
    finally:
        sys.dont_write_bytecode = write_setting
        sys.path.pop(0)
        _imp.release_lock()


class TwinAdapter:
    _axes = frozenset("XYZU")
    _capabilities = frozenset(
        {
            "axes_xyz",
            "axes_xyzu",
            "tool_lock_u",
            "collision_proxy",
            "deck_labware",
            "abort_oob",
        }
    )
    _deck_spec = "autodna_deck"
    _labware_spec = "autodna_rack"
    _home_axes: ClassVar[dict[str, str]] = {
        "motion.home_all": "XYZU",
        "motion.home_xyu": "XYU",
        "motion.home_z": "Z",
        "expert.home_u": "U",
    }
    _moves = frozenset(
        {"motion.move_safe", "motion.move_to", "motion.move_rel", "motion.move_well"}
    )

    def __init__(self, *, autodna_root="/workspace/autodna_sim_a"):
        import mujoco

        config, tube_xy, pose, DigitalTwin = _load_autodna(autodna_root)

        self._settings = config("platform")
        self._rack = self._settings["rack"]
        self._model_pose = pose
        self._safe_z = float(self._rack["safeZ"])
        initial = (2.0, 2.0, self._safe_z)
        twin = DigitalTwin(self._settings, initial)
        if twin.model is None:
            raise FileNotFoundError("AutoDNA MuJoCo XML is missing")
        self._model = twin.model
        self._data = twin.data
        self._scratch = mujoco.MjData(self._model)
        self._position = initial
        self._homed = frozenset()
        self._active_tool = None
        self._locked = False
        self._collision_count = 0
        self._actions = threading.Lock()
        self._state = threading.Lock()
        self._settled = threading.Event()
        self._settled.set()
        self._aborted = False
        self._fault = False
        self._identity = None

        # Firmware Z at the modeled tube rim, distinct from the tube's height above the bed.
        rim_z = self._rack["sampleZ"] + self._rack["tube_insert_depth_mm"]
        self._rack_model = LabwareState(
            slot="1",
            spec=self._labware_spec,
            top_z_mm=float(self._rack["tube_height_mm"]),
            well_map={
                label: (*tube_xy(self._rack, label), rim_z)
                for row in range(self._rack["rows"])
                for col in range(1, self._rack["cols"] + 1)
                for label in (f"{chr(65 + row)}{col}",)
            },
        )
        self._deck = DeckState(self._deck_spec, ("1",), {"1": self._rack_model})
        self._pose(initial)

    def capabilities(self) -> frozenset[str]:
        return self._capabilities

    def detect(self) -> Identity:
        self._identity = Identity(
            target="mujoco_twin",
            endpoint="mujoco://local",
            api_mode=ApiDialect.MUJOCO_TWIN,
            firmware="AutoDNA MuJoCo",
            capabilities=self.capabilities(),
            details={"simulated_axes": ["U"], "tool_observation": "logical_simulation"},
        )
        return self._identity

    def preflight(self) -> PreflightReport:
        state = self.poll_state()
        checks = {
            "detected": state.detected,
            "model_loaded": True,
            "no_halt_or_fault": state.lifecycle
            not in {MachineLifecycle.HALTED, MachineLifecycle.FAULT},
            "no_proxy_contacts": state.collision_count == 0,
        }
        return PreflightReport(
            passed=all(checks.values()),
            checks=checks,
            warnings=("Tool attachment and U axis are logical simulations.",),
            blocking_reasons=tuple(name for name, passed in checks.items() if not passed),
        )

    def poll_state(self) -> CanonicalMachineState:
        with self._state:
            lifecycle = MachineLifecycle.IDLE if self._settled.is_set() else MachineLifecycle.MOVING
            if self._aborted:
                lifecycle = MachineLifecycle.HALTED
            elif self._fault:
                lifecycle = MachineLifecycle.FAULT
            deck = DeckState(
                self._deck.spec,
                self._deck.slots,
                {
                    slot: LabwareState(item.slot, item.spec, item.top_z_mm, item.well_map.copy())
                    for slot, item in self._deck.labware.items()
                },
            )
            return CanonicalMachineState(
                lifecycle=lifecycle,
                position=Position(*self._position),
                homed=frozenset() if self._aborted or self._fault else self._homed,
                active_tool=self._active_tool,
                tool_presence=ToolPresence.NONE
                if self._active_tool is None
                else ToolPresence.LOADED,
                tool_locked=self._locked,
                estop_latched=self._aborted,
                safe_z_mm=self._safe_z,
                detected=self._identity is not None,
                identity=self._identity,
                deck=deck,
                capabilities=self.capabilities(),
                collision_count=self._collision_count,
            )

    def joints_mm(self):
        with self._state:
            return tuple(float(self._data.joint(axis).qpos[0]) * 1000 for axis in "xyz")

    def abort_now(self) -> AbortResult:
        start = time.monotonic()
        # This mutex protects published values only. Physics and action I/O run outside it.
        with self._state:
            self._aborted = True
            self._settled.set()
        return AbortResult(True, (time.monotonic() - start) * 1000, "mujoco.stop_trajectory")

    def reset_estop(self) -> CanonicalMachineState:
        with self._actions, self._state:
            if self._aborted:
                self._homed = frozenset()
                self._aborted = False
        return self.poll_state()

    def await_settled(self, timeout_s: float) -> CanonicalMachineState:
        if not self._settled.wait(timeout_s):
            raise AdapterTimeoutError("MuJoCo trajectory did not settle")
        state = self.poll_state()
        if state.lifecycle != MachineLifecycle.IDLE:
            raise HardwareFaultError(f"MuJoCo {state.lifecycle.value}", step="await_settled")
        return state

    def _check_running(self):
        if self._aborted or self._fault:
            raise HardwareFaultError("MuJoCo halted or faulted")

    def _pose(self, position):
        self._check_running()
        self._model_pose(self._model, self._scratch, position)
        with self._state:
            self._check_running()
            if self._scratch.ncon:
                self._collision_count += int(self._scratch.ncon)
                self._fault = True
                raise HardwareFaultError("MuJoCo trajectory hit a contact proxy")
            self._position = position
            self._data, self._scratch = self._scratch, self._data

    def _bounds(self, position):
        limits = self._settings["motion"]["limits_mm"]
        if not all(lo <= value <= hi for value, (lo, hi) in zip(position, limits, strict=True)):
            raise ValueError("position outside MuJoCo bounds")

    def _paths(self, request):
        verb, args = request.service, request.args
        x, y, z = self._position
        safe = self._safe_z if request.safe_z_mm is None else max(self._safe_z, request.safe_z_mm)
        if verb == "motion.home_all":
            return [(x, y, safe), (0.0, 0.0, safe)]
        if verb == "motion.home_z":
            return [(x, y, safe)]
        if verb in {"motion.home_xyu", "expert.home_u"}:
            if z < safe:
                raise HardwareFaultError("raise to safe Z before XYU home", step=verb)
            return [(0.0, 0.0, z)] if verb == "motion.home_xyu" else []
        if verb == "motion.move_well":
            labware = self._deck.labware.get(str(args["slot"]))
            if labware is None or args["well"] not in labware.well_map:
                raise ValueError("well not present in the modeled AutoDNA rack")
            tx, ty, rim = labware.well_map[args["well"]]
            tz = rim + float(args.get("dz", 5.0))
        elif verb == "motion.move_rel":
            tx, ty, tz = (
                value + float(args[f"d{axis}"])
                for axis, value in zip("xyz", self._position, strict=True)
            )
        else:
            tx, ty = float(args["x"]), float(args["y"])
            tz = z if args.get("z") is None else float(args["z"])
        if verb in {"motion.move_well", "motion.move_safe"}:
            return [(x, y, max(z, safe)), (tx, ty, safe), (tx, ty, tz)]
        if (tx, ty) != (x, y) and min(z, tz) < safe:
            raise ValueError("XY travel requires safe Z")
        return [(tx, ty, tz)]

    def _travel(self, waypoints, deadline):
        for target in waypoints:
            start = self._position
            steps = max(1, math.ceil(math.dist(start, target)))
            for step in range(1, steps + 1):
                if time.monotonic() >= deadline:
                    raise AdapterTimeoutError("MuJoCo trajectory timed out", step="execute")
                if step == steps:
                    self._pose(target)
                else:
                    position = tuple(
                        a + (b - a) * step / steps for a, b in zip(start, target, strict=True)
                    )
                    self._pose(position)

    def _execute_motion(self, request, deadline):
        verb = request.service
        if verb in self._moves and self._homed != self._axes:
            raise HardwareFaultError("home all axes before motion", step=verb)
        if verb in self._home_axes and self._active_tool is not None:
            raise HardwareFaultError("park tool before homing", step=verb)
        waypoints = self._paths(request)
        for waypoint in waypoints:
            self._bounds(waypoint)
        self._settled.clear()
        try:
            self._travel(waypoints, deadline)
            with self._state:
                self._check_running()
                self._homed = self._homed.union(self._home_axes.get(verb, ""))
        finally:
            self._settled.set()
        return {"waypoints": waypoints}

    def _execute_tool(self, verb, args):
        if self._homed != self._axes:
            raise HardwareFaultError("home all axes before tool change", step=verb)
        if verb == "tool.pickup":
            if self._active_tool is not None:
                raise HardwareFaultError("park the active tool before pickup", step=verb)
            if type(args["tool_id"]) is not int or args["tool_id"] != 0:
                raise ValueError("only logical tool 0 is modeled")
            self._active_tool, self._locked = 0, True
        elif verb == "tool.park":
            if self._active_tool is None:
                raise HardwareFaultError("no tool to park", step=verb)
            self._active_tool, self._locked = None, False
        else:
            if self._active_tool is not None:
                raise HardwareFaultError("park tool before testing the logical lock", step=verb)
            self._locked = verb == "expert.tool_lock"
        return {"logical_tool": self._active_tool, "logical_lock": self._locked}

    def _execute_configuration(self, verb, args):
        if verb == "deck.safe_z":
            safe = float(args["z_mm"])
            self._bounds((*self._position[:2], safe))
            if safe < self._rack["safeZ"]:
                raise ValueError("safe Z below the model's minimum clearance")
            self._safe_z = safe
            return {"safe_z": safe}
        if verb == "deck.load_deck" and args["spec"] == self._deck_spec:
            self._deck = DeckState(self._deck_spec, ("1",))
            return {"slots": ["1"], "safe_z": self._safe_z}
        if (
            verb == "deck.load_labware"
            and str(args["slot"]) == "1"
            and args["spec"] == self._labware_spec
        ):
            self._deck = DeckState(self._deck_spec, ("1",), {"1": self._rack_model})
            return self._rack_model.to_dict()
        raise UnsupportedCapabilityError(
            f"MuJoCo supports deck {self._deck_spec!r} and "
            f"slot 1 labware {self._labware_spec!r} only",
            step=verb,
        )

    def execute(self, request: ActionRequest) -> ActionTelemetry:
        verb = request.service
        if verb == "motion.stop":
            self.abort_now()
            return ActionTelemetry(verb, True, {"command": "stop_trajectory"})
        deadline = time.monotonic() + request.timeout_s
        with self._actions:
            self._check_running()
            if verb in self._home_axes or verb in self._moves:
                raw = self._execute_motion(request, deadline)
            else:
                with self._state:
                    self._check_running()
                    if verb in {
                        "tool.pickup",
                        "tool.park",
                        "expert.tool_lock",
                        "expert.tool_unlock",
                    }:
                        raw = self._execute_tool(verb, request.args)
                    elif verb in {"deck.load_deck", "deck.load_labware", "deck.safe_z"}:
                        raw = self._execute_configuration(verb, request.args)
                    else:
                        raise UnsupportedCapabilityError(f"MuJoCo cannot execute {verb}", step=verb)
        return ActionTelemetry(verb, True, raw)

    def apply_fail_safe(self, policy: str, reason: str) -> str:
        if policy == "none":
            return "none"
        if policy not in {"adapter_abort", "abort_m112", "retract_safe_z_or_halt"}:
            raise UnsupportedCapabilityError(f"MuJoCo cannot apply fail-safe {policy}")
        self.abort_now()
        return "MuJoCo trajectory halted"


def create_adapter(config: AdapterConfig) -> AdapterPair:
    root = config.options.get(
        "autodna_root", os.getenv("CLI_ANY_DEVICE_AUTODNA_ROOT", "/workspace/autodna_sim_a")
    )
    try:
        adapter = TwinAdapter(autodna_root=root)
    except (OSError, ImportError) as exc:
        raise AdapterTransportError(
            f"Cannot load AutoDNA MuJoCo assets: {exc}", step="detect"
        ) from exc
    return AdapterPair(config.target, adapter, adapter)
