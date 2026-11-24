"""CLI entrypoint for the ``cli-device-d`` daemon process."""

from __future__ import annotations

import signal
import sys
import threading
from pathlib import Path

import click

from compiler import BundleValidationError, load_bundle, resolve_bundle_dir, validate_bundle
from core_types import ExitCode
from daemon.ipc import DaemonIPCServer
from daemon.service import DeviceDaemon

DEFAULT_SOCKET_PATH = "/tmp/cli-device-d.sock"
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
def main(
    bundle: str, socket_path: str, endpoints: tuple[str, ...], allow_live_hardware: bool
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
    )
    server = DaemonIPCServer(socket_path=Path(socket_path), daemon=daemon)
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
        f"cli-device-d listening on {socket_path} "
        f"(bundle={loaded.device.device_id} v{loaded.device.version}, targets=[{targets}])"
    )
    sys.stdout.flush()
    try:
        while serve_thread.is_alive() and not stop_event.is_set():
            stop_event.wait(timeout=0.1)
    finally:
        server.shutdown()
        serve_thread.join(timeout=2.0)
        server.server_close()
        signal.signal(signal.SIGINT, prev_int)
        signal.signal(signal.SIGTERM, prev_term)


if __name__ == "__main__":
    main()
