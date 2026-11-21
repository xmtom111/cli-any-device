# Vertex consult 05 — v1 scope, usefulness, and acceptance tests

- Date: 2026-11-21 10:20 +08
- Model: gemini-4-carbon (default)
- Tools: default (google_search, url_context)

## Prompt

```
Prior conclusions (tight):
- Architecture: compile declarative Device Bundle (device.yaml + catalog.yaml) into SKILL.md / HOOKUP.md / CLI schema; do NOT generate runtime safety code. Shared runtime: thin CLI, daemon safety kernel (R0–R4, mutex, lock-bypass abort), plugin adapters (SupervisorChannel + ActionDriver).
- v1 hand-authors Jubilee bundle + duet_rrf (science-jubilee + direct RRF supervisor) + mujoco_twin + grbl_sim adapters, but already compiles docs/CLI from YAML. Later Asset Inspector drafts bundles from CAD/docs. --target mujoco_twin | grbl_sim | duet_real; GRBL sim must not pretend to be a real Jubilee (unsupported capabilities fail cleanly).
- Real Jubilee hookup: official Ethernet HTTP 192.168.1.2. Safety and catalog as previously agreed.

Question: v1 scope for the FIRST useful version, with Jubilee as the first test platform. The user insists v1 must actually work and be useful — not a slideware generator. Local assets already include a CAD-backed MuJoCo twin and a GRBL-based AutoDNA sim (use them; do not redo that sim). There may or may not be a physical Jubilee available at first.

Please answer:
1. What must ship in v1 to be useful (must-have vs should-have vs defer). Be concrete: commands, files, tests, manuals.
2. Acceptance tests: simulator path (must pass without hardware) and real-hardware path (when a machine is present). Include safety refusals, dry-run, abort, hookup detect, and a representative useful workflow (e.g. detect → preflight → dry-run home → confirmed home → pickup tool → move-well → park).
3. What “useful” means if there is no physical machine yet (digital twin demo + generated hookup/SKILL that a human can follow later).
4. Explicit deferrals (generic Asset Inspector, Hub marketplace, non-Jubilee boards, MCP server, firmware patching, liquid-dispense AutoDNA services unless a real dispenser tool profile exists).
5. Definition of done / quality gates (tests, import boundaries, no tangled scripts).

Design recommendations. Keep under ~800 words.
```

## Reply

### 1. v1 Deliverables: Must-Have vs. Should-Have

**Must-Have (Core Ship List)**
* **Declarative Bundle & Compiler:** `bundles/jubilee/{device.yaml, catalog.yaml}` compiled via `rig bundle build jubilee` into `SKILL.md`, `HOOKUP.md`, and `cli_schema.json` (zero generated Python safety code).
* **Shared Daemon (`rigd`) & Thin CLI (`rig`):**
  * Kernel enforcing R0–R4 gates, workspace/deck bounds, tool-state tracking, single-writer action mutex, `--dry-run` planner, and a **lock-bypass `abort` path** directly to `SupervisorChannel`.
  * Commands (`--target mujoco_twin | grbl_sim | duet_real`): `rig daemon start`, `rig detect`, `rig preflight`, `rig status`, `rig abort`, and catalog actions `home`, `pickup-tool`, `park-tool`, `move-xyz`, `move-well` (mutating actions require `--dry-run` or `--confirm`).
* **Three Clean Adapters (`SupervisorChannel` + `ActionDriver`):**
  * `mujoco_twin`: wraps the existing local CAD-backed MuJoCo twin (full XYZU kinematics, tool-lock actuation, deck collision/stall flags).
  * `grbl_sim`: wraps the existing local AutoDNA GRBL simulator over PTY/socket (XYZ + homing + realtime `!`/`0x18` abort; explicitly lacks toolchanger capabilities).
  * `duet_rrf`: `science-jubilee` `ActionDriver` + direct HTTP `SupervisorChannel` to `192.168.1.2` (`M409` object-model polling, `M112` emergency stop).
* **Generated Manuals:** Human-actionable `HOOKUP.md` (Ethernet subnet `192.168.1.x/24` $\rightarrow$ `192.168.1.2`, tool-dock verification, umbilical clearance) and agent-ready `SKILL.md` (exact flags, R0–R4 rules, recovery steps).

**Should-Have (High Leverage if Time Permits)**
* Append-only JSONL flight recorder (`logs/audit.jsonl`) logging every preflight, dry-run, state transition, refusal, and abort latency.
* `capture-frame` read action on `mujoco_twin` (renders offscreen PNG) so an agent can visually inspect deck state.
* Deterministic mock-RRF HTTP server fixture to integration-test `duet_rrf` serialization and `M112` paths in CI without hardware.

---

### 2. Acceptance Tests

**Simulator Path (Mandatory in Headless CI — No Hardware)**
1. **End-to-End Useful Workflow (`mujoco_twin`):**
   `rig detect` $\rightarrow$ `rig preflight` $\rightarrow$ `rig home --dry-run` (emits motion/clearance plan; asserts zero sim state delta) $\rightarrow$ `rig home --confirm` $\rightarrow$ `rig pickup-tool --tool 0 --confirm` $\rightarrow$ `rig move-well --labware plate_96 --well A1 --z-offset 5` $\rightarrow$ `rig park-tool --confirm`. Asserts twin joint positions, tool attachment state, and zero collisions.
2. **Safety Refusals:**
   * Attempt `move-well` before homing $\rightarrow$ kernel rejects (`E_UNHOMED` / R2 gate) before driver call.
   * Attempt `pickup-tool` while a tool is already mounted $\rightarrow$ rejected (`E_TOOL_STATE_CONFLICT`).
   * Attempt `move-xyz --z -20` $\rightarrow$ rejected (`E_BOUNDS_VIOLATION`).
3. **Capability Honesty (`grbl_sim`):**
   `detect` $\rightarrow$ `preflight` $\rightarrow$ `home --confirm` $\rightarrow$ `move-xyz` succeeds; `pickup-tool` fails immediately with structured `E_UNSUPPORTED_CAPABILITY` (never silently mocked).
4. **Lock-Bypass Abort (Both Sims):**
   Launch a slow diagonal `move-xyz`; while the `ActionDriver` holds the execution mutex, invoke `rig abort` from a second process. Assert `SupervisorChannel` halts motion in $<100\text{ ms}$, transitions daemon to safe/faulted state, and invalidates homing if required.

**Real-Hardware Path (`--target duet_real`, When Physical Jubilee Is Present)**
1. **Hookup Detect & Failure Mode:** With Ethernet unplugged or wrong subnet, `rig detect --target duet_real` fails cleanly citing the exact `HOOKUP.md` section. When connected to `192.168.1.2`, verifies RRF firmware identity and `M409` telemetry.
2. **Preflight & Physical Workflow:** `rig preflight` inspects axis homed flags, endstops, VIn voltage, and dock state. Operator runs the exact same workflow (`home --dry-run` $\rightarrow$ `home --confirm` $\rightarrow$ `pickup-tool` $\rightarrow$ `move-well` at safe Z clearance above an empty plate $\rightarrow$ `park-tool`).
3. **Live Abort:** Mid-move `rig abort` fires direct HTTP `M112` bypassing any blocked `science-jubilee` call, immediately halting steppers.

---

### 3. What “Useful” Means Without Physical Hardware
v1 is immediately useful without a physical machine if:
1. **Real Agent Protocol Validation:** An LLM/operator given *only* the compiled `SKILL.md` can discover tools/wells, navigate R0–R4 refusals, use `--dry-run`, and execute multi-step plate-indexing workflows against the physics-backed `mujoco_twin`.
2. **Zero-Rewrite Hardware Cutover:** `HOOKUP.md` gives a lab technician a step-by-step Ethernet/power/dock checklist (`192.168.1.2`) so when a Jubilee arrives, switching `--target mujoco_twin` to `--target duet_real` runs the exact same validated CLI workflow.

---

### 4. Explicit Deferrals (Out of Scope for v1)
* **Generic Asset Inspector** (auto-drafting YAML bundles from CAD/manuals).
* **Hub/Marketplace** bundle registry and multi-machine fleet auth.
* **MCP Server wrapper** (thin CLI + compiled `SKILL.md` is leaner and sufficient for v1 agents).
* **Non-Jubilee boards** (Klipper, Smoothieware) and **RRF firmware/`config.g` patching** (`preflight` validates `config.g` assumptions read-only; never mutates SD card files).
* **AutoDNA liquid-dispense services** (aspirate/dispense/mix), unless a physical syringe/pipette tool profile and tip-rack geometry are explicitly added to `device.yaml`.

---

### 5. Definition of Done & Quality Gates
* **Enforced Import Boundaries (CI Linter):**
  * `compiler/` imports YAML/schema libs only—zero runtime or driver imports.
  * `cli/` speaks IPC to `rigd` only—zero imports of `mujoco`, `serial`, `requests`, or `science_jubilee`.
  * `kernel/` owns all R0–R4 checks, bounds, and locking; `adapters/` implement strictly `SupervisorChannel` and `ActionDriver`.
* **Zero Tangled Sim Scripts:** Existing MuJoCo and AutoDNA GRBL sims remain standalone external/wrapped assets behind clean adapter interfaces—no copy-pasted simulator internals inside `kernel/`.
* **Zero Doc Drift:** `rig bundle build jubilee --check` fails CI if committed `SKILL.md`, `HOOKUP.md`, or `cli_schema.json` diverge from `device.yaml` + `catalog.yaml`.
* **Design Recommendation:** Keep `SupervisorChannel` on its own dedicated HTTP session / serial writer thread in `rigd` so a hung `ActionDriver` socket or GIL-bound call can never starve `rig abort`.


## Meta

First call failed with HTTP 429 RESOURCE_EXHAUSTED after the Vertex CLI's built-in retries. One explicit retry after ~90s succeeded (CLI also retried transient 'service unavailable' three times). Tools were enabled; no search-query dump on the successful call.
