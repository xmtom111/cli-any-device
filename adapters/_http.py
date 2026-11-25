"""Direct HTTP transport and JSON response mapping for the HTTP adapters."""

from __future__ import annotations

import http.client
import json
import os
import re
from typing import Any
from urllib.parse import urlsplit

from adapters._generic import time_left

from core_types import AdapterTimeoutError, AdapterTransportError

_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_MAX_BODY = 1 << 20
MISSING = object()


def json_path(value: Any, path: str) -> Any:
    for key in path.split("."):
        if isinstance(value, dict) and key in value:
            value = value[key]
        elif isinstance(value, list) and key.isascii() and key.isdecimal():
            index = int(key)
            value = value[index] if index < len(value) else MISSING
        else:
            return MISSING
    return value


def map_properties(payload: Any, mapping: dict[str, str]) -> dict[str, Any]:
    return {
        name: value
        for name, path in mapping.items()
        if (value := json_path(payload, path)) is not MISSING
    }


class JsonHttpClient:
    def __init__(self, endpoint: str, options: dict[str, Any]) -> None:
        try:
            uri = urlsplit(endpoint)
            if (
                uri.scheme not in {"http", "https"} or not uri.hostname
                or "@" in uri.netloc or uri.query or uri.fragment
            ):
                raise ValueError
            self._host = uri.hostname
            self._port = uri.port
            self._prefix = uri.path.rstrip("/")
            self._connection = (
                http.client.HTTPSConnection if uri.scheme == "https" else http.client.HTTPConnection
            )
        except ValueError as exc:
            raise AdapterTransportError("Expected an HTTP(S) endpoint URI.", step="detect") from exc
        self._auth_header: str | None = options.get("auth_header")
        self._auth_env_var: str | None = options.get("auth_env_var")
        if bool(self._auth_header) != bool(self._auth_env_var) or (
            self._auth_header and (
                not _TOKEN.fullmatch(self._auth_header)
                or self._auth_header.lower() in {
                    "host", "connection", "content-length", "content-type", "transfer-encoding",
                }
            )
        ):
            raise AdapterTransportError(
                "Specify auth_header and auth_env_var together.", step="detect"
            )

    def request(
        self, method: str, path: str, deadline: float, step: str, *,
        body: Any = None, abort: bool = False,
    ) -> Any:
        if (
            not _TOKEN.fullmatch(method) or not path.startswith("/") or path.startswith("//")
            or "#" in path or any(ord(char) <= 32 or ord(char) == 127 for char in path)
        ):
            raise AdapterTransportError("Invalid HTTP method or endpoint-relative path.", step=step)
        headers = {"Accept": "application/json", "Connection": "close"}
        if self._auth_header and self._auth_env_var:
            secret = os.getenv(self._auth_env_var)
            if not secret or "\r" in secret or "\n" in secret:
                raise AdapterTransportError(
                    "HTTP auth environment value is missing or invalid.", step=step
                )
            headers[self._auth_header] = secret
        conn = self._connection(self._host, self._port, timeout=time_left(deadline, step))
        try:
            data = None
            if body is not None:
                data = (
                    body if isinstance(body, str) else json.dumps(body, allow_nan=False)
                ).encode()
                headers["Content-Type"] = "application/json"
            conn.connect()
            sock = conn.sock
            if sock is None:
                raise OSError("HTTP connection has no socket.")
            sock.settimeout(time_left(deadline, step))
            conn.request(method, self._prefix + path, body=data, headers=headers)
            try:
                sock.settimeout(time_left(deadline, step))
                response = conn.getresponse()
            except (AdapterTimeoutError, TimeoutError):
                if abort:
                    return None  # The stop request has already been transmitted.
                raise
            with response:
                if not 200 <= response.status < 300:
                    raise AdapterTransportError(
                        f"Device returned HTTP {response.status}.", step=step
                    )
                if abort:
                    return None
                payload = bytearray()
                while not response.isclosed():
                    sock.settimeout(time_left(deadline, step))
                    chunk = response.read1(min(65536, _MAX_BODY + 1 - len(payload)))
                    if not chunk:
                        break
                    payload.extend(chunk)
                    if len(payload) > _MAX_BODY:
                        raise AdapterTransportError("Device JSON reply is too large.", step=step)
                time_left(deadline, step)
                return json.loads(payload) if payload else None
        except TimeoutError as exc:
            raise AdapterTimeoutError("HTTP device response timed out.", step=step) from exc
        except (OSError, http.client.HTTPException, ValueError) as exc:
            raise AdapterTransportError(
                f"HTTP device exchange failed ({type(exc).__name__}).", step=step,
                hookup_ref="HOOKUP.md#sec-troubleshooting",
            ) from exc
        finally:
            conn.close()
