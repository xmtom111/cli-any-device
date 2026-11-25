"""Threaded local JSON IPC over Unix sockets or authenticated loopback TCP."""

from __future__ import annotations

import errno
import hmac
import json
import os
import secrets
import socket
import socketserver
import stat
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from core_types import CommandEnvelope, ErrorCode, ErrorDetail, ExecutionMode, ExitCode, RiskClass
from daemon.service import DeviceDaemon

DEFAULT_SOCKET_PATH = "/tmp/cli-device-d.sock" if os.name != "nt" else str(
    Path.home() / ".cli-device-d.sock"
)


def tcp_address(listen: str) -> tuple[str, int]:
    parsed = urlsplit(listen)
    host = parsed.hostname
    if (
        parsed.scheme != "tcp"
        or host not in ("127.0.0.1", "localhost", "::1")
        or parsed.port is None
        or parsed.username is not None
        or parsed.path
        or "?" in listen
        or "#" in listen
    ):
        raise ValueError("TCP IPC requires tcp://127.0.0.1:<port>, localhost, or [::1].")
    return ("127.0.0.1" if host == "localhost" else host), parsed.port


def unix_path(listen: str) -> Path:
    path = listen.removeprefix("unix://")
    if path == listen or not path:
        raise ValueError("Specify IPC as unix:///path/to.sock or tcp://127.0.0.1:<port>.")
    return Path(path)


def ipc_error(
    code: ErrorCode,
    exit_code: ExitCode,
    step: str,
    reason: str,
    *,
    service: str = "ipc",
    target: str = "",
    dry_run: bool = False,
) -> dict[str, Any]:
    return CommandEnvelope(
        ok=False,
        service=service,
        target=target,
        mode=ExecutionMode.DRY_RUN if dry_run else ExecutionMode.LIVE,
        duration_ms=0,
        exit_code=exit_code,
        state={},
        error=ErrorDetail(code, step, reason),
    ).to_dict()


def _dispatch(daemon: DeviceDaemon, req: dict[str, Any]) -> CommandEnvelope:
    op = req.get("op", "dispatch")
    target = str(req.get("target", ""))
    if op == "dispatch":
        return daemon.dispatch(
            target=target,
            service=str(req.get("service", "")),
            args=dict(req.get("args") or {}),
            dry_run=bool(req.get("dry_run", False)),
            confirm=bool(req.get("confirm", False)),
            plan_id=str(req["plan_id"]) if req.get("plan_id") is not None else None,
        )
    if op == "approve_plan":
        return daemon.approve_plan(str(req["plan_id"]))
    if op == "grant_lease":
        allowed = req.get("allowed_services")
        if allowed is not None and (
            not isinstance(allowed, list) or not all(isinstance(name, str) for name in allowed)
        ):
            raise ValueError("allowed_services must be a list of service names.")
        return daemon.grant_lease(
            target,
            RiskClass(req["max_risk"]),
            float(req["ttl_s"]),
            frozenset(allowed) if allowed is not None else None,
        )
    if op == "revoke_lease":
        return daemon.revoke_lease(target)
    if op == "lease_status":
        return daemon.lease_status(target)
    raise ValueError(f"Unknown IPC operation {op!r}.")


class _IPCRequestHandler(socketserver.StreamRequestHandler):
    server: _DaemonServer

    def handle(self) -> None:
        raw_line = self.rfile.readline()
        if not raw_line:
            return
        try:
            req = json.loads(raw_line)
            if not isinstance(req, dict):
                raise ValueError("IPC request must be a JSON object.")
            token = self.server.auth_token
            supplied = req.get("token")
            if token is not None and not (
                isinstance(supplied, str) and hmac.compare_digest(
                    supplied.encode("utf-8", "surrogatepass"), token.encode("utf-8")
                )
            ):
                payload = ipc_error(
                    ErrorCode.E_PRECONDITION_FAILED, ExitCode.SAFETY_DENIED,
                    "ipc_auth", "A valid IPC bearer token is required.",
                )
            else:
                payload = _dispatch(self.server.daemon, req).to_dict()
                payload["confirm_mode"] = self.server.daemon.confirm_mode.value
                payload["daemon_pid"] = os.getpid()
        except Exception as exc:
            payload = ipc_error(
                ErrorCode.E_INVALID_ARGS, ExitCode.INVALID_USAGE, "ipc_decode", str(exc)
            )
        with suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write((json.dumps(payload) + "\n").encode())


class _DaemonServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    daemon: DeviceDaemon
    listen_uri: str
    auth_token: str | None

    def server_close(self) -> None:
        super().server_close()
        self.daemon.close()


class DaemonIPCServer(_DaemonServer):
    """Threaded Unix domain socket server."""

    address_family = getattr(socket, "AF_UNIX", socket.AF_UNSPEC)

    def __init__(self, *, socket_path: Path, daemon: DeviceDaemon) -> None:
        if self.address_family == socket.AF_UNSPEC:
            raise ValueError("Unix sockets are unavailable. Use loopback TCP IPC.")
        self.socket_path = socket_path
        self.daemon = daemon
        self.listen_uri = f"unix://{socket_path}"
        self.auth_token = None
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        if socket_path.exists() and stat.S_ISSOCK(socket_path.lstat().st_mode):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                try:
                    probe.settimeout(0.2)
                    probe.connect(str(socket_path))
                except OSError as exc:
                    if exc.errno == errno.ECONNREFUSED:
                        socket_path.unlink(missing_ok=True)
                    else:
                        raise
        self._socket_identity: tuple[int, int] | None = None
        # TCPServer also accepts path addresses when address_family is AF_UNIX.
        super().__init__(str(socket_path), _IPCRequestHandler)  # type: ignore[arg-type]

    def server_bind(self) -> None:
        super().server_bind()
        info = self.socket_path.stat()
        self._socket_identity = (info.st_dev, info.st_ino)
        self.socket_path.chmod(0o600)

    def server_close(self) -> None:
        try:
            super().server_close()
        finally:
            if self._socket_identity is not None and self.socket_path.exists():
                info = self.socket_path.stat()
                if (info.st_dev, info.st_ino) == self._socket_identity:
                    self.socket_path.unlink(missing_ok=True)


class DaemonTCPServer(_DaemonServer):
    """Threaded TCP server bound exclusively to a loopback address."""

    allow_reuse_address = True

    def __init__(
        self, *, listen: str, daemon: DeviceDaemon, auth_token: str | None = None
    ) -> None:
        host, port = tcp_address(listen)
        self.address_family = socket.AF_INET6 if host == "::1" else socket.AF_INET
        self.auth_token = auth_token or os.environ.get("CLI_DEVICE_IPC_TOKEN")
        if not self.auth_token:
            self.auth_token = secrets.token_urlsafe(32)
        self.daemon = daemon
        super().__init__((host, port), _IPCRequestHandler)
        uri_host = f"[{host}]" if ":" in host else host
        self.listen_uri = f"tcp://{uri_host}:{self.server_address[1]}"


class DaemonIPCClient:
    """IPC client with backward-compatible positional Unix socket support."""

    def __init__(
        self,
        socket_path: Path | None = None,
        *,
        listen: str | None = None,
        token: str | None = None,
        timeout_s: float = 120.0,
    ) -> None:
        if listen is None:
            listen = f"unix://{socket_path or DEFAULT_SOCKET_PATH}"
        self._listen = listen
        self._tcp_address = tcp_address(listen) if listen.startswith("tcp://") else None
        self._socket_path = unix_path(listen) if self._tcp_address is None else None
        self._token = token if token is not None else os.environ.get("CLI_DEVICE_IPC_TOKEN")
        self._timeout_s = timeout_s

    def _connect(self) -> socket.socket:
        if self._tcp_address is not None:
            return socket.create_connection(self._tcp_address, self._timeout_s)
        if DaemonIPCServer.address_family == socket.AF_UNSPEC:
            raise OSError("Unix sockets are unavailable. Use loopback TCP IPC.")
        conn = socket.socket(DaemonIPCServer.address_family, socket.SOCK_STREAM)
        try:
            conn.settimeout(self._timeout_s)
            conn.connect(str(self._socket_path))
        except OSError:
            conn.close()
            raise
        return conn

    def call(
        self,
        *,
        target: str = "",
        service: str = "",
        args: dict[str, Any] | None = None,
        dry_run: bool = False,
        confirm: bool = False,
        plan_id: str | None = None,
        op: str = "dispatch",
        **control: Any,
    ) -> dict[str, Any]:
        req = {
            "op": op,
            "target": target,
            "service": service,
            "args": args or {},
            "dry_run": dry_run,
            "confirm": confirm,
            "plan_id": plan_id,
            **control,
        }
        if self._tcp_address is not None:
            req["token"] = self._token

        connected = False
        try:
            with self._connect() as conn:
                connected = True
                conn.sendall((json.dumps(req) + "\n").encode())
                with conn.makefile("rb") as reader:
                    raw = reader.readline()
        except OSError as exc:
            code = ErrorCode.E_DAEMON_UNAVAILABLE
            if connected:
                code = (
                    ErrorCode.E_TIMEOUT if isinstance(exc, TimeoutError)
                    else ErrorCode.E_TRANSPORT_ERROR
                )
            return ipc_error(
                code,
                ExitCode.TIMEOUT,
                "ipc_client",
                f"IPC request to {self._listen} failed: {exc}.",
                service=service or op,
                target=target,
                dry_run=dry_run,
            )
        if not raw:
            return ipc_error(
                ErrorCode.E_TRANSPORT_ERROR, ExitCode.EXECUTION_ERROR, "ipc_client",
                f"Daemon at {self._listen} closed the connection before replying.",
                service=service or op, target=target, dry_run=dry_run,
            )
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("Malformed IPC response from daemon.")
        return parsed


def read_token_file(path: Path) -> str:
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError("IPC token file is empty.")
    return token
