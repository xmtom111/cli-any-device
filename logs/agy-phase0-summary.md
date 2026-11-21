# Antigravity Phase 0 Summary — CLI-Any-Device

- **Date:** 2026-11-21
- **Scope:** Phase 0 frozen contracts, generic device-bundle onboarding & compiler toolchain, device-agnostic safety kernel, multi-threaded daemon IPC (`cli-device-d`), thin schema-driven CLI (`cli-device`), and in-memory fake closed-loop verification.
- **Boundary Compliance:** Did not modify `/workspace/autodna_sim_a`, `/workspace/autodna_sim_b`, `/home/box/autodna_sim`, `技术路线与要求.md/.pdf`, or Codex-owned paths (`adapters/duet_rrf/`, `adapters/mujoco_twin/`, `adapters/grbl_sim/`, `tests/adapter_compliance/`, `docs/adapter-constraints.md`). No real-machine motion emitted; no remote push/PR created.

---

## 1. Landed Modules & Responsibilities

| Module / Path | Responsibility | Import Boundary |
|---|---|---|
| `pyproject.toml` | Python 3.11+ project (`cli-device`, `cli-device-d`), strict `mypy`, `ruff`, `pytest` | — |
| `core_types/` | Pure domain types (zero I/O, zero third-party deps): `RiskClass` (R0–R4), `ExitCode` (0/1/2/3/4/5/99), `ErrorCode`, `ErrorDetail`, `CanonicalMachineState` (with generic `properties` + optional motion/tool/deck fields), `Identity`, `ApiDialect`, `CommandEnvelope`, `DeviceBundle` / `DeviceSpec` / `CatalogSpec` / `ServiceSpec` | stdlib only |
| `adapter_spi/` | Frozen SPI protocols `SupervisorChannel` & `ActionDriver`, `ADAPTER_ENTRY_POINT_GROUP = "cli_any_device.adapters"`, `ABORT_LATENCY_SLA_MS = 100.0`, `AdapterConfig`/`AdapterPair`/`AdapterFactory`, plus thread-safe `InMemoryFakeAdapter` with lock-free out-of-band `abort_now()` | `core_types` only |
| `compiler/` | Generic bundle toolchain: `loader.py` (strict YAML schema + hardcoded-secret rejection), `ingestor.py` (`draft_bundle` — safe AST inspection of Python host APIs + manual hint extraction to draft `device.yaml` + `catalog.yaml` for ANY open-source machine), `builder.py` (deterministic compiler generating `SKILL.md`, `HOOKUP.md`, `cli_schema.json` with `--check` drift gate) | `core_types` only |
| `safety_kernel/` | Device-agnostic runtime safety gate (`SafetyKernel`, `predicates.py`): R0–R4 gating, default-deny preconditions, device/arg bounds & forbidden zones, `--dry-run` (zero actuation), `--confirm` (exit 4), honest capability check (exit 2), mandatory `await_settled` + postcondition verification + catalog `fail_safe`, lock-free `sys.abort` + exit 99 e-stop latch | `core_types`, `adapter_spi` |
| `daemon/` | `cli-device-d`: `AdapterRegistry` (entry points + DI + `in_memory`), `DeviceDaemon`, `DaemonIPCServer` (`ThreadingUnixStreamServer` so 2nd-process `sys abort` runs concurrently without waiting on blocked actions), `DaemonIPCClient` | `core_types`, `adapter_spi`, `compiler`, `safety_kernel` |
| `cli/` | Thin schema-driven `cli-device`: `bundle draft/build/validate/list`, interactive `repl`, and dynamic Click command groups loaded at runtime from the selected bundle's `cli_schema.json` (never hardcoded to Jubilee) | `core_types`, `compiler`, `daemon` |
| `bundles/jubilee/` | First verification fixture (`device.yaml` + `catalog.yaml`) encoding real Jubilee facts: Duet/RRF Ethernet HTTP (`192.168.1.2` / `192.168.1.3`), `M400` + object model `idle` completion (never `/rr_gcode` HTTP 200), lock-free `M112` + mandatory physical e-stop, and honest GRBL capability boundary (`GRBL != real Jubilee`) | declarative YAML |
| `docs/generated/jubilee/` | Compiler outputs ONLY (`SKILL.md`, `HOOKUP.md`, `cli_schema.json`), enforced by `cli-device bundle build jubilee --check` | generated only |
| `adapters/README.md` | Documents Codex ownership of `duet_rrf`, `mujoco_twin`, and `grbl_sim` | docs |

---

## 2. How the Implementation Was Realigned from a "Jubilee Project" Back to a "Generic Tool + Jubilee Verification Fixture"

In response to `PRODUCT.md` and the course correction, we ensured the product is genuinely a **generic "turn any open-source machine into a safe, AI-callable CLI" toolkit** rather than a Jubilee-only tool or an AutoDNA liquid-handling continuation:

1. **Device-Agnostic `core_types` & `safety_kernel`:**
   - Added generic `properties: dict[str, Any]`, `verified_flags: frozenset[str]`, `NumericLimit` (`safety_limits`), `device_kind`, `source_inputs`, `review_status`, and generic transport dialects (`SERIAL_TEXT`, `HTTP_REST`, `SCPI_TCP`, `PYTHON_HOST_API`) so non-motion hardware (open-source radios, vintage robot arms, benchtop instruments) requires zero fake X/Y/Z axes or safe-Z configuration.
   - `safety_kernel` enforces generic predicates (`prop_eq:*`, `prop_set:*`, `flag:*`, `safety_limits`) alongside optional motion/tool predicates, with AST tests (`tests/test_import_dag.py`) statically forbidding any hardcoded `"jubilee"`, `"duet_real"`, `"mujoco_twin"`, or `"grbl_sim"` branches inside `core_types/`, `compiler/`, or `safety_kernel/`.
2. **Exposed Generic Onboarding Toolchain (`Interface + Manual/Host API → Bundle + CLI + HOOKUP/SKILL`):**
   - Implemented `compiler/ingestor.py` and `cli-device bundle draft`: accepts `--name`, `--kind`, `--interface`, `--endpoint`, `--manual` (repeatable), and `--host-api` (repeatable, parsed safely via Python `ast` without importing vendor code) and drafts a human-reviewable `device.yaml` + `catalog.yaml` (`review_status: draft`) that compiles directly into `SKILL.md`, `HOOKUP.md`, and `cli_schema.json`.
   - `cli-device` dynamically mounts command groups from the active bundle's `cli_schema.json` (`--bundle <name|path>`), with zero Jubilee commands hardcoded in `cli/`.
3. **Proven with Two Completely Different Devices:**
   - **Non-motion open-source fixture (`tests/fixtures/open_radio/`):** `OpenRadio-100` (USB serial manual + Python host API) runs end-to-end through `cli-device bundle draft` → `bundle build --check` → `cli-device-d` → dynamic `cli-device --bundle open_radio radio tune` + `--confirm` gating on `expert send-raw-at`, with zero mention of Jubilee in generated docs.
   - **Primary physical-platform verification fixture (`bundles/jubilee/`):** Exercises the full v1 workflow (`detect → preflight → dry-run home-all → confirm home-all → load-deck → load-labware → pickup → move-well → park`), all denial cases, GRBL capability honesty (`E_UNSUPPORTED_CAPABILITY` / exit 2), and concurrent lock-free IPC abort.

---

## 3. Verification Performed

All checks passed cleanly in `.venv`:

```bash
.venv/bin/pytest tests/test_import_dag.py tests/test_contracts_and_spi.py tests/test_compiler.py tests/test_safety_kernel.py tests/test_daemon_and_cli.py -v
.venv/bin/mypy
.venv/bin/ruff check core_types adapter_spi compiler safety_kernel daemon cli tests/test_import_dag.py tests/test_contracts_and_spi.py tests/test_compiler.py tests/test_safety_kernel.py tests/test_daemon_and_cli.py
.venv/bin/cli-device bundle build jubilee --check
.venv/bin/cli-device --bundle jubilee --help
.venv/bin/cli-device-d --help
```

- **Tests:** 22/22 passed (import DAG & zero target-branching, SPI deadlock abort `< 100 ms`, GRBL capability honesty, generic `open_radio` draft+build+IPC closed loop, Jubilee compile+drift check, R0–R4 & refusal suite, fail-safe on postcondition mismatch, concurrent IPC `sys abort`).
- **Type checking:** `mypy --strict` passed with 0 errors across all 23 source files.
- **Linting:** `ruff check` passed with 0 errors.

---

## 4. Key SPI Points for Codex (`adapters/duet_rrf`, `adapters/mujoco_twin`, `adapters/grbl_sim`)

1. **Implement Exact Frozen Protocols from `adapter_spi.protocols`:**
   - Register factories under Python entry point group `cli_any_device.adapters` (keys matching `device.yaml`: `duet_rrf`, `mujoco_twin`, `grbl_sim`) with signature `(config: AdapterConfig) -> AdapterPair`.
   - `SupervisorChannel`: `detect() -> Identity`, `poll_state() -> CanonicalMachineState`, `preflight() -> PreflightReport`, `await_settled(timeout_s: float) -> CanonicalMachineState`, `abort_now() -> AbortResult`, `reset_estop() -> CanonicalMachineState`.
   - `ActionDriver`: `capabilities() -> frozenset[str]`, `execute(request: ActionRequest) -> ActionTelemetry`, `apply_fail_safe(policy: str, reason: str) -> str`.
2. **Completion Semantics (Critical for `duet_rrf`):**
   - `ActionDriver.execute()` returns `ActionTelemetry(accepted=True, ...)` for command submission ONLY.
   - `SupervisorChannel.await_settled(timeout_s)` MUST append/verify `M400` and poll the RRF object model (`/rr_model` or `/machine/model`) until `state.status == "idle"`. **NEVER** treat `/rr_gcode` HTTP 200 as motion completion.
   - On connection failure in `detect()`, raise `AdapterTransportError(..., hookup_ref="HOOKUP.md#sec-troubleshooting")`.
3. **Lock-Free Emergency Stop (`abort_now` < 100 ms SLA):**
   - `SupervisorChannel.abort_now()` MUST NEVER acquire `ActionDriver`'s lock.
   - `duet_rrf`: send `M112` on an independent HTTP session/thread. `grbl_sim`: send realtime abort byte (`!` / `0x18`). `mujoco_twin`: halt trajectory immediately.
   - `reset_estop()` MUST clear `homed` (`frozenset()`) so re-homing is enforced before any motion.
4. **Honest Capability Reporting (`grbl_sim` != real Jubilee):**
   - `grbl_sim.capabilities()` MUST report only `{"axes_xyz", "serial_gcode", "abort_oob"}` (NO `tool_lock_u`, NO `rrf_object_model`, NO `deck_labware`).
   - Any direct call to `tool.*` on `grbl_sim` MUST raise `UnsupportedCapabilityError` immediately and NEVER fabricate tool state.
5. **External Simulator Paths & Isolation:**
   - Reference `/workspace/autodna_sim_a` (or `b`) read-only via configuration / environment variables (`config.options` / env var); never copy simulator code into kernel or modify external repos.
   - Keep `science-jubilee` pinned and strictly isolated inside `adapters/duet_rrf/`.
