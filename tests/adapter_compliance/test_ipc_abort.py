import json
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from adapters.grbl_sim import create_adapter

from adapter_spi import ABORT_LATENCY_SLA_MS, AdapterConfig
from compiler.loader import load_bundle
from core_types import ExitCode, MachineLifecycle
from daemon.ipc import DaemonIPCServer
from daemon.registry import AdapterRegistry
from daemon.service import DeviceDaemon

_BUNDLE = Path(__file__).resolve().parents[2] / "bundles" / "jubilee"


def test_second_process_aborts_grbl_diagonal():
    pair = create_adapter(AdapterConfig("grbl_sim", options={"time_scale": 30}))
    registry = AdapterRegistry(load_entry_points=False)
    registry.register("grbl_sim", lambda _: pair)

    with tempfile.TemporaryDirectory(dir=Path(__file__).parent, prefix=".ipc-") as directory:
        socket = Path(directory) / "cli.sock"
        daemon = DeviceDaemon(bundle=load_bundle(_BUNDLE), registry=registry)
        server = DaemonIPCServer(socket_path=socket, daemon=daemon)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        client = """
import json
import sys
from pathlib import Path
from daemon.ipc import DaemonIPCClient

response = DaemonIPCClient(Path(sys.argv[1])).call(**json.loads(sys.argv[2]))
print(json.dumps(response))
raise SystemExit(response["exit_code"])
"""

        def command(service, *, confirm=False, **args):
            request = {"target": "grbl_sim", "service": service, "confirm": confirm, "args": args}
            return [sys.executable, "-B", "-c", client, str(socket), json.dumps(request)]

        motion = None
        try:
            for startup in (command("sys.detect"), command("motion.home_all", confirm=True)):
                result = subprocess.run(
                    startup,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                assert result.returncode == ExitCode.OK, result.stdout + result.stderr

            motion = subprocess.Popen(
                command("motion.move_to", x=90, y=90, z=5, speed=60),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            deadline = time.monotonic() + 5
            while pair.supervisor.poll_state().lifecycle != MachineLifecycle.MOVING:
                if motion.poll() is not None:
                    output, errors = motion.communicate()
                    raise AssertionError(output + errors)
                assert time.monotonic() < deadline
                time.sleep(0.005)
            abort = subprocess.run(
                command("sys.abort"),
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            aborted = json.loads(abort.stdout)
            assert abort.returncode == ExitCode.OK, abort.stdout + abort.stderr
            assert aborted["data"]["abort"]["latency_ms"] < ABORT_LATENCY_SLA_MS
            assert aborted["state"]["estop_latched"]
            stopped = pair.supervisor.poll_state()
            assert stopped.lifecycle == MachineLifecycle.HALTED
            assert 0 < stopped.position.x < 90
            assert stopped.position.x == stopped.position.y
            assert not stopped.homed
            output, errors = motion.communicate(timeout=5)
            assert motion.returncode != ExitCode.OK, output + errors
            assert pair.supervisor.poll_state().position == stopped.position
        finally:
            if motion is not None and motion.poll() is None:
                motion.kill()
                motion.communicate(timeout=5)
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)
