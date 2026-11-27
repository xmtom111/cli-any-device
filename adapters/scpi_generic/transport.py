"""SCPI text framing over TCP, USBTMC and optional pyserial."""

from __future__ import annotations

import errno
import importlib
import select
import socket
import struct
from contextlib import suppress
from typing import Any
from urllib.parse import unquote, urlsplit

from adapters._generic import time_left

from core_types import AdapterTimeoutError, AdapterTransportError, ApiDialect

_MAX_REPLY = 65536
_USBTMC_SET_TIMEOUT = 0x40045B0A  # Linux _IOW(91, 10, __u32)


class ScpiTransport:
    def __init__(self, endpoint: str, terminator: str, baudrate: int) -> None:
        try:
            uri = urlsplit(endpoint)
            self.scheme = uri.scheme
            self._path = unquote(uri.path)
            self._terminator = terminator.encode("ascii")
            self._baudrate = baudrate
            if not self._terminator or "@" in uri.netloc or uri.query or uri.fragment:
                raise ValueError
            if uri.scheme == "tcp" and uri.hostname and uri.port and not uri.path:
                self._address = (uri.hostname, uri.port)
            elif (
                uri.scheme not in {"usbtmc", "serial"}
                or uri.netloc
                or not self._path.startswith("/")
            ):
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise AdapterTransportError(
                "Expected a tcp://host:port, usbtmc:///dev/... or serial:///dev/... endpoint "
                "and a nonempty ASCII terminator.",
                step="detect",
            ) from exc
        self.dialect = ApiDialect.TCP_SCPI if self.scheme == "tcp" else ApiDialect.SERIAL_TEXT
        self._socket: socket.socket | None = None
        self._serial: Any = None

    def close(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        if self._serial is not None:
            self._serial.close()
            self._serial = None

    def exchange(
        self, command: str, deadline: float, step: str, *, query: bool = False,
        independent: bool = False,
    ) -> str:
        try:
            frame = command.encode("ascii") + self._terminator
            if self.scheme == "tcp":
                reply = self._tcp(frame, deadline, step, query, independent)
            elif self.scheme == "serial":
                reply = self._tty(frame, deadline, step, query, independent)
            else:
                reply = self._usb(frame, deadline, step, query)
            time_left(deadline, step)
            if query and not reply:
                raise AdapterTransportError("SCPI device closed without a reply.", step=step)
            return reply.removesuffix(self._terminator).decode("ascii").strip()
        except (TimeoutError, AdapterTimeoutError) as exc:
            raise AdapterTimeoutError("SCPI exchange timed out.", step=step) from exc
        except OSError as exc:
            if exc.errno == errno.ETIMEDOUT:
                raise AdapterTimeoutError("SCPI exchange timed out.", step=step) from exc
            raise AdapterTransportError(
                f"SCPI {self.scheme} exchange failed: {exc}", step=step,
                hookup_ref="HOOKUP.md#sec-troubleshooting",
            ) from exc
        except UnicodeError as exc:
            raise AdapterTransportError("SCPI exchange requires ASCII text.", step=step) from exc

    def _tcp(
        self, frame: bytes, deadline: float, step: str, query: bool, independent: bool
    ) -> bytes:
        conn = None if independent else self._socket
        if conn is not None and select.select([conn], [], [], 0)[0]:
            # A completed write-only exchange may have been closed by the peer.
            pending = b""
            with suppress(OSError):
                pending = conn.recv(1, socket.MSG_PEEK)
            conn.close()
            conn = self._socket = None
            if pending:
                raise AdapterTransportError("Unexpected SCPI reply before command.", step=step)
        if conn is None:
            conn = socket.create_connection(self._address, timeout=time_left(deadline, step))
            if not independent:
                self._socket = conn
        try:
            conn.settimeout(time_left(deadline, step))
            conn.sendall(frame)
            if not query:
                return b""
            data = bytearray()
            while self._terminator not in data and len(data) < _MAX_REPLY:
                conn.settimeout(time_left(deadline, step))
                chunk = conn.recv(min(4096, _MAX_REPLY - len(data)))
                if not chunk:
                    conn.close()
                    self._socket = None
                    break
                data.extend(chunk)
            if len(data) == _MAX_REPLY and self._terminator not in data:
                raise AdapterTransportError("SCPI reply exceeds the maximum frame size.", step=step)
            return bytes(data).partition(self._terminator)[0]
        except (OSError, AdapterTimeoutError, AdapterTransportError):
            if not independent:
                conn.close()
                self._socket = None
            raise
        finally:
            if independent:
                conn.close()

    def _tty(
        self, frame: bytes, deadline: float, step: str, query: bool, independent: bool
    ) -> bytes:
        try:
            serial = importlib.import_module("serial")
        except ImportError as exc:
            raise AdapterTransportError(
                "serial:// endpoints require the optional pyserial package.", step=step
            ) from exc
        port = None if independent else self._serial
        try:
            if port is None:
                port = serial.Serial(
                    self._path, baudrate=self._baudrate, write_timeout=time_left(deadline, step)
                )
                if not independent:
                    self._serial = port
            port.write_timeout = time_left(deadline, step)
            port.write(frame)
            if not query:
                return b""
            port.timeout = time_left(deadline, step)
            data: bytes = port.read_until(self._terminator, _MAX_REPLY)
            if data.endswith(self._terminator):
                return data
            if len(data) >= _MAX_REPLY:
                raise AdapterTransportError("SCPI reply exceeds the maximum frame size.", step=step)
            raise AdapterTimeoutError("No complete serial SCPI reply.", step=step)
        except serial.SerialTimeoutException as exc:
            raise AdapterTimeoutError("Serial SCPI write timed out.", step=step) from exc
        except (OSError, AdapterTimeoutError, AdapterTransportError):
            if port is not None and not independent:
                port.close()
                self._serial = None
            raise
        finally:
            if port is not None and independent:
                port.close()

    def _usb(self, frame: bytes, deadline: float, step: str, query: bool) -> bytes:
        import fcntl

        with open(self._path, "r+b", buffering=0) as device:
            fcntl.ioctl(
                device, _USBTMC_SET_TIMEOUT,
                struct.pack("=I", max(1, int(time_left(deadline, step) * 1000))),
            )
            if device.write(frame) != len(frame):
                raise AdapterTransportError("Incomplete USBTMC write.", step=step)
            if not query:
                return b""
            fcntl.ioctl(
                device, _USBTMC_SET_TIMEOUT,
                struct.pack("=I", max(1, int(time_left(deadline, step) * 1000))),
            )
            return device.read(_MAX_REPLY) or b""
