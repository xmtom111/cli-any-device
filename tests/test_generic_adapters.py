"""Loopback integration tests for YAML-driven instrument and HTTP adapters."""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import BaseServer, StreamRequestHandler, ThreadingTCPServer
from threading import Event, Thread
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import pytest

from adapter_spi import ABORT_LATENCY_SLA_MS
from compiler import load_bundle, validate_bundle
from core_types import ApiDialect, CommandEnvelope, DeviceBundle, ErrorCode, ExitCode
from daemon import AdapterRegistry, DeviceDaemon

SCPI_BUNDLE = Path(__file__).parent / "fixtures" / "scpi_psu" / "bundle"
SYSTEM_SERVICES = """
  - name: sys.detect
    risk_class: R0_READ
    postconditions: [detected]
  - name: sys.status
    risk_class: R0_READ
  - name: sys.preflight
    risk_class: R0_READ
    preconditions: [detected]
  - name: sys.abort
    risk_class: R0_READ
    bypasses_action_lock: true
  - name: sys.reset_estop
    risk_class: R3_PHYSICAL_INIT
    requires_confirm: true
    preconditions: [detected]
    postconditions: [not_estop_latched]
"""


@contextmanager
def running(server: BaseServer) -> Iterator[None]:
    with server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            yield
        finally:
            server.shutdown()
            thread.join()


def error_is(result: CommandEnvelope, code: ErrorCode, exit_code: ExitCode) -> None:
    assert not result.ok
    assert result.exit_code == exit_code
    assert result.error is not None and result.error.code == code


def assert_out_of_band_abort(daemon: DeviceDaemon, target: str, stopped: Event) -> None:
    start = time.monotonic()
    result = daemon.dispatch(target=target, service="sys.abort")
    elapsed_ms = (time.monotonic() - start) * 1000
    assert result.ok, result.error
    assert elapsed_ms < ABORT_LATENCY_SLA_MS
    assert result.data["abort"]["latency_ms"] < ABORT_LATENCY_SLA_MS
    assert result.state["lifecycle"] == "HALTED"
    assert result.state["estop_latched"]
    assert stopped.wait(1)


class ScpiHandler(StreamRequestHandler):
    def handle(self) -> None:
        assert isinstance(self.server, ScpiPSU)
        for frame in self.rfile:
            command = frame.decode("ascii").rstrip("\r\n")
            reply = self.server.exchange(command, self.client_address[1])
            if reply is not None:
                self.wfile.write(f"{reply}\n".encode())


class ScpiPSU(ThreadingTCPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), ScpiHandler)
        self.commands: list[tuple[str, int]] = []
        self.voltage = 0.0
        self.output = False
        self.allow_output = True
        self.ready = Event()
        self.ready.set()
        self.saw_pending = Event()
        self.probe_blocked = Event()
        self.release_probe = Event()
        self.stopped = Event()

    @property
    def endpoint(self) -> str:
        return f"tcp://127.0.0.1:{self.server_address[1]}"

    def exchange(self, command: str, client_port: int) -> str | None:
        self.commands.append((command, client_port))
        if command == "*IDN?":
            return "Open Instruments,OpenPSU-305,PSU-001,2.3"
        if command == "*OPC?":
            if self.ready.is_set():
                return "1"
            self.saw_pending.set()
            return "0"
        if command == "MEAS:TEMP? CH2":
            return "23.75"
        if command == "MEAS:TEMP? CH4":
            self.probe_blocked.set()
            assert self.release_probe.wait(3)
            return "25.5"
        if command == "SOUR:VOLT?":
            return str(self.voltage)
        if command == "OUTP?":
            return "1" if self.output else "0"
        if command.startswith("SOUR:VOLT "):
            self.voltage = float(command.split()[1])
        elif command == "OUTP ON":
            self.output = self.allow_output
        elif command == "OUTP OFF":
            self.output = False
            self.stopped.set()
        else:
            raise AssertionError(f"Unexpected SCPI command {command!r}")
        return None


def test_scpi_daemon_barrier_and_abort() -> None:
    bundle = load_bundle(SCPI_BUNDLE)
    validate_bundle(bundle)
    psu = ScpiPSU()
    target = "scpi_sim"
    with running(psu), closing(
        DeviceDaemon(bundle=bundle, endpoint_overrides={target: psu.endpoint})
    ) as daemon, ThreadPoolExecutor(max_workers=1) as pool:
        detected = daemon.dispatch(target=target, service="sys.detect")
        assert detected.ok
        assert detected.data["identity"]["api_mode"] == ApiDialect.TCP_SCPI
        assert detected.data["identity"]["board"] == "OpenPSU-305"
        assert detected.data["identity"]["firmware"] == "2.3"
        assert not detected.data["identity"]["is_real_hardware"]
        assert psu.commands[0][0] == "*IDN?"
        assert daemon.dispatch(target=target, service="sys.preflight").ok

        probe = daemon.dispatch(target=target, service="instrument.read_probe", args={"channel": 2})
        assert probe.ok and probe.data["telemetry"]["value"] == 23.75
        assert any(command == "MEAS:TEMP? CH2" for command, _ in psu.commands)

        before = list(psu.commands)
        rejected = daemon.dispatch(
            target=target, service="instrument.set_voltage", args={"voltage_v": 31},
        )
        error_is(rejected, ErrorCode.E_OUT_OF_BOUNDS, ExitCode.SAFETY_DENIED)
        assert rejected.exit_code == 3
        assert psu.commands == before

        psu.ready.clear()
        change = pool.submit(
            daemon.dispatch, target=target, service="instrument.set_voltage",
            args={"voltage_v": 12},
        )
        try:
            assert psu.saw_pending.wait(1)
            assert psu.voltage == 12
            assert not change.done()
        finally:
            psu.ready.set()
        changed = change.result(2)
        assert changed.ok and changed.state["properties"]["voltage_v"] == 12.0

        slow_probe = pool.submit(
            daemon.dispatch, target=target, service="instrument.read_probe", args={"channel": 4},
        )
        try:
            assert psu.probe_blocked.wait(1)
            assert_out_of_band_abort(daemon, target, psu.stopped)
            assert not slow_probe.done()
            assert psu.commands[-1][0] == "OUTP OFF"
            assert psu.commands[-2][0] == "MEAS:TEMP? CH4"
            assert psu.commands[-1][1] != psu.commands[-2][1]
        finally:
            psu.release_probe.set()
        error_is(slow_probe.result(2), ErrorCode.E_HARDWARE_FAULT, ExitCode.EXECUTION_ERROR)
        assert daemon.dispatch(target=target, service="sys.reset_estop", confirm=True).ok


def test_scpi_confirmation_and_fail_safe() -> None:
    psu = ScpiPSU()
    target = "scpi_sim"
    with running(psu), closing(
        DeviceDaemon(bundle=load_bundle(SCPI_BUNDLE), endpoint_overrides={target: psu.endpoint})
    ) as daemon:
        assert daemon.dispatch(target=target, service="sys.detect").ok
        denied = daemon.dispatch(target=target, service="instrument.enable_output")
        error_is(denied, ErrorCode.E_CONFIRM_REQUIRED, ExitCode.CONFIRM_REQUIRED)
        assert all(command != "OUTP ON" for command, _ in psu.commands)

        enabled = daemon.dispatch(target=target, service="instrument.enable_output", confirm=True)
        assert enabled.ok and enabled.state["properties"]["output_enabled"] is True

        psu.output = psu.allow_output = False
        failed = daemon.dispatch(target=target, service="instrument.enable_output", confirm=True)
        error_is(failed, ErrorCode.E_POSTCONDITION_FAILED, ExitCode.EXECUTION_ERROR)
        assert failed.error is not None
        assert failed.error.fail_safe_applied == "SCPI fail-safe 'disable_output' sent"
        assert psu.stopped.wait(1)
        enable_ports = [port for command, port in psu.commands if command == "OUTP ON"]
        off_ports = [port for command, port in psu.commands if command == "OUTP OFF"]
        assert len(off_ports) == 1 and off_ports[0] not in enable_ports


@pytest.mark.parametrize("allow_live", [False, True])
def test_scpi_live_hardware_unlock(allow_live: bool) -> None:
    psu = ScpiPSU()
    target = "hardware_real"
    with running(psu), closing(DeviceDaemon(
        bundle=load_bundle(SCPI_BUNDLE),
        endpoint_overrides={target: psu.endpoint},
        allow_live_hardware=allow_live,
    )) as daemon:
        detected = daemon.dispatch(target=target, service="sys.detect")
        assert detected.ok and detected.data["identity"]["is_real_hardware"]
        result = daemon.dispatch(
            target=target, service="instrument.set_voltage", args={"voltage_v": 5},
        )
        if allow_live:
            assert result.ok and psu.voltage == 5
        else:
            error_is(result, ErrorCode.E_LIVE_LOCKED, ExitCode.SAFETY_DENIED)
            assert all(not command.startswith("SOUR:VOLT ") for command, _ in psu.commands)


@dataclass(frozen=True)
class Exchange:
    method: str
    path: str
    body: Any
    auth: str | None


class JsonHandler(BaseHTTPRequestHandler):
    def exchange(self) -> None:
        assert isinstance(self.server, JsonDevice)
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        request = Exchange(
            self.command, self.path, json.loads(body) if body else None,
            self.headers.get("X-Device-Token"),
        )
        self.server.requests.append(request)
        status, payload = self.server.exchange(request)
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_PUT = exchange

    def log_message(self, format: str, *args: Any) -> None:
        pass


class JsonDevice(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), JsonHandler)
        self.requests: list[Exchange] = []
        self.command_blocked = Event()
        self.release_command = Event()
        self.stopped = Event()

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"

    def exchange(self, request: Exchange) -> tuple[int, Any]:
        raise NotImplementedError


class SmartRelay(JsonDevice):
    def __init__(self, token: str) -> None:
        super().__init__()
        self.token = token
        self.level = 0.0
        self.ready = Event()
        self.ready.set()
        self.saw_pending = Event()

    def exchange(self, request: Exchange) -> tuple[int, Any]:
        if request.auth != self.token:
            return 401, {"error": "auth required"}
        path = urlsplit(request.path).path
        if (request.method, path) == ("POST", "/api/identify"):
            return 200, {"metadata": {"firmware": "esp32-1.2"}}
        if (request.method, path) == ("GET", "/api/telemetry"):
            return 200, {"sensors": [{"celsius": 24.5}], "relay": {"level": self.level}}
        if (request.method, path) == ("GET", "/api/settled"):
            phase = "DONE" if self.ready.is_set() else "PENDING"
            if phase == "PENDING":
                self.saw_pending.set()
            return 200, {"operation": {"phase": phase}}
        if request.method == "POST" and path.startswith("/api/relay/"):
            if path.endswith("/slow"):
                self.command_blocked.set()
                assert self.release_command.wait(3)
            else:
                self.level = float(request.body["level"])
            return 200, {"result": {"receipt": ["cmd-1"]}}
        if (request.method, path) in {("POST", "/api/abort"), ("PUT", "/api/fail-safe")}:
            self.level = 0
            self.stopped.set()
            return 200, {"ok": True}
        if (request.method, path) == ("PUT", "/api/reset"):
            return 200, {"ok": True}
        return 404, {"error": request.path}


def http_bundle(tmp_path: Path, adapter: str, options: str, services: str) -> DeviceBundle:
    device = f"""
device_id: {adapter}_fixture
display_name: Loopback {adapter}
version: "1.0.0"
default_safe_z_mm: 10
axes:
  X: {{min_mm: 0, max_mm: 100}}
  Y: {{min_mm: 0, max_mm: 100}}
  Z: {{min_mm: 0, max_mm: 100}}
safety_limits:
  level: {{min_value: 0, max_value: 100}}
hookup:
  primary_interface: HTTP REST
  default_endpoint: http://127.0.0.1
  completion_semantics: Wait for the readiness barrier.
  estop_policy: Send an independent stop request.
targets:
  sim:
    adapter_id: {adapter}
    is_real_hardware: false
    capabilities: [http_rest, abort_oob, axes_xyz]
{options}
"""
    catalog = f"""
device_id: {adapter}_fixture
version: "1.0.0"
services:{SYSTEM_SERVICES}{services}
"""
    (tmp_path / "device.yaml").write_text(device, encoding="utf-8")
    (tmp_path / "catalog.yaml").write_text(catalog, encoding="utf-8")
    bundle = load_bundle(tmp_path)
    validate_bundle(bundle)
    return bundle


def test_http_json_daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle = http_bundle(tmp_path, "http_json", """
    adapter_options:
      detect_method: POST
      detect_path: /identify
      firmware_key: metadata.firmware
      status_path: /telemetry
      status_map:
        temperature_c: sensors.0.celsius
        level: relay.level
      settled_path: /settled
      settled_key: operation.phase
      settled_value: DONE
      auth_header: X-Device-Token
      auth_env_var: TEST_SMART_RELAY_TOKEN
      abort_body: {reason: operator}
      reset_path: /reset
      reset_method: PUT
      reset_body: {acknowledged: true}
      fail_safe_requests:
        relay_off: {method: PUT, path: /fail-safe, body: {level: 0}}
""", """
  - name: relay.set_level
    risk_class: R1_CONFIG
    preconditions: [detected, not_estop_latched]
    postconditions: [prop_set:command_id]
    args: &relay_args
      - {name: relay, type: string}
      - {name: label, type: string}
      - {name: level, type: float}
    protocol: &relay_protocol
      path: "/relay/{relay}?fixed=1"
      query: {label: "{label}", level: "{level:.0f}"}
      body: {level: "{level:.1f}", nested: [{label: "{label}", constant: 42}]}
      response_map: {command_id: result.receipt.0}
  - name: relay.verify_output
    risk_class: R1_CONFIG
    preconditions: [detected, not_estop_latched]
    postconditions: [prop_eq:level=0]
    args: *relay_args
    protocol: *relay_protocol
    fail_safe: relay_off
""")
    token = secrets.token_urlsafe(16)
    relay = SmartRelay(token)
    with running(relay), closing(DeviceDaemon(
        bundle=bundle, endpoint_overrides={"sim": relay.endpoint + "/api"}
    )) as daemon, ThreadPoolExecutor(max_workers=1) as pool:

        def dispatch(service: str, **kwargs: Any) -> CommandEnvelope:
            return daemon.dispatch(target="sim", service=service, **kwargs)

        monkeypatch.delenv("TEST_SMART_RELAY_TOKEN", raising=False)
        error_is(dispatch("sys.detect"), ErrorCode.E_TRANSPORT_ERROR, ExitCode.TIMEOUT)
        assert not relay.requests
        monkeypatch.setenv("TEST_SMART_RELAY_TOKEN", token)

        detected = dispatch("sys.detect")
        assert detected.ok
        assert detected.data["identity"]["api_mode"] == ApiDialect.HTTP_REST
        assert detected.data["identity"]["firmware"] == "esp32-1.2"
        assert dispatch("sys.preflight").ok
        assert dispatch("sys.status").state["properties"] == {"temperature_c": 24.5, "level": 0}

        args = {"relay": "zone/../A?side=#%2F", "label": "main & lab=1", "level": 25}
        before = list(relay.requests)
        rejected = dispatch("relay.set_level", args=args | {"level": 101})
        error_is(rejected, ErrorCode.E_OUT_OF_BOUNDS, ExitCode.SAFETY_DENIED)
        assert relay.requests == before
        rejected = dispatch("relay.set_level", args=args | {"relay": ".."})
        error_is(rejected, ErrorCode.E_INVALID_ARGS, ExitCode.INVALID_USAGE)
        assert all(req.method == "GET" for req in relay.requests[len(before):])

        relay.ready.clear()
        change = pool.submit(dispatch, "relay.set_level", args=args)
        try:
            assert relay.saw_pending.wait(1)
            assert not change.done()
            command = next(req for req in relay.requests if req.path.startswith("/api/relay/"))
            assert command.method == "POST"
            url = urlsplit(command.path)
            assert url.path == "/api/relay/" + quote(args["relay"], safe="")
            assert parse_qs(url.query) == {
                "fixed": ["1"], "label": [args["label"]], "level": ["25"],
            }
            assert command.body == {
                "level": "25.0", "nested": [{"label": args["label"], "constant": 42}],
            }
        finally:
            relay.ready.set()
        settled = change.result(2)
        assert settled.ok
        assert settled.state["properties"] == {
            "temperature_c": 24.5, "level": 25.0, "command_id": "cmd-1",
        }

        failed = dispatch("relay.verify_output", args=args)
        error_is(failed, ErrorCode.E_POSTCONDITION_FAILED, ExitCode.EXECUTION_ERROR)
        assert failed.error and failed.error.fail_safe_applied == "HTTP fail-safe 'relay_off' sent"
        assert Exchange("PUT", "/api/fail-safe", {"level": 0}, token) in relay.requests
        relay.stopped.clear()

        slow = pool.submit(dispatch, "relay.set_level", args=args | {"relay": "slow"})
        try:
            assert relay.command_blocked.wait(1)
            assert_out_of_band_abort(daemon, "sim", relay.stopped)
            assert not slow.done()
            assert Exchange("POST", "/api/abort", {"reason": "operator"}, token) in relay.requests
        finally:
            relay.release_command.set()
        error_is(slow.result(2), ErrorCode.E_HARDWARE_FAULT, ExitCode.EXECUTION_ERROR)
        assert dispatch("sys.reset_estop", confirm=True).ok
        assert Exchange("PUT", "/api/reset", {"acknowledged": True}, token) in relay.requests
        assert all(request.auth == token for request in relay.requests)


class Moonraker(JsonDevice):
    def __init__(self) -> None:
        super().__init__()
        self.klippy_state = "ready"
        self.idle_state = "Ready"
        self.homed_axes = ""
        self.position = [0.0, 0.0, 2.0, 0.0]
        self.offset = [-2.0, 0.0, 0.0, 0.0]
        self.hold_move = False
        self.saw_printing = Event()
        self.saw_idle = Event()
        self.scripts: list[str] = []

    def exchange(self, request: Exchange) -> tuple[int, Any]:
        if (request.method, request.path) == ("GET", "/server/info"):
            return 200, {"result": {"klippy_state": self.klippy_state}}
        if (request.method, request.path) == ("GET", "/printer/info"):
            return 200, {"result": {"hostname": "klipper-pi", "software_version": "v0.12.0"}}
        if request.method == "GET" and request.path.startswith("/printer/objects/query?"):
            idle = self.idle_state
            if request.path.endswith("?idle_timeout&toolhead&gcode_move"):
                if idle == "Printing":
                    self.saw_printing.set()
                elif idle == "Idle":
                    self.saw_idle.set()
            return 200, {"result": {"status": {
                "idle_timeout": {"state": idle},
                "toolhead": {"position": self.position, "homed_axes": self.homed_axes},
                "gcode_move": {
                    "position": self.position,
                    "gcode_position": [
                        pos + offset for pos, offset in zip(self.position, self.offset, strict=True)
                    ],
                },
            }}}
        if (request.method, request.path) == ("POST", "/printer/gcode/script"):
            self.execute(request.body["script"])
            return 200, {"result": "ok"}
        if (request.method, request.path) == ("POST", "/printer/emergency_stop"):
            self.klippy_state = "shutdown"
            self.stopped.set()
            return 200, {"result": "ok"}
        if (request.method, request.path) == ("POST", "/printer/firmware_restart"):
            self.klippy_state = "ready"
            return 200, {"result": "ok"}
        return 404, {"error": request.path}

    def execute(self, script: str) -> None:
        self.scripts.append(script)
        if script == "G28":
            self.homed_axes = "xyz"
        if script == "RESPOND MSG=slow":
            self.command_blocked.set()
            assert self.release_command.wait(3)
        relative = False
        for line in script.splitlines():
            if line in {"G90", "G91"}:
                relative = line == "G91"
            elif line.startswith("G1 "):
                for word in line.split()[1:]:
                    axis = word[0]
                    if axis in "XYZ":
                        index = "XYZ".index(axis)
                        coordinate = float(word[1:])
                        origin = self.position[index] if relative else -self.offset[index]
                        self.position[index] = origin + coordinate
                if self.hold_move:
                    self.idle_state = "Printing"


def test_moonraker_daemon(tmp_path: Path) -> None:
    bundle = http_bundle(tmp_path, "moonraker_http", "", """
  - name: motion.home_all
    risk_class: R3_PHYSICAL_INIT
    requires_confirm: true
    preconditions: [detected, not_estop_latched]
    postconditions: [homed_xyz]
    fail_safe: abort_m112
  - name: motion.move_safe
    risk_class: R2_MOTION
    args: &absolute_args
      - {name: x, type: float}
      - {name: y, type: float}
      - {name: z, type: float}
      - {name: speed, type: float, required: false}
    preconditions: [detected, not_estop_latched, homed_xyz, target_in_bounds]
    postconditions: [idle]
    fail_safe: retract_safe_z_or_halt
  - name: motion.move_to
    risk_class: R2_MOTION
    args: *absolute_args
    preconditions: [detected, not_estop_latched, homed_xyz, target_in_bounds]
    postconditions: [idle]
  - name: motion.move_rel
    risk_class: R2_MOTION
    args:
      - {name: dx, type: float}
    preconditions: [detected, not_estop_latched, homed_xyz, target_in_bounds]
    postconditions: [idle]
  - name: motion.macro
    risk_class: R1_CONFIG
    args:
      - {name: message, type: string}
    preconditions: [detected, not_estop_latched]
    protocol: {gcode: "RESPOND MSG={message}"}
  - name: motion.wait_ready
    risk_class: R1_CONFIG
    preconditions: [detected, not_estop_latched]
    timeout_s: 0.2
    protocol: {gcode: "RESPOND MSG=verify"}
    fail_safe: retract_safe_z_or_halt
""")
    printer = Moonraker()
    with running(printer), closing(DeviceDaemon(
        bundle=bundle, endpoint_overrides={"sim": printer.endpoint}
    )) as daemon, ThreadPoolExecutor(max_workers=1) as pool:

        def dispatch(service: str, **kwargs: Any) -> CommandEnvelope:
            return daemon.dispatch(target="sim", service=service, **kwargs)

        detected = dispatch("sys.detect")
        assert detected.ok
        assert detected.data["identity"]["api_mode"] == ApiDialect.HTTP_REST
        assert detected.data["identity"]["firmware"] == "v0.12.0"
        assert detected.data["identity"]["board"] == "klipper-pi"
        assert [req.path for req in printer.requests[:2]] == ["/server/info", "/printer/info"]
        assert detected.state["homed"] == []
        assert detected.state["pos"] == {"X": 0.0, "Y": 0.0, "Z": 2.0}
        assert detected.state["safe_z"] == 10
        assert dispatch("sys.preflight").ok
        printer.idle_state = "Printing"
        assert dispatch("sys.status").state["lifecycle"] == "MOVING"
        printer.idle_state = "Idle"
        assert dispatch("sys.status").state["lifecycle"] == "IDLE"
        printer.idle_state = "Ready"

        homes = dispatch("motion.home_all", confirm=True)
        assert homes.ok and homes.state["homed"] == ["X", "Y", "Z"]
        assert printer.scripts == ["G28", "M400"]

        move_args = {"x": 20, "y": 30, "z": 4, "speed": 800}
        bad_move = dispatch("motion.move_safe", args=move_args | {"x": 101})
        error_is(bad_move, ErrorCode.E_OUT_OF_BOUNDS, ExitCode.SAFETY_DENIED)
        assert printer.scripts == ["G28", "M400"]

        printer.hold_move = True
        move = pool.submit(dispatch, "motion.move_safe", args=move_args)
        try:
            assert printer.saw_printing.wait(1)
            assert not move.done()
            assert printer.scripts[-2:] == [
                "G90\nG1 Z10.0 F800.0\nG1 X18.0 Y30.0 F800.0\nG1 Z4.0 F800.0", "M400",
            ]
            printer.idle_state = "Idle"
            assert printer.saw_idle.wait(1)
            assert not move.done()
        finally:
            printer.hold_move = False
            printer.idle_state = "Ready"
        moved = move.result(2)
        assert moved.ok
        assert moved.state["pos"] == {"X": 20.0, "Y": 30.0, "Z": 4.0}

        assert dispatch("motion.move_to", args={"x": 30, "y": 40, "z": 10}).ok
        assert printer.scripts[-2:] == ["G90\nG1 X28.0 Y40.0 Z10.0", "M400"]
        relative = dispatch("motion.move_rel", args={"dx": 0.00001})
        assert relative.ok and relative.state["pos"]["X"] == pytest.approx(30.00001)
        assert printer.scripts[-2:] == ["G91\nG1 X0.00001\nG90", "M400"]

        before = list(printer.scripts)
        injection = dispatch("motion.macro", args={"message": "hello\r\nG28"})
        error_is(injection, ErrorCode.E_INVALID_ARGS, ExitCode.INVALID_USAGE)
        assert printer.scripts == before

        slow = pool.submit(dispatch, "motion.macro", args={"message": "slow"})
        try:
            assert printer.command_blocked.wait(1)
            assert_out_of_band_abort(daemon, "sim", printer.stopped)
            assert not slow.done()
            assert printer.requests[-1].path == "/printer/emergency_stop"
        finally:
            printer.release_command.set()
        error_is(slow.result(2), ErrorCode.E_HARDWARE_FAULT, ExitCode.EXECUTION_ERROR)
        assert dispatch("sys.status").state["homed"] == []

        reset = dispatch("sys.reset_estop", confirm=True)
        assert reset.ok and reset.state["homed"] == [] and not reset.state["estop_latched"]
        assert printer.requests[-1].path == "/printer/firmware_restart"
        assert dispatch("sys.status").state["homed"] == []
        denied = dispatch("motion.move_safe", args=move_args)
        error_is(denied, ErrorCode.E_NOT_HOMED, ExitCode.SAFETY_DENIED)
        assert dispatch("motion.home_all", confirm=True).state["homed"] == ["X", "Y", "Z"]

        printer.klippy_state = "shutdown"
        halted = dispatch("sys.status")
        assert halted.state["lifecycle"] == "HALTED"
        assert halted.state["homed"] == [] and halted.state["estop_latched"]
        assert dispatch("sys.reset_estop", confirm=True).ok

        printer.stopped.clear()
        printer.idle_state = "Idle"
        failed = dispatch("motion.wait_ready")
        error_is(failed, ErrorCode.E_TIMEOUT, ExitCode.TIMEOUT)
        assert failed.error and failed.error.fail_safe_applied == "Klipper emergency stop sent"
        assert printer.stopped.is_set()
        assert printer.scripts[-2:] == ["RESPOND MSG=verify", "M400"]


def test_adapter_registry_discovers_all_adapters() -> None:
    assert {
        "duet_rrf", "grbl_sim", "http_json", "in_memory", "moonraker_http", "mujoco_twin",
        "scpi_generic",
    } <= set(AdapterRegistry().available_adapters())
