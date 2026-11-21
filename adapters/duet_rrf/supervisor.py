"""Direct RRF telemetry and safety commands for standalone and SBC HTTP."""

import json
import time
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _SessionExpired(ConnectionError):
    pass


class RrfSupervisor:
    def __init__(self, endpoint, password, *, request_timeout=2.0):
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Duet endpoint must be an HTTP(S) origin")
        self.endpoint = endpoint.rstrip("/")
        self._password = password
        self._request_timeout = request_timeout
        self._session = None

    def _request(self, path, *, query=None, body=None, key=None, deadline=None):
        timeout = self._request_timeout
        if deadline is not None:
            timeout = min(timeout, deadline - time.monotonic())
        if timeout <= 0:
            raise TimeoutError("Duet supervision deadline expired")
        uri = self.endpoint + path
        if query is not None:
            uri += "?" + urlencode(query)
        headers = {"Connection": "close"}
        if key is not None:
            headers["X-Session-Key"] = str(key)
        if body is not None:
            headers["Content-Type"] = "text/plain"
        request = Request(uri, headers=headers, data=None if body is None else body.encode("ascii"))
        # Each call opens its own connection, including abort during a blocked poll.
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        try:
            with opener.open(request, timeout=timeout) as response:
                return response.read().decode("utf-8")
        except HTTPError as exc:
            status = exc.code
            exc.close()
            if status == 404:
                raise FileNotFoundError(f"Duet HTTP endpoint unavailable: {path}") from None
            if status in {401, 403} and path not in {"/machine/connect", "/rr_connect"}:
                raise _SessionExpired(f"Duet session rejected at {path}") from None
            raise ConnectionError(f"Duet HTTP {status}: {path}") from None
        except (OSError, HTTPException) as exc:
            cause = exc.reason if isinstance(exc, URLError) else exc
            if deadline is not None and isinstance(cause, TimeoutError):
                raise TimeoutError(f"Duet HTTP request timed out at {path}") from None
            raise ConnectionError(f"Duet request failed at {path}: {type(exc).__name__}") from None

    def _connect(self, deadline=None):
        credentials = {"password": self._password}
        try:
            response = json.loads(
                self._request("/machine/connect", query=credentials, deadline=deadline)
            )
        except FileNotFoundError:
            response = json.loads(
                self._request(
                    "/rr_connect",
                    query={**credentials, "sessionKey": "yes"},
                    deadline=deadline,
                )
            )
            if response.get("err") != 0 or response.get("isEmulated") is True:
                raise ConnectionError("standalone RRF session rejected") from None
            return ("standalone", response.get("sessionKey"))
        if response.get("sessionKey") is None:
            raise ConnectionError("SBC Duet response has no session key")
        return ("sbc", response["sessionKey"])

    def _read(self, session, name, deadline=None):
        mode, key = session
        if mode == "sbc":
            return json.loads(self._request("/machine/model", key=key, deadline=deadline))[name]
        response = self._request(
            "/rr_model",
            query={"key": name, "flags": "d3"},
            key=key,
            deadline=deadline,
        )
        return json.loads(response)["result"]

    def _state(self, session, deadline=None):
        mode, key = session
        if mode == "sbc":
            model = json.loads(self._request("/machine/model", key=key, deadline=deadline))
        else:
            model = {name: self._read(session, name, deadline) for name in ("state", "move.axes")}
            model["move"] = {"axes": model.pop("move.axes")}
        axes = model["move"]["axes"]
        state = model["state"]
        tool = state.get("currentTool")
        status = state["status"].lower()
        return {
            "status": status,
            "position": {
                axis["letter"]: axis["userPosition"]
                for axis in axes
                if axis.get("userPosition") is not None
            },
            "homed": {axis["letter"] for axis in axes if axis.get("homed") is True},
            "active_tool": tool if type(tool) is int and tool >= 0 else None,
            "tool_known": type(tool) is int and tool >= -1,
            "tool_locked": None,
        }

    def detect(self):
        try:
            session = self._connect()
            self._state(session)
            boards = self._read(session, "boards")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ConnectionError(
                "Duet detect failed. See HOOKUP.md for Ethernet addressing, HTTP enablement, "
                f"and RRF 3.x object model checks ({exc})."
            ) from None
        self._session = session
        board = next((item for item in boards if item), {})
        return {
            "target": "duet_real",
            "endpoint": self.endpoint,
            "api_mode": session[0],
            "firmware": board.get("firmwareVersion"),
            "board": board.get("name"),
            "capabilities": {"rrf_object_model"},
        }

    def poll_state(self):
        if self._session is None:
            self.detect()
        try:
            return self._state(self._session)
        except _SessionExpired:
            self.detect()
            return self._state(self._session)

    def read_preflight_details(self):
        if self._session is None:
            self.detect()
        sensors = self._read(self._session, "sensors")
        boards = self._read(self._session, "boards")
        return {
            "endstops": [
                stop.get("triggered") if stop is not None else None
                for stop in (sensors or {}).get("endstops", [])
            ],
            "input_voltage_v": [
                (board.get("vIn") or {}).get("current") if board is not None else None
                for board in boards
            ],
        }

    def _send(self, session, command, deadline=None):
        mode, key = session
        if mode == "sbc":
            return self._request(
                "/machine/code",
                query={"async": "false"},
                body=command,
                key=key,
                deadline=deadline,
            )
        response = self._request("/rr_gcode", query={"gcode": command}, key=key, deadline=deadline)
        buffer_space = json.loads(response).get("buff")
        if type(buffer_space) is not int or buffer_space <= 0:
            raise RuntimeError("RRF did not report available G-code buffer space")
        return None

    def abort_now(self):
        # Separate authenticated session on keyed firmware, and a fresh HTTP
        # connection even on legacy firmware with one session per source IP.
        session = self._connect()
        reply = self._send(session, "M112")
        if reply is not None:
            self._check_reply(reply, "M112")

    @staticmethod
    def _check_reply(reply, command="M400"):
        if any(
            line.lstrip().lower().startswith(("error", "warning")) for line in reply.splitlines()
        ):
            raise RuntimeError(f"RRF reported an error or warning executing {command}")

    def await_settled(self, timeout):
        deadline = time.monotonic() + timeout
        session = self._session or self._connect(deadline)
        mode, key = session
        if mode == "standalone":
            # Standalone uses one outstanding command on this supervision
            # session. Drain previous replies before recording the baseline.
            self._request("/rr_reply", key=key, deadline=deadline)
            baseline = self._read(session, "seqs", deadline)["reply"]
            # An unrelated HTTP reply can also advance seqs.reply. Match the
            # HTTP M118 marker queued after M400 before checking machine state.
            marker = f"m400-{uuid4().hex}"
            self._send(session, f'M400\nM118 P3 S"{marker}" L0', deadline)
            while True:
                sequence = self._read(session, "seqs", deadline)["reply"]
                if sequence != baseline:
                    reply = self._request("/rr_reply", key=key, deadline=deadline)
                    self._check_reply(reply)
                    if any(line.strip() == marker for line in reply.splitlines()):
                        break
                    baseline = sequence
                self._pause(deadline)
        else:
            self._check_reply(self._send(session, "M400", deadline))

        while True:
            state = self._state(session, deadline)
            if state["status"] == "idle":
                return state
            if state["status"] in {"halted", "off", "updating"}:
                raise RuntimeError(f"Duet {state['status']}")
            self._pause(deadline)

    @staticmethod
    def _pause(deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("RRF did not settle")
        time.sleep(min(0.025, remaining))
