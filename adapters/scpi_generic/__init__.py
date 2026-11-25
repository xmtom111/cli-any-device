"""Config-driven SCPI instrument adapter."""

from __future__ import annotations

import math
import time
from dataclasses import replace
from typing import Any

from adapters._generic import ConfiguredAdapter, invalid_protocol, render, time_left
from adapters.scpi_generic.transport import ScpiTransport

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
    CanonicalMachineState,
    Identity,
    UnsupportedCapabilityError,
)

_BOOLS = {"0": False, "1": True, "off": False, "on": True, "false": False, "true": True}
_PARSERS = frozenset({"float", "int", "bool", "string"})


def _parse(value: str, parser: str | None) -> Any:
    if parser == "float":
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("Expected a finite SCPI number.")
        return number
    if parser == "int":
        return int(value)
    if parser == "bool":
        if value.lower() not in _BOOLS:
            raise ValueError("Expected a SCPI boolean response.")
        return _BOOLS[value.lower()]
    if parser == "string":
        return value
    if value.lower() in _BOOLS and value not in {"0", "1"}:
        return _BOOLS[value.lower()]
    for kind in ("int", "float"):
        try:
            return _parse(value, kind)
        except ValueError:
            pass
    return value


class ScpiAdapter(ConfiguredAdapter):
    def __init__(self, config: AdapterConfig) -> None:
        options = config.options
        self._transport = ScpiTransport(
            config.endpoint or "", options.get("terminator", "\n"), options.get("baudrate", 9600)
        )
        capabilities = ("tcp_scpi", "abort_oob") if self._transport.scheme == "tcp" else (
            "serial_text",
        )
        super().__init__(config, self._transport.dialect, *capabilities)
        self._detect_cmd: str = options.get("detect_cmd", "*IDN?")
        self._settled_cmd: str = options.get("settled_cmd", "*OPC?")
        self._settled_expected = str(options.get("settled_expected", "1"))
        self._abort_cmd: str = options.get("abort_cmd", "OUTP OFF")
        self._reset_cmd: str | None = options.get("reset_cmd")
        self._poll_queries: dict[str, str] = options.get("poll_queries", {})
        self._poll_parsers: dict[str, str] = options.get("poll_parsers", {})
        self._fail_safe_commands: dict[str, str] = options.get("fail_safe_commands", {})
        self._properties: dict[str, Any] = {}

    def close(self) -> None:
        self._transport.close()

    def detect(self) -> Identity:
        with self._actions:
            reply = self._transport.exchange(
                self._detect_cmd, time.monotonic() + self._timeout, "detect", query=True
            )
            manufacturer, model, serial_number, firmware = (reply.split(",", 3) + [""] * 3)[:4]
            return self._identify(
                firmware.strip() or reply,
                model.strip() or None,
                manufacturer=manufacturer.strip(),
                serial_number=serial_number.strip(),
                idn=reply,
            )

    def _poll(self, deadline: float, step: str) -> CanonicalMachineState:
        properties = dict(self._properties)
        for name, command in self._poll_queries.items():
            if self._halted:
                break
            reply = self._transport.exchange(command, deadline, step, query=True)
            try:
                properties[name] = _parse(reply, self._poll_parsers.get(name))
            except ValueError as exc:
                raise AdapterTransportError(
                    f"Invalid SCPI reply for '{name}': {exc}", step=step
                ) from exc
        state = self._state()
        return state if state.estop_latched else replace(state, properties=properties)

    def poll_state(self) -> CanonicalMachineState:
        state = self._state()
        if state.estop_latched or not state.detected:
            return state
        with self._actions:
            return self._poll(time.monotonic() + self._timeout, "poll_state")

    def preflight(self) -> PreflightReport:
        return self._preflight(self.poll_state())

    def execute(self, request: ActionRequest) -> ActionTelemetry:
        step = request.service
        command = request.protocol.get("command")
        query = request.protocol.get("query")
        if command is None and query is None:
            raise UnsupportedCapabilityError(f"{step} has no SCPI command or query.", step=step)
        if command is not None and query is not None:
            raise invalid_protocol("Specify either a SCPI command or a query.", step)
        template = query if query is not None else command
        if not isinstance(template, str):
            raise invalid_protocol("SCPI templates must be strings.", step)
        frame = render(template, request.args, step, forbidden="\r\n;")
        if not frame.strip() or any(char in frame for char in "\r\n\x00"):
            raise invalid_protocol("SCPI templates must render a single nonempty frame.", step)

        parser = request.protocol.get("parse", "string")
        name = request.protocol.get("property")
        if parser not in _PARSERS or (name is not None and not isinstance(name, str)):
            raise invalid_protocol("Invalid SCPI parse or property option.", step)
        # For a write, a matching argument is the submitted value. Otherwise record the frame.
        value = request.args.get(name, frame)
        if query is None and parser != "string":
            try:
                value = _parse(str(value), parser)
            except ValueError as exc:
                raise invalid_protocol(str(exc), step) from exc
        with self._actions:
            self._check_running(step)
            reply = self._transport.exchange(
                frame, time.monotonic() + min(request.timeout_s, self._timeout), step,
                query=query is not None,
            )
            self._check_running(step)
            if query is not None:
                try:
                    value = _parse(reply, parser)
                except ValueError as exc:
                    raise AdapterTransportError(f"Invalid SCPI reply: {exc}", step=step) from exc
            if name is not None:
                self._properties[name] = value
        return ActionTelemetry(step, True, {"value": value} if query is not None else {})

    def await_settled(self, timeout_s: float) -> CanonicalMachineState:
        step = "await_settled"
        deadline = time.monotonic() + timeout_s
        with self._actions:
            try:
                while True:
                    self._check_running(step)
                    reply = self._transport.exchange(
                        self._settled_cmd, min(deadline, time.monotonic() + self._timeout),
                        step, query=True,
                    )
                    if reply == self._settled_expected:
                        state = self._poll(deadline, step)
                        self._check_running(step)
                        return state
                    time.sleep(min(0.02, time_left(deadline, step)))
            except (AdapterTimeoutError, AdapterTransportError):
                self._check_running(step)
                raise

    def _send_abort(self, command: str) -> AbortResult:
        return self._abort(
            f"scpi.{self._transport.scheme}",
            lambda deadline: self._transport.exchange(
                command, deadline, "abort_now", independent=True
            ),
        )

    def abort_now(self) -> AbortResult:
        return self._send_abort(self._abort_cmd)

    def reset_estop(self) -> CanonicalMachineState:
        def reset() -> None:
            if self._reset_cmd is not None:
                self._transport.exchange(
                    self._reset_cmd, time.monotonic() + self._timeout, "reset_estop"
                )
            self._properties.clear()

        with self._actions:
            self._reset(reset)
            return self._state()

    def apply_fail_safe(self, policy: str, reason: str) -> str:
        if policy == "none":
            return policy
        if policy == "adapter_abort":
            if self._halted:
                return "SCPI emergency stop already latched"
            result = self._send_abort(self._fail_safe_commands.get(policy, self._abort_cmd))
            return "SCPI emergency stop sent" if result.sent else result.note
        if policy not in self._fail_safe_commands:
            raise UnsupportedCapabilityError(f"SCPI fail-safe '{policy}' is not configured.")
        self._transport.exchange(
            self._fail_safe_commands[policy], time.monotonic() + self._timeout, "fail_safe",
            independent=True,
        )
        return f"SCPI fail-safe '{policy}' sent"


def create_adapter(config: AdapterConfig) -> AdapterPair:
    adapter = ScpiAdapter(config)
    return AdapterPair(config.target, adapter, adapter, adapter.close)
