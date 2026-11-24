"""Deck and labware safety predicates and well coordinate resolution."""

from __future__ import annotations

from typing import Any

from core_types import CanonicalMachineState, ErrorCode, ExitCode
from safety_kernel.domains import DomainExtension, PreconditionContext
from safety_kernel.predicates import GateRejection, PostconditionError


def _well_coords(args: dict[str, Any], state: CanonicalMachineState) -> dict[str, float] | None:
    if state.deck is None:
        return None
    slot, well = str(args.get("slot", "")), str(args.get("well", ""))
    labware = state.deck.labware.get(slot)
    if labware is None or well not in labware.well_map:
        return None
    wx, wy, wz = labware.well_map[well]
    return {"X": wx, "Y": wy, "Z": wz + float(args.get("dz", 0.0))}


def _deck_loaded(ctx: PreconditionContext) -> None:
    if ctx.state.deck is None:
        raise GateRejection(
            ErrorCode.E_DECK_NOT_LOADED,
            ExitCode.SAFETY_DENIED,
            "precondition:deck_loaded",
            "Deck geometry is not loaded; run deck.load_deck first.",
        )


def _labware_loaded(ctx: PreconditionContext) -> None:
    slot = str(ctx.args.get("slot", ""))
    if ctx.state.deck is None or slot not in ctx.state.deck.labware:
        raise GateRejection(
            ErrorCode.E_LABWARE_NOT_LOADED,
            ExitCode.SAFETY_DENIED,
            "precondition:labware_loaded",
            f"No labware loaded in slot '{slot}'.",
        )


def _well_exists(ctx: PreconditionContext) -> None:
    slot, well = str(ctx.args.get("slot", "")), str(ctx.args.get("well", ""))
    if ctx.state.deck is None or slot not in ctx.state.deck.labware:
        raise GateRejection(
            ErrorCode.E_LABWARE_NOT_LOADED,
            ExitCode.SAFETY_DENIED,
            "precondition:well_exists",
            f"No labware loaded in slot '{slot}'.",
        )
    if well not in ctx.state.deck.labware[slot].well_map:
        raise GateRejection(
            ErrorCode.E_WELL_NOT_FOUND,
            ExitCode.SAFETY_DENIED,
            "precondition:well_exists",
            f"Well '{well}' not found in slot '{slot}'.",
        )


def _verify_deck(_args: dict[str, Any], state: CanonicalMachineState) -> None:
    if state.deck is None:
        raise PostconditionError("Deck was not loaded in machine state.")


def _verify_labware(args: dict[str, Any], state: CanonicalMachineState) -> None:
    slot = str(args.get("slot", ""))
    if state.deck is None or slot not in state.deck.labware:
        raise PostconditionError(f"Labware was not registered in slot '{slot}'.")


LABWARE_DECK = DomainExtension(
    preconditions={
        "deck_loaded": _deck_loaded,
        "labware_loaded": _labware_loaded,
        "well_exists": _well_exists,
    },
    postconditions={
        "deck_loaded": _verify_deck,
        "labware_loaded": _verify_labware,
    },
    coord_resolver={"motion.move_well": _well_coords},
)
