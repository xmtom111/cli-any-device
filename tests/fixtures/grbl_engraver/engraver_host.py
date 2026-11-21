"""Mock Python host API for OpenCNC-3018 3-axis GRBL engraver (read-only AST fixture)."""

from __future__ import annotations


class OpenCnc3018Host:
    """Host controller for a desktop 3-axis GRBL engraver (no tool changer)."""

    def get_position(self) -> dict[str, float]:
        """Query current XYZ machine position in mm."""
        raise NotImplementedError

    def home_axes(self) -> bool:
        """Home X, Y, and Z limit switches (requires clear work area)."""
        raise NotImplementedError

    def move_xyz(
        self,
        *,
        x_mm: float,
        y_mm: float,
        z_mm: float,
        feed_rate: float = 600.0,
    ) -> dict[str, float]:
        """Move toolhead linearly to target XYZ coordinates in mm."""
        raise NotImplementedError

    def set_spindle(self, *, spindle_rpm: int) -> int:
        """Set spindle rotation speed in RPM (0 stops spindle)."""
        raise NotImplementedError

    def send_raw_gcode(self, *, command: str) -> str:
        """Send raw GRBL G-code line directly (expert escape hatch)."""
        raise NotImplementedError
