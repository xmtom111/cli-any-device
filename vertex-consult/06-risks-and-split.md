# Vertex consult 06 — Risks, non-goals, quality bar, and agent work split

- Date: 2026-11-21 10:32 +08
- Model: gemini-4-carbon (default)
- Tools: default (google_search, url_context)

## Prompt

```
Prior conclusions (tight):
- Feasible with limits: host daemon + thin CLI; no firmware fork; official Duet Ethernet HTTP for real Jubilee; wrap science-jubilee + direct RRF supervisor.
- Architecture: hand-authored Device Bundle compiled to SKILL.md/HOOKUP.md/CLI schema; shared safety kernel; plugin adapters; --target mujoco_twin | grbl_sim | duet_real; GRBL must not pretend to be Duet.
- v1 must-haves: jubilee bundle, compiler, daemon+CLI, three adapters wrapping existing local sims, generated manuals, CI tests for workflow/refusals/abort/capability honesty. Defer generic Asset Inspector, Hub, MCP, firmware patching, non-Jubilee boards, AutoDNA liquid services unless a real dispenser profile exists.

Question: Risks, non-goals, and quality bar. Also recommend a work split for two coding agents (Antigravity and Codex) that have not started yet. The human asked for excellent architecture, no tangled code, and that the first version actually work and be useful.

Please answer:
1. Explicit non-goals for v1 and for the product overall (what we will refuse to build or claim).
2. Top risks with mitigations (safety incidents, interface variance Duet 2 vs 3, science-jubilee drift, sim-to-real gap, token-catalog drift, session/DTR reset, concurrent DWC clients, unobservable world state).
3. Quality bar and engineering rules (module DAG, tests, documentation generated from YAML, review checklist before any live motion).
4. Suggested split of work between Antigravity and Codex: clear module ownership, interfaces first, no overlapping files, order of integration, what each must not do (e.g. do not invent firmware, do not treat GRBL sim as real Jubilee, do not start coding until the human approves this plan).
5. Any remaining open questions that must be decided before implementation (naming, physical machine availability, default --confirm policy).

Design recommendations. Keep under ~800 words.
```

## Reply

### 1. Explicit Non-Goals
* **v1 Non-Goals:** No generic Asset Inspector, Hub/registry, or MCP server. No non-Jubilee boards or kinematics. No AutoDNA/liquid-handling services unless a real physical dispenser profile is supplied.
* **Permanent Product Non-Goals:** We will never fork or patch RepRapFirmware (RRF) or GRBL. We will never perform host-side real-time step generation. We will never make `grbl_sim` spoof Duet macros, toolchanges, or RRF object models. We will never claim collision immunity against unmodeled deck obstacles or manual human intervention.

### 2. Top Risks & Mitigations
* **Safety incidents (crashes/pinch):** All motion passes through a shared `safety_kernel` enforcing axis soft-limits, homing prerequisites, and tool-lock verification. `abort` uses an out-of-band priority channel to fire immediate `M112`/feed-hold without waiting behind queued moves.
* **Interface variance (Duet 2 vs. Duet 3):** Duet 2 Standalone (`/rr_*` HTTP endpoints) differs from Duet 3 SBC (`dsf` REST API). Mitigation: target RRF ≥3.4 unified object model; RRF supervisor probes endpoints on connect and normalizes status into a single internal state struct.
* **`science-jubilee` drift:** Pin an exact commit/version and isolate it strictly inside `adapters/duet_real`. Never leak `science-jubilee` classes into the daemon or safety kernel.
* **Sim-to-real gap:** Enforce *capability honesty*. Each target declares capabilities (`axes_3`, `toolchange`, `rrf_macros`). `grbl_sim` explicitly refuses toolchanges (`ERR_CAPABILITY_UNSUPPORTED`) rather than faking success; `mujoco_twin` validates kinematics/clearances, not RRF G-code edge cases.
* **Token-catalog drift (LLM docs vs. CLI):** `SKILL.md`, `HOOKUP.md`, and CLI schemas are 100% compiled from the hand-authored `jubilee.bundle.yaml`. CI fails if `git diff` is dirty after running the compiler.
* **Session / DTR resets:** Real Jubilee uses Duet Ethernet HTTP (immune to USB DTR resets). For `grbl_sim` over PTY/serial, explicitly disable `hupcl`/DTR toggling on port open so reconnecting clients never silently reset machine state.
* **Concurrent DWC (Duet Web Control) clients:** RRF supervisor polls object-model sequence counters (`seqs.reply`, `seqs.state`) and movement status. Uncommanded state changes immediately invalidate the daemon’s session lease and pause execution until re-verified.
* **Unobservable world state:** Treat deck layout and initial tool state on boot as `UNKNOWN`. Refuse Z-plunges or tool pickups until endstops/tool-lock sensors confirm state and the operator attests deck clearance.

### 3. Quality Bar & Engineering Rules
* **Strict Acyclic Module DAG (enforced in CI):**
  `core_types` ← `bundle_compiler`
  `core_types` ← `adapter_spi` ← `safety_kernel` ← `daemon` ← `cli`
  `adapter_spi` ← `adapters/{mujoco_twin, grbl_sim, duet_real}` (loaded via entrypoints/DI by `daemon`). Zero cross-adapter or upward imports.
* **Generated Truth:** Zero hand-editing of `SKILL.md`, `HOOKUP.md`, or CLI schemas.
* **CI Test Gate:** Automated tests must pass before merge: (1) valid workflow execution, (2) safety kernel refusals (unhomed, soft-limit breach, unknown tool), (3) sub-second async abort preemption, and (4) capability-honesty refusal on `grbl_sim`.
* **Live-Motion Readiness Checklist:** Physical E-stop tested; RRF firmware version & Duet topology verified; tool-lock state read back cleanly; zero active external DWC motion; full workflow dry-run passed on `mujoco_twin`; bounding-box limits audited against physical deck.

### 4. Two-Agent Work Split (Antigravity & Codex)
**Hard Rule:** Neither agent writes code until the human approves this plan.

* **Antigravity — Control Plane, Compiler & UX**
  * *Owns:* `core_types/`, `adapter_spi/` (lead author), `bundles/jubilee/`, `compiler/`, `safety_kernel/`, `daemon/`, `cli/`, `docs/generated/`.
  * *Must NOT:* Import `science-jubilee`/MuJoCo/serial libraries, edit `adapters/*`, or embed target-specific `if target == ...` hacks inside `safety_kernel` (use capability flags instead).
* **Codex — Hardware/Sim Adapters & Compliance Harness**
  * *Owns:* `adapters/duet_real/` (`science-jubilee` wrapper + RRF HTTP supervisor), `adapters/mujoco_twin/`, `adapters/grbl_sim/`, `tests/adapter_compliance/`.
  * *Must NOT:* Invent firmware behavior, fake toolchanges in `grbl_sim`, bypass `adapter_spi`, modify `core_types`/`compiler`, or touch physical hardware unattended.
* **Integration Order (Zero Overlapping Files):**
  1. **Phase 0 (Contracts):** Antigravity drafts `core_types`, `adapter_spi`, and `jubilee.bundle.yaml`; Codex reviews for hardware/sim realism; human signs off and freezes interfaces.
  2. **Phase 1 (Parallel Build):** Antigravity builds compiler, safety kernel, daemon, and CLI against an in-memory dummy adapter. Codex builds `grbl_sim`, `mujoco_twin`, and `duet_real` against a standalone SPI compliance test suite.
  3. **Phase 2 (Sim Integration):** Wire Codex’s `grbl_sim` and `mujoco_twin` into Antigravity’s daemon; make full CI green.
  4. **Phase 3 (Hardware Bring-Up):** Supervised `duet_real` read-only telemetry test first, then air-cut motion, then toolchange.

### 5. Open Questions to Decide Before Coding
1. **Naming & Stack:** Exact repo/CLI binary name (e.g., `jubilee-ctl` vs. generic daemon name) and implementation language lock (e.g., Python 3.12+ with strict `mypy`/`ruff`)?
2. **Physical Jubilee Specs:** Duet 2 Ethernet or Duet 3 (Standalone vs. SBC)? Exact RRF version, bed dimensions, and installed tools for v1 `jubilee.bundle.yaml`?
3. **Default `--confirm` Policy:** For `--target duet_real`, require interactive confirmation on *every* physical motion command, or issue a time-bounded session lease (`--confirm-deck` once per setup, invalidated on abort/DWC interference)?
4. **Local Sim Packaging:** Are the existing local MuJoCo twin and GRBL simulator invoked via local paths/env vars, git submodules, or Docker?

## Meta

Tools were enabled; this call produced no stderr meta (no search-query dump).
