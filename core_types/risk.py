"""Risk classification for atomic device services (R0-R4)."""

from __future__ import annotations

from enum import StrEnum


class ConfirmMode(StrEnum):
    TTY = "tty"
    TOKEN = "token"
    FLAG = "flag"


class RiskClass(StrEnum):
    """Risk classes defined in Technical Roadmap Section 6.1."""

    R0_READ = "R0_READ"
    R1_CONFIG = "R1_CONFIG"
    R2_MOTION = "R2_MOTION"
    R3_PHYSICAL_INIT = "R3_PHYSICAL_INIT"
    R4_EXPERT = "R4_EXPERT"
