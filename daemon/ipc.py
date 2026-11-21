"""Local Unix domain socket JSON IPC server and client for cli-device-d <-> cli-device.

Uses ThreadingUnixStreamServer so a second client sending ``sys.abort`` is served
concurrently without waiting on another connection's active motion call.
"""

from __future__ import annotations

import json
import os
import socket
import socketserver
from pathlib import Path
from typing import Any

from core_types import ErrorCode, ErrorDetail, ExecutionMode, ExitCode
from daemon.service import DeviceDaemon


class _IPCRequestHandler(socketserver.StreamRequestHandler):
    server: DaemonIPCServer

    def handle(self) -> None:
        raw_line = self.rfile.readline()
        if not raw_line:
            return
        try:
            req = json.loads(raw_line.decode("utf-8"))
            if not isinstance(req, dict):
                raise ValueError("IPC request must be a JSON object.")
            envelope = self.server.daemon.dispatch(
                target=str(req.get("target", "")),
                service=str(req.get("service", "")),
                args=dict(req.get("args") or {}),
                dry_run=bool(req.get("dry_run", False)),
                confirm=bool(req.get("confirm", False)),
            )
            payload = envelope.to_dict()
        except Exception as exc:
            payload = {
                "ok": False,
                "service": "ipc",
                "target": "",
                "mode": ExecutionMode.LIVE.value,
                "duration_ms": 0,
                "exit_code": int(ExitCode.INVALID_USAGE),
                "state": {},
                "data": {},
                "error": ErrorDetail(
                    code=ErrorCode.E_INVALID_ARGS,
                    step="ipc_decode",
                    reason=str(exc),
                ).to_dict(),
            }
        self.wfile.write((json.dumps(payload) + "\n").encode("utf-8"))
        self.wfile.flush()


class DaemonIPCServer(socketserver.ThreadingUnixStreamServer):
    """Multi-threaded Unix domain socket server hosting a DeviceDaemon."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, *, socket_path: Path, daemon: DeviceDaemon) -> None:
        self.socket_path = socket_path
        self.daemon = daemon
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        if socket_path.exists():
            os.unlink(socket_path)
        super().__init__(str(socket_path), _IPCRequestHandler)

    def server_close(self) -> None:
        super().server_close()
        self.daemon.close()
        if self.socket_path.exists():
            os.unlink(self.socket_path)


class DaemonIPCClient:
    """Thin IPC client used by cli-device to invoke commands on cli-device-d."""

    def __init__(self, socket_path: Path, *, timeout_s: float = 120.0) -> None:
        self._socket_path = socket_path
        self._timeout_s = timeout_s

    def call(
        self,
        *,
        target: str,
        service: str,
        args: dict[str, Any] | None = None,
        dry_run: bool = False,
        confirm: bool = False,
    ) -> dict[str, Any]:
        req = {
            "target": target,
            "service": service,
            "args": dict(args or {}),
            "dry_run": dry_run,
            "confirm": confirm,
        }
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self._timeout_s)
                sock.connect(str(self._socket_path))
                sock.sendall((json.dumps(req) + "\n").encode("utf-8"))
                chunks: list[bytes] = []
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    if b"\n" in chunk:
                        break
        except TimeoutError:
            return {
                "ok": False,
                "service": service,
                "target": target,
                "mode": ExecutionMode.DRY_RUN.value if dry_run else ExecutionMode.LIVE.value,
                "duration_ms": 0,
                "exit_code": int(ExitCode.TIMEOUT),
                "state": {},
                "data": {},
                "error": ErrorDetail(
                    code=ErrorCode.E_TIMEOUT,
                    step="ipc_client",
                    reason=f"Timed out waiting for daemon at {self._socket_path}.",
                ).to_dict(),
            }
        except OSError as exc:
            return {
                "ok": False,
                "service": service,
                "target": target,
                "mode": ExecutionMode.DRY_RUN.value if dry_run else ExecutionMode.LIVE.value,
                "duration_ms": 0,
                "exit_code": int(ExitCode.TIMEOUT),
                "state": {},
                "data": {},
                "error": ErrorDetail(
                    code=ErrorCode.E_DAEMON_UNAVAILABLE,
                    step="ipc_connect",
                    reason=(
                        f"Cannot connect to cli-device-d at {self._socket_path}: {exc}. "
                        "Start daemon with `cli-device-d --bundle <bundle>`."
                    ),
                ).to_dict(),
            }

        raw = b"".join(chunks).decode("utf-8").strip()
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("Malformed IPC response from daemon.")
        return parsed
