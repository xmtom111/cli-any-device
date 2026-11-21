"""Mock Python host API for OpenRadio-100 (read-only AST input fixture, never imported by core)."""

from __future__ import annotations


class OpenRadioHost:
    """Host controller for OpenRadio-100 open-source SDR transceiver."""

    def get_signal(self) -> dict[str, float]:
        """Read current RSSI and SNR telemetry."""
        raise NotImplementedError

    def set_volume(self, volume_db: float = -12.0) -> float:
        """Set audio output volume in dB."""
        raise NotImplementedError

    def tune(self, frequency_mhz: float) -> float:
        """Tune receiver front-end to target frequency in MHz."""
        raise NotImplementedError

    def enable_tx(self, power_w: float) -> bool:
        """Enable RF transmit amplifier at requested power in Watts (requires verified antenna)."""
        raise NotImplementedError

    def calibrate_frontend(self) -> bool:
        """Run physical IQ DC-offset calibration routine."""
        raise NotImplementedError

    def send_raw_at(self, command: str) -> str:
        """Send raw AT command directly to radio MCU (expert escape hatch)."""
        raise NotImplementedError
