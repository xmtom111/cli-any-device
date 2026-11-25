"""MCP tools over newline-delimited JSON-RPC on stdio."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from math import isfinite
from typing import Any, BinaryIO, Never

_ARG_TYPES = {"float": "number", "int": "integer", "bool": "boolean", "string": "string"}
_CONTROL_FIELDS = {
    "dry_run": {"type": "boolean", "default": False},
    "plan_id": {"type": "string"},
}

Dispatch = Callable[[str, dict[str, Any], bool, str | None], dict[str, Any]]


class RPCError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class _Tool:
    service: str
    definition: dict[str, Any]


def _make_tools(schema: dict[str, Any]) -> dict[str, _Tool]:
    tools: dict[str, _Tool] = {}
    for service in schema["services"]:
        risk = service["risk_class"]
        if risk == "R4_EXPERT" or service["cli_group"] == "expert":
            continue
        service_name = service["name"]
        name = service_name.replace(".", "_")
        if name in tools:
            raise ValueError(
                f"Services {tools[name].service!r} and {service_name!r} map to {name!r}."
            )

        properties: dict[str, Any] = {}
        required = []
        for arg in service["args"]:
            arg_name = arg["name"]
            if arg_name in _CONTROL_FIELDS or arg_name == "confirm":
                raise ValueError(
                    f"Service {service_name!r} uses reserved MCP argument {arg_name!r}."
                )
            prop = {"type": _ARG_TYPES[arg["type"]], "description": arg["description"]}
            if arg["min_value"] is not None:
                prop["minimum"] = arg["min_value"]
            if arg["max_value"] is not None:
                prop["maximum"] = arg["max_value"]
            properties[arg_name] = prop
            if arg["required"]:
                required.append(arg_name)
        properties.update(_CONTROL_FIELDS)
        tools[name] = _Tool(service_name, {
            "name": name,
            "description": (
                f"[{risk}] {service['summary']} "
                "Use dry_run to preview and plan_id to execute after human approval "
                "with cli-device approve."
            ),
            "inputSchema": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        })
    return tools


def _matches_type(value: Any, json_type: str) -> bool:
    if json_type == "boolean":
        return isinstance(value, bool)
    if json_type == "string":
        return isinstance(value, str)
    if isinstance(value, float):
        return isfinite(value) and (json_type == "number" or value.is_integer())
    return isinstance(value, int) and not isinstance(value, bool)


def _reject_constant(value: str) -> Never:
    raise ValueError(f"Invalid JSON constant {value}.")


class _Server:
    def __init__(self, schema: dict[str, Any], dispatch: Dispatch) -> None:
        self.tools = _make_tools(schema)
        self.dispatch = dispatch
        try:
            self.version = version("cli-any-device")
        except PackageNotFoundError:
            self.version = "unknown"

    def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or name not in self.tools:
            raise RPCError(-32602, f"Unknown tool {name!r}.")
        tool = self.tools[name]
        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict):
            raise RPCError(-32602, "Tool arguments must be an object.")

        properties = tool.definition["inputSchema"]["properties"]
        for key, value in arguments.items():
            if key not in properties:
                raise RPCError(-32602, f"Unknown argument {key!r} for tool {name!r}.")
            json_type = properties[key]["type"]
            if not _matches_type(value, json_type):
                raise RPCError(-32602, f"Argument {key!r} must have type {json_type}.")

        device_args = {key: value for key, value in arguments.items() if key not in _CONTROL_FIELDS}
        envelope = self.dispatch(
            tool.service, device_args, arguments.get("dry_run", False), arguments.get("plan_id"),
        )
        return {
            "content": [{"type": "text", "text": json.dumps(envelope)}],
            "isError": not bool(envelope.get("ok")),
            "structuredContent": envelope,
        }

    def _request(self, method: str, params: Any) -> dict[str, Any]:
        if method not in ("initialize", "ping", "tools/list", "tools/call"):
            raise RPCError(-32601, f"Method not found: {method}.")
        if not isinstance(params, dict):
            raise RPCError(-32602, "Params must be an object.")
        if method == "initialize":
            protocol = params.get("protocolVersion")
            return {
                "protocolVersion": protocol if isinstance(protocol, str) else "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "cli-any-device", "version": self.version},
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [tool.definition for tool in self.tools.values()]}
        return self._call_tool(params)

    def handle(self, line: bytes) -> dict[str, Any] | None:
        response: dict[str, Any] = {"jsonrpc": "2.0", "id": None}
        try:
            request = json.loads(line.decode("utf-8"), parse_constant=_reject_constant)
        except (ValueError, RecursionError):
            return {**response, "error": {"code": -32700, "message": "Parse error."}}
        try:
            if (
                not isinstance(request, dict)
                or request.get("jsonrpc") != "2.0"
                or not isinstance(request.get("method"), str)
            ):
                raise RPCError(-32600, "Invalid request.")
            method = request["method"]
            if "id" not in request or method == "notifications/initialized":
                return None
            request_id = request["id"]
            if request_id is not None and not (
                isinstance(request_id, str) or _matches_type(request_id, "number")
            ):
                raise RPCError(-32600, "Invalid request ID.")
            response["id"] = request_id
            response["result"] = self._request(method, request.get("params", {}))
        except RPCError as exc:
            response["error"] = {"code": exc.code, "message": str(exc)}
        return response


def serve(
    schema: dict[str, Any], dispatch: Dispatch, stdin: BinaryIO, stdout: BinaryIO,
) -> None:
    server = _Server(schema, dispatch)
    for line in stdin:
        response = server.handle(line)
        if response is not None:
            stdout.write((json.dumps(response) + "\n").encode("utf-8"))
            stdout.flush()
