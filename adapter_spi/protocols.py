"""Adapter Service Provider Interface (SPI): SupervisorChannel & ActionDriver.

Import rule: adapter_spi may ONLY import core_types and Python stdlib.
Adapters (adapters/duet_rrf, adapters/mujoco_twin, adapters/grbl_sim) implement
these two protocols and register via the ``cli_any_device.adapters`` entry point.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from core_types import CanonicalMachineState, Identity

ADAPTER_ENTRY_POINT_GROUP = "cli_any_device.adapters"
ABORT_LATENCY_SLA_MS = 100.0


@dataclass(frozen=True, slots=True)
class ActionRequest:
    """Validated action request dispatched from safety_kernel to ActionDriver."""

    service: str
    args: dict[str, Any]
    timeout_s: float
    safe_z_mm: float | None = None
    target_coords: dict[str, float] | None = None
    protocol: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ActionTelemetry:
    """Raw submission telemetry returned by ActionDriver.execute().

    IMPORTANT: ``accepted=True`` ONLY indicates the command was accepted/queued
    (e.g., HTTP 200 on Duet ``/rr_gcode``). Physical completion MUST be determined
    by ``safety_kernel`` calling ``SupervisorChannel.await_settled()`` and verifying
    service postconditions.
    """

    service: str
    accepted: bool
    raw: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AbortResult:
    """Outcome of a lock-free, out-of-band emergency stop call."""

    sent: bool
    latency_ms: float
    channel: str
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "sent": self.sent,
            "latency_ms": round(self.latency_ms, 3),
            "channel": self.channel,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class PreflightReport:
    """Read-only readiness check returned by SupervisorChannel.preflight()."""

    passed: bool
    checks: dict[str, bool]
    warnings: tuple[str, ...] = ()
    blocking_reasons: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "checks": dict(self.checks),
            "warnings": list(self.warnings),
            "blocking_reasons": list(self.blocking_reasons),
            "details": dict(self.details),
        }


@runtime_checkable
class SupervisorChannel(Protocol):
    """State observation, completion barrier, and lock-free emergency stop channel.

    Every adapter MUST implement SupervisorChannel independently from ActionDriver's
    execution lock so ``abort_now()`` always works even if ``ActionDriver.execute()``
    is blocked or deadlocked.
    """

    def detect(self) -> Identity:
        """Probe controller/simulator and return normalized Identity.

        On connection failure, raise ``AdapterTransportError`` with ``hookup_ref``
        pointing to the relevant HOOKUP.md anchor (e.g. ``HOOKUP.md#sec-troubleshooting``).
        """
        ...

    def poll_state(self) -> CanonicalMachineState:
        """Poll current canonical machine state without emitting motion."""
        ...

    def preflight(self) -> PreflightReport:
        """Run read-only preflight checks (dialect, firmware, endstops, e-stop, tool state)."""
        ...

    def await_settled(self, timeout_s: float) -> CanonicalMachineState:
        """Block until queued motion has physically completed and machine is idle.

        On real Duet/RRF hardware, this MUST append/verify ``M400`` and poll the
        object model until ``state.status == 'idle'`` (NEVER treat ``/rr_gcode``
        HTTP 200 as settled).
        Raises ``AdapterTimeoutError`` on timeout or ``HardwareFaultError`` on fault/halt.
        """
        ...

    def abort_now(self) -> AbortResult:
        """Lock-free, out-of-band emergency stop.

        MUST NOT acquire or wait on ActionDriver's mutex.
        Real Duet/RRF: send ``M112`` over an independent HTTP session/thread.
        GRBL sim: send realtime abort byte (``!`` / ``0x18``).
        MuJoCo twin: halt trajectory immediately.
        Compliance target: complete within 100 ms even under ActionDriver deadlock.
        """
        ...

    def reset_estop(self) -> CanonicalMachineState:
        """Explicitly clear a latched e-stop after human inspection (e.g. ``M999``).

        MUST invalidate homed flags so the machine must re-home before moving.
        """
        ...


@runtime_checkable
class ActionDriver(Protocol):
    """Executes validated domain actions and catalog fail-safe policies."""

    def capabilities(self) -> frozenset[str]:
        """Return the truthful capability flags supported by this adapter."""
        ...

    def execute(self, request: ActionRequest) -> ActionTelemetry:
        """Dispatch a validated action and return raw submission telemetry.

        MUST raise ``UnsupportedCapabilityError`` immediately if the service requires
        an unsupported capability (e.g. ``grbl_sim`` MUST reject ``tool.pickup`` and
        MUST NOT fabricate tool state).
        """
        ...

    def apply_fail_safe(self, policy: str, reason: str) -> str:
        """Apply the catalog-declared fail-safe action and return a summary string."""
        ...


@dataclass(frozen=True, slots=True)
class AdapterConfig:
    """Configuration passed to an AdapterFactory when instantiating a target."""

    target: str
    endpoint: str | None = None
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AdapterPair:
    """Paired SupervisorChannel and ActionDriver for a single target instance."""

    target: str
    supervisor: SupervisorChannel
    driver: ActionDriver
    close: Callable[[], None] | None = None


AdapterFactory = Callable[[AdapterConfig], AdapterPair]
