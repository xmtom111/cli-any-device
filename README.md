# CLI-Any-Device

**Let AI drive all your devices**

Turn any open-source hardware with an accessible interface (serial, Ethernet HTTP, TCP/SCPI, or Python host API) plus a manual or host software into a declarative device bundle, a safety-gated AI CLI (`cli-device` + `cli-device-d`), an agent `SKILL.md`, and a human `HOOKUP.md`.

Jubilee (`bundles/jubilee/`) is the first verification fixture, not the product itself. AutoDNA is referenced for its compact atomic-service pattern, not as a liquid-handling continuation.

## Quickstart

```bash
# 1. Draft a bundle for any machine from interface + manual / Python host API (AST-only, no vendor exec)
cli-device bundle draft \
  --name open_radio --display-name "OpenRadio-100" --kind radio \
  --interface usb_serial --endpoint serial:///dev/ttyUSB0 \
  --manual path/to/manual.md --host-api path/to/host.py \
  --output bundles/open_radio

# 2. Validate & compile SKILL.md, HOOKUP.md, and cli_schema.json (CI drift gate with --check)
cli-device bundle build jubilee
cli-device bundle build jubilee --check

# 3. Start the safety daemon and invoke schema-driven commands
cli-device-d --bundle jubilee --socket /tmp/cli-device-d.sock
cli-device --bundle jubilee --target in_memory --socket /tmp/cli-device-d.sock --json sys detect
cli-device --bundle jubilee --target in_memory --socket /tmp/cli-device-d.sock --json --dry-run motion home-all
cli-device --bundle jubilee --target in_memory --socket /tmp/cli-device-d.sock --json --confirm motion home-all
```

## Architecture & Safety Guarantees

Import DAG (enforced by tests): `core_types <- adapter_spi <- safety_kernel <- daemon <- cli`, `core_types <- compiler`. Vendor/simulator libraries are strictly isolated in `adapters/*` plugins loaded via `cli_any_device.adapters` entry points; the kernel contains zero target-name branches.

- **Risk classes R0–R4 & default deny:** unknown state, unhomed motion, out-of-bounds/forbidden zones, and unsafe Z are rejected before `ActionDriver.execute()` (exit `3`).
- **Explicit `--dry-run` & `--confirm`:** `--dry-run` verifies gates and prints a plan with zero actuation; high-risk services require `--confirm` (exit `4`).
- **Completion barrier:** never treats command acceptance (e.g. HTTP 200 on `/rr_gcode`) as physical completion; requires `SupervisorChannel.await_settled()` (`M400` + object model `idle` on Duet/RRF) plus postcondition checks and catalog `fail_safe`.
- **Lock-free out-of-band abort:** `sys abort` bypasses the action mutex (`< 100 ms` compliance SLA) and latches e-stop (exit `99`). Software abort never replaces a tested physical power-cut emergency stop.
- **Honest capabilities:** capability-limited targets (e.g. `grbl_sim`, which is not a real Jubilee) fail unsupported verbs immediately with `E_UNSUPPORTED_CAPABILITY` (exit `2`).

## Verification

```bash
.venv/bin/pytest -v
.venv/bin/mypy
.venv/bin/ruff check core_types adapter_spi compiler safety_kernel daemon cli tests
.venv/bin/cli-device bundle build jubilee --check
```
