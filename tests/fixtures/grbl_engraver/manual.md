# OpenCNC-3018 Desktop 3-Axis GRBL Engraver Manual (Test Fixture)

## Interface & Wiring
- Control interface: USB Serial at 115200 baud (`/dev/ttyUSB0`), GRBL 1.1 line protocol.
- No automatic tool changer (manual collet only; no U-axis or tool macros).

## Operating Limits
- X travel: 0.0 to 300.0 mm.
- Y travel: 0.0 to 180.0 mm.
- Z travel: 0.0 to 45.0 mm.
- Spindle speed: 0 to 10000 rpm.
- Feed rate: 50.0 to 2000.0 mm/min.

## Safety Warnings
- NEVER move axes before homing limit switches are verified.
- NEVER start the spindle without enclosure closed and eye protection worn.
- Physical AC mains kill switch MUST be within reach; software abort halts motion but does not cut mains power.
- Raw G-code commands are expert-only and require confirmation.
