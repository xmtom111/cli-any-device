# CLI-Any-Device

**Let AI drive all your devices**

Turn any hardware with a reachable interface (serial, Ethernet HTTP, TCP/SCPI, or a Python host API) plus its manual or host software into a safe, AI-callable CLI (`cli-device` + `cli-device-d`), a short agent `SKILL.md`, and a human `HOOKUP.md`.

## Why It Exists

Hardware interfaces are fragmented and risky for AI agents: letting an agent read raw firmware manuals or send raw G-code/SCPI wastes context and invites unsafe actuation. CLI-Any-Device compiles device knowledge into a small declarative pack and executes every command through one shared, device-agnostic safety runtime.

## Status & Scope

- **Working and tested today:** the generic bundle drafter/compiler, safety kernel, local IPC daemon (`cli-device-d`), schema-driven CLI (`cli-device`), in-memory fake, and local simulation adapters (`mujoco_twin`, `grbl_sim`, plus loopback HTTP fixtures for `duet_rrf`).
- **Not yet verified on physical hardware:** end-to-end physical machine acceptance has **not** been completed. Do not treat this repository as field-certified for unattended actuation on real hardware.
- **Fixtures vs. product:** `bundles/jubilee/` is the first complex verification fixture, not the product itself. `tests/fixtures/` (`grbl_engraver`, `scpi_psu`, `open_radio`) are **synthetic offline test fixtures** used to verify non-Jubilee onboarding—they are not purchasable hardware models, and `open_radio` is strictly offline (do not transmit RF without legal authorization and a proper load/antenna).

## How It Works

```text
Interface + Manual / Host API (AST-only, never executed)
  │
  ▼  cli-device bundle draft
Declarative Device Pack (device.yaml + catalog.yaml)
  │
  ▼  cli-device bundle build [--check]
Generated Artifacts (SKILL.md + HOOKUP.md + cli_schema.json)
  │
  ▼  runtime (zero firmware changes)
cli-device (thin CLI) ──Unix IPC──▶ cli-device-d (SafetyKernel) ──SPI──▶ adapters/*
```

Adapters implement `SupervisorChannel` + `ActionDriver` (`adapter_spi/`) and only translate existing device interfaces—vendor firmware is never modified.

## Install

Requires Python 3.11+.

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -e ".[dev]"
```

## Quickstart (Offline / In-Memory)

### 1. Run the bundled Jubilee fixture against the built-in `in_memory` target

```bash
# Terminal 1: start the safety daemon (stop cleanly with Ctrl-C or SIGTERM)
.venv/bin/cli-device-d --bundle jubilee --socket /tmp/cli-device-demo.sock

# Terminal 2: inspect commands, detect, dry-run, confirm, and check status
.venv/bin/cli-device --bundle jubilee --help
.venv/bin/cli-device --bundle jubilee --target in_memory --socket /tmp/cli-device-demo.sock --json sys detect
.venv/bin/cli-device --bundle jubilee --target in_memory --socket /tmp/cli-device-demo.sock --json sys preflight
.venv/bin/cli-device --bundle jubilee --target in_memory --socket /tmp/cli-device-demo.sock --json --dry-run motion home-all
.venv/bin/cli-device --bundle jubilee --target in_memory --socket /tmp/cli-device-demo.sock --json --confirm motion home-all
.venv/bin/cli-device --bundle jubilee --target in_memory --socket /tmp/cli-device-demo.sock sys status
```

### 2. Draft & compile a brand-new device pack from a manual + Python host API

```bash
.venv/bin/cli-device bundle draft \
  --name bench_psu \
  --display-name "Synthetic SCPI PSU Fixture" \
  --kind instrument \
  --interface tcp_scpi \
  --endpoint tcp://192.168.10.50:5025 \
  --manual tests/fixtures/scpi_psu/manual.md \
  --host-api tests/fixtures/scpi_psu/psu_host.py \
  --output /tmp/bench_psu_bundle

.venv/bin/cli-device bundle validate /tmp/bench_psu_bundle
.venv/bin/cli-device bundle build /tmp/bench_psu_bundle --output-dir /tmp/bench_psu_gen
.venv/bin/cli-device bundle build /tmp/bench_psu_bundle --output-dir /tmp/bench_psu_gen --check
.venv/bin/cli-device --bundle /tmp/bench_psu_bundle --help
```

## Safety Boundaries

- **Risk classes R0–R4 & default deny:** unknown state, unhomed motion, out-of-bounds parameters, forbidden zones, and unsafe Z are rejected before `ActionDriver.execute()` is called (exit `3`).
- **`--dry-run` & `--confirm`:** `--dry-run` checks gates and prints a plan with zero actuation (including `--dry-run sys abort`); high-risk actions require explicit `--confirm` (exit `4`).
- **True completion semantics:** command acceptance (such as HTTP 200 on Duet `/rr_gcode`) is never treated as completion; live hardware/simulator adapters must block in `SupervisorChannel.await_settled()` (`M400` + object model `idle` on Duet/RRF) and pass postcondition checks or trigger catalog `fail_safe`.
- **Lock-free abort vs. physical e-stop:** `sys abort` bypasses the action mutex (`< 100 ms` target) and latches e-stop (exit `99`). **Software abort never disconnects mains/motor power and never replaces a tested physical emergency stop.**
- **Honest capabilities:** capability-limited targets (e.g. `grbl_sim`, which is not a real Jubilee) fail unsupported actions with `E_UNSUPPORTED_CAPABILITY` (exit `2`) instead of faking success.

### Exit Codes

`0` OK / dry-run verified · `1` execution fault (fail-safe applied) · `2` usage / schema / unsupported capability · `3` safety denied (zero actuation) · `4` `--confirm` required · `5` timeout / daemon unavailable · `99` e-stop latched

## Repository Layout

| Path | Role |
|---|---|
| `core_types/` | Pure domain types, risk classes, error/exit codes (no I/O) |
| `adapter_spi/` | `SupervisorChannel` & `ActionDriver` protocols + `InMemoryFakeAdapter` |
| `compiler/` | Bundle loader/validator, AST+manual drafter, `SKILL.md`/`HOOKUP.md`/`cli_schema.json` compiler |
| `safety_kernel/` | Device-agnostic R0–R4 gate, preconditions, completion & fail-safe |
| `daemon/` | `cli-device-d` Unix socket IPC server and lazy `cli_any_device.adapters` registry |
| `cli/` | Thin schema-driven `cli-device` frontend and `bundle` toolchain |
| `bundles/jubilee/` | Hand-authored Jubilee verification fixture (`docs/generated/jubilee/` is compiled output) |
| `adapters/` | Pluggable adapters (`duet_rrf`, `mujoco_twin`, `grbl_sim`), strictly isolated from core |

## Verification

```bash
.venv/bin/pytest -v
.venv/bin/mypy
.venv/bin/ruff check core_types adapter_spi compiler safety_kernel daemon cli tests/test_import_dag.py tests/test_contracts_and_spi.py tests/test_compiler.py tests/test_safety_kernel.py tests/test_daemon_and_cli.py tests/test_framework_fixes_and_fixtures.py
.venv/bin/cli-device bundle build jubilee --check
```
