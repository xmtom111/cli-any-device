# Adapters Directory Ownership

Per Section 10 of `技术路线与要求.md`, hardware and simulation adapter plugins are owned by **Codex**:

- `adapters/duet_rrf/` — Real Jubilee (`science-jubilee` as `ActionDriver` + direct RRF HTTP `SupervisorChannel`, `M400` + object model `idle` completion barrier, lock-free out-of-band `M112`).
- `adapters/mujoco_twin/` — Read-only wrapper around the local CAD-backed MuJoCo twin (path via configuration / environment variable; never modifies `/workspace/autodna_sim_*`).
- `adapters/grbl_sim/` — Read-only wrapper around the local AutoDNA GRBL 1.1 TCP emulator (motion subset ONLY; `tool.*` MUST fail honestly with `UnsupportedCapabilityError` / `E_UNSUPPORTED_CAPABILITY`).

All adapters MUST implement `adapter_spi.SupervisorChannel` and `adapter_spi.ActionDriver` and register under the `cli_any_device.adapters` entry point group (or be injected into `daemon`).
