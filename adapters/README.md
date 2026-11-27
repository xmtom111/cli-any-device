# Adapters

Installing CLI-Any-Device registers six built-in plugins under the `cli_any_device.adapters` entry point group. Select one with `adapter_id` in a bundle target. Each adapter provides an `ActionDriver` for commands and a `SupervisorChannel` for detection, state, completion, and abort.

| Adapter | Interface | Supported use |
| --- | --- | --- |
| `scpi_generic` | TCP or serial SCPI | Instrument identity, typed queries, commands, and configurable completion polling |
| `http_json` | HTTP JSON API | Request templates, response-to-state mappings, and configurable status, completion, and abort endpoints |
| `moonraker_http` | Moonraker HTTP API | Klipper status, XYZ motion, catalog G-code templates, and Moonraker emergency stop |
| `duet_rrf` | Duet RepRapFirmware HTTP | Jubilee detection, preflight, and state, with an independent software abort request |
| `mujoco_twin` | AutoDNA MuJoCo model | CAD-backed XYZ motion and logical tool, deck, and labware simulation |
| `grbl_sim` | AutoDNA GRBL 1.1 emulator | In-process XYZ homing and motion with realtime reset |

The three generic adapters support new devices over their existing interfaces with YAML alone. Put the endpoint, capabilities, and `adapter_options` on the target in `device.yaml`. Put service-level mappings in `catalog.yaml` under `protocol`.

| Adapter | `adapter_options` examples | `protocol` examples |
| --- | --- | --- |
| `scpi_generic` | `detect_cmd`, `settled_cmd`, `poll_queries`, `abort_cmd` | `command` or `query`, optional `parse` and `property` |
| `http_json` | `status_path`, `status_map`, `settled_key`, `abort_path` | `path`, `method`, `query`, `body`, `response_map` |
| `moonraker_http` | `safe_z_mm`, `auth_header`, `auth_env_var` | `gcode` for catalog-defined commands |

See the [synthetic SCPI supply bundle](../tests/fixtures/scpi_psu/bundle/) for a complete configuration and the [adapter constraints](../docs/adapter-constraints.md) for simulator setup and Duet limitations.
