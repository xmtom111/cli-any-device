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
from typing import Any

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
from core_types import ErrorCode, ErrorDetail, ExecutionMode, ExitCode
from daemon import DEFAULT_SOCKET_PATH, DaemonIPCClient

REPO_ROOT = Path(__file__).resolve().parent.parent
HELP_CONTEXT = {"help_option_names": ["-h", "--help"]}
SLOGAN = "Let AI drive all your devices"


@dataclass
class CLIContext:
    """Shared top-level options propagated to dynamic device subcommands."""

    bundle: str | None
    target: str | None
    socket_path: Path
    as_json: bool
    dry_run: bool
    confirm: bool


def _load_schema_for_bundle(bundle_ref: str) -> dict[str, Any]:
    bundle_dir = resolve_bundle_dir(bundle_ref, repo_root=REPO_ROOT)
    loaded = load_bundle(bundle_dir)
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
        if err.get("code") == ErrorCode.E_CONFIRM_REQUIRED.value:
            click.echo("  hint: pass --confirm if you have verified physical safety.")
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
    elif isinstance(data, dict) and data:
        click.echo(f"  data: {json.dumps(data)}")

    state = payload.get("state")
    if isinstance(state, dict) and state:
        brief = _format_state_brief(state)
        if brief:
            click.echo(f"  state: {brief}")


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
        ctx = click.get_current_context()
        state: CLIContext = ctx.obj
        if not state.target:
            payload = {
                "ok": False,
                "service": svc_name,
                "target": "",
                "mode": ExecutionMode.DRY_RUN.value if state.dry_run else ExecutionMode.LIVE.value,
                "duration_ms": 0,
                "exit_code": int(ExitCode.INVALID_USAGE),
                "state": {},
                "data": {},
                "error": ErrorDetail(
                    code=ErrorCode.E_UNKNOWN_TARGET,
                    step="cli_args",
                    reason="Pass --target <target> (or set CLI_DEVICE_TARGET).",
                ).to_dict(),
            }
            _emit_envelope(payload, as_json=state.as_json)
            ctx.exit(int(ExitCode.INVALID_USAGE))

        call_args = {k: v for k, v in kwargs.items() if v is not None}
        client = DaemonIPCClient(state.socket_path)
        envelope = client.call(
            target=state.target,
            service=svc_name,
            args=call_args,
            dry_run=state.dry_run,
            confirm=state.confirm,
        )
        _emit_envelope(envelope, as_json=state.as_json)
        ctx.exit(int(envelope.get("exit_code", ExitCode.EXECUTION_ERROR)))

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
        group_help = "Expert escape hatches (R4, always requires --confirm)."
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


@click.command(
    name="repl",
    help="Interactive session reusing top-level --bundle/--target/--socket flags.",
    context_settings=HELP_CONTEXT,
)
@click.pass_context
def repl_cmd(ctx: click.Context) -> None:
    parent = ctx.parent
    if parent is None:
        return
    state: CLIContext | None = ctx.obj if isinstance(ctx.obj, CLIContext) else None
    prefix: list[str] = []
    if state is not None:
        if state.bundle:
            prefix.extend(["--bundle", state.bundle])
        if state.target:
            prefix.extend(["--target", state.target])
        prefix.extend(["--socket", str(state.socket_path)])
        if state.as_json:
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
        if isinstance(ctx.obj, CLIContext) and ctx.obj.bundle:
            return ctx.obj.bundle
        eager = ctx.meta.get("_active_bundle")
        if isinstance(eager, str) and eager:
            return eager
        bundle_param = ctx.params.get("bundle")
        if isinstance(bundle_param, str) and bundle_param:
            return bundle_param
        return os.environ.get("CLI_DEVICE_BUNDLE") or None

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
                    "(or set CLI_DEVICE_BUNDLE) to load device commands, "
                    "or run 'cli-device bundle list'.",
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
            "Pass --bundle <name|dir> (or CLI_DEVICE_BUNDLE) to load device commands. "
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
        default=DEFAULT_SOCKET_PATH,
        envvar="CLI_DEVICE_SOCKET",
        show_default=True,
        help="Unix socket path for cli-device-d.",
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
        help="Confirm physical or high-risk actions (R2-real/R3/R4).",
    )
    @click.pass_context
    def app(
        ctx: click.Context,
        bundle: str | None,
        target: str | None,
        socket_path: str,
        as_json: bool,
        dry_run: bool,
        confirm: bool,
    ) -> None:
        ctx.obj = CLIContext(
            bundle=bundle,
            target=target,
            socket_path=Path(socket_path),
            as_json=as_json,
            dry_run=dry_run,
            confirm=confirm,
        )

    app.add_command(bundle_group)
    app.add_command(repl_cmd)
    return app


def main() -> None:
    app = build_cli_app()
    app()


if __name__ == "__main__":
    main()
