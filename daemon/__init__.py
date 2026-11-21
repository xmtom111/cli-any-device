"""CLI-Any-Device daemon (cli-device-d): loads any bundle and pluggable adapter, serves IPC."""

from daemon.ipc import DaemonIPCClient, DaemonIPCServer
from daemon.main import DEFAULT_SOCKET_PATH
from daemon.registry import AdapterRegistry
from daemon.service import DeviceDaemon

__all__ = [
    "DEFAULT_SOCKET_PATH",
    "AdapterRegistry",
    "DaemonIPCClient",
    "DaemonIPCServer",
    "DeviceDaemon",
]
