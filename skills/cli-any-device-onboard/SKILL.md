---
name: cli-any-device-onboard
description: Onboard a device into cli-any-device by drafting its bundle, declaring safety gates, and verifying it for human review.
---

# Onboard a device

Follow this protocol in Claude Code, Codex, Cursor, or OpenClaw.

1. Inspect the device interface and its manual, PDF, datasheet, or host API. Identify commands, argument units and limits, completion signals, and emergency-stop behavior.
2. Draft `device.yaml` and `catalog.yaml` with `cli-device bundle draft` or write the YAML directly. Prefer the built-in `scpi_generic`, `http_json`, or `moonraker_http` adapter. Define connection settings in the target's `adapter_options` and requests in each service's `protocol` with zero per-device Python. Write a custom `SupervisorChannel` / `ActionDriver` SPI adapter and a compliance test only when YAML cannot express the protocol.
3. Declare risk classes (`R0_READ` through `R4_EXPERT`), bounds, `confirm_if`, preconditions, postconditions, and `fail_safe` in YAML. Let `SafetyKernel` enforce them. Never write custom runtime safety logic or bypass confirmation gates.
4. Keep `review_status: draft` until a human verifies the limits, dry-run plans, and hardware safety. Live physical actuation requires `review_status: verified` and `cli-device-d --allow-live-hardware`.
5. Run `cli-device bundle validate <bundle>` and `cli-device bundle build <bundle> --check` before handoff. If needed, generate the artifacts with `cli-device bundle build <bundle>` before checking for drift. When adding an adapter, also run `pytest tests/adapter_compliance`.
