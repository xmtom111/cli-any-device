"""Error codes, exit codes, structured error details, and typed exceptions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import Any


class ExitCode(IntEnum):
    """CLI process exit codes defined in Technical Roadmap Section 6.4."""

    OK = 0
    EXECUTION_ERROR = 1
    INVALID_USAGE = 2
    SAFETY_DENIED = 3
    CONFIRM_REQUIRED = 4
    TIMEOUT = 5
    ESTOP_LATCHED = 99


class ErrorCode(StrEnum):
    """Machine-readable error codes emitted in JSON command envelopes."""

    E_NOT_DETECTED = "E_NOT_DETECTED"
    E_NOT_HOMED = "E_NOT_HOMED"
    E_UNKNOWN_STATE = "E_UNKNOWN_STATE"
    E_UNSAFE_Z = "E_UNSAFE_Z"
    E_OUT_OF_BOUNDS = "E_OUT_OF_BOUNDS"
    E_TOOL_ALREADY_LOADED = "E_TOOL_ALREADY_LOADED"
    E_NO_TOOL_LOADED = "E_NO_TOOL_LOADED"
    E_TOOL_PRESENT = "E_TOOL_PRESENT"
    E_DECK_NOT_LOADED = "E_DECK_NOT_LOADED"
    E_LABWARE_NOT_LOADED = "E_LABWARE_NOT_LOADED"
    E_WELL_NOT_FOUND = "E_WELL_NOT_FOUND"
    E_SAFE_Z_TOO_LOW = "E_SAFE_Z_TOO_LOW"
    E_PRECONDITION_FAILED = "E_PRECONDITION_FAILED"
    E_POSTCONDITION_FAILED = "E_POSTCONDITION_FAILED"
    E_CONFIRM_REQUIRED = "E_CONFIRM_REQUIRED"
    E_LIVE_LOCKED = "E_LIVE_LOCKED"
    E_UNSUPPORTED_CAPABILITY = "E_UNSUPPORTED_CAPABILITY"
    E_INVALID_ARGS = "E_INVALID_ARGS"
    E_UNKNOWN_SERVICE = "E_UNKNOWN_SERVICE"
    E_UNKNOWN_TARGET = "E_UNKNOWN_TARGET"
    E_ESTOP_LATCHED = "E_ESTOP_LATCHED"
    E_BUSY = "E_BUSY"
    E_TIMEOUT = "E_TIMEOUT"
    E_EXTERNAL_INTERVENTION = "E_EXTERNAL_INTERVENTION"
    E_TRANSPORT_ERROR = "E_TRANSPORT_ERROR"
    E_EXECUTION_FAILED = "E_EXECUTION_FAILED"
    E_HARDWARE_FAULT = "E_HARDWARE_FAULT"
    E_DAEMON_UNAVAILABLE = "E_DAEMON_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class ErrorDetail:
    """Structured error object embedded in failed CommandEnvelope responses."""

    code: ErrorCode
    step: str
    reason: str
    fail_safe_applied: str | None = None
    hookup_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code.value,
            "step": self.step,
            "reason": self.reason,
            "fail_safe_applied": self.fail_safe_applied,
        }
        if self.hookup_ref is not None:
            payload["hookup_ref"] = self.hookup_ref
        return payload


class CliAnyDeviceError(Exception):
    """Base exception carrying structured error code and exit code."""

    def __init__(
        self,
        code: ErrorCode,
        reason: str,
        *,
        step: str = "unknown",
        exit_code: ExitCode = ExitCode.EXECUTION_ERROR,
        hookup_ref: str | None = None,
    ) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.step = step
        self.exit_code = exit_code
        self.hookup_ref = hookup_ref


class UnsupportedCapabilityError(CliAnyDeviceError):
    """Raised when an adapter or target does not support a required capability."""

    def __init__(self, reason: str, *, step: str = "capability_check") -> None:
        super().__init__(
            ErrorCode.E_UNSUPPORTED_CAPABILITY,
            reason,
            step=step,
            exit_code=ExitCode.INVALID_USAGE,
        )


class AdapterTransportError(CliAnyDeviceError):
    """Raised on network/serial/transport failures (with optional HOOKUP.md section ref)."""

    def __init__(
        self,
        reason: str,
        *,
        step: str = "transport",
        hookup_ref: str | None = None,
    ) -> None:
        super().__init__(
            ErrorCode.E_TRANSPORT_ERROR,
            reason,
            step=step,
            exit_code=ExitCode.TIMEOUT,
            hookup_ref=hookup_ref,
        )


class AdapterTimeoutError(CliAnyDeviceError):
    """Raised when awaiting motion completion or supervisor response times out."""

    def __init__(self, reason: str, *, step: str = "await_settled") -> None:
        super().__init__(
            ErrorCode.E_TIMEOUT,
            reason,
            step=step,
            exit_code=ExitCode.TIMEOUT,
        )


class HardwareFaultError(CliAnyDeviceError):
    """Raised when hardware enters a fault or halted state during execution."""

    def __init__(self, reason: str, *, step: str = "execute") -> None:
        super().__init__(
            ErrorCode.E_HARDWARE_FAULT,
            reason,
            step=step,
            exit_code=ExitCode.EXECUTION_ERROR,
        )
