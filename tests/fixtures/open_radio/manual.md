# OpenRadio-100 Open-Source Transceiver Manual (Test Fixture)

## Interface & Wiring
- Control interface: USB Serial at 115200 baud (`/dev/ttyUSB0`), ASCII line commands.
- Connect a 50-ohm dummy load or tuned antenna to the SMA RF port before powering on.

## Operating Limits
- Receive frequency range: 87.5 to 108.0 MHz.
- Audio volume: -40.0 to 0.0 dB.
- Transmit power: 0.0 to 5.0 W (requires human confirmation and verified antenna).

## Safety Warnings
- NEVER enable TX without a connected antenna or 50-ohm dummy load.
- Physical power switch MUST remain within reach; software abort mutes TX immediately but does not disconnect DC supply.
- Raw AT commands are expert-only and require confirmation.
