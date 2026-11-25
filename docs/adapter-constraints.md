# Adapter constraints

## Duet / RepRapFirmware

The Duet adapter uses Ethernet HTTP. Standalone RRF uses `rr_connect`, `rr_model`, `rr_gcode`, and `rr_reply`. SBC mode uses `machine/connect`, `machine/model`, and `machine/code`. Set the endpoint and supply the password through `CLI_ANY_DEVICE_DUET_PASSWORD` or local adapter configuration. With no connection configured, detection reports an offline identity and state remains `DISCONNECTED`.

The adapter supports detection, preflight, state polling, and explicit software abort. The supervisor confirms a completion barrier with `M400` followed by an idle object model. Standalone RRF uses a unique `M118` marker to correlate the barrier reply. The live action driver rejects physical actions and software reset. The optional `science-jubilee` dependency supports offline XYZ frame rehearsal with `vendor_rehearsal: true`. The dependency is pinned in [the Duet requirements file](../adapters/duet_rrf/requirements.txt).

Abort sends `M112` on a separate HTTP request, bypassing the action lock. Physical emergency stop and power isolation require on-site verification. RRF 2.x compatibility, machine macros, tools, soft limits, lock sensors, and physical stop timing also require hardware validation. Missing telemetry stays unknown.

## AutoDNA simulations

Set `CLI_ANY_DEVICE_AUTODNA_ROOT` to a local AutoDNA checkout, or set the target's `adapter_options.autodna_root`. Use one checkout per process. The adapters read the simulator source and assets in place.

- **MuJoCo** requires `mujoco` and `numpy` in the Python environment. The twin uses CAD-backed XYZ kinematics and samples geometric contacts along the commanded path. Its default `autodna_deck` and `autodna_rack` define 96 simulated wells in slot 1. Well Z offsets use the tube-rim plane. Tool pickup, park, U-axis homing, and locking are logical states. The simulated geometry and contact checks apply to the model.
- **GRBL** runs the emulator in-process and supports absolute, relative, and safe-Z XYZ motion. It rejects tool changing, tool locking, partial homing, and raw G-code. Completion waits for `Idle`. Realtime reset stops motion, clears the queue, and invalidates homing.

Run `pytest -v tests/adapter_compliance` from the repository root. The external simulation cases run when their checkout and Python dependencies are available. The Duet offline and loopback cases run without AutoDNA.
