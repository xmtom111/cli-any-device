# OpenPSU-305 Benchtop SCPI Power Supply & Probe Manual (Test Fixture)

## Interface & Wiring
- Control interface: Ethernet TCP/SCPI at `tcp://192.168.10.50:5025` (newline-terminated SCPI commands).
- Connect load leads and temperature probe sensors before enabling output channels.

## Operating Limits
- Output voltage: 0.0 to 30.0 V.
- Current limit: 0.0 to 5.0 A.
- Probe channel: 1 to 4.

## Safety Warnings
- NEVER enable high-current output without verifying load rating and current limit.
- Physical mains power switch MUST remain accessible; software abort disables outputs but does not disconnect AC mains.
- Raw SCPI commands are expert-only and require confirmation.
