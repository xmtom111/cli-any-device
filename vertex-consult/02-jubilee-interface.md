# Vertex consult 02 — Real Jubilee control interface and human hookup

- Date: 2026-11-21 10:17 +08
- Model: gemini-4-carbon (default)
- Tools: default (google_search, url_context)

## Prompt

```
Prior conclusion (tight): Wrapping open hardware as a host-side atomic CLI is feasible with limits if the controller has queryable state, a true completion barrier, and an out-of-band abort. Default is a long-lived host daemon + thin CLI, not a firmware fork. Optional controller macros are OK; surgical firmware patches are last resort. Do not invent a new physical bus if an open interface exists. CLI-Anything-style undo/source scraping is unsafe on hardware.

This question is specifically about a REAL Jubilee (open-source multi-tool motion platform from Machine Agency), NOT the AutoDNA sim on this box. Important distinction (verified on-disk vs official docs):
- The local AutoDNA simulation uses a GRBL 1.1 TCP emulator plus a custom HTTP host. That is a benchmark design default, not how a real Jubilee is controlled.
- Official Jubilee is typically Duet 2 Ethernet (or Duet 3), RepRapFirmware, G-code, and Duet Web Control.
- Official hookup wiki (jubilee3d.com Connecting to Jubilee): USB-to-Ethernet dongle + Ethernet cable; default config.g sets M552 P192.168.1.2 S1, gateway 192.168.1.3, netmask 255.255.255.0; PC is set to 192.168.1.3; browse http://192.168.1.2 or http://jubilee.local (RRF 2.4+).
- Official host software for lab use: machineagency/science-jubilee. Machine(address="192.168.1.2") talks HTTP; also supports USB serial (port, 115200) and simulated=True. Features include home_all/home_xyu/home_z, move_to/move, pickup_tool/park_tool, load_deck/load_labware, tool_lock/unlock, gcode(), position/state, safe_z_movement, crash_detection option.
- Duet standalone HTTP: GET /rr_connect, /rr_gcode?gcode=, /rr_reply, /rr_model. Duet 3 SBC: GET /machine/connect, POST /machine/code, GET /machine/model.
- Config is installed by copying Jubilee sys/ and macros/ onto the Duet MicroSD card (official wiki). Tool change is T<n> / T-1 plus macros.

Question: What control interface should v1 use for a real Jubilee, and how should a human hook it up?

Please answer:
1. Recommended primary interface for v1 (and fallback). Justify. Do NOT recommend inventing a new bus.
2. Concrete hookup steps a human can follow (cables, IP, first health checks, what NOT to do before homing). Reference official wiki/docs.
3. How the CLI/daemon should talk to Duet (which HTTP API, session, completion wait, e-stop path). Note standalone Duet 2 vs Duet 3 SBC differences.
4. Whether to wrap science-jubilee APIs as the atomic catalog source vs talking to RRF directly vs both.
5. What must be confirmed on the physical machine (facts we cannot verify from docs alone).

Distinguish verified official facts from design recommendations. Keep under ~800 words.
```

## Reply

### 1. Primary Interface & Fallback
* **[Official Fact]** Real Jubilees run RepRapFirmware (RRF) on Duet 2 Ethernet or Duet 3 hardware, configured out-of-the-box for point-to-point Ethernet HTTP and Duet Web Control (DWC). `science-jubilee` uses HTTP as its primary transport and supports 115200-baud USB serial as an alternative.
* **[Recommendation]** **Use Duet Ethernet HTTP as the v1 primary interface; keep USB serial strictly as a local diagnostic/recovery fallback.** Do not invent a new bus. Ethernet HTTP matches official documentation, isolates ground loops better than USB around stepper power, and allows concurrent state polling plus out-of-band emergency stop (`M112`) without blocking behind a full serial G-code ring buffer.

### 2. Human Hookup & Safe Bring-Up
* **[Official Fact]** Per the `jubilee3d.com` *Connecting to Jubilee* wiki:
  1. Ensure Jubilee’s `sys/` and `macros/` directories are copied onto the Duet MicroSD card. Default `sys/config.g` sets `M552 P192.168.1.2 S1` (netmask `255.255.255.0`, gateway `192.168.1.3`).
  2. Connect a host USB-to-Ethernet adapter directly to the Duet Ethernet port.
  3. Set the host adapter to static IPv4 `192.168.1.3`, subnet `255.255.255.0`.
  4. Power on Jubilee and open `http://192.168.1.2` (or `http://jubilee.local` on RRF 2.4+) to load DWC.
* **[Recommendation]** **First health checks & pre-homing rules:**
  1. Close DWC (or keep it read-only) once verified so two clients do not fight over standalone HTTP sessions.
  2. Visually verify the carriage is empty (no tool half-mounted) and manually test XYUZ/tool-lock endstop indicators in DWC or via daemon status before moving.
  3. **What NOT to do before homing:** Never run `move_to`/`G0`/`G1` coordinates, `T<n>`/`T-1` tool changes, or `tool_lock`/`tool_unlock` before homing (`U` twist-lock + `Z` bed drop + `XY`). Moving unhomed risks ramming parked tools or labware; unlocking unparked drops the tool.

### 3. Daemon-to-Duet Protocol Design
* **[Official Fact]** Duet exposes two HTTP dialects:
  * **Standalone (Duet 2 / Duet 3 without SBC):** `GET /rr_connect`, `GET /rr_gcode?gcode=...` (queues code, does not wait for physical completion), `GET /rr_reply`, and `GET /rr_model` (RRF 3.x object model; legacy RRF 2.x used `/rr_status`).
  * **SBC Mode (Duet 3 + Raspberry Pi running DSF):** `GET /machine/connect`, `POST /machine/code` (blocks until G-code execution finishes and returns reply text), and `GET /machine/model`.
* **[Recommendation]** **Daemon session, barrier, and abort architecture:**
  * **Session:** Maintain one long-lived host daemon holding the Duet session (keepalive via periodic model polls) and serialize CLI commands through a local lock. Auto-detect `/machine/model` vs `/rr_connect` at startup.
  * **True completion barrier:** Append `M400` (wait for motion queue to drain) to every mutating action, *then* read `/rr_reply` (standalone) and verify the object model (`state.status == "idle"`, expected `move.axes[].homed`, and `state.currentTool`) before returning CLI exit `0`. Never treat standalone `/rr_gcode` HTTP 200 as motion completion.
  * **Out-of-band abort:** Implement `cli abort` as a dedicated, lock-bypassing HTTP call sending `M112` (RRF parses emergency stop ahead of queued moves), latching the daemon into a `HALTED` state requiring explicit human inspection, `M999`, and re-homing. Always pair with a physical 24V power E-stop within arm's reach.

### 4. `science-jubilee` vs. Direct RRF
* **[Recommendation]** **Use both inside the daemon (layered hybrid):**
  * **Use `science-jubilee` as the domain & safety catalog** for atomic CLI verbs (`home_all`, `pickup_tool`, `park_tool`, `move_to` with `safe_z_movement`, deck/labware transforms, and `crash_detection`). Re-implementing Jubilee’s deck coordinate math and safe-Z clearance from scratch invites hardware collisions.
  * **Add a thin direct-RRF transport/supervisor** beneath or alongside it for capabilities `science-jubilee` leaves minimal: strict `M400` + object-model completion barriers, pre-flight `homed`/`status` assertions, SBC-vs-standalone endpoint normalization, and lock-bypassing `M112` aborts.

### 5. What Must Be Confirmed on Physical Hardware
Docs alone cannot tell us the exact state of a specific machine. Before running motion, confirm:
1. **Controller & Firmware:** Duet 2 Ethernet vs. Duet 3 (standalone vs. SBC), and exact RRF version (RRF 3.x is required for `/rr_model` and modern `science-jubilee`; RRF 2.x requires firmware upgrade or `/rr_status` fallback).
2. **Network Config:** Whether `sys/config.g` still uses default `192.168.1.2` or was modified for lab LAN/DHCP or an HTTP password (`M551`).
3. **Homing & Macro Order:** Exact contents of `homeall.g`, `homeu.g`, `tpre*.g`/`tpost*.g`/`tfree*.g`, and how much Z drops before XY moves.
4. **Deck & Tool Geometry:** Installed tool indices, park post coordinates, tallest labware height (`safe_z` clearance), and whether mechanical crash/tool-presence switches are wired for `crash_detection`.
5. **Hardware E-Stop:** Presence and location of a physical power-cut E-stop.

## Meta

Tools were enabled; this call produced no stderr meta (no search-query dump).
