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
    # Prefer compiled cli_schema.json in docs/generated/<device_id>/ if present
    loaded = load_bundle(bundle_dir)
    schema_path = REPO_ROOT / "docs" / "generated" / loaded.device.device_id / "cli_schema.json"
    if schema_path.exists():
        parsed = json.loads(schema_path.read_text(encoding="utf-8"))
        if isinstance(parsed, dict) and parsed.get("device_id") == loaded.device.device_id:
            return parsed
    return render_cli_schema(loaded)


def _emit_envelope(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        click.echo(json.dumps(payload, indent=2))
    else:
        status = "OK" if payload.get("ok") else "FAIL"
        svc = payload.get("service", "")
        tgt = payload.get("target", "")
        click.echo(f"[{status}] {svc} (target={tgt}, mode={payload.get('mode')})")
        err = payload.get("error")
        if isinstance(err, dict):
            click.echo(f"  error: {err.get('code')} at {err.get('step')}: {err.get('reason')}")
            if err.get("hookup_ref"):
                click.echo(f"  see: {err.get('hookup_ref')}")
            if err.get("fail_safe_applied"):
                click.echo(f"  fail-safe: {err.get('fail_safe_applied')}")
        elif payload.get("data"):
            click.echo(f"  data: {json.dumps(payload['data'])}")


def _make_service_command(svc_schema: dict[str, Any]) -> click.Command:
    svc_name = str(svc_schema["name"])
    cli_command = str(svc_schema["cli_command"])
    summary = str(svc_schema.get("summary", ""))
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
        params.append(
            click.Option(
                param_decls=[flag, arg_name],
                required=bool(arg.get("required", True)),
                default=arg.get("default"),
                type=click_type,
                help=str(arg.get("description", "")),
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
                    reason="Explicit --target (or CLI_DEVICE_TARGET) is required.",
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

    return click.Command(name=cli_command, params=params, callback=_callback, help=summary)


def _make_group_from_schema(group_name: str, services: list[dict[str, Any]]) -> click.Group:
    group = click.Group(name=group_name, help=f"Atomic '{group_name}' services from bundle schema.")
    for svc in services:
        group.add_command(_make_service_command(svc))
    return group


@click.group(name="bundle", help="Generic device bundle authoring, validation, and compilation.")
def bundle_group() -> None:
    pass


@bundle_group.command(name="draft")
@click.option("--name", "device_id", required=True, help="Machine identifier (e.g. open_radio).")
@click.option("--display-name", required=True, help="Human-readable machine name.")
@click.option(
    "--kind",
    "device_kind",
    default="generic_machine",
    show_default=True,
    help="Machine category (e.g. radio, robot_arm, motion_platform, instrument).",
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
    help="Path to hardware manual / README markdown or text file (repeatable).",
)
@click.option(
    "--host-api",
    "host_apis",
    multiple=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to Python host software module for safe AST inspection (repeatable).",
)
@click.option(
    "--output",
    "output_dir",
    required=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Directory to write drafted device.yaml and catalog.yaml.",
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
    """Draft a new device bundle from interface info + manual(s) / host API(s)."""
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
    click.echo(f"Drafted device bundle at {written} (review_status=draft).")


@bundle_group.command(name="build")
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
    help="Verify generated files match YAML without writing (CI drift gate).",
)
def bundle_build_cmd(bundle_ref: str, output_dir: Path | None, check: bool) -> None:
    """Compile device.yaml + catalog.yaml into SKILL.md, HOOKUP.md, and cli_schema.json."""
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


@bundle_group.command(name="validate")
@click.argument("bundle_ref")
def bundle_validate_cmd(bundle_ref: str) -> None:
    """Validate a device bundle's YAML schema and safety invariants."""
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


@bundle_group.command(name="list")
def bundle_list_cmd() -> None:
    """List declarative device bundles under bundles/."""
    bundles_root = REPO_ROOT / "bundles"
    if not bundles_root.exists():
        click.echo("No bundles directory found.")
        return
    for item in sorted(bundles_root.iterdir()):
        if item.is_dir() and (item / "device.yaml").exists():
            click.echo(item.name)


@click.command(name="repl", help="Start an interactive CLI-Any-Device REPL session.")
@click.pass_context
def repl_cmd(ctx: click.Context) -> None:
    parent = ctx.parent
    if parent is None:
        return
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
            parent.command.main(args=tokens, standalone_mode=False)
        except SystemExit:
            pass
        except click.ClickException as exc:
            exc.show()


class DynamicDeviceCLI(click.Group):
    """Click group that dynamically loads device service groups from the active bundle schema."""

    def _resolve_active_bundle(self, ctx: click.Context) -> str | None:
        if isinstance(ctx.obj, CLIContext) and ctx.obj.bundle:
            return ctx.obj.bundle
        bundle_param = ctx.params.get("bundle")
        if isinstance(bundle_param, str) and bundle_param:
            return bundle_param
        env_bundle = os.environ.get("CLI_DEVICE_BUNDLE")
        if env_bundle:
            return env_bundle
        return None

    def _load_dynamic_groups(self, ctx: click.Context) -> dict[str, click.Group]:
        bundle_ref = self._resolve_active_bundle(ctx)
        if not bundle_ref:
            return {}
        try:
            schema = _load_schema_for_bundle(bundle_ref)
        except BundleValidationError:
            return {}
        grouped: dict[str, list[dict[str, Any]]] = {}
        for svc in list(schema.get("services") or []):
            grp = str(svc["cli_group"])
            grouped.setdefault(grp, []).append(svc)
        return {name: _make_group_from_schema(name, svcs) for name, svcs in grouped.items()}

    def list_commands(self, ctx: click.Context) -> list[str]:
        names = set(super().list_commands(ctx))
        names.update(self._load_dynamic_groups(ctx).keys())
        return sorted(names)

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        builtin = super().get_command(ctx, cmd_name)
        if builtin is not None:
            return builtin
        return self._load_dynamic_groups(ctx).get(cmd_name)


def build_cli_app() -> click.Group:
    """Construct the thin schema-driven ``cli-device`` Click application."""

    @click.group(
        cls=DynamicDeviceCLI,
        name="cli-device",
        help="CLI-Any-Device: turn any open-source machine into a safe, AI-callable CLI.",
    )
    @click.option(
        "--bundle",
        default=None,
        envvar="CLI_DEVICE_BUNDLE",
        help="Active device bundle name or directory path.",
    )
    @click.option(
        "--target",
        default=None,
        envvar="CLI_DEVICE_TARGET",
        help="Explicit execution target declared in the bundle's device.yaml.",
    )
    @click.option(
        "--socket",
        "socket_path",
        default=DEFAULT_SOCKET_PATH,
        envvar="CLI_DEVICE_SOCKET",
        show_default=True,
        help="Unix domain socket path for cli-device-d.",
    )
    @click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON envelope.")
    @click.option(
        "--dry-run",
        is_flag=True,
        default=False,
        help="Validate safety gates and print plan without actuating hardware.",
    )
    @click.option(
        "--confirm",
        is_flag=True,
        default=False,
        help="Explicitly confirm physical/high-risk operations (R2-real/R3/R4).",
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
