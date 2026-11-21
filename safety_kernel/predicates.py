"""Pure argument validation, coordinate resolution, and pre/postcondition evaluation.

Strictly device-agnostic: evaluates declarative predicates from DeviceBundle against
CanonicalMachineState without any target-name branching or vendor imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core_types import (
    ArgSpec,
    CanonicalMachineState,
    DeviceSpec,
    ErrorCode,
    ExitCode,
    MachineLifecycle,
    ServiceSpec,
    ToolPresence,
)


@dataclass(frozen=True, slots=True)
class GateRejection(Exception):
    """Structured rejection raised before any driver actuation occurs."""

    code: ErrorCode
    exit_code: ExitCode
    step: str
    reason: str


class PostconditionError(Exception):
    """Raised when settled machine state fails a service's declared postcondition."""


def _coerce_arg(spec: ArgSpec, raw: Any) -> Any:
    if spec.arg_type == "string":
        if isinstance(raw, (dict, list)):
            raise GateRejection(
                ErrorCode.E_INVALID_ARGS,
                ExitCode.INVALID_USAGE,
                "arg_validation",
                f"Argument '{spec.name}' expects string.",
            )
        return str(raw)
    if spec.arg_type == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str) and raw.lower() in ("true", "false", "1", "0"):
            return raw.lower() in ("true", "1")
        raise GateRejection(
            ErrorCode.E_INVALID_ARGS,
            ExitCode.INVALID_USAGE,
            "arg_validation",
            f"Argument '{spec.name}' expects boolean.",
        )
    if spec.arg_type == "int":
        if isinstance(raw, bool):
            raise GateRejection(
                ErrorCode.E_INVALID_ARGS,
                ExitCode.INVALID_USAGE,
                "arg_validation",
                f"Argument '{spec.name}' expects int, not bool.",
            )
        try:
            return int(raw)
        except (TypeError, ValueError) as exc:
            raise GateRejection(
                ErrorCode.E_INVALID_ARGS,
                ExitCode.INVALID_USAGE,
                "arg_validation",
                f"Argument '{spec.name}' expects int.",
            ) from exc
    if spec.arg_type == "float":
        if isinstance(raw, bool):
            raise GateRejection(
                ErrorCode.E_INVALID_ARGS,
                ExitCode.INVALID_USAGE,
                "arg_validation",
                f"Argument '{spec.name}' expects float, not bool.",
            )
        try:
            return float(raw)
        except (TypeError, ValueError) as exc:
            raise GateRejection(
                ErrorCode.E_INVALID_ARGS,
                ExitCode.INVALID_USAGE,
                "arg_validation",
                f"Argument '{spec.name}' expects float.",
            ) from exc
    raise GateRejection(
        ErrorCode.E_INVALID_ARGS,
        ExitCode.INVALID_USAGE,
        "arg_validation",
        f"Unsupported arg_type '{spec.arg_type}' on '{spec.name}'.",
    )


def validate_and_coerce_args(
    service: ServiceSpec,
    raw_args: dict[str, Any],
    device: DeviceSpec,
) -> dict[str, Any]:
    """Validate required args, types, declared bounds, and device safety_limits."""
    spec_by_name = {a.name: a for a in service.args}
    unknown = set(raw_args) - set(spec_by_name)
    if unknown:
        raise GateRejection(
            ErrorCode.E_INVALID_ARGS,
            ExitCode.INVALID_USAGE,
            "arg_validation",
            f"Unknown argument(s) for '{service.name}': {sorted(unknown)}.",
        )

    coerced: dict[str, Any] = {}
    for arg_spec in service.args:
        if arg_spec.name not in raw_args or raw_args[arg_spec.name] is None:
            if arg_spec.required:
                raise GateRejection(
                    ErrorCode.E_INVALID_ARGS,
                    ExitCode.INVALID_USAGE,
                    "arg_validation",
                    f"Missing required argument '{arg_spec.name}' for '{service.name}'.",
                )
            if arg_spec.default is not None:
                coerced[arg_spec.name] = arg_spec.default
            continue

        val = _coerce_arg(arg_spec, raw_args[arg_spec.name])
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            if arg_spec.min_value is not None and val < arg_spec.min_value:
                raise GateRejection(
                    ErrorCode.E_OUT_OF_BOUNDS,
                    ExitCode.SAFETY_DENIED,
                    "bounds_check",
                    f"Argument '{arg_spec.name}'={val} below minimum {arg_spec.min_value}.",
                )
            if arg_spec.max_value is not None and val > arg_spec.max_value:
                raise GateRejection(
                    ErrorCode.E_OUT_OF_BOUNDS,
                    ExitCode.SAFETY_DENIED,
                    "bounds_check",
                    f"Argument '{arg_spec.name}'={val} exceeds maximum {arg_spec.max_value}.",
                )
            # Also enforce device-level generic safety_limits matching this parameter name
            if arg_spec.name in device.safety_limits:
                lim = device.safety_limits[arg_spec.name]
                if val < lim.min_value or val > lim.max_value:
                    raise GateRejection(
                        ErrorCode.E_OUT_OF_BOUNDS,
                        ExitCode.SAFETY_DENIED,
                        "safety_limit_check",
                        f"Argument '{arg_spec.name}'={val} outside device safety limit "
                        f"[{lim.min_value}, {lim.max_value}] {lim.unit}.",
                    )
        coerced[arg_spec.name] = val
    return coerced


def resolve_target_coords(
    service: ServiceSpec,
    args: dict[str, Any],
    state: CanonicalMachineState,
) -> dict[str, float] | None:
    """Resolve destination XYZ coordinates when a motion service targets spatial coordinates."""
    pos = state.position
    if service.name == "motion.move_well":
        if state.deck is None:
            return None
        slot = str(args.get("slot", ""))
        well = str(args.get("well", ""))
        lw = state.deck.labware.get(slot)
        if lw is None or well not in lw.well_map:
            return None
        wx, wy, wz = lw.well_map[well]
        dz = float(args.get("dz", 0.0))
        return {"X": wx, "Y": wy, "Z": wz + dz}

    if service.name in ("motion.move_safe", "motion.move_to"):
        tx = float(args["x"]) if "x" in args else (pos.x or 0.0)
        ty = float(args["y"]) if "y" in args else (pos.y or 0.0)
        default_z = pos.z if pos.z is not None else (state.safe_z_mm or 0.0)
        tz = float(args["z"]) if "z" in args else default_z
        return {"X": tx, "Y": ty, "Z": tz}

    if service.name == "motion.move_rel":
        tx = (pos.x or 0.0) + float(args.get("dx", 0.0))
        ty = (pos.y or 0.0) + float(args.get("dy", 0.0))
        tz = (pos.z or 0.0) + float(args.get("dz", 0.0))
        return {"X": tx, "Y": ty, "Z": tz}

    return None


def _active_tool_length(state: CanonicalMachineState, device: DeviceSpec) -> float:
    if state.active_tool is not None and state.active_tool in device.tools:
        return device.tools[state.active_tool].length_mm
    return 0.0


def evaluate_preconditions(
    service: ServiceSpec,
    args: dict[str, Any],
    state: CanonicalMachineState,
    device: DeviceSpec,
    target_coords: dict[str, float] | None,
) -> None:
    """Evaluate all declared service preconditions; default-deny on any unmet or unknown rule."""
    for pred in service.preconditions:
        if pred == "detected":
            if not state.detected:
                raise GateRejection(
                    ErrorCode.E_NOT_DETECTED,
                    ExitCode.SAFETY_DENIED,
                    "precondition:detected",
                    "Machine has not passed sys.detect yet; run sys.detect before actuation.",
                )
            if state.identity is not None and not state.identity.hookup_verified:
                raise GateRejection(
                    ErrorCode.E_NOT_DETECTED,
                    ExitCode.SAFETY_DENIED,
                    "precondition:detected",
                    "Hardware hookup is not verified; complete hookup detection first.",
                )
        elif pred == "not_estop_latched":
            if state.estop_latched or state.lifecycle == MachineLifecycle.HALTED:
                raise GateRejection(
                    ErrorCode.E_ESTOP_LATCHED,
                    ExitCode.ESTOP_LATCHED,
                    "precondition:not_estop_latched",
                    "Emergency stop is latched; inspect machine and run confirmed sys.reset_estop.",
                )
        elif pred == "tool_presence_known":
            if state.tool_presence == ToolPresence.UNKNOWN:
                raise GateRejection(
                    ErrorCode.E_UNKNOWN_STATE,
                    ExitCode.SAFETY_DENIED,
                    "precondition:tool_presence_known",
                    "Tool presence is UNKNOWN; sensor verification required before actuation.",
                )
        elif pred == "homed_xyz":
            missing = {"X", "Y", "Z"} - set(state.homed)
            if missing:
                raise GateRejection(
                    ErrorCode.E_NOT_HOMED,
                    ExitCode.SAFETY_DENIED,
                    "precondition:homed_xyz",
                    f"Required axes not homed: {sorted(missing)}.",
                )
        elif pred == "homed_xyzu":
            missing = {"X", "Y", "Z", "U"} - set(state.homed)
            if missing:
                raise GateRejection(
                    ErrorCode.E_NOT_HOMED,
                    ExitCode.SAFETY_DENIED,
                    "precondition:homed_xyzu",
                    f"Required axes not homed: {sorted(missing)}.",
                )
        elif pred == "no_tool_loaded":
            if state.tool_presence == ToolPresence.UNKNOWN:
                raise GateRejection(
                    ErrorCode.E_UNKNOWN_STATE,
                    ExitCode.SAFETY_DENIED,
                    "precondition:no_tool_loaded",
                    "Tool presence is UNKNOWN; refusing operation requiring empty carriage.",
                )
            if state.tool_presence == ToolPresence.LOADED or state.active_tool is not None:
                raise GateRejection(
                    ErrorCode.E_TOOL_ALREADY_LOADED,
                    ExitCode.SAFETY_DENIED,
                    "precondition:no_tool_loaded",
                    f"Tool {state.active_tool} is already mounted; park current tool first.",
                )
        elif pred == "tool_loaded":
            if state.tool_presence != ToolPresence.LOADED or state.active_tool is None:
                raise GateRejection(
                    ErrorCode.E_NO_TOOL_LOADED,
                    ExitCode.SAFETY_DENIED,
                    "precondition:tool_loaded",
                    "No tool is currently mounted.",
                )
        elif pred == "tool_id_known":
            tool_id = int(args["tool_id"])
            if device.tools and tool_id not in device.tools:
                raise GateRejection(
                    ErrorCode.E_INVALID_ARGS,
                    ExitCode.INVALID_USAGE,
                    "precondition:tool_id_known",
                    f"Tool ID {tool_id} is not declared in device.yaml.",
                )
        elif pred == "deck_loaded":
            if state.deck is None:
                raise GateRejection(
                    ErrorCode.E_DECK_NOT_LOADED,
                    ExitCode.SAFETY_DENIED,
                    "precondition:deck_loaded",
                    "Deck geometry is not loaded; run deck.load_deck first.",
                )
        elif pred == "labware_loaded":
            slot = str(args.get("slot", ""))
            if state.deck is None or slot not in state.deck.labware:
                raise GateRejection(
                    ErrorCode.E_LABWARE_NOT_LOADED,
                    ExitCode.SAFETY_DENIED,
                    "precondition:labware_loaded",
                    f"No labware loaded in slot '{slot}'.",
                )
        elif pred == "well_exists":
            slot = str(args.get("slot", ""))
            well = str(args.get("well", ""))
            if state.deck is None or slot not in state.deck.labware:
                raise GateRejection(
                    ErrorCode.E_LABWARE_NOT_LOADED,
                    ExitCode.SAFETY_DENIED,
                    "precondition:well_exists",
                    f"No labware loaded in slot '{slot}'.",
                )
            if well not in state.deck.labware[slot].well_map:
                raise GateRejection(
                    ErrorCode.E_WELL_NOT_FOUND,
                    ExitCode.SAFETY_DENIED,
                    "precondition:well_exists",
                    f"Well '{well}' not found in slot '{slot}'.",
                )
        elif pred == "z_at_or_above_safe_z":
            safe_z = state.safe_z_mm
            if safe_z is not None and (state.position.z is None or state.position.z < safe_z):
                raise GateRejection(
                    ErrorCode.E_UNSAFE_Z,
                    ExitCode.SAFETY_DENIED,
                    "precondition:z_at_or_above_safe_z",
                    f"Current Z ({state.position.z}) is below safe Z ({safe_z} mm).",
                )
        elif pred == "xy_requires_safe_z":
            safe_z = state.safe_z_mm
            if safe_z is not None and target_coords is not None:
                cur_x = state.position.x if state.position.x is not None else target_coords["X"]
                cur_y = state.position.y if state.position.y is not None else target_coords["Y"]
                xy_moves = (
                    abs(target_coords["X"] - cur_x) > 1e-6
                    or abs(target_coords["Y"] - cur_y) > 1e-6
                )
                cur_z = state.position.z
                tgt_z = target_coords["Z"]
                if xy_moves and ((cur_z is not None and cur_z < safe_z) or tgt_z < safe_z):
                    raise GateRejection(
                        ErrorCode.E_UNSAFE_Z,
                        ExitCode.SAFETY_DENIED,
                        "precondition:xy_requires_safe_z",
                        f"XY translation denied below safe Z ({safe_z} mm); "
                        f"current Z={cur_z}, target Z={tgt_z}. Use motion.move_safe.",
                    )
        elif pred == "safe_z_above_clearance":
            requested_z = float(args["z_mm"])
            tallest_labware = state.deck.max_labware_top_z_mm if state.deck is not None else 0.0
            tool_len = _active_tool_length(state, device)
            min_required = tallest_labware + tool_len + device.min_safe_z_margin_mm
            if requested_z < min_required:
                raise GateRejection(
                    ErrorCode.E_SAFE_Z_TOO_LOW,
                    ExitCode.SAFETY_DENIED,
                    "precondition:safe_z_above_clearance",
                    f"Requested safe_z={requested_z} mm is below required clearance "
                    f"{min_required} mm (labware={tallest_labware} + tool={tool_len} "
                    f"+ margin={device.min_safe_z_margin_mm}).",
                )
        elif pred == "target_in_bounds":
            if target_coords is not None:
                for axis_name, coord in target_coords.items():
                    axis_lim = device.axes.get(axis_name)
                    if axis_lim is not None and (
                        coord < axis_lim.min_mm or coord > axis_lim.max_mm
                    ):
                        raise GateRejection(
                            ErrorCode.E_OUT_OF_BOUNDS,
                            ExitCode.SAFETY_DENIED,
                            "precondition:target_in_bounds",
                            f"Target {axis_name}={coord} mm outside soft limits "
                            f"[{axis_lim.min_mm}, {axis_lim.max_mm}] mm.",
                        )
        elif pred == "target_outside_forbidden_zones":
            if target_coords is not None:
                tx = target_coords["X"]
                ty = target_coords["Y"]
                tz = target_coords["Z"]
                for zone in device.forbidden_zones:
                    if zone.contains(tx, ty, tz):
                        raise GateRejection(
                            ErrorCode.E_OUT_OF_BOUNDS,
                            ExitCode.SAFETY_DENIED,
                            "precondition:target_outside_forbidden_zones",
                            f"Target ({tx}, {ty}, {tz}) enters forbidden zone '{zone.name}': "
                            f"{zone.reason}",
                        )
        elif pred.startswith("prop_eq:"):
            expr = pred.split(":", 1)[1]
            key, expected = expr.split("=", 1)
            actual = state.properties.get(key)
            if str(actual).lower() != expected.lower():
                raise GateRejection(
                    ErrorCode.E_PRECONDITION_FAILED,
                    ExitCode.SAFETY_DENIED,
                    f"precondition:{pred}",
                    f"Property '{key}' is {actual!r}, expected {expected!r}.",
                )
        elif pred.startswith("prop_set:"):
            key = pred.split(":", 1)[1]
            if key not in state.properties or state.properties[key] is None:
                raise GateRejection(
                    ErrorCode.E_PRECONDITION_FAILED,
                    ExitCode.SAFETY_DENIED,
                    f"precondition:{pred}",
                    f"Required state property '{key}' is not set.",
                )
        elif pred.startswith("flag:"):
            flag_name = pred.split(":", 1)[1]
            if flag_name not in state.verified_flags:
                raise GateRejection(
                    ErrorCode.E_PRECONDITION_FAILED,
                    ExitCode.SAFETY_DENIED,
                    f"precondition:{pred}",
                    f"Required verified flag '{flag_name}' is not present.",
                )
        else:
            # Default deny on unknown precondition predicate
            raise GateRejection(
                ErrorCode.E_PRECONDITION_FAILED,
                ExitCode.SAFETY_DENIED,
                f"precondition:{pred}",
                f"Unknown precondition predicate '{pred}' (default deny).",
            )


def evaluate_postconditions(
    service: ServiceSpec,
    args: dict[str, Any],
    state: CanonicalMachineState,
) -> None:
    """Verify settled machine state satisfies all declared postconditions."""
    for post in service.postconditions:
        if post == "detected":
            if not state.detected:
                raise PostconditionError("Machine state is not marked detected after sys.detect.")
        elif post == "idle":
            if state.lifecycle != MachineLifecycle.IDLE:
                raise PostconditionError(
                    f"Expected lifecycle IDLE after '{service.name}', got {state.lifecycle.value}."
                )
        elif post == "estop_latched":
            if not state.estop_latched:
                raise PostconditionError("Expected estop_latched=True after sys.abort.")
        elif post == "not_estop_latched":
            if state.estop_latched:
                raise PostconditionError("Expected estop_latched=False after sys.reset_estop.")
        elif post == "deck_loaded":
            if state.deck is None:
                raise PostconditionError("Deck was not loaded in machine state.")
        elif post == "labware_loaded":
            slot = str(args.get("slot", ""))
            if state.deck is None or slot not in state.deck.labware:
                raise PostconditionError(f"Labware was not registered in slot '{slot}'.")
        elif post == "safe_z_matches":
            expected_z = float(args["z_mm"])
            if state.safe_z_mm is None or abs(state.safe_z_mm - expected_z) > 1e-3:
                raise PostconditionError(
                    f"Expected safe_z={expected_z}, settled state has {state.safe_z_mm}."
                )
        elif post == "homed_xyz":
            missing = {"X", "Y", "Z"} - set(state.homed)
            if missing:
                raise PostconditionError(f"Postcondition failed: axes not homed {sorted(missing)}.")
        elif post == "homed_xyu":
            missing = {"X", "Y", "U"} - set(state.homed)
            if missing:
                raise PostconditionError(f"Postcondition failed: axes not homed {sorted(missing)}.")
        elif post == "homed_z":
            if "Z" not in state.homed:
                raise PostconditionError("Postcondition failed: Z axis not homed.")
        elif post == "active_tool_matches":
            expected_tool = int(args["tool_id"])
            if state.active_tool != expected_tool:
                raise PostconditionError(
                    f"Expected active_tool={expected_tool}, got {state.active_tool}."
                )
        elif post == "tool_locked":
            if state.tool_locked is not True:
                raise PostconditionError("Expected tool_locked=True after tool operation.")
        elif post == "no_tool_loaded":
            if state.active_tool is not None or state.tool_presence == ToolPresence.LOADED:
                raise PostconditionError(
                    f"Expected no tool mounted, but active_tool={state.active_tool}."
                )
        else:
            raise PostconditionError(f"Unknown postcondition predicate '{post}' (default deny).")
