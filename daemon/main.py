"""CLI entrypoint for the ``cli-device-d`` daemon process."""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
from pathlib import Path

import click

from compiler import BundleValidationError, load_bundle, resolve_bundle_dir, validate_bundle
from core_types import ConfirmMode, ExitCode
from daemon.ipc import (
    DEFAULT_SOCKET_PATH,
    DaemonIPCServer,
    DaemonTCPServer,
    read_token_file,
    unix_path,
)
from daemon.service import DeviceDaemon

HELP_CONTEXT = {"help_option_names": ["-h", "--help"]}


def _parse_endpoint_overrides(values: tuple[str, ...]) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for item in values:
        if "=" not in item:
            raise click.BadParameter(
                f"Expected TARGET=ENDPOINT format for --endpoint, got {item!r}."
            )
        target, endpoint = item.split("=", 1)
        overrides[target.strip()] = endpoint.strip()
    return overrides


@click.command(
    name="cli-device-d",
    context_settings=HELP_CONTEXT,
    epilog=(
        "Stop cleanly with SIGTERM or SIGINT. "
        "Software abort never replaces a physical power-cut emergency stop."
    ),
)
@click.option(
    "--bundle",
    required=True,
    envvar="CLI_DEVICE_BUNDLE",
    help="Device bundle name (under bundles/) or directory path.",
)
@click.option(
    "--socket",
    "socket_path",
    default=DEFAULT_SOCKET_PATH,
    envvar="CLI_DEVICE_SOCKET",
    show_default=True,
    help="Unix socket path for local IPC.",
)
@click.option(
    "--endpoint",
    "endpoints",
    multiple=True,
    help="Override target endpoint as TARGET=ENDPOINT (repeatable).",
)
@click.option(
    "--allow-live-hardware",
    is_flag=True,
    default=False,
    help="Enable live mutations on real targets in verified device bundles.",
)
@click.option("--listen", help="Local IPC URI (unix:///path/to.sock or loopback tcp://host:port).")
@click.option("--token-file", type=click.Path(path_type=Path), help="Read the TCP bearer token.")
@click.option(
    "--confirm-mode",
    type=click.Choice([ConfirmMode.TTY.value, ConfirmMode.TOKEN.value]),
    default=ConfirmMode.TTY.value,
    show_default=True,
    help="Require human approval for live changes.",
)
@click.option("--run-dir", type=click.Path(path_type=Path, file_okay=False), hidden=True)
def main(
    bundle: str,
    socket_path: str,
    endpoints: tuple[str, ...],
    allow_live_hardware: bool,
    listen: str | None,
    token_file: Path | None,
    confirm_mode: str,
    run_dir: Path | None,
) -> None:
    """Let AI drive all your devices — safety daemon for declarative device bundles."""
    try:
        bundle_dir = resolve_bundle_dir(bundle)
        loaded = load_bundle(bundle_dir)
        validate_bundle(loaded)
    except BundleValidationError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(int(ExitCode.INVALID_USAGE))

    daemon = DeviceDaemon(
        bundle=loaded,
        endpoint_overrides=_parse_endpoint_overrides(endpoints),
        allow_live_hardware=allow_live_hardware,
        confirm_mode=ConfirmMode(confirm_mode),
    )
    server: DaemonIPCServer | DaemonTCPServer
    try:
        if listen is not None and listen.startswith("tcp://"):
            token = read_token_file(token_file) if token_file is not None else None
            server = DaemonTCPServer(listen=listen, daemon=daemon, auth_token=token)
        else:
            path = unix_path(listen) if listen is not None else Path(socket_path)
            server = DaemonIPCServer(socket_path=path, daemon=daemon)
    except (ValueError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    stop_event = threading.Event()

    def _handle_sig(_signum: int, _frame: object) -> None:
        stop_event.set()

    prev_int = signal.signal(signal.SIGINT, _handle_sig)
    prev_term = signal.signal(signal.SIGTERM, _handle_sig)
    serve_thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.05},
        daemon=True,
    )
    serve_thread.start()
    targets = ", ".join(sorted(loaded.device.targets))
    click.echo(
        f"cli-device-d listening on {server.listen_uri} "
        f"(bundle={loaded.device.device_id} v{loaded.device.version}, targets=[{targets}])"
    )
    sys.stdout.flush()
    try:
        if run_dir is not None:
            ready_path = run_dir / "ready"
            fd = os.open(ready_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as ready:
                json.dump({"listen": server.listen_uri, "token": server.auth_token}, ready)
        while serve_thread.is_alive() and not stop_event.is_set():
            if run_dir is not None and (run_dir / "stop").exists():
                break
            stop_event.wait(timeout=0.1)
    finally:
        server.shutdown()
        serve_thread.join(timeout=2.0)
        server.server_close()
        signal.signal(signal.SIGINT, prev_int)
        signal.signal(signal.SIGTERM, prev_term)
        if run_dir is not None:
            (run_dir / "stopped").touch()


if __name__ == "__main__":
    main()
