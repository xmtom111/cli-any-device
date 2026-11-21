"""CLI entrypoint for the ``cli-device-d`` daemon process."""

from __future__ import annotations

import signal
import sys
import threading
from pathlib import Path

import click

from compiler import load_bundle, resolve_bundle_dir, validate_bundle
from daemon.ipc import DaemonIPCServer
from daemon.service import DeviceDaemon

DEFAULT_SOCKET_PATH = "/tmp/cli-device-d.sock"


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


@click.command(name="cli-device-d")
@click.option(
    "--bundle",
    required=True,
    envvar="CLI_DEVICE_BUNDLE",
    help="Device bundle name (under bundles/) or path to bundle directory.",
)
@click.option(
    "--socket",
    "socket_path",
    default=DEFAULT_SOCKET_PATH,
    envvar="CLI_DEVICE_SOCKET",
    show_default=True,
    help="Unix domain socket path for local IPC.",
)
@click.option(
    "--endpoint",
    "endpoints",
    multiple=True,
    help="Optional TARGET=ENDPOINT override (repeatable).",
)
def main(bundle: str, socket_path: str, endpoints: tuple[str, ...]) -> None:
    """Let AI drive all your devices — safety daemon (cli-device-d)."""
    bundle_dir = resolve_bundle_dir(bundle)
    loaded = load_bundle(bundle_dir)
    validate_bundle(loaded)

    daemon = DeviceDaemon(
        bundle=loaded,
        endpoint_overrides=_parse_endpoint_overrides(endpoints),
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
    click.echo(
        f"cli-device-d listening on {socket_path} "
        f"(bundle={loaded.device.device_id} v{loaded.device.version})"
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
