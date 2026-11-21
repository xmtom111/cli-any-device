# Vertex consult 03 — Atomic-service catalog, SKILL.md, and safety gates

- Date: 2026-11-21 10:18 +08
- Model: gemini-4-carbon (default)
- Tools: default (google_search, url_context)

## Prompt

```
Prior conclusions (tight):
- Feasible with limits: host daemon + thin CLI, no firmware fork; true completion barriers; out-of-band abort; no CLI-Anything-style undo.
- Real Jubilee v1: primary interface is official Duet Ethernet HTTP (default 192.168.1.2), USB serial is diagnostic fallback. Hookup follows jubilee3d.com (dongle, PC 192.168.1.3). Daemon auto-detects standalone /rr_* vs SBC /machine/*. Append M400 then poll object model; never treat /rr_gcode HTTP 200 as done. Abort is lock-bypassing M112 plus physical E-stop. Catalog should wrap science-jubilee verbs, with a thin direct-RRF supervisor for session, barriers, and abort.

Question: Design the atomic-service catalog and CLI surface for agents.

Borrow AutoDNA atomic services and the local AutoDNA sim catalogue style (20-line compact catalogue; each service blocks until finished; uniform error with step+reason; on failure, fail-safe and clear the queue). If the device has host software, CLI-ize those features (science-jubilee for Jubilee: home_all, home_xyu, home_z, move_to, move, pickup_tool, park_tool, load_deck, load_labware, tool_lock/unlock, safe_z, status/position, gcode escape hatch). Outcome should resemble CLI-Anything: --help, --json, optional REPL, and a token-minimal SKILL.md. Safety is mandatory: detect, preflight, dry-run, limits, e-stop. Agents must not re-read original control code on every call. Minimize tokens. Do not expose raw unsafe primitives as first-class agent verbs (e.g. unlock tool while suspended over a plate).

Please answer:
1. Catalog schema (fields per service: name, args, preconditions, effects, fail-safe, timeout, risk class).
2. Proposed v1 Jubilee command groups and concrete CLI examples (including detect, preflight, dry-run, status, abort/estop). Which commands are agent-default vs expert/escape-hatch (raw gcode).
3. How SKILL.md should be structured so an agent can work from ~1–2 screens of text, not the Duet manual.
4. Safety gate policy: when to refuse, when to require --confirm or a human, default-deny rules (unhomed motion, tool change without clear deck, raw gcode, etc.).
5. Output contract: JSON schema, exit codes, and what "completion" means.

Design recommendations unless citing official APIs. Keep under ~900 words.
```

## Reply

### 1. Atomic-Service Catalog Schema

The host daemon (`jubileed`) exposes atomic, blocking services wrapping `science-jubilee` state and Duet HTTP (`/rr_*` or `/machine/*`). Every service entry in `catalog.yaml` defines seven fields:

* **`name`**: `<group>.<verb>` (e.g., `tool.pickup`).
* **`args`**: Typed parameters with units and bounds (`x_mm: float[0..300]`, `tool_id: int`).
* **`preconditions`**: Machine/daemon predicates checked *before* emission (`homed == [X,Y,Z,U]`, `active_tool == null`, `z_mm >= safe_z`).
* **`effects`**: Expected post-state mutations (`active_tool = index`, `pos = park_post[index]`).
* **`fail_safe`**: Daemon action upon mid-service fault before returning (`clear_queue + hold` for XY/tool collisions; `clear_queue + z_retract(safe_z)` if vertical clearance is known; `m112` on runaway/stall).
* **`timeout_s`**: Hard deadline for `M400` + idle confirmation (e.g., `5s` state, `30s` move, `90s` homing).
* **`risk_class`**:
  * `R0_READ`: Read-only queries (`sys.status`, `sys.detect`).
  * `R1_CONFIG`: Virtual deck/labware mutation (`deck.load_labware`).
  * `R2_MOTION`: Bounded, collision-checked motion (`motion.move_safe`).
  * `R3_PHYSICAL_INIT`: Homing and tool swaps (`motion.home_all`, `tool.pickup`).
  * `R4_EXPERT`: Unsafe/raw primitives hidden from standard agents (`expert.gcode`, `expert.tool_unlock`).

---

### 2. v1 Jubilee Command Groups & CLI Surface

Agents call a thin stateless binary (`jubilee --json ...`) over a local socket to `jubileed` (`jubilee repl` optional for interactive sessions).

**Agent-Default Commands** (exposed in `SKILL.md`):
* **`sys`**: `detect`, `status`, `preflight`, `abort`
* **`deck`**: `load-deck <file>`, `load-labware --slot <n> <file>`, `safe-z <mm>`
* **`motion`**: `home-all`, `home-xyu`, `home-z`, `move-safe` (Z-hop via `safe_z`, then XY, then Z), `move-to` (direct guarded linear), `move-rel`, `move-well` (labware-indexed)
* **`tool`**: `pickup <id>`, `park` (atomic approach, U-lock/unlock, wing-sensor check, retract)

**Expert / Escape-Hatch Commands** (omitted from agent prompt; gated behind `expert`):
* **`expert`**: `tool-lock`, `tool-unlock`, `home-u`, `gcode "<raw>"` — Raw `tool-unlock` is never a first-class agent verb because unlocking `U` away from a tool rack drops the payload onto the deck.

**Concrete CLI Examples**:
```bash
jubilee --json sys detect                          # Probes 192.168.1.2 (/rr_model vs /machine/status), USB fallback
jubilee --json deck load-labware --slot 1 corning_96_wellplate.json
jubilee --json sys preflight                       # Verifies comms, voltages, endstops, tool-rack occupancy, safe_z
jubilee --json --confirm HUMAN_CLEAR motion home-all
jubilee --json --dry-run motion move-safe --x 120 --y 85 --z 45
jubilee --json tool pickup 0                       # Blocks until picked, locked, retracted to safe_z, M400 idle
jubilee --json sys abort                           # Lock-bypassing out-of-band M112; reminds operator of physical E-stop
```

---

### 3. Token-Minimal `SKILL.md` Structure (~1.5 Screens / ~350 Tokens)

Agents never read `science-jubilee` Python internals or Duet G-code docs. `SKILL.md` contains four compact blocks:

1. **Operating Rules (4 lines)**: Always pass `--json`. Every command blocks until physical completion. Run `sys preflight` once per session and `--dry-run` before mutating tool/deck sequences. Never guess coordinates—use `move-well` when labware is loaded.
2. **20-Line AutoDNA-Style Service Catalog**:
   ```text
   sys.detect()                        -> endpoint, api_mode          [R0]
   sys.status()                        -> pos, homed, tool, safe_z    [R0]
   sys.preflight()                     -> readiness_report            [R0]
   sys.abort()                         -> ESTOP_LATCHED (M112)        [R0, bypasses lock]
   deck.load_deck(spec)                -> slots, safe_z               [R1]
   deck.load_labware(slot, spec)       -> well_map, top_z             [R1, req: deck]
   deck.safe_z(z_mm)                   -> safe_z                      [R1, z >= max_labware_z+5]
   motion.home_all()                   -> homed=[X,Y,Z,U]             [R3, req: --confirm, clear_bed]
   motion.home_xyu()                   -> homed+=[X,Y,U]              [R3, req: z >= safe_z, tool_parked]
   motion.home_z()                     -> homed+=[Z]                  [R3, req: --confirm, active_tool==null]
   motion.move_safe(x,y,z?,speed?)     -> pos (Z->safe_z->XY->Z)      [R2, req: homed_all]
   motion.move_to(x,y,z,speed?)        -> pos (linear; z>=safe_z req) [R2, req: homed_all]
   motion.move_rel(dx,dy,dz,speed?)    -> pos (bounded)               [R2, req: homed_all]
   motion.move_well(slot,well,dz?)     -> pos                         [R2, req: homed_all, labware]
   tool.pickup(id)                     -> active_tool=id              [R3, req: homed_all, active_tool==null]
   tool.park()                         -> active_tool=null            [R3, req: homed_all, active_tool!=null]
   ```
3. **Standard Loop (3 lines)**: `sys status` -> `deck load-*` -> `--dry-run` -> execute -> inspect `.ok` and `.error.step`.
4. **Fault Protocol (2 lines)**: Any non-zero exit halts and clears the RRF queue automatically. Do not blind-retry `R3` faults; inspect `sys status` or escalate to human.

---

### 4. Safety Gate Policy

* **Default-Deny (Hard Refuse, Exit `3`)**:
  * Any `motion.move*` or `tool.*` when any axis `[X,Y,Z,U]` is unhomed.
  * `motion.move_to` / `move_rel` that crosses XY across deck slots below `max(deck_obstacles_z) + 5mm` (`safe_z`).
  * Coordinates outside soft machine envelope or active tool Z-offset bounds.
  * `tool.pickup` when `active_tool != null`, or `home-xyu` with an unverified/dangling carriage state.
  * Lowering `safe_z` below the tallest loaded labware + tip/syringe length.
* **Confirmation / Human Gate (Requires `--confirm <token>`, Exit `4`)**:
  * `motion.home_all` and `motion.home_z` (requires human visual verification that bed is clear for Z-probe and all tools rest on posts).
  * Overwriting loaded deck geometry mid-session.
  * Resetting daemon latch after `sys.abort` (`M112`).
  * All `expert.*` commands (`gcode`, manual `tool-unlock`, which additionally checks XY proximity to a rack post unless `--force-human-override` is supplied interactively).

---

### 5. Output Contract & Completion Semantics

* **What "Completion" Means**: In live mode, `jubileed` translates the verb into guarded G-code, appends `M400`, dispatches it, and polls the Duet object model (`state.status == "idle"`, `move.axes[*].homed`, endstop/tool sensors matching `effects`). HTTP `200` from `/rr_gcode` only means *buffered*; the CLI never exits `0` until motion physically settles and postconditions pass. On failure or timeout, `jubileed` aborts the RRF queue, runs `fail_safe`, and reports the exact sub-step.
* **JSON Schema**:
  ```json
  {
    "ok": false,
    "service": "tool.pickup",
    "mode": "live",
    "duration_ms": 4120,
    "state": {"homed": ["X","Y","Z","U"], "active_tool": null, "pos": [285.0, 42.0, 120.0], "safe_z": 120.0},
    "error": {"code": "PRECONDITION_OR_HW_FAULT", "step": "u_lock_engage", "reason": "Torque/endstop mismatch on U", "fail_safe_applied": "queue_cleared_hold_pos"}
  }
  ```
* **Exit Codes**:
  * `0`: Completed (or valid `--dry-run` trajectory verified).
  * `1`: Hardware/execution fault mid-service (queue cleared, `fail_safe` executed).
  * `2`: Invalid CLI arguments / schema error.
  * `3`: Safety gate refusal (precondition/envelope violation; zero motion emitted).
  * `4`: Human `--confirm` token required.
  * `5`: Daemon/Duet transport timeout.
  * `99`: Emergency stop active (`M112` latched; requires physical inspection + reset).

## Meta

Tools were enabled; this call produced no stderr meta (no search-query dump).
