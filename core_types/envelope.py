"""JSON response envelope returned by daemon and CLI."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from core_types.errors import ErrorDetail, ExitCode


class ExecutionMode(StrEnum):
    """Whether a command executed live or in dry-run planning mode."""

    LIVE = "live"
    DRY_RUN = "dry_run"


@dataclass(frozen=True, slots=True)
class CommandEnvelope:
    """Standardized JSON response envelope defined in Section 6.4."""

    ok: bool
    service: str
    target: str
    mode: ExecutionMode
    duration_ms: int
    exit_code: ExitCode
    state: dict[str, Any]
    data: dict[str, Any] = field(default_factory=dict)
    error: ErrorDetail | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": self.ok,
            "service": self.service,
            "target": self.target,
            "mode": self.mode.value,
            "duration_ms": self.duration_ms,
            "exit_code": int(self.exit_code),
            "state": self.state,
            "data": self.data,
            "error": self.error.to_dict() if self.error is not None else None,
        }
        return payload
