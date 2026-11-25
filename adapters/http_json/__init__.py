"""Config-driven HTTP JSON adapter."""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Any
from urllib.parse import quote, urlencode

from adapters._generic import ConfiguredAdapter, invalid_protocol, render, time_left
from adapters._http import MISSING, JsonHttpClient, json_path, map_properties

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
    UnsupportedCapabilityError,
)


def _render_value(value: Any, args: dict[str, Any], step: str) -> Any:
    if isinstance(value, str):
        return render(value, args, step)
    if isinstance(value, list):
        return [_render_value(item, args, step) for item in value]
    if isinstance(value, dict):
        return {
            _render_value(key, args, step): _render_value(item, args, step)
            for key, item in value.items()
        }
    return value


class HttpJsonAdapter(ConfiguredAdapter):
    def __init__(self, config: AdapterConfig) -> None:
        super().__init__(config, ApiDialect.HTTP_REST, "http_rest", "abort_oob")
        options = config.options
        self._http = JsonHttpClient(self.endpoint, options)
        self._detect_path: str = options.get("detect_path", "/status")
        self._detect_method: str = options.get("detect_method", "GET")
        self._firmware_key: str | None = options.get("firmware_key")
        self._status_path: str = options.get("status_path", "/status")
        self._status_map: dict[str, str] = options.get("status_map", {})
        self._settled_path: str = options.get("settled_path") or self._status_path
        self._settled_key: str | None = options.get("settled_key")
        self._settled_value = options.get("settled_value", True)
        self._abort_method: str = options.get("abort_method", "POST")
        self._abort_path: str = options.get("abort_path", "/abort")
        self._abort_body = options.get("abort_body")
        self._reset_method: str = options.get("reset_method") or "POST"
        self._reset_path: str | None = options.get("reset_path")
        self._reset_body = options.get("reset_body")
        self._fail_safe_requests: dict[str, dict[str, Any]] = options.get("fail_safe_requests", {})
        self._properties: dict[str, Any] = {}

    def detect(self) -> Identity:
        with self._actions:
            payload = self._http.request(
                self._detect_method, self._detect_path, time.monotonic() + self._timeout, "detect"
            )
            firmware = json_path(payload, self._firmware_key) if self._firmware_key else MISSING
            return self._identify(str(firmware) if firmware is not MISSING else "unknown", None)

    def _status(self, payload: Any) -> CanonicalMachineState:
        state = self._state()
        if state.estop_latched:
            return state
        properties = self._properties | map_properties(payload, self._status_map)
        return replace(state, properties=properties)

    def poll_state(self) -> CanonicalMachineState:
        state = self._state()
        if state.estop_latched or not state.detected:
            return state
        with self._actions:
            if self._halted:
                return self._state()
            return self._status(self._http.request(
                "GET", self._status_path, time.monotonic() + self._timeout, "poll_state"
            ))

    def preflight(self) -> PreflightReport:
        return self._preflight(self.poll_state())

    def execute(self, request: ActionRequest) -> ActionTelemetry:
        step, args, protocol = request.service, request.args, request.protocol
        template = protocol.get("path")
        if template is None:
            raise UnsupportedCapabilityError(f"{step} has no HTTP path.", step=step)
        method = protocol.get("method", "POST")
        query = protocol.get("query", {})
        body = protocol.get("body")
        response_map = protocol.get("response_map", {})
        if (
            not isinstance(template, str) or not isinstance(method, str)
            or not isinstance(query, dict) or not isinstance(body, (dict, str, type(None)))
            or not isinstance(response_map, dict)
            or not all(isinstance(path, str) for path in response_map.values())
        ):
            raise invalid_protocol("Invalid HTTP request protocol.", step)

        path = render(
            template, {name: quote(str(value), safe="") for name, value in args.items()}, step
        )
        if any(segment in {".", ".."} for segment in path.partition("?")[0].split("/")):
            raise invalid_protocol("HTTP paths cannot contain dot segments.", step)
        if query:
            separator = "&" if "?" in path else "?"
            path += separator + urlencode(_render_value(query, args, step), doseq=True)
        body = _render_value(body, args, step)

        with self._actions:
            self._check_running(step)
            payload = self._http.request(
                method, path, time.monotonic() + min(request.timeout_s, self._timeout),
                step, body=body,
            )
            self._check_running(step)
            self._properties.update(map_properties(payload, response_map))
        return ActionTelemetry(step, True, {"response": payload})

    def await_settled(self, timeout_s: float) -> CanonicalMachineState:
        step = "await_settled"
        deadline = time.monotonic() + timeout_s
        with self._actions:
            try:
                while True:
                    self._check_running(step)
                    payload = self._http.request(
                        "GET", self._settled_path,
                        min(deadline, time.monotonic() + self._timeout), step,
                    )
                    if self._settled_key is None or (
                        json_path(payload, self._settled_key) == self._settled_value
                    ):
                        self._check_running(step)
                        if self._settled_path != self._status_path:
                            payload = self._http.request(
                                "GET", self._status_path,
                                min(deadline, time.monotonic() + self._timeout), step,
                            )
                        state = self._status(payload)
                        self._check_running(step)
                        return state
                    time.sleep(min(0.02, time_left(deadline, step)))
            except (AdapterTimeoutError, AdapterTransportError):
                self._check_running(step)
                raise

    def _send_abort(self, method: str, path: str, body: Any) -> AbortResult:
        return self._abort(
            "http_json",
            lambda deadline: self._http.request(
                method, path, deadline, "abort_now", body=body, abort=True
            ),
        )

    def abort_now(self) -> AbortResult:
        return self._send_abort(self._abort_method, self._abort_path, self._abort_body)

    def reset_estop(self) -> CanonicalMachineState:
        def reset() -> None:
            if self._reset_path is not None:
                self._http.request(
                    self._reset_method, self._reset_path, time.monotonic() + self._timeout,
                    "reset_estop", body=self._reset_body,
                )
            self._properties.clear()

        with self._actions:
            self._reset(reset)
            return self._state()

    def apply_fail_safe(self, policy: str, reason: str) -> str:
        if policy == "none":
            return policy
        request = self._fail_safe_requests.get(policy)
        if policy == "adapter_abort":
            if self._halted:
                return "HTTP emergency stop already latched"
            result = self.abort_now() if request is None else self._send_abort(
                request["method"], request["path"], request.get("body")
            )
            if not result.sent:
                raise HardwareFaultError(result.note, step="fail_safe")
            return "HTTP emergency stop sent"
        if request is None:
            raise UnsupportedCapabilityError(f"HTTP fail-safe '{policy}' is not configured.")
        self._http.request(
            request["method"], request["path"], time.monotonic() + self._timeout,
            "fail_safe", body=request.get("body"),
        )
        return f"HTTP fail-safe '{policy}' sent"


def create_adapter(config: AdapterConfig) -> AdapterPair:
    adapter = HttpJsonAdapter(config)
    return AdapterPair(config.target, adapter, adapter)
