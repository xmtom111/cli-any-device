"""CLI entrypoint for the ``cli-device-d`` daemon process."""

from __future__ import annotations

import signal
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
    """Start the CLI-Any-Device safety daemon for any declarative device bundle."""
    bundle_dir = resolve_bundle_dir(bundle)
    loaded = load_bundle(bundle_dir)
    validate_bundle(loaded)

    daemon = DeviceDaemon(
        bundle=loaded,
        endpoint_overrides=_parse_endpoint_overrides(endpoints),
    )
    server = DaemonIPCServer(socket_path=Path(socket_path), daemon=daemon)

    def _handle_sig(_signum: int, _frame: object) -> None:
        server.shutdown()

    signal.signal(signal.SIGINT, _handle_sig)
    signal.signal(signal.SIGTERM, _handle_sig)
    click.echo(
        f"cli-device-d listening on {socket_path} "
        f"(bundle={loaded.device.device_id} v{loaded.device.version})"
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
