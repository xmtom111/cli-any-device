"""Single-use approvals and target-scoped session leases."""

from __future__ import annotations

import hashlib
import json
import math
import secrets
import threading
import time
from dataclasses import dataclass, replace
from typing import Any

from core_types import CliAnyDeviceError, ErrorCode, ExitCode, RiskClass

_RISK_ORDER = tuple(RiskClass)


def canonical_args_hash(args: dict[str, Any]) -> str:
    encoded = json.dumps(args, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class _Plan:
    target: str
    service: str
    args_hash: str
    state_seq: int | None
    expires_at: float
    approved: bool = False


@dataclass(frozen=True, slots=True)
class SessionLease:
    target: str
    max_risk: RiskClass
    expires_at: float
    allowed_services: frozenset[str] | None
    state_seq: int | None

    def covers(self, service: str, risk: RiskClass) -> bool:
        if _RISK_ORDER.index(risk) > _RISK_ORDER.index(self.max_risk):
            return False
        if self.allowed_services is None:
            return risk != RiskClass.R4_EXPERT
        return service in self.allowed_services

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "max_risk": self.max_risk.value,
            "expires_in_s": max(0.0, self.expires_at - time.monotonic()),
            "allowed_services": (
                sorted(self.allowed_services) if self.allowed_services is not None else None
            ),
            "state_seq": self.state_seq,
        }


class ApprovalStore:
    def __init__(self, plan_ttl_s: float = 120) -> None:
        self.plan_ttl_s = plan_ttl_s
        self._lock = threading.Lock()
        self._plans: dict[str, _Plan] = {}
        self._leases: dict[str, SessionLease] = {}

    def mint_plan(
        self, target: str, service: str, args_hash: str, state_seq: int | None
    ) -> dict[str, Any]:
        plan_id = secrets.token_urlsafe(24)
        with self._lock:
            now = time.monotonic()
            self._plans = {
                key: plan for key, plan in self._plans.items() if plan.expires_at > now
            }
            self._plans[plan_id] = _Plan(
                target, service, args_hash, state_seq, now + self.plan_ttl_s
            )
        return {"plan_id": plan_id, "plan_expires_in_s": self.plan_ttl_s}

    def _active_plan(self, plan_id: str) -> _Plan | None:
        plan = self._plans.get(plan_id)
        if plan is not None and plan.expires_at <= time.monotonic():
            del self._plans[plan_id]
            return None
        return plan

    def approve_plan(self, plan_id: str) -> bool:
        with self._lock:
            plan = self._active_plan(plan_id)
            if plan is None:
                return False
            self._plans[plan_id] = replace(plan, approved=True)
            return True

    def consume_plan(
        self, plan_id: str, target: str, service: str, args_hash: str, state_seq: int | None
    ) -> bool:
        with self._lock:
            plan = self._active_plan(plan_id)
            if plan is None or (plan.target, plan.service, plan.args_hash) != (
                target, service, args_hash
            ):
                return False
            if plan.state_seq != state_seq:
                del self._plans[plan_id]
                return False
            if not plan.approved:
                return False
            del self._plans[plan_id]
            return True

    def grant_lease(
        self,
        target: str,
        max_risk: RiskClass,
        ttl_s: float,
        allowed_services: frozenset[str] | None = None,
        state_seq: int | None = None,
    ) -> SessionLease:
        if not math.isfinite(ttl_s) or ttl_s <= 0:
            raise CliAnyDeviceError(
                ErrorCode.E_INVALID_ARGS,
                "Lease TTL must be finite and positive.",
                step="grant_lease",
                exit_code=ExitCode.INVALID_USAGE,
            )
        if max_risk == RiskClass.R4_EXPERT and not allowed_services:
            raise CliAnyDeviceError(
                ErrorCode.E_INVALID_ARGS,
                "An R4 lease requires explicit allowed services.",
                step="grant_lease",
                exit_code=ExitCode.INVALID_USAGE,
            )
        lease = SessionLease(
            target, max_risk, time.monotonic() + ttl_s, allowed_services, state_seq
        )
        with self._lock:
            self._leases[target] = lease
        return lease

    def revoke_lease(self, target: str) -> None:
        with self._lock:
            self._leases.pop(target, None)

    def _active_lease(self, target: str) -> SessionLease | None:
        lease = self._leases.get(target)
        if lease is not None and lease.expires_at <= time.monotonic():
            del self._leases[target]
            return None
        return lease

    def lease_status(self, target: str) -> SessionLease | None:
        with self._lock:
            return self._active_lease(target)

    def check_lease(self, target: str, state_seq: int | None) -> SessionLease | None:
        with self._lock:
            lease = self._active_lease(target)
            if lease is not None and lease.state_seq != state_seq:
                del self._leases[target]
                return None
            return lease

    def advance_lease(self, lease: SessionLease, state_seq: int | None) -> None:
        with self._lock:
            if self._active_lease(lease.target) is lease:
                self._leases[lease.target] = replace(lease, state_seq=state_seq)
