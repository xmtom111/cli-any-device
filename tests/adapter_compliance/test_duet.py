import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest
from adapters.duet_rrf import DuetAdapter
from adapters.duet_rrf import create_adapter as duet_factory
from adapters.duet_rrf.supervisor import RrfSupervisor

from adapter_spi import ActionRequest, AdapterConfig
from core_types import (
    AdapterTransportError,
    ApiDialect,
    MachineLifecycle,
    ToolPresence,
    UnsupportedCapabilityError,
)


class FakeRrf(RrfSupervisor):
    def __init__(self, dialect):
        super().__init__("http://127.0.0.1:1", "local test only")
        self.dialect = dialect
        self.calls = []
        self.barrier_sent = threading.Event()
        self.barrier_finished = threading.Event()
        self.idle = threading.Event()
        self.abort_sent = threading.Event()
        self.sessions = 0
        self.reply = ""
        self.marker = ""
        self.unrelated_reply = threading.Event()
        self.model_reads = 0
        self.poll_gate = None
        self.poll_entered = threading.Event()

    def _request(self, path, *, query=None, body=None, key=None, deadline=None):
        self.calls.append((path, query, body, key))
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("mock HTTP deadline")
        if path == "/machine/connect" and self.dialect != "sbc":
            raise FileNotFoundError(path)
        if path.endswith("connect"):
            self.sessions += 1
            return json.dumps({"err": 0, "sessionKey": self.sessions})
        command = body if path == "/machine/code" else (query or {}).get("gcode")
        if command == "M112":
            self.abort_sent.set()
            return '{"buff": 512}' if self.dialect == "standalone" else ""
        if command and command.startswith("M400"):
            if self.dialect == "standalone":
                self.marker = re.fullmatch(r'M400\nM118 P3 S"(m400-[0-9a-f]+)" L0', command)[1]
            self.barrier_sent.set()
            if self.dialect == "standalone":
                return '{"buff": 512}'
            if not self.barrier_finished.wait(1):
                raise TimeoutError()
            return self.reply
        if path == "/rr_reply":
            if self.barrier_finished.is_set():
                return self.reply or self.marker
            return "unrelated reply" if self.unrelated_reply.is_set() else "prior reply"
        assert path in {"/machine/model", "/rr_model"}
        self.model_reads += 1
        if self.poll_gate is not None:
            self.poll_entered.set()
            assert self.poll_gate.wait(2)
        model = {
            "state": {"status": "idle" if self.idle.is_set() else "busy", "currentTool": -1},
            "boards": [{"name": "HTTP fixture", "firmwareVersion": "3.test"}],
            "move": {"axes": [{"letter": "X", "userPosition": 20.0, "homed": True}]},
            "seqs": {"reply": 1 + self.unrelated_reply.is_set() + self.barrier_finished.is_set()},
        }
        if self.dialect == "sbc":
            return json.dumps(model)
        name = query["key"]
        result = model["move"]["axes"] if name == "move.axes" else model[name]
        return json.dumps({"result": result})


@pytest.mark.parametrize("dialect", ["standalone", "sbc"])
def test_completion_requires_barrier_execution_and_idle(dialect):
    channel = FakeRrf(dialect)
    assert channel.detect()["api_mode"] == dialect
    with ThreadPoolExecutor() as worker:
        result = worker.submit(channel.await_settled, 1)
        assert channel.barrier_sent.wait(0.2)
        assert not result.done()
        channel.barrier_finished.set()
        assert not result.done()
        channel.idle.set()
        state = result.result(timeout=1)
    assert state["homed"] == {"X"}
    assert state["active_tool"] is None


def test_standalone_immediate_idle_without_barrier_reply_times_out():
    channel = FakeRrf("standalone")
    channel.idle.set()
    with ThreadPoolExecutor() as worker:
        result = worker.submit(channel.await_settled, 0.15)
        assert channel.barrier_sent.wait(0.1)
        channel.unrelated_reply.set()
        with pytest.raises(TimeoutError):
            result.result(timeout=1)
    assert sum(path == "/rr_reply" for path, *_ in channel.calls) >= 2


def test_m400_error_is_failure_even_when_idle():
    channel = FakeRrf("standalone")
    channel.idle.set()
    channel.reply = "Error: barrier failed"
    with ThreadPoolExecutor() as worker:
        result = worker.submit(channel.await_settled, 1)
        assert channel.barrier_sent.wait(0.2)
        channel.barrier_finished.set()
        with pytest.raises(RuntimeError, match="M400"):
            result.result(timeout=1)


@pytest.mark.parametrize("dialect", ["standalone", "sbc"])
def test_abort_has_independent_request_and_session(dialect):
    channel = FakeRrf(dialect)
    channel.detect()
    channel.poll_gate = threading.Event()
    with ThreadPoolExecutor() as worker:
        result = worker.submit(channel.poll_state)
        try:
            assert channel.poll_entered.wait(0.2)
            start = time.monotonic()
            channel.abort_now()
            assert time.monotonic() - start < 0.1
            assert channel.abort_sent.is_set()
            assert not result.done()
        finally:
            channel.poll_gate.set()
        result.result(timeout=1)
    abort = next(
        call for call in channel.calls if call[1] == {"gcode": "M112"} or call[2] == "M112"
    )
    assert abort[3] != channel._session[1]


def test_no_live_motion_by_default():
    adapter = DuetAdapter(endpoint="http://127.0.0.1:1", password="local test only")
    with pytest.raises(UnsupportedCapabilityError, match="read-only"):
        adapter.execute(ActionRequest("motion.home_all", {}, timeout_s=2))
    with pytest.raises(UnsupportedCapabilityError, match="onsite"):
        adapter.reset_estop()


def test_no_endpoint_is_offline():
    adapter = DuetAdapter()
    assert not adapter.poll_state().detected
    identity = adapter.detect()
    assert identity.details["offline"]
    state = adapter.poll_state()
    assert state.detected
    assert state.identity == identity
    assert state.lifecycle == MachineLifecycle.DISCONNECTED
    assert not adapter.preflight().passed
    with pytest.raises(AdapterTransportError, match="completion barrier"):
        adapter.await_settled(0.1)
    assert not adapter.abort_now().sent


def test_pinned_vendor_rehearsal_captures_frames():
    pytest.importorskip("science_jubilee")
    request = ActionRequest("motion.move_to", {"x": 1, "y": 2, "z": 3}, timeout_s=2)
    pair = duet_factory(AdapterConfig("duet_real", options={"vendor_rehearsal": True}))
    assert pair.supervisor.detect().details["vendor_rehearsal"]
    result = pair.driver.execute(request)
    assert result.raw["rehearsal"]
    assert not result.accepted
    assert any(frame.startswith("G0") and "X1" in frame for frame in result.raw["frames"])


def test_sbc_probe_and_abort_over_loopback_http():
    requests = []
    model = {
        "boards": [{"name": "Duet fixture", "firmwareVersion": "3.test", "vIn": {"current": 24.0}}],
        "sensors": {"endstops": [{"triggered": False}, None]},
        "state": {"status": "idle", "currentTool": 0},
        "move": {"axes": [{"letter": "X", "userPosition": 42, "homed": True}]},
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            uri = urlsplit(self.path)
            requests.append((uri.path, self.headers.get("X-Session-Key"), None))
            if uri.path == "/machine/connect":
                assert parse_qs(uri.query)["password"] == ["mock secret"]
                payload = {"sessionKey": len(requests)}
            else:
                assert uri.path == "/machine/model"
                assert self.headers["X-Session-Key"] == "1"
                payload = model
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode("ascii"))

        def do_POST(self):
            uri = urlsplit(self.path)
            body = self.rfile.read(int(self.headers["Content-Length"])).decode("ascii")
            requests.append((uri.path, self.headers.get("X-Session-Key"), body))
            assert parse_qs(uri.query) == {"async": ["false"]}
            self.send_response(200)
            self.end_headers()

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server, ThreadPoolExecutor() as worker:
        worker.submit(server.serve_forever, 0.01)
        try:
            endpoint = f"http://127.0.0.1:{server.server_port}"
            pair = duet_factory(AdapterConfig("duet_real", endpoint, {"password": "mock secret"}))
            supervisor = pair.supervisor
            assert supervisor.detect().api_mode == ApiDialect.RRF_SBC
            assert supervisor.poll_state().tool_presence == ToolPresence.UNKNOWN
            report = supervisor.preflight()
            assert not report.passed
            assert report.details["endstops"] == [False, None]
            assert report.details["input_voltage_v"] == [24.0]
            assert all(body is None for _, _, body in requests)
            assert supervisor.abort_now().sent
            assert requests[-1] == ("/machine/code", str(len(requests) - 1), "M112")
            assert supervisor.poll_state().estop_latched
        finally:
            server.shutdown()
