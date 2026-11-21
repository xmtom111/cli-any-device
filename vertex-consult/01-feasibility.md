# Vertex consult 01 — Feasibility of wrapping open hardware as atomic CLI

- Date: 2026-11-21 10:15 +08
- Model: gemini-4-carbon (default)
- Tools: default (google_search, url_context)

## Prompt

```
You are advising on a new open-source tool: turn arbitrary open-source hardware into a safe, AI-callable CLI (similar outcome to CLI-Anything, but for real machines).

Question (feasibility only; be concrete, not marketing):

Is it feasible to wrap existing open-source hardware as atomic CLI services WITHOUT forking or rewriting vendor firmware?

Constraints:
- Borrow AutoDNA "atomic services": each service is one self-contained hardware operation with a natural-language description, preconditions, and a uniform result. Agents use a compact catalog (dozens of lines), not the original control code on every call.
- Do not scramble original control code. Prefer a host-side adapter over firmware forks. If firmware changes are needed, they must be optional, surgical, and backward compatible.
- Prefer an already-open data/control interface (HTTP, serial G-code, vendor SDK, host software API). Do not invent a new physical bus unless no usable interface exists.
- Safety is mandatory: detect, preflight, dry-run, limits, e-stop. Hardware actions have irreversible side effects.
- First target later will be Jubilee (Duet/RepRapFirmware), but this question is general.

Please answer:
1. Feasibility verdict: yes / yes-with-limits / no. One sentence why.
2. The right wrapping layer (host adapter vs firmware patch vs both) and when each is justified.
3. What classes of open hardware this works for vs where it breaks (no open interface, unsafe implicit state, real-time closed loops, etc.).
4. Hard requirements so a first version can actually work and be useful, not a demo that only talks to a simulator.
5. Pitfalls if we blindly copy CLI-Anything (software GUI→CLI) onto hardware.

Cite official docs or well-known interfaces when you use search. Distinguish verified facts from design recommendations. Keep the reply under ~800 words.
```

## Reply

### 1. Feasibility Verdict
**Yes-with-limits:** Wrapping open-source hardware in a host-side atomic CLI without forking firmware is feasible whenever the controller exposes queryable machine state, a completion barrier, and an out-of-band abort path, but it breaks on high-frequency closed-loop control and cannot replace hardwired emergency power cutoffs.

---

### 2. The Right Wrapping Layer
*Design recommendation:* Use a **persistent host daemon + thin CLI client** as the default architecture, paired with optional controller macros rather than firmware C/C++ forks.

* **Host adapter alone (default, ~85% of targets):** Justified when the machine already exposes structured state and control APIs.
  * *Verified facts:* Duet/RepRapFirmware (RRF) exposes its full machine state ("Object Model") over HTTP (`/rr_model`) and Duet Software Framework REST/Python APIs; Klipper exposes state and `printer.emergency_stop` via Moonraker HTTP/WebSocket; Jubilee already has a host Python driver (`science-jubilee`) over Duet HTTP/G-code.
* **Host adapter + controller config/macros (no firmware fork):** Justified when multi-step physical handshakes (e.g., Jubilee tool pickup/park) must execute atomically without host network jitter.
  * *Verified fact:* RRF supports custom G-code macros in `0:/sys/`, custom Object Model keys, and endstop-triggered actions (`M581`) without recompiling firmware.
* **Surgical firmware patch (last resort):** Justified *only* on bare-MCU firmwares (e.g., custom Arduino sketches) that lack status reporting, command-completion acknowledgments, or an unqueued abort byte.

---

### 3. Where It Works vs. Where It Breaks
* **Works well:** Discrete, supervisory "move–settle–actuate–verify" machines: 3D printers, multi-tool lab platforms (Jubilee), CNC/laser gantries (GRBL v1.1), SCPI benchtop instruments, and Modbus/GPIO relays.
  * *Verified fact:* AutoDNA (`IPADS-X/AutoDNA`) demonstrated this pattern across 20+ lab instruments by pairing natural-language "atomic services" with a separate stateful Hardware Executor & Validator (HEVA).
* **Where it breaks:**
  * **Real-time closed loops (>10–50 Hz):** Force-controlled assembly, active damping, or visual servoing cannot tolerate CLI process startup and HTTP/serial jitter.
  * **Write-only / buffered-ACK protocols:** Controllers that return `ok` upon *queuing* a move rather than *finishing* it, without state polling. (*Verified fact:* RRF acknowledges `G0`/`G1` immediately upon entering the look-ahead move queue.)
  * **Unobservable implicit state:** Manual labware swaps, bent needles, or open-loop stepper stalls unless physical sensors exist to verify preconditions/postconditions.

---

### 4. Hard Requirements for a Useful V1
*(Design recommendations grounded in hardware behavior)*

1. **Long-lived local daemon owning the transport:** Individual CLI invocations must talk to a local daemon via IPC, not directly to hardware. Directly opening USB serial per CLI call toggles DTR and resets many AVR/Arduino controllers, and prevents cross-process locking.
2. **True physical completion barriers:** Every atomic service must block until motion/thermal settle (e.g., issuing synchronization + polling RRF's Object Model) and check postconditions (e.g., tool-lock switch asserted) before returning structured JSON.
3. **Dedicated out-of-band abort channel + physical E-stop requirement:**
   * *Verified facts:* In RRF, sending `M112` on the primary serial link waits behind blocking commands like `G28` unless sent over HTTP, a secondary channel, or out-of-band bytes `0xF0 0x0F`. In GRBL v1.1, queued G-code waits in the planner buffer while single-byte real-time commands (`!` feed hold, `Ctrl-X` reset) execute immediately—yet GRBL feed hold pauses motion *without* guaranteed laser/spindle shutoff, and RRF `M112` does *not* cut 12/24V PSU power.
   * *Requirement:* V1 must wire software `abort` to the controller's unqueued channel *and* mandate an independent hardwired E-stop/relay for hazardous energy.
4. **Live-state preflight & kinematic dry-run:** `dry-run` cannot just lint CLI arguments; it must query live controller state (`homed` flags, active tool offset, soft limits) and check 3D deck/fixture bounding boxes before permitting execution.

---

### 5. Pitfalls of Blindly Copying CLI-Anything onto Hardware
*Verified fact:* `CLI-Anything` (`HKUDS/CLI-Anything`) generates CLIs by analyzing software source code and relies on JSON session files, file locking, dry-run, and undo/redo state snapshots (e.g., for Blender/FreeCAD/GIMP). Copying this directly to hardware fails in four ways:

1. **No `undo` stack in physics:** Restoring a JSON snapshot cannot un-dispense liquid, un-mill metal, or un-crash a pipette tip. Recovery requires explicit compensating services (`park_safely`, `rehome`), not state rollback.
2. **World state mutates between calls:** Unlike a static `.blend` file on disk, heaters cool, steppers time out and sag, and humans bump labware between CLI calls. Host session JSON can store *calibration/deck config*, never *ground-truth machine state*.
3. **Killing the CLI process ≠ stopping the machine:** If an agent times out or sends `SIGINT` to a CLI call, buffered G-code and heaters continue running on the MCU. Signal traps must actively command a controlled hold/heater-off on the daemon.
4. **Source-code auto-generation exposes unsafe primitives:** Scraping `science-jubilee` or firmware repos to expose every public method lets an agent call low-level actions (e.g., unlatching the tool twist-lock while suspended over a well plate). Hardware atomic services must be **invariant-preserving transactions** (e.g., `dropoff_tool(index)` enforcing dock coordinates and sensor checks), never raw 1:1 API mirrors.

## Meta

Tools used: google_search (default).
Search queries (from stderr):

- "CLI-Anything" github; "AutoDNA" "atomic services"; RepRapFirmware object model HTTP API; Klipper Moonraker API e-stop; "science-jubilee" github; RepRapFirmware M112 emergency stop queue rr_gcode; grbl real-time commands feed hold

Cited domains (redirect URLs omitted): duet3d.com, github.com, readthedocs.io, theindustrialmaker.com, easel.com, openai-hub.com, pebblous.ai
