"""Mock Python host API for OpenPSU-305 SCPI benchtop power supply & probe (AST fixture)."""

from __future__ import annotations


class OpenPsu305Host:
    """Host controller for a programmable SCPI benchtop PSU with temperature probes."""

    def read_probe(self, *, channel: int) -> float:
        """Read temperature probe in Celsius for a specific channel."""
        raise NotImplementedError

    def get_measurements(self, *, channel: int = 1) -> dict[str, float]:
        """Read live output voltage and current for a channel."""
        raise NotImplementedError

    def set_voltage(self, *, channel: int, voltage_v: float) -> float:
        """Program target DC output voltage in Volts."""
        raise NotImplementedError

    def set_current_limit(self, *, channel: int, current_a: float) -> float:
        """Program over-current protection limit in Amps."""
        raise NotImplementedError

    def enable_output(self, *, channel: int, voltage_v: float, current_a: float = 1.0) -> bool:
        """Enable DC power output on a channel (requires operator confirmation)."""
        raise NotImplementedError

    def send_raw_scpi(self, *, command: str) -> str:
        """Send raw SCPI command directly to instrument (expert escape hatch)."""
        raise NotImplementedError
