"""Canonical machine state, identity, and lifecycle types for any device (no I/O)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class MachineLifecycle(StrEnum):
    """Normalized machine lifecycle states across any hardware or simulator."""

    DISCONNECTED = "DISCONNECTED"
    UNKNOWN = "UNKNOWN"
    IDLE = "IDLE"
    MOVING = "MOVING"
    BUSY = "BUSY"
    HALTED = "HALTED"
    FAULT = "FAULT"


class ApiDialect(StrEnum):
    """Control-plane API dialect discovered during sys.detect."""

    HTTP_REST = "http_rest"
    SERIAL_TEXT = "serial_text"
    TCP_SCPI = "tcp_scpi"
    PYTHON_HOST_API = "python_host_api"
    RRF_STANDALONE = "rrf_standalone"
    RRF_SBC = "rrf_sbc"
    MUJOCO_TWIN = "mujoco_twin"
    GRBL_SERIAL_TCP = "grbl_serial_tcp"
    IN_MEMORY_FAKE = "in_memory_fake"
    UNKNOWN = "unknown"


class ToolPresence(StrEnum):
    """Optional end-effector/tool occupancy state; UNKNOWN must be treated as unsafe."""

    NONE = "NONE"
    LOADED = "LOADED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class Position:
    """Optional spatial/joint coordinates (mm or device-specific units)."""

    x: float | None = None
    y: float | None = None
    z: float | None = None
    u: float | None = None
    extra: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, float]:
        out: dict[str, float] = {}
        if self.x is not None:
            out["X"] = self.x
        if self.y is not None:
            out["Y"] = self.y
        if self.z is not None:
            out["Z"] = self.z
        if self.u is not None:
            out["U"] = self.u
        for axis, val in sorted(self.extra.items()):
            out[axis] = val
        return out


@dataclass(frozen=True, slots=True)
class Identity:
    """Discovered identity of any connected hardware device or simulator."""

    target: str
    endpoint: str
    api_mode: ApiDialect
    firmware: str
    board: str | None = None
    capabilities: frozenset[str] = frozenset()
    is_real_hardware: bool = False
    hookup_verified: bool = True
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "endpoint": self.endpoint,
            "api_mode": self.api_mode.value,
            "firmware": self.firmware,
            "board": self.board,
            "capabilities": sorted(self.capabilities),
            "is_real_hardware": self.is_real_hardware,
            "hookup_verified": self.hookup_verified,
            "details": dict(self.details),
        }


@dataclass(frozen=True, slots=True)
class LabwareState:
    """Optional carrier/fixture sub-item geometry (e.g. wellplate on a deck slot)."""

    slot: str
    spec: str
    top_z_mm: float
    well_map: dict[str, tuple[float, float, float]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot": self.slot,
            "spec": self.spec,
            "top_z_mm": self.top_z_mm,
            "well_count": len(self.well_map),
        }


@dataclass(frozen=True, slots=True)
class DeckState:
    """Optional multi-slot work-surface configuration."""

    spec: str
    slots: tuple[str, ...]
    labware: dict[str, LabwareState] = field(default_factory=dict)

    @property
    def max_labware_top_z_mm(self) -> float:
        if not self.labware:
            return 0.0
        return max(item.top_z_mm for item in self.labware.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "spec": self.spec,
            "slots": list(self.slots),
            "labware": {slot: lw.to_dict() for slot, lw in sorted(self.labware.items())},
        }


@dataclass(frozen=True, slots=True)
class CanonicalMachineState:
    """Device-agnostic canonical state polled from SupervisorChannel.

    Non-motion devices (e.g. open-source radios, lab instruments, power supplies)
    populate ``properties`` and ``verified_flags`` while leaving motion/tool fields
    empty. Motion platforms (3-axis stages, robot arms, Jubilee) additionally
    populate ``position``, ``homed``, ``safe_z_mm``, ``active_tool``, and ``deck``.
    """

    lifecycle: MachineLifecycle = MachineLifecycle.UNKNOWN
    detected: bool = False
    estop_latched: bool = False
    identity: Identity | None = None
    capabilities: frozenset[str] = frozenset()
    state_seq: int | None = None
    properties: dict[str, Any] = field(default_factory=dict)
    verified_flags: frozenset[str] = frozenset()

    # Optional spatial / motion / tooling state
    homed: frozenset[str] = frozenset()
    position: Position = field(default_factory=Position)
    active_tool: int | None = None
    tool_presence: ToolPresence = ToolPresence.UNKNOWN
    tool_locked: bool | None = None
    safe_z_mm: float | None = None
    deck: DeckState | None = None
    collision_count: int = 0

    def to_summary_dict(self) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "lifecycle": self.lifecycle.value,
            "detected": self.detected,
            "estop_latched": self.estop_latched,
            "properties": dict(self.properties),
            "homed": sorted(self.homed),
            "active_tool": self.active_tool,
            "tool_presence": self.tool_presence.value,
            "tool_locked": self.tool_locked,
            "pos": self.position.to_dict(),
            "safe_z": self.safe_z_mm,
            "collision_count": self.collision_count,
        }
        return summary
