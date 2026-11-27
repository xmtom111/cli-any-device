"""Thin schema-driven CLI (``cli-device``) and generic device bundle toolchain.

Import rule: cli may ONLY import core_types, compiler, daemon, click, and stdlib.
Device subcommands are generated dynamically at runtime from the selected bundle's
compiled ``cli_schema.json`` (or YAML bundle), never hardcoded to a single machine.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

import click

from compiler import (
    BuildDriftError,
    BundleValidationError,
    build_bundle,
    draft_bundle,
    load_bundle,
    render_cli_schema,
    resolve_bundle_dir,
    validate_bundle,
)
from core_types import ConfirmMode, ErrorCode, ExitCode, RiskClass
from daemon import DEFAULT_SOCKET_PATH, DaemonIPCClient
from daemon.ipc import ipc_error, read_token_file, tcp_address, unix_path

from . import mcp
from .config import MANAGED_LISTEN, read_config, read_daemon, save_config, start_daemon, stop_daemon
from .skills import install_skill

REPO_ROOT = Path(__file__).resolve().parent.parent
HELP_CONTEXT = {"help_option_names": ["-h", "--help"]}
SLOGAN = "Let AI drive all your devices"


@dataclass
class CLIContext:
    """Shared top-level options propagated to dynamic device subcommands."""

    bundle: str | None
    target: str | None
    listen: str
    requested_listen: str
    explicit_listen: str | None
    auto_start: bool
    token_file: Path | None
    managed_token: str | None
    as_json: bool
    dry_run: bool
    confirm: bool
    plan_id: str | None


def _load_schema_for_bundle(bundle_ref: str, *, use_generated: bool = True) -> dict[str, Any]:
    bundle_dir = resolve_bundle_dir(bundle_ref, repo_root=REPO_ROOT)
    loaded = load_bundle(bundle_dir)
    if not use_generated:
        validate_bundle(loaded)
        return render_cli_schema(loaded)
    schema_path = REPO_ROOT / "docs" / "generated" / loaded.device.device_id / "cli_schema.json"
    if schema_path.exists():
        parsed = json.loads(schema_path.read_text(encoding="utf-8"))
        if isinstance(parsed, dict) and parsed.get("device_id") == loaded.device.device_id:
            return parsed
    return render_cli_schema(loaded)


def _format_state_brief(state: dict[str, Any]) -> str:
    parts: list[str] = []
    if state.get("lifecycle"):
        parts.append(f"lifecycle={state['lifecycle']}")
    if state.get("estop_latched"):
        parts.append("ESTOP=LATCHED")
    if state.get("homed"):
        parts.append(f"homed={','.join(state['homed'])}")
    if state.get("active_tool") is not None:
        parts.append(f"tool={state['active_tool']}")
    pos = state.get("pos")
    if isinstance(pos, dict) and pos:
        coords = " ".join(f"{k}={v}" for k, v in pos.items())
        parts.append(f"pos=({coords})")
    props = state.get("properties")
    if isinstance(props, dict) and props:
        kv = ", ".join(f"{k}={v}" for k, v in sorted(props.items()))
        parts.append(f"props=({kv})")
    return " ".join(parts)


def _emit_envelope(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        click.echo(json.dumps(payload, indent=2))
        return

    status = "OK" if payload.get("ok") else "FAIL"
    svc = payload.get("service", "")
    tgt = payload.get("target", "")
    mode = payload.get("mode", "live")
    dur = payload.get("duration_ms")
    dur_str = f", {dur}ms" if isinstance(dur, int) else ""
    click.echo(f"[{status}] {svc} (target={tgt}, mode={mode}{dur_str})")

    err = payload.get("error")
    if isinstance(err, dict):
        click.echo(f"  error: {err.get('code')} ({err.get('step')}): {err.get('reason')}")
        if err.get("hookup_ref"):
            click.echo(f"  hookup: {err.get('hookup_ref')}")
        if err.get("fail_safe_applied"):
            click.echo(f"  fail-safe: {err.get('fail_safe_applied')}")
        return

    data = payload.get("data")
    if isinstance(data, dict) and "plan" in data and isinstance(data["plan"], dict):
        plan = data["plan"]
        confirm_note = "yes" if plan.get("requires_confirm_when_live") else "no"
        click.echo(
            f"  plan: risk={plan.get('risk_class')} "
            f"confirm_when_live={confirm_note} "
            f"args={json.dumps(plan.get('args', {}))}"
        )
        click.echo(f"  plan ID: {plan['plan_id']}")
    elif isinstance(data, dict) and isinstance(data.get("identity"), dict):
        ident = data["identity"]
        click.echo(
            f"  identity: api={ident.get('api_mode')} "
            f"endpoint={ident.get('endpoint')} "
            f"firmware={ident.get('firmware')}"
        )
    elif isinstance(data, dict) and data:
        click.echo(f"  data: {json.dumps(data)}")

    state = payload.get("state")
    if isinstance(state, dict) and state:
        brief = _format_state_brief(state)
        if brief:
            click.echo(f"  state: {brief}")


def _is_interactive_tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _finish(state: CLIContext, envelope: dict[str, Any]) -> NoReturn:
    _emit_envelope(envelope, as_json=state.as_json)
    click.get_current_context().exit(int(envelope["exit_code"]))


def _confirmation_error(service: str, reason: str, *, target: str = "") -> dict[str, Any]:
    return ipc_error(
        ErrorCode.E_CONFIRM_REQUIRED, ExitCode.CONFIRM_REQUIRED, "cli_confirm", reason,
        service=service, target=target,
    )


def _require_target(state: CLIContext, service: str, target: str | None = None) -> str:
    target = target or state.target
    if not target:
        _finish(state, ipc_error(
            ErrorCode.E_UNKNOWN_TARGET, ExitCode.INVALID_USAGE, "cli_args",
            "Pass --target, set CLI_DEVICE_TARGET, or save a target with cli-device use.",
            service=service, dry_run=state.dry_run,
        ))
    return target


def _ipc_token(token_file: Path | None) -> str | None:
    if token_file is None:
        return os.environ.get("CLI_DEVICE_IPC_TOKEN")
    try:
        return read_token_file(token_file)
    except (OSError, ValueError) as exc:
        raise click.BadParameter(str(exc), param_hint="--token-file") from exc


def _parse_listen(
    _ctx: click.Context, param: click.Parameter, value: str | None,
) -> str | None:
    if value is not None:
        try:
            if value.startswith("tcp://"):
                tcp_address(value)
            else:
                unix_path(value)
        except ValueError as exc:
            raise click.BadParameter(str(exc), param=param) from exc
    return value


def _call_daemon(state: CLIContext, **request: Any) -> dict[str, Any]:
    token = _ipc_token(state.token_file) or state.managed_token
    result = DaemonIPCClient(listen=state.listen, token=token).call(**request)
    if not (
        state.auto_start and state.bundle
        and (result.get("error") or {}).get("code") == ErrorCode.E_DAEMON_UNAVAILABLE.value
    ):
        return result

    try:
        record = start_daemon(
            state.bundle, state.requested_listen, ConfirmMode.TTY.value, False, token,
            reuse_running=True,
        )
    except (OSError, ValueError) as exc:
        return ipc_error(
            ErrorCode.E_DAEMON_UNAVAILABLE, ExitCode.TIMEOUT, "daemon_start", str(exc),
            service=request.get("service") or request.get("op", "ipc"),
            target=request.get("target", ""), dry_run=request.get("dry_run", False),
        )
    state.listen = record["listen"]
    state.managed_token = record["token"]
    return DaemonIPCClient(listen=state.listen, token=token or state.managed_token).call(**request)


def _dispatch_service(state: CLIContext, service: str, args: dict[str, Any]) -> dict[str, Any]:
    target = _require_target(state, service)
    request = {"target": target, "service": service, "args": args}
    if state.dry_run:
        return _call_daemon(state, **request, dry_run=True)
    if state.plan_id is not None:
        return _call_daemon(state, **request, plan_id=state.plan_id)
    if not state.confirm or not _is_interactive_tty():
        return _call_daemon(state, **request, confirm=state.confirm)

    preview = _call_daemon(state, **request, dry_run=True)
    if not preview["ok"]:
        return preview
    plan = preview["data"]["plan"]
    click.echo(
        f"{service} on {target} [{plan['risk_class']}] {json.dumps(plan['args'])}", err=True,
    )
    if not click.confirm("Approve this plan and execute it?", err=True):
        return _confirmation_error(service, "Plan approval declined.", target=target)
    approval = _call_daemon(state, op="approve_plan", plan_id=plan["plan_id"])
    if not approval["ok"]:
        return approval
    return _call_daemon(state, **request, plan_id=plan["plan_id"])


def _risk_badge(svc_schema: dict[str, Any]) -> str:
    risk = str(svc_schema.get("risk_class", "R0_READ")).split("_", 1)[0]
    tags = [risk]
    if svc_schema.get("bypasses_action_lock"):
        tags.append("lock-free")
    if svc_schema.get("requires_confirm"):
        tags.append("--confirm")
    return f"[{', '.join(tags)}]"


def _make_service_command(svc_schema: dict[str, Any]) -> click.Command:
    svc_name = str(svc_schema["name"])
    cli_command = str(svc_schema["cli_command"])
    summary = str(svc_schema.get("summary", "")).strip()
    badge = _risk_badge(svc_schema)
    help_text = f"{badge} {summary}" if summary else badge
    arg_schemas: list[dict[str, Any]] = list(svc_schema.get("args") or [])

    params: list[click.Parameter] = []
    for arg in arg_schemas:
        arg_name = str(arg["name"])
        flag = f"--{arg_name.replace('_', '-')}"
        arg_type_str = str(arg.get("type", "string"))
        click_type: click.ParamType[Any]
        if arg_type_str == "float":
            click_type = click.FLOAT
        elif arg_type_str == "int":
            click_type = click.INT
        elif arg_type_str == "bool":
            click_type = click.BOOL
        else:
            click_type = click.STRING
        desc = str(arg.get("description", "")).strip()
        unit = arg.get("unit")
        lo, hi = arg.get("min_value"), arg.get("max_value")
        bounds = ""
        if lo is not None or hi is not None:
            unit_suffix = f" {unit}" if unit else ""
            lo_s = "-inf" if lo is None else str(lo)
            hi_s = "inf" if hi is None else str(hi)
            bounds = f" [{lo_s}..{hi_s}{unit_suffix}]"
        params.append(
            click.Option(
                param_decls=[flag, arg_name],
                required=bool(arg.get("required", True)),
                default=arg.get("default"),
                show_default=arg.get("default") is not None,
                type=click_type,
                help=f"{desc}{bounds}".strip(),
            )
        )

    def _callback(**kwargs: Any) -> None:
        state: CLIContext = click.get_current_context().obj
        call_args = {k: v for k, v in kwargs.items() if v is not None}
        _finish(state, _dispatch_service(state, svc_name, call_args))

    return click.Command(
        name=cli_command,
        params=params,
        callback=_callback,
        help=help_text,
        short_help=help_text,
        context_settings=HELP_CONTEXT,
    )


def _make_group_from_schema(group_name: str, services: list[dict[str, Any]]) -> click.Group:
    if group_name == "expert":
        group_help = "Expert escape hatches (R4, approval required)."
    elif group_name == "sys":
        group_help = "System discovery, status, preflight, and lock-free abort."
    else:
        group_help = f"Device '{group_name}' commands from active bundle."
    group = click.Group(name=group_name, help=group_help, context_settings=HELP_CONTEXT)
    for svc in services:
        group.add_command(_make_service_command(svc))
    return group


@click.group(
    name="bundle",
    help="Draft, validate, compile, and list declarative device bundles.",
    context_settings=HELP_CONTEXT,
)
def bundle_group() -> None:
    pass


@bundle_group.command(name="draft", context_settings=HELP_CONTEXT)
@click.option("--name", "device_id", required=True, help="Bundle identifier (e.g. bench_psu).")
@click.option("--display-name", required=True, help="Human-readable device name.")
@click.option(
    "--kind",
    "device_kind",
    default="generic_machine",
    show_default=True,
    help="Device category (e.g. instrument, engraver, lamp, motion_platform).",
)
@click.option(
    "--interface",
    "interface_type",
    required=True,
    help="Control interface (e.g. usb_serial, ethernet_http, tcp_scpi, python_api).",
)
@click.option("--endpoint", required=True, help="Default connection endpoint.")
@click.option(
    "--manual",
    "manuals",
    multiple=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Manual or README file for limit/warning extraction (repeatable).",
)
@click.option(
    "--host-api",
    "host_apis",
    multiple=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Python host API file parsed via AST without execution (repeatable).",
)
@click.option(
    "--output",
    "output_dir",
    required=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Directory to write device.yaml and catalog.yaml.",
)
def bundle_draft_cmd(
    device_id: str,
    display_name: str,
    device_kind: str,
    interface_type: str,
    endpoint: str,
    manuals: tuple[Path, ...],
    host_apis: tuple[Path, ...],
    output_dir: Path,
) -> None:
    """Draft device.yaml and catalog.yaml from interface + manual/host API."""
    written = draft_bundle(
        device_id=device_id,
        display_name=display_name,
        device_kind=device_kind,
        interface_type=interface_type,
        endpoint=endpoint,
        manual_paths=manuals,
        host_api_paths=host_apis,
        output_dir=output_dir,
    )
    click.echo(f"Drafted bundle at {written} (review_status=draft).")


@bundle_group.command(name="build", context_settings=HELP_CONTEXT)
@click.argument("bundle_ref")
@click.option(
    "--output-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Output directory (defaults to docs/generated/<device_id>).",
)
@click.option(
    "--check",
    is_flag=True,
    default=False,
    help="Fail if generated files differ from YAML (CI drift gate).",
)
def bundle_build_cmd(bundle_ref: str, output_dir: Path | None, check: bool) -> None:
    """Compile a bundle into SKILL.md, HOOKUP.md, and cli_schema.json."""
    try:
        bundle_dir = resolve_bundle_dir(bundle_ref, repo_root=REPO_ROOT)
        loaded = load_bundle(bundle_dir)
        target_out = output_dir or (REPO_ROOT / "docs" / "generated" / loaded.device.device_id)
        result = build_bundle(bundle_dir, target_out, check=check)
    except BundleValidationError as exc:
        click.echo(f"Validation error: {exc}", err=True)
        sys.exit(int(ExitCode.INVALID_USAGE))
    except BuildDriftError as exc:
        click.echo(f"Drift detected:\n{exc}", err=True)
        sys.exit(int(ExitCode.EXECUTION_ERROR))

    action = "Verified (no drift)" if result.checked else "Compiled"
    click.echo(f"{action} bundle '{result.device_id}' -> {result.output_dir}")


@bundle_group.command(name="validate", context_settings=HELP_CONTEXT)
@click.argument("bundle_ref")
def bundle_validate_cmd(bundle_ref: str) -> None:
    """Validate device.yaml and catalog.yaml schema and safety rules."""
    try:
        bundle_dir = resolve_bundle_dir(bundle_ref, repo_root=REPO_ROOT)
        loaded = load_bundle(bundle_dir)
        validate_bundle(loaded)
    except BundleValidationError as exc:
        click.echo(f"Validation error: {exc}", err=True)
        sys.exit(int(ExitCode.INVALID_USAGE))
    click.echo(
        f"Bundle '{loaded.device.device_id}' v{loaded.device.version} is valid "
        f"({len(loaded.catalog.services)} services, {len(loaded.device.targets)} targets)."
    )


@bundle_group.command(name="list", context_settings=HELP_CONTEXT)
def bundle_list_cmd() -> None:
    """List device bundles under bundles/."""
    bundles_root = REPO_ROOT / "bundles"
    if not bundles_root.exists():
        click.echo("No bundles/ directory found.")
        return
    found = [
        item.name
        for item in sorted(bundles_root.iterdir())
        if item.is_dir() and (item / "device.yaml").exists()
    ]
    if not found:
        click.echo("No bundles found under bundles/.")
        return
    for name in found:
        click.echo(name)


@click.group(name="skill", context_settings=HELP_CONTEXT)
def skill_group() -> None:
    """Install skills for coding agents."""


@skill_group.command(name="install", context_settings=HELP_CONTEXT)
@click.argument("bundle", required=False)
@click.option("--meta", is_flag=True, help="Install the device onboarding skill.")
@click.option(
    "--dest", default="all", show_default=True, help="claude, agents, all, or a directory.",
)
@click.option(
    "--project-dir", default=".", type=click.Path(file_okay=False, path_type=Path),
    help="Project directory for .claude and .agents installations.",
)
@click.pass_obj
def skill_install_cmd(
    state: CLIContext, bundle: str | None, meta: bool, dest: str, project_dir: Path,
) -> None:
    """Install a bundle skill or the onboarding skill."""
    bundle_ref = bundle or state.bundle
    try:
        if meta and bundle is not None:
            raise ValueError("Choose either BUNDLE or --meta.")
        if not meta and not bundle_ref:
            raise ValueError("Pass BUNDLE or --meta, or save a default with cli-device use.")
        name, paths = install_skill(
            None if meta else bundle_ref, dest=dest, project_dir=project_dir, repo_root=REPO_ROOT,
        )
    except (ValueError, OSError) as exc:
        if state.as_json:
            _finish(state, ipc_error(
                ErrorCode.E_INVALID_ARGS, ExitCode.INVALID_USAGE, "skill_install", str(exc),
                service="skill.install",
            ))
        raise click.UsageError(str(exc)) from exc

    if state.as_json:
        click.echo(json.dumps({"ok": True, "skill": name, "paths": [str(path) for path in paths]}))
    else:
        for path in paths:
            click.echo(f"Installed {path}")


@click.group(name="mcp", context_settings=HELP_CONTEXT)
def mcp_group() -> None:
    """Expose device services to MCP clients."""


@mcp_group.command(name="serve", context_settings=HELP_CONTEXT)
@click.option("--bundle", help="Device bundle name or directory path.")
@click.option("--target", help="Execution target from device.yaml.")
@click.pass_obj
def mcp_serve_cmd(state: CLIContext, bundle: str | None, target: str | None) -> None:
    """Serve MCP tools over stdio."""
    state.bundle = bundle or state.bundle
    target = target or state.target
    if not state.bundle or not target:
        raise click.UsageError("Pass --bundle and --target or save defaults with cli-device use.")

    def dispatch(
        service: str, args: dict[str, Any], dry_run: bool, plan_id: str | None,
    ) -> dict[str, Any]:
        try:
            return _call_daemon(
                state, service=service, target=target, args=args, dry_run=dry_run,
                plan_id=plan_id, confirm=False,
            )
        except (click.ClickException, OSError, ValueError) as exc:
            raise mcp.RPCError(-32603, str(exc)) from exc

    try:
        schema = _load_schema_for_bundle(state.bundle, use_generated=False)
        mcp.serve(schema, dispatch, sys.stdin.buffer, sys.stdout.buffer)
    except (ValueError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc


@click.command(name="approve", context_settings=HELP_CONTEXT)
@click.argument("plan_id")
@click.pass_obj
def approve_cmd(state: CLIContext, plan_id: str) -> None:
    """Approve a dry-run plan for one live execution."""
    if not _is_interactive_tty():
        _finish(state, _confirmation_error("approve_plan", "Approval requires an interactive TTY."))
    _finish(state, _call_daemon(state, op="approve_plan", plan_id=plan_id))


@click.group(name="lease", context_settings=HELP_CONTEXT)
def lease_group() -> None:
    """Manage time-limited approval for a target."""


@lease_group.command(name="grant", context_settings=HELP_CONTEXT)
@click.option("--target", required=True)
@click.option("--ttl", "ttl_s", type=float, required=True, help="Lease lifetime in seconds.")
@click.option(
    "--max-risk",
    type=click.Choice([risk.value for risk in RiskClass if risk != RiskClass.R0_READ]),
    required=True,
)
@click.option("--allow-service", "allowed_services", multiple=True, help="Allowed service name.")
@click.pass_obj
def lease_grant_cmd(
    state: CLIContext, target: str, ttl_s: float, max_risk: str, allowed_services: tuple[str, ...],
) -> None:
    """Grant a lease from an interactive terminal."""
    if not _is_interactive_tty():
        _finish(state, _confirmation_error(
            "grant_lease", "Granting a lease requires an interactive TTY.", target=target,
        ))
    _finish(state, _call_daemon(
        state, op="grant_lease", target=target, ttl_s=ttl_s, max_risk=max_risk,
        allowed_services=list(allowed_services) if allowed_services else None,
    ))


@lease_group.command(name="status", context_settings=HELP_CONTEXT)
@click.option("--target")
@click.pass_obj
def lease_status_cmd(state: CLIContext, target: str | None) -> None:
    """Show the current lease."""
    _finish(state, _call_daemon(
        state, op="lease_status", target=_require_target(state, "lease_status", target),
    ))


@lease_group.command(name="revoke", context_settings=HELP_CONTEXT)
@click.option("--target")
@click.pass_obj
def lease_revoke_cmd(state: CLIContext, target: str | None) -> None:
    """Revoke the current lease."""
    _finish(state, _call_daemon(
        state, op="revoke_lease", target=_require_target(state, "revoke_lease", target),
    ))


@click.command(name="use", context_settings=HELP_CONTEXT)
@click.argument("bundle_ref")
@click.option("--target", required=True)
@click.option("--listen", callback=_parse_listen)
@click.pass_obj
def use_cmd(state: CLIContext, bundle_ref: str, target: str, listen: str | None) -> None:
    """Save the default bundle, target, and optional managed endpoint."""
    try:
        bundle_dir = resolve_bundle_dir(bundle_ref, repo_root=REPO_ROOT)
        bundle = load_bundle(bundle_dir)
    except BundleValidationError as exc:
        raise click.BadParameter(str(exc), param_hint="bundle") from exc
    if target not in bundle.device.targets:
        raise click.BadParameter(
            f"Unknown target {target!r} for {bundle_ref!r}.", param_hint="--target",
        )
    if Path(bundle_ref).is_dir():
        bundle_ref = str(bundle_dir)
    listen = listen or state.explicit_listen
    try:
        save_config(bundle_ref, target, listen)
    except OSError as exc:
        raise click.ClickException(str(exc)) from exc
    if state.as_json:
        status = {"ok": True, "bundle": bundle_ref, "target": target, "listen": listen}
        click.echo(json.dumps(status))
    else:
        click.echo(f"Saved defaults for {bundle_ref} (target={target}).")


@click.command(name="up", context_settings=HELP_CONTEXT)
@click.option("--bundle")
@click.option("--listen", callback=_parse_listen)
@click.option(
    "--confirm-mode",
    type=click.Choice([ConfirmMode.TTY.value, ConfirmMode.TOKEN.value]),
    default=ConfirmMode.TTY.value,
    show_default=True,
)
@click.option("--allow-live-hardware", is_flag=True)
@click.option("--token-file", type=click.Path(dir_okay=False, path_type=Path))
@click.pass_obj
def up_cmd(
    state: CLIContext,
    bundle: str | None,
    listen: str | None,
    confirm_mode: str,
    allow_live_hardware: bool,
    token_file: Path | None,
) -> None:
    """Start the managed daemon."""
    bundle = bundle or state.bundle
    if not bundle:
        raise click.UsageError("Pass --bundle or save a bundle with cli-device use.")
    token = _ipc_token(token_file or state.token_file)
    try:
        record = start_daemon(
            bundle, listen or state.requested_listen, confirm_mode, allow_live_hardware, token,
        )
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    if state.as_json:
        status = {key: record[key] for key in ("pid", "bundle", "listen", "confirm_mode")}
        click.echo(json.dumps({"ok": True, **status}))
    else:
        click.echo(f"Managed daemon {record['pid']} listening on {record['listen']}.")


@click.command(name="down", context_settings=HELP_CONTEXT)
@click.pass_obj
def down_cmd(state: CLIContext) -> None:
    """Stop the managed daemon."""
    try:
        stopped = stop_daemon()
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    if state.as_json:
        click.echo(json.dumps({"ok": True, "stopped": stopped}))
    else:
        click.echo("Managed daemon stopped." if stopped else "No managed daemon is running.")


@click.command(
    name="repl",
    help="Interactive session reusing top-level connection options.",
    context_settings=HELP_CONTEXT,
)
@click.pass_context
def repl_cmd(ctx: click.Context) -> None:
    parent = ctx.parent
    if parent is None:
        return
    prefix: list[str] = []
    for param, flag in (
        ("bundle", "--bundle"), ("target", "--target"), ("listen", "--listen"),
        ("socket_path", "--socket"), ("token_file", "--token-file"),
    ):
        if parent.params[param] is not None:
            prefix.extend([flag, str(parent.params[param])])
    if parent.params["auto_start"] is not None:
        prefix.append("--auto-start" if parent.params["auto_start"] else "--no-auto-start")
    if parent.params["as_json"]:
        prefix.append("--json")

    click.echo("cli-device REPL (type 'exit' or Ctrl-D to quit).")
    while True:
        try:
            line = input("cli-device> ").strip()
        except EOFError:
            break
        if not line:
            continue
        if line in ("exit", "quit"):
            break
        tokens = shlex.split(line)
        try:
            parent.command.main(args=[*prefix, *tokens], standalone_mode=False)
        except SystemExit:
            pass
        except click.ClickException as exc:
            exc.show()


class DynamicDeviceCLI(click.Group):
    """Click group loading device command groups dynamically from the active bundle."""

    def _resolve_active_bundle(self, ctx: click.Context) -> str | None:
        if isinstance(ctx.obj, CLIContext):
            return ctx.obj.bundle
        eager = ctx.meta.get("_active_bundle")
        if isinstance(eager, str) and eager:
            return eager
        bundle_param = ctx.params.get("bundle")
        if isinstance(bundle_param, str) and bundle_param:
            return bundle_param
        default = (
            os.environ.get("CLI_DEVICE_BUNDLE") or read_config().get("bundle")
            or read_daemon().get("bundle")
        )
        return default if isinstance(default, str) else None

    def _load_dynamic_state(
        self, ctx: click.Context
    ) -> tuple[str | None, dict[str, click.Group], str | None]:
        bundle_ref = self._resolve_active_bundle(ctx)
        if not bundle_ref:
            return (None, {}, None)
        cache_key = f"_dynamic_bundle_cache:{bundle_ref}"
        cached = ctx.meta.get(cache_key)
        if cached is not None:
            return cached  # type: ignore[no-any-return]
        try:
            schema = _load_schema_for_bundle(bundle_ref)
        except BundleValidationError as exc:
            result: tuple[str | None, dict[str, click.Group], str | None] = (
                bundle_ref,
                {},
                str(exc),
            )
            ctx.meta[cache_key] = result
            return result
        grouped: dict[str, list[dict[str, Any]]] = {}
        for svc in list(schema.get("services") or []):
            grouped.setdefault(str(svc["cli_group"]), []).append(svc)
        groups = {name: _make_group_from_schema(name, svcs) for name, svcs in grouped.items()}
        result = (str(schema.get("device_id", bundle_ref)), groups, None)
        ctx.meta[cache_key] = result
        return result

    def list_commands(self, ctx: click.Context) -> list[str]:
        names = set(super().list_commands(ctx))
        _, groups, _ = self._load_dynamic_state(ctx)
        names.update(groups.keys())
        return sorted(names)

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        builtin = super().get_command(ctx, cmd_name)
        if builtin is not None:
            return builtin
        _, groups, _ = self._load_dynamic_state(ctx)
        return groups.get(cmd_name)

    def resolve_command(
        self, ctx: click.Context, args: list[str]
    ) -> tuple[str | None, click.Command | None, list[str]]:
        try:
            return super().resolve_command(ctx, args)
        except click.UsageError as exc:
            cmd_name = args[0] if args else ""
            bundle_ref = self._resolve_active_bundle(ctx)
            if not bundle_ref:
                raise click.UsageError(
                    f"No such command '{cmd_name}'. Pass --bundle <name|dir> "
                    "or run 'cli-device use' to load device commands. "
                    "Run 'cli-device bundle list' to see available bundles.",
                    ctx=ctx,
                ) from exc
            _, _, err = self._load_dynamic_state(ctx)
            if err:
                raise click.UsageError(f"Invalid --bundle '{bundle_ref}': {err}", ctx=ctx) from exc
            raise

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        device_id, dynamic_groups, load_err = self._load_dynamic_state(ctx)
        builtin_names = sorted(super().list_commands(ctx))
        builtin_rows: list[tuple[str, str]] = []
        for name in builtin_names:
            cmd = super().get_command(ctx, name)
            if cmd is not None and not cmd.hidden:
                builtin_rows.append((name, cmd.get_short_help_str(limit=formatter.width)))

        if dynamic_groups:
            dev_rows: list[tuple[str, str]] = []
            ordered = sorted(
                dynamic_groups.keys(),
                key=lambda k: (0 if k == "sys" else (2 if k == "expert" else 1), k),
            )
            for name in ordered:
                cmd = dynamic_groups[name]
                dev_rows.append((name, cmd.get_short_help_str(limit=formatter.width)))
            with formatter.section(f"Device Commands ({device_id})"):
                formatter.write_dl(dev_rows)
        elif load_err:
            with formatter.section("Device Commands"):
                formatter.write_text(f"Unable to load bundle: {load_err}")

        if builtin_rows:
            with formatter.section("Toolkit Commands"):
                formatter.write_dl(builtin_rows)


def _record_active_bundle(
    ctx: click.Context, _param: click.Parameter, value: str | None
) -> str | None:
    if value:
        ctx.meta["_active_bundle"] = value
    return value


def build_cli_app() -> click.Group:
    """Construct the thin schema-driven ``cli-device`` Click application."""

    @click.group(
        cls=DynamicDeviceCLI,
        name="cli-device",
        help=f"{SLOGAN} — safety-gated CLI for any declarative device bundle.",
        epilog=(
            "Pass --bundle <name|dir> or save defaults with 'cli-device use'. "
            "Software abort never replaces a physical power-cut emergency stop."
        ),
        context_settings=HELP_CONTEXT,
    )
    @click.option(
        "--bundle",
        default=None,
        envvar="CLI_DEVICE_BUNDLE",
        is_eager=True,
        callback=_record_active_bundle,
        help="Device bundle name (under bundles/) or directory path.",
    )
    @click.option(
        "--target",
        default=None,
        envvar="CLI_DEVICE_TARGET",
        help="Execution target from device.yaml (e.g. in_memory).",
    )
    @click.option(
        "--socket",
        "socket_path",
        envvar="CLI_DEVICE_SOCKET",
        help="Unix socket path for cli-device-d.",
    )
    @click.option(
        "--listen", callback=_parse_listen,
        help="Local IPC URI (unix:///path/to.sock or tcp://127.0.0.1:<port>).",
    )
    @click.option(
        "--token-file", type=click.Path(dir_okay=False, path_type=Path),
        help="Read the TCP bearer token from a file.",
    )
    @click.option("--plan-id", help="Execute a previously approved dry-run plan.")
    @click.option(
        "--auto-start/--no-auto-start", default=None,
        help="Start the daemon if unavailable (automatic for saved defaults).",
    )
    @click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON envelope.")
    @click.option(
        "--dry-run",
        is_flag=True,
        default=False,
        help="Check safety gates and print plan without actuating.",
    )
    @click.option(
        "--confirm",
        is_flag=True,
        default=False,
        help="Review and approve the plan in an interactive terminal.",
    )
    @click.pass_context
    def app(
        ctx: click.Context,
        bundle: str | None,
        target: str | None,
        socket_path: str | None,
        listen: str | None,
        token_file: Path | None,
        plan_id: str | None,
        auto_start: bool | None,
        as_json: bool,
        dry_run: bool,
        confirm: bool,
    ) -> None:
        config = read_config()
        managed = read_daemon()
        explicit_listen = listen or (f"unix://{socket_path}" if socket_path else None)
        use_defaults = bool(config) and explicit_listen is None
        should_start = auto_start if auto_start is not None else use_defaults
        configured_listen = config.get("listen") or MANAGED_LISTEN
        requested_listen = explicit_listen or managed.get("requested_listen") or configured_listen
        endpoint = explicit_listen or managed.get("listen")
        if endpoint is None:
            endpoint = configured_listen if config or should_start else f"unix://{DEFAULT_SOCKET_PATH}"
        if should_start and endpoint == managed.get("requested_listen"):
            endpoint = managed["listen"]
        ctx.obj = CLIContext(
            bundle=bundle or config.get("bundle") or managed.get("bundle"),
            target=target or config.get("target"),
            listen=endpoint,
            requested_listen=requested_listen,
            explicit_listen=explicit_listen,
            auto_start=should_start,
            token_file=token_file,
            managed_token=managed.get("token") if endpoint == managed.get("listen") else None,
            as_json=as_json,
            dry_run=dry_run,
            confirm=confirm,
            plan_id=plan_id,
        )

    app.add_command(bundle_group)
    app.add_command(skill_group)
    app.add_command(mcp_group)
    app.add_command(approve_cmd)
    app.add_command(lease_group)
    app.add_command(use_cmd)
    app.add_command(up_cmd)
    app.add_command(down_cmd)
    app.add_command(repl_cmd)
    return app


def main() -> None:
    app = build_cli_app()
    app()


if __name__ == "__main__":
    main()
