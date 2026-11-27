"""State and template helpers for the declarative adapters."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from string import Formatter
from threading import Event, Lock
from typing import Any

from adapter_spi import ABORT_LATENCY_SLA_MS, AbortResult, AdapterConfig, PreflightReport
from core_types import (
    AdapterTimeoutError,
    AdapterTransportError,
    ApiDialect,
    CanonicalMachineState,
    CliAnyDeviceError,
    ErrorCode,
    ExitCode,
    HardwareFaultError,
    Identity,
    MachineLifecycle,
)


def time_left(deadline: float, step: str) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise AdapterTimeoutError("Device response deadline expired.", step=step)
    return remaining


def invalid_protocol(reason: str, step: str) -> CliAnyDeviceError:
    return CliAnyDeviceError(
        ErrorCode.E_INVALID_ARGS, reason, step=step, exit_code=ExitCode.INVALID_USAGE
    )


def render(
    template: str, args: dict[str, Any], step: str, *, forbidden: str = ""
) -> str:
    try:
        for _, field, spec, _ in Formatter().parse(template):
            if field is None:
                continue
            if not field.isidentifier() or "{" in spec or "}" in spec:
                raise ValueError("Placeholders must use argument names and literal format specs.")
            if field not in args:
                raise ValueError(f"Missing template argument '{field}'.")
            if any(char in str(args[field]) for char in forbidden):
                raise ValueError(f"Unsafe characters in template argument '{field}'.")
        return template.format_map(args)
    except (ValueError, TypeError) as exc:
        raise invalid_protocol(str(exc), step) from exc


class ConfiguredAdapter:
    def __init__(self, config: AdapterConfig, dialect: ApiDialect, *capabilities: str) -> None:
        if not config.endpoint:
            raise AdapterTransportError("Adapter endpoint is required.", step="detect")
        self.endpoint = config.endpoint
        self._target = config.target
        self._dialect = dialect
        self._capabilities = frozenset(config.options.get("capabilities", ())).union(capabilities)
        self._real = bool(config.options.get("is_real_hardware", False))
        self._hookup_verified = bool(config.options.get("hookup_verified", True))
        try:
            self._timeout = float(config.options.get("timeout_s", 2.0))
            if not math.isfinite(self._timeout) or self._timeout <= 0:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise AdapterTransportError("timeout_s must be finite and positive.") from exc
        self._identity: Identity | None = None
        self._actions = Lock()
        self._stop: Event | None = None
        self._cleared_stop: Event | None = None

    def capabilities(self) -> frozenset[str]:
        return self._capabilities

    def _identify(self, firmware: str, board: str | None, **details: Any) -> Identity:
        self._identity = Identity(
            target=self._target,
            endpoint=self.endpoint,
            api_mode=self._dialect,
            firmware=firmware,
            board=board,
            capabilities=self.capabilities(),
            is_real_hardware=self._real,
            hookup_verified=self._hookup_verified,
            details=details,
        )
        return self._identity

    @property
    def _halted(self) -> bool:
        return self._stop is not self._cleared_stop

    def _state(self, lifecycle: MachineLifecycle = MachineLifecycle.IDLE) -> CanonicalMachineState:
        halted = self._halted
        if halted:
            lifecycle = MachineLifecycle.HALTED
        elif self._identity is None:
            lifecycle = MachineLifecycle.UNKNOWN
        return CanonicalMachineState(
            lifecycle=lifecycle,
            detected=self._identity is not None,
            estop_latched=halted,
            identity=self._identity,
            capabilities=self.capabilities(),
        )

    @staticmethod
    def _preflight(state: CanonicalMachineState) -> PreflightReport:
        checks = {"detected": state.detected, "idle": state.lifecycle == MachineLifecycle.IDLE}
        return PreflightReport(
            passed=all(checks.values()),
            checks=checks,
            blocking_reasons=tuple(name for name, passed in checks.items() if not passed),
        )

    def _check_running(self, step: str) -> None:
        if self._halted:
            raise HardwareFaultError("Device is halted.", step=step)

    def _abort(self, channel: str, send: Callable[[float], None]) -> AbortResult:
        start = time.monotonic()
        stop = Event()
        self._stop = stop
        note = ""
        try:
            send(start + min(self._timeout, 0.8 * ABORT_LATENCY_SLA_MS / 1000))
        except (AdapterTransportError, AdapterTimeoutError) as exc:
            note = exc.reason
        finally:
            stop.set()
        return AbortResult(not note, (time.monotonic() - start) * 1000, channel, note)

    def _reset(self, send: Callable[[], None]) -> None:
        stop = self._stop
        if stop is not None and not stop.is_set():
            raise HardwareFaultError("Emergency stop is still being sent.", step="reset_estop")
        send()
        self._cleared_stop = stop
