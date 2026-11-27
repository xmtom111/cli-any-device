"""Verify agent skills and MCP dispatch through the installed CLI."""

from __future__ import annotations

import json
import os
import select
import subprocess
from dataclasses import replace
from importlib.metadata import version
from pathlib import Path
from typing import Any

import pytest
import yaml

from compiler import load_bundle, render_skill_md
from compiler.builder import MAX_SKILL_LINES
from core_types import ErrorCode, ExitCode
from test_security_and_ergonomics import (
    BUNDLE,
    REPO_ROOT,
    TARGET,
    _agent,
    _entrypoint,
    _human,
    _real_daemon,
)
from test_security_and_ergonomics import isolated_cli_config as isolated_cli_config

JUBILEE_SKILL = REPO_ROOT / "docs/generated/jubilee/SKILL.md"
META_SKILL = REPO_ROOT / "skills/cli-any-device-onboard/SKILL.md"


def _frontmatter(text: str, name: str) -> dict[str, Any]:
    lines = text.splitlines()
    assert lines[0] == "---"
    metadata = yaml.safe_load("\n".join(lines[1 : lines.index("---", 1)]))
    assert metadata["name"] == name
    assert isinstance(metadata["description"], str) and metadata["description"].strip()
    return metadata


def test_generated_skill_frontmatter_and_drift_gate() -> None:
    text = JUBILEE_SKILL.read_text(encoding="utf-8")
    _frontmatter(text, "cli-device-jubilee")
    assert len(text.splitlines()) <= MAX_SKILL_LINES
    result = subprocess.run(
        [_entrypoint("cli-device"), "bundle", "build", "jubilee", "--check"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_large_catalog_keeps_skill_within_line_budget() -> None:
    bundle = load_bundle(BUNDLE)
    services = dict(bundle.catalog.services)
    for index in range(MAX_SKILL_LINES):
        name = f"catalog.read_{index}"
        services[name] = replace(services["sys.detect"], name=name, returns_summary="line\nline")
    device = replace(bundle.device, display_name='Display: "α" #\nsecond line')
    text = render_skill_md(replace(bundle, device=device, catalog=replace(
        bundle.catalog, services=services,
    )))
    metadata = _frontmatter(text, "cli-device-jubilee")
    assert device.display_name.replace("\n", " ") in metadata["description"]
    assert len(text.splitlines()) <= MAX_SKILL_LINES
    assert "More commands: cli-device --bundle jubilee --help" in text
    assert "E-stop policy" in text


def test_onboarding_skill_frontmatter_and_protocol() -> None:
    text = META_SKILL.read_text(encoding="utf-8")
    _frontmatter(text, "cli-any-device-onboard")
    for term in (
        "device.yaml", "catalog.yaml", "cli-device bundle draft", "scpi_generic", "http_json",
        "moonraker_http", "adapter_options", "protocol", "SupervisorChannel", "ActionDriver",
        "R0_READ", "R4_EXPERT", "bounds", "confirm_if", "preconditions", "postconditions",
        "fail_safe", "SafetyKernel", "review_status: draft", "review_status: verified",
        "cli-device-d --allow-live-hardware", "cli-device bundle validate <bundle>",
        "cli-device bundle build <bundle> --check", "pytest tests/adapter_compliance",
    ):
        assert term in text


@pytest.mark.parametrize(
    ("selection", "name", "source"),
    [
        ("jubilee", "cli-device-jubilee", JUBILEE_SKILL),
        ("--meta", "cli-any-device-onboard", META_SKILL),
    ],
)
def test_skill_install_all(tmp_path: Path, selection: str, name: str, source: Path) -> None:
    status = _agent("skill", "install", selection, "--dest", "all", "--project-dir", str(tmp_path))
    paths = [tmp_path / host / "skills" / name / "SKILL.md" for host in (".claude", ".agents")]
    assert status == {"ok": True, "skill": name, "paths": [str(path) for path in paths]}
    for path in paths:
        assert path.read_bytes() == source.read_bytes()
        _frontmatter(path.read_text(encoding="utf-8"), name)


def test_install_default_bundle_to_custom_directory(tmp_path: Path) -> None:
    _agent("use", "jubilee", "--target", TARGET)
    dest = tmp_path / "custom skills"
    installed = _agent("skill", "install", "--dest", str(dest), "--project-dir", str(tmp_path))
    path = dest / "cli-device-jubilee" / "SKILL.md"
    assert installed["paths"] == [str(path)]
    assert path.read_bytes() == JUBILEE_SKILL.read_bytes()


@pytest.mark.parametrize("selection", [(), ("jubilee", "--meta"), ("missing-bundle",)])
def test_skill_install_rejects_invalid_selection(
    tmp_path: Path, selection: tuple[str, ...],
) -> None:
    result = _agent(
        "skill", "install", *selection, "--project-dir", str(tmp_path), code=ExitCode.INVALID_USAGE,
    )
    assert result["error"]["code"] == ErrorCode.E_INVALID_ARGS
    assert not (tmp_path / ".agents").exists()
    assert not (tmp_path / ".claude").exists()


def _rpc(method: str, request_id: Any, **params: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


def _envelope(result: dict[str, Any], code: ExitCode = ExitCode.OK) -> dict[str, Any]:
    envelope = result["structuredContent"]
    assert result["isError"] is (code != ExitCode.OK)
    assert result["content"] == [{"type": "text", "text": json.dumps(envelope)}]
    assert envelope["ok"] is (code == ExitCode.OK)
    assert envelope["exit_code"] == code
    return envelope


@pytest.mark.skipif(os.name != "posix", reason="Requires a POSIX pseudoterminal for plan approval.")
def test_mcp_stdio_with_authenticated_daemon_and_human_approval(tmp_path: Path) -> None:
    with _real_daemon(tmp_path, "--confirm-mode", "token") as base, subprocess.Popen(
        [_entrypoint("cli-device"), "--json", "--confirm", *base[4:], "mcp", "serve", *base[:4]],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ) as process:
        assert process.stdin is not None and process.stdout is not None
        reader, writer = process.stdout, process.stdin
        request_id = 0

        def exchange(message: str, expected_id: int | None) -> dict[str, Any]:
            writer.write(message + "\n")
            writer.flush()
            assert select.select([reader], [], [], 10)[0], "MCP response timed out."
            response = json.loads(reader.readline())
            assert response["id"] == expected_id
            assert response["jsonrpc"] == "2.0"
            return response

        def request(method: str, **params: Any) -> dict[str, Any]:
            nonlocal request_id
            request_id += 1
            return exchange(json.dumps(_rpc(method, request_id, **params)), request_id)

        def call(tool: str, **arguments: Any) -> dict[str, Any]:
            return request("tools/call", name=tool, arguments=arguments)["result"]

        try:
            initialized = request("initialize", protocolVersion="2025-06-18")["result"]
            assert initialized == {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "cli-any-device", "version": version("cli-any-device")},
            }
            assert request("initialize", protocolVersion=42)["result"]["protocolVersion"] == (
                "2024-11-05"
            )
            writer.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
            writer.write('{"jsonrpc":"2.0","method":"ping"}\n')
            assert request("ping")["result"] == {}

            listed = request("tools/list")["result"]["tools"]
            tools = {tool["name"]: tool for tool in listed}
            assert len(tools) == len(listed)
            assert {"sys_detect", "motion_home_all", "sys_abort"} <= tools.keys()
            assert all(not name.startswith("expert_") for name in tools)
            assert all(not tool["description"].startswith("[R4_EXPERT]") for tool in listed)
            assert all("confirm" not in tool["inputSchema"]["properties"] for tool in listed)
            assert tools["motion_move_to"]["inputSchema"]["required"] == ["x", "y", "z"]
            props = tools["motion_move_to"]["inputSchema"]["properties"]
            assert props["x"] == {
                "type": "number", "description": "Target X coordinate in mm.",
                "minimum": 0.0, "maximum": 300.0,
            }
            assert props["dry_run"] == {"type": "boolean", "default": False}
            assert props["plan_id"] == {"type": "string"}

            detected = _envelope(call("sys_detect"))
            assert detected["confirm_mode"] == "token"
            blocked = _envelope(call("motion_home_all"), ExitCode.CONFIRM_REQUIRED)
            assert blocked["error"]["code"] == ErrorCode.E_CONFIRM_REQUIRED
            assert blocked["state"] == detected["state"]
            assert _agent(*base, "sys", "status")["state"]["homed"] == []
            preview = _envelope(call("motion_home_all", dry_run=True))
            assert preview["mode"] == "dry_run"
            assert preview["state"] == detected["state"]
            plan_id = preview["data"]["plan"]["plan_id"]
            assert isinstance(plan_id, str) and plan_id
            unapproved = _envelope(
                call("motion_home_all", plan_id=plan_id), ExitCode.CONFIRM_REQUIRED,
            )
            assert unapproved["error"]["code"] == ErrorCode.E_CONFIRM_REQUIRED
            forged = request("tools/call", name="motion_home_all", arguments={"confirm": True})
            assert forged["error"]["code"] == -32602
            _human(*base, "approve", plan_id)
            homed = _envelope(call("motion_home_all", plan_id=plan_id))
            assert {"X", "Y", "Z"} <= set(homed["state"]["homed"])
            assert _agent(*base, "sys", "status")["state"] == homed["state"]

            denied = _envelope(call("motion_move_to", x=9999, y=0, z=0), ExitCode.SAFETY_DENIED)
            assert denied["error"]["code"] == ErrorCode.E_OUT_OF_BOUNDS
            assert denied["state"] == homed["state"]
            for name in ("expert_gcode", "not_a_tool"):
                assert request("tools/call", name=name)["error"]["code"] == -32602
            assert request("tools/call", name="sys_detect", arguments=[])["error"]["code"] == -32602
            assert request("missing/method")["error"]["code"] == -32601
            assert exchange('{"jsonrpc":"1.0","id":1,"method":"ping"}', None)["error"]["code"] == (
                -32600
            )
            assert exchange("{broken JSON", None)["error"]["code"] == -32700
            assert request("ping")["result"] == {}
        finally:
            writer.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                raise
        assert process.returncode == 0
        assert reader.read() == ""
        assert process.stderr is not None and process.stderr.read() == ""


def _mcp_once(message: dict[str, Any], *options: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_entrypoint("cli-device"), "mcp", "serve", *options],
        input=json.dumps(message) + "\n", capture_output=True, text=True, timeout=10, check=False,
    )


def test_mcp_reuses_saved_defaults_and_managed_daemon(isolated_cli_config: Path) -> None:
    _agent("use", "jubilee", "--target", TARGET)
    request = _rpc("tools/call", 1, name="sys_detect")
    started = _mcp_once(request)
    assert started.returncode == 0, started.stderr
    before = _envelope(json.loads(started.stdout)["result"])
    assert (isolated_cli_config / "daemon.json").is_file()

    attached = _mcp_once(request)
    assert attached.returncode == 0, attached.stderr
    after = _envelope(json.loads(attached.stdout)["result"])
    assert after["daemon_pid"] == before["daemon_pid"]


def test_mcp_filters_expert_group_and_risk_using_selected_bundle(tmp_path: Path) -> None:
    (tmp_path / "device.yaml").write_bytes((BUNDLE / "device.yaml").read_bytes())
    catalog_path = tmp_path / "catalog.yaml"
    catalog = yaml.safe_load((BUNDLE / "catalog.yaml").read_text(encoding="utf-8"))
    services = {service["name"]: service for service in catalog["services"]}
    services["expert.gcode"].update(risk_class="R0_READ", expert_only=False)
    services["motion.home_all"].update(risk_class="R4_EXPERT", expert_only=False)
    catalog_path.write_text(yaml.safe_dump(catalog), encoding="utf-8")

    options = ("--bundle", str(tmp_path), "--target", TARGET)
    request = _rpc("tools/list", 1)
    listed = _mcp_once(request, *options)
    assert listed.returncode == 0, listed.stderr
    names = {tool["name"] for tool in json.loads(listed.stdout)["result"]["tools"]}
    assert {"expert_gcode", "motion_home_all"}.isdisjoint(names)
    assert "sys_detect" in names

    catalog["services"].extend([
        {"name": name, "risk_class": "R0_READ"} for name in ("test.read_value", "test_read.value")
    ])
    catalog_path.write_text(yaml.safe_dump(catalog), encoding="utf-8")
    collision = _mcp_once(request, *options)
    assert collision.returncode != 0
    assert collision.stdout == ""
    assert "map to 'test_read_value'" in collision.stderr
