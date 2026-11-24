"""Spatial motion and tool safety predicates and coordinate resolvers."""

from __future__ import annotations

from functools import partial
from typing import Any

from core_types import CanonicalMachineState, ErrorCode, ExitCode, ToolPresence
from safety_kernel.domains import DomainExtension, PreconditionContext
from safety_kernel.predicates import GateRejection, PostconditionError


def _absolute_coords(args: dict[str, Any], state: CanonicalMachineState) -> dict[str, float]:
    pos = state.position
    default_z = pos.z if pos.z is not None else (state.safe_z_mm or 0.0)
    return {
        "X": float(args["x"]) if "x" in args else (pos.x or 0.0),
        "Y": float(args["y"]) if "y" in args else (pos.y or 0.0),
        "Z": float(args["z"]) if "z" in args else default_z,
    }


def _relative_coords(args: dict[str, Any], state: CanonicalMachineState) -> dict[str, float]:
    pos = state.position
    return {
        "X": (pos.x or 0.0) + float(args.get("dx", 0.0)),
        "Y": (pos.y or 0.0) + float(args.get("dy", 0.0)),
        "Z": (pos.z or 0.0) + float(args.get("dz", 0.0)),
    }


def _require_homed(ctx: PreconditionContext, *, axes: str) -> None:
    missing = set(axes) - ctx.state.homed
    if missing:
        raise GateRejection(
            ErrorCode.E_NOT_HOMED,
            ExitCode.SAFETY_DENIED,
            f"precondition:homed_{axes.lower()}",
            f"Required axes not homed: {sorted(missing)}.",
        )


def _tool_presence_known(ctx: PreconditionContext) -> None:
    if ctx.state.tool_presence == ToolPresence.UNKNOWN:
        raise GateRejection(
            ErrorCode.E_UNKNOWN_STATE,
            ExitCode.SAFETY_DENIED,
            "precondition:tool_presence_known",
            "Tool presence is UNKNOWN; sensor verification required before actuation.",
        )


def _require_empty_carriage(ctx: PreconditionContext) -> None:
    state = ctx.state
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


def _tool_loaded(ctx: PreconditionContext) -> None:
    if ctx.state.tool_presence != ToolPresence.LOADED or ctx.state.active_tool is None:
        raise GateRejection(
            ErrorCode.E_NO_TOOL_LOADED,
            ExitCode.SAFETY_DENIED,
            "precondition:tool_loaded",
            "No tool is currently mounted.",
        )


def _tool_id_known(ctx: PreconditionContext) -> None:
    tool_id = int(ctx.args["tool_id"])
    if ctx.device.tools and tool_id not in ctx.device.tools:
        raise GateRejection(
            ErrorCode.E_INVALID_ARGS,
            ExitCode.INVALID_USAGE,
            "precondition:tool_id_known",
            f"Tool ID {tool_id} is not declared in device.yaml.",
        )


def _z_at_or_above_safe_z(ctx: PreconditionContext) -> None:
    safe_z, z = ctx.state.safe_z_mm, ctx.state.position.z
    if safe_z is not None and (z is None or z < safe_z):
        raise GateRejection(
            ErrorCode.E_UNSAFE_Z,
            ExitCode.SAFETY_DENIED,
            "precondition:z_at_or_above_safe_z",
            f"Current Z ({z}) is below safe Z ({safe_z} mm).",
        )


def _xy_requires_safe_z(ctx: PreconditionContext) -> None:
    safe_z, target = ctx.state.safe_z_mm, ctx.target_coords
    if safe_z is None or target is None:
        return
    pos = ctx.state.position
    cur_x = pos.x if pos.x is not None else target["X"]
    cur_y = pos.y if pos.y is not None else target["Y"]
    xy_moves = abs(target["X"] - cur_x) > 1e-6 or abs(target["Y"] - cur_y) > 1e-6
    tgt_z = target["Z"]
    if xy_moves and ((pos.z is not None and pos.z < safe_z) or tgt_z < safe_z):
        raise GateRejection(
            ErrorCode.E_UNSAFE_Z,
            ExitCode.SAFETY_DENIED,
            "precondition:xy_requires_safe_z",
            f"XY translation denied below safe Z ({safe_z} mm); "
            f"current Z={pos.z}, target Z={tgt_z}. Use motion.move_safe.",
        )


def _safe_z_above_clearance(ctx: PreconditionContext) -> None:
    requested_z = float(ctx.args["z_mm"])
    deck = ctx.state.deck
    tallest_labware = deck.max_labware_top_z_mm if deck is not None else 0.0
    active_tool = ctx.state.active_tool
    tool = ctx.device.tools.get(active_tool) if active_tool is not None else None
    tool_len = tool.length_mm if tool is not None else 0.0
    margin = ctx.device.min_safe_z_margin_mm
    min_required = tallest_labware + tool_len + margin
    if requested_z < min_required:
        raise GateRejection(
            ErrorCode.E_SAFE_Z_TOO_LOW,
            ExitCode.SAFETY_DENIED,
            "precondition:safe_z_above_clearance",
            f"Requested safe_z={requested_z} mm is below required clearance "
            f"{min_required} mm (labware={tallest_labware} + tool={tool_len} + margin={margin}).",
        )


def _target_in_bounds(ctx: PreconditionContext) -> None:
    if ctx.target_coords is None:
        return
    for axis_name, coord in ctx.target_coords.items():
        axis_lim = ctx.device.axes.get(axis_name)
        if axis_lim is not None and (coord < axis_lim.min_mm or coord > axis_lim.max_mm):
            raise GateRejection(
                ErrorCode.E_OUT_OF_BOUNDS,
                ExitCode.SAFETY_DENIED,
                "precondition:target_in_bounds",
                f"Target {axis_name}={coord} mm outside soft limits "
                f"[{axis_lim.min_mm}, {axis_lim.max_mm}] mm.",
            )


def _target_outside_forbidden_zones(ctx: PreconditionContext) -> None:
    target = ctx.target_coords
    if target is None:
        return
    tx, ty, tz = target["X"], target["Y"], target["Z"]
    for zone in ctx.device.forbidden_zones:
        if zone.contains(tx, ty, tz):
            raise GateRejection(
                ErrorCode.E_OUT_OF_BOUNDS,
                ExitCode.SAFETY_DENIED,
                "precondition:target_outside_forbidden_zones",
                f"Target ({tx}, {ty}, {tz}) enters forbidden zone '{zone.name}': {zone.reason}",
            )


def _verify_homed(_args: dict[str, Any], state: CanonicalMachineState, *, axes: str) -> None:
    missing = set(axes) - state.homed
    if missing:
        if axes == "Z":
            raise PostconditionError("Postcondition failed: Z axis not homed.")
        raise PostconditionError(f"Postcondition failed: axes not homed {sorted(missing)}.")


def _safe_z_matches(args: dict[str, Any], state: CanonicalMachineState) -> None:
    expected_z = float(args["z_mm"])
    if state.safe_z_mm is None or abs(state.safe_z_mm - expected_z) > 1e-3:
        raise PostconditionError(
            f"Expected safe_z={expected_z}, settled state has {state.safe_z_mm}."
        )


def _active_tool_matches(args: dict[str, Any], state: CanonicalMachineState) -> None:
    expected_tool = int(args["tool_id"])
    if state.active_tool != expected_tool:
        raise PostconditionError(f"Expected active_tool={expected_tool}, got {state.active_tool}.")


def _tool_locked(_args: dict[str, Any], state: CanonicalMachineState) -> None:
    if state.tool_locked is not True:
        raise PostconditionError("Expected tool_locked=True after tool operation.")


def _verify_empty_carriage(_args: dict[str, Any], state: CanonicalMachineState) -> None:
    if state.active_tool is not None or state.tool_presence == ToolPresence.LOADED:
        raise PostconditionError(f"Expected no tool mounted, but active_tool={state.active_tool}.")


MOTION_3D = DomainExtension(
    preconditions={
        "homed_xyz": partial(_require_homed, axes="XYZ"),
        "homed_xyzu": partial(_require_homed, axes="XYZU"),
        "homed_z": partial(_require_homed, axes="Z"),
        "tool_presence_known": _tool_presence_known,
        "no_tool_loaded": _require_empty_carriage,
        "tool_loaded": _tool_loaded,
        "tool_id_known": _tool_id_known,
        "z_at_or_above_safe_z": _z_at_or_above_safe_z,
        "xy_requires_safe_z": _xy_requires_safe_z,
        "safe_z_above_clearance": _safe_z_above_clearance,
        "target_in_bounds": _target_in_bounds,
        "target_outside_forbidden_zones": _target_outside_forbidden_zones,
    },
    postconditions={
        "homed_xyz": partial(_verify_homed, axes="XYZ"),
        "homed_xyu": partial(_verify_homed, axes="XYU"),
        "homed_z": partial(_verify_homed, axes="Z"),
        "safe_z_matches": _safe_z_matches,
        "active_tool_matches": _active_tool_matches,
        "tool_locked": _tool_locked,
        "no_tool_loaded": _verify_empty_carriage,
    },
    coord_resolver={
        "motion.move_safe": _absolute_coords,
        "motion.move_to": _absolute_coords,
        "motion.move_rel": _relative_coords,
    },
)
