"""Device-agnostic SafetyKernel enforcing R0-R4 gating, mutex, completion, and lock-free abort.

Import rule: safety_kernel may ONLY import core_types, adapter_spi, and stdlib.
Never branches on target names or device IDs; everything derives from DeviceBundle
and SupervisorChannel / ActionDriver capabilities.
"""

from __future__ import annotations

import threading
import time
from copy import deepcopy
from typing import Any

from adapter_spi import ActionDriver, ActionRequest, SupervisorChannel
from core_types import (
    AdapterTimeoutError,
    AdapterTransportError,
    CanonicalMachineState,
    CliAnyDeviceError,
    CommandEnvelope,
    DeviceBundle,
    ErrorCode,
    ErrorDetail,
    ExecutionMode,
    ExitCode,
    HardwareFaultError,
    RiskClass,
    ServiceSpec,
    TargetProfile,
    UnsupportedCapabilityError,
)
from safety_kernel.domains import DomainExtension, PreconditionContext, register_domains
from safety_kernel.domains.labware_deck import LABWARE_DECK
from safety_kernel.domains.motion_3d import MOTION_3D
from safety_kernel.predicates import (
    GateRejection,
    PostconditionError,
    evaluate_confirm_if,
    evaluate_postcondition,
    evaluate_precondition,
    validate_and_coerce_args,
)

DEFAULT_DOMAINS = register_domains(MOTION_3D, LABWARE_DECK)

ESTOP_ALLOWED_SERVICES = frozenset(
    {
        "sys.status",
        "sys.detect",
        "sys.preflight",
        "sys.abort",
        "sys.reset_estop",
    }
)


class SafetyKernel:
    """Shared runtime safety gate for any DeviceBundle and SupervisorChannel/ActionDriver pair."""

    def __init__(
        self,
        *,
        bundle: DeviceBundle,
        target_name: str,
        supervisor: SupervisorChannel,
        driver: ActionDriver,
        allow_live_hardware: bool = False,
        domains: DomainExtension = DEFAULT_DOMAINS,
    ) -> None:
        if target_name not in bundle.device.targets:
            raise ValueError(
                f"Target '{target_name}' is not declared in device bundle "
                f"'{bundle.device.device_id}'."
            )
        self._bundle = bundle
        self._target_name = target_name
        self._profile: TargetProfile = bundle.device.targets[target_name]
        self._supervisor = supervisor
        self._driver = driver
        self._allow_live_hardware = allow_live_hardware
        self._domains = domains
        self._action_lock = threading.Lock()
        self._last_state = CanonicalMachineState()

    @property
    def target_name(self) -> str:
        return self._target_name

    def invoke(
        self,
        service_name: str,
        *,
        args: dict[str, Any] | None = None,
        dry_run: bool = False,
        confirm: bool = False,
    ) -> CommandEnvelope:
        """Execute a catalog service through all safety gates and return a CommandEnvelope."""
        t0 = time.perf_counter()
        raw_args = dict(args or {})
        mode = ExecutionMode.DRY_RUN if dry_run else ExecutionMode.LIVE

        service = self._bundle.catalog.services.get(service_name)
        if service is None:
            state = self._safe_poll()
            return self._error_envelope(
                service_name=service_name,
                mode=mode,
                t0=t0,
                exit_code=ExitCode.INVALID_USAGE,
                state=state,
                error=ErrorDetail(
                    code=ErrorCode.E_UNKNOWN_SERVICE,
                    step="catalog_lookup",
                    reason=f"Unknown service '{service_name}' in bundle "
                    f"'{self._bundle.device.device_id}'.",
                ),
            )

        if service.bypasses_action_lock:
            if dry_run:
                state = self._safe_poll()
                abort_plan: dict[str, Any] = {
                    "service": service.name,
                    "risk_class": service.risk_class.value,
                    "args": raw_args,
                    "bypasses_action_lock": True,
                    "effects": list(service.effects),
                    "postconditions": list(service.postconditions),
                    "requires_confirm_when_live": False,
                    "timeout_s": service.timeout_s,
                }
                return CommandEnvelope(
                    ok=True,
                    service=service.name,
                    target=self._target_name,
                    mode=ExecutionMode.DRY_RUN,
                    duration_ms=self._elapsed_ms(t0),
                    exit_code=ExitCode.OK,
                    state=state.to_summary_dict(),
                    data={"plan": abort_plan},
                    error=None,
                )
            return self._handle_lock_free_abort(service, mode, t0)

        try:
            coerced_args = validate_and_coerce_args(service, raw_args, self._bundle.device)
        except GateRejection as rej:
            return self._error_envelope(
                service_name=service.name,
                mode=mode,
                t0=t0,
                exit_code=rej.exit_code,
                state=self._last_state,
                error=ErrorDetail(code=rej.code, step=rej.step, reason=rej.reason),
            )

        effective_caps = self._profile.capabilities & self._driver.capabilities()
        missing_caps = service.requires_capabilities - effective_caps
        if missing_caps:
            return self._error_envelope(
                service_name=service.name,
                mode=mode,
                t0=t0,
                exit_code=ExitCode.INVALID_USAGE,
                state=self._safe_poll(),
                error=ErrorDetail(
                    code=ErrorCode.E_UNSUPPORTED_CAPABILITY,
                    step="capability_check",
                    reason=(
                        f"Target '{self._target_name}' does not support required capability "
                        f"{sorted(missing_caps)} for service '{service.name}'."
                    ),
                ),
            )

        if dry_run:
            return self._invoke_checked(service, coerced_args, mode, t0, confirm=confirm)
        with self._action_lock:
            return self._invoke_checked(service, coerced_args, mode, t0, confirm=confirm)

    def _invoke_checked(
        self,
        service: ServiceSpec,
        coerced_args: dict[str, Any],
        mode: ExecutionMode,
        t0: float,
        *,
        confirm: bool,
    ) -> CommandEnvelope:
        state = self._safe_poll()
        if state.estop_latched and service.name not in ESTOP_ALLOWED_SERVICES:
            return self._error_envelope(
                service_name=service.name,
                mode=mode,
                t0=t0,
                exit_code=ExitCode.ESTOP_LATCHED,
                state=state,
                error=ErrorDetail(
                    code=ErrorCode.E_ESTOP_LATCHED,
                    step="estop_latch",
                    reason=(
                        "Emergency stop is latched; inspect hardware and run "
                        "confirmed sys.reset_estop before actuation."
                    ),
                ),
            )

        if (
            mode == ExecutionMode.LIVE
            and service.risk_class != RiskClass.R0_READ
            and self._profile.is_real_hardware
            and (
                self._bundle.device.review_status != "verified"
                or self._allow_live_hardware is not True
            )
        ):
            return self._error_envelope(
                service_name=service.name,
                mode=mode,
                t0=t0,
                exit_code=ExitCode.SAFETY_DENIED,
                state=state,
                error=ErrorDetail(
                    code=ErrorCode.E_LIVE_LOCKED,
                    step="live_hardware_gate",
                    reason=(
                        "Live hardware changes require review_status: verified "
                        "and daemon startup with --allow-live-hardware."
                    ),
                ),
            )

        resolver = self._domains.coord_resolver.get(service.name)
        try:
            target_coords = resolver(coerced_args, state) if resolver is not None else None
            context = PreconditionContext(coerced_args, state, self._bundle.device, target_coords)
            self._evaluate_preconditions(service, context)
            needs_confirm = self._needs_confirmation(service, state)
        except GateRejection as rej:
            return self._error_envelope(
                service_name=service.name,
                mode=mode,
                t0=t0,
                exit_code=rej.exit_code,
                state=state,
                error=ErrorDetail(code=rej.code, step=rej.step, reason=rej.reason),
            )

        if mode == ExecutionMode.DRY_RUN:
            plan = {
                "service": service.name,
                "risk_class": service.risk_class.value,
                "args": coerced_args,
                "target_coords": target_coords,
                "effects": list(service.effects),
                "postconditions": list(service.postconditions),
                "requires_confirm_when_live": needs_confirm,
                "timeout_s": service.timeout_s,
            }
            return CommandEnvelope(
                ok=True,
                service=service.name,
                target=self._target_name,
                mode=ExecutionMode.DRY_RUN,
                duration_ms=self._elapsed_ms(t0),
                exit_code=ExitCode.OK,
                state=state.to_summary_dict(),
                data={"plan": plan},
                error=None,
            )

        if needs_confirm and not confirm:
            return self._error_envelope(
                service_name=service.name,
                mode=mode,
                t0=t0,
                exit_code=ExitCode.CONFIRM_REQUIRED,
                state=state,
                error=ErrorDetail(
                    code=ErrorCode.E_CONFIRM_REQUIRED,
                    step="confirm_gate",
                    reason=(
                        f"Service '{service.name}' ({service.risk_class.value}) requires "
                        "explicit --confirm."
                    ),
                ),
            )

        return self._execute_locked(service, coerced_args, target_coords, t0)

    def _needs_confirmation(self, service: ServiceSpec, state: CanonicalMachineState) -> bool:
        if service.requires_confirm:
            return True
        if service.risk_class in self._profile.require_confirm_for_risks:
            return True
        try:
            return any(evaluate_confirm_if(rule, state) for rule in service.confirm_if)
        except ValueError as exc:
            raise GateRejection(
                ErrorCode.E_INVALID_ARGS, ExitCode.INVALID_USAGE, "confirm_gate", str(exc)
            ) from exc

    def _evaluate_preconditions(self, service: ServiceSpec, context: PreconditionContext) -> None:
        for predicate in service.preconditions:
            if evaluate_precondition(predicate, service, context.state):
                continue
            check = self._domains.preconditions.get(predicate)
            if check is None:
                raise GateRejection(
                    ErrorCode.E_PRECONDITION_FAILED,
                    ExitCode.SAFETY_DENIED,
                    f"precondition:{predicate}",
                    f"Unknown precondition predicate '{predicate}' (default deny).",
                )
            check(context)

    def _evaluate_postconditions(
        self,
        service: ServiceSpec,
        args: dict[str, Any],
        state: CanonicalMachineState,
    ) -> None:
        for predicate in service.postconditions:
            if evaluate_postcondition(predicate, service, state):
                continue
            check = self._domains.postconditions.get(predicate)
            if check is None:
                raise PostconditionError(
                    f"Unknown postcondition predicate '{predicate}' (default deny)."
                )
            check(args, state)

    def _handle_lock_free_abort(
        self,
        service: ServiceSpec,
        mode: ExecutionMode,
        t0: float,
    ) -> CommandEnvelope:
        abort_res = self._supervisor.abort_now()
        state = self._safe_poll()
        return CommandEnvelope(
            ok=abort_res.sent,
            service=service.name,
            target=self._target_name,
            mode=mode,
            duration_ms=self._elapsed_ms(t0),
            exit_code=ExitCode.OK if abort_res.sent else ExitCode.EXECUTION_ERROR,
            state=state.to_summary_dict(),
            data={"abort": abort_res.to_dict()},
            error=None
            if abort_res.sent
            else ErrorDetail(
                code=ErrorCode.E_EXECUTION_FAILED,
                step="sys.abort",
                reason="Failed to transmit emergency stop.",
            ),
        )

    def _execute_locked(
        self,
        service: ServiceSpec,
        coerced_args: dict[str, Any],
        target_coords: dict[str, float] | None,
        t0: float,
    ) -> CommandEnvelope:
        try:
            if service.name == "sys.detect":
                identity = self._supervisor.detect()
                settled = self._supervisor.poll_state()
                self._last_state = settled
                self._evaluate_postconditions(service, coerced_args, settled)
                return CommandEnvelope(
                    ok=True,
                    service=service.name,
                    target=self._target_name,
                    mode=ExecutionMode.LIVE,
                    duration_ms=self._elapsed_ms(t0),
                    exit_code=ExitCode.OK,
                    state=settled.to_summary_dict(),
                    data={"identity": identity.to_dict()},
                    error=None,
                )

            if service.name == "sys.status":
                settled = self._supervisor.poll_state()
                self._last_state = settled
                return CommandEnvelope(
                    ok=True,
                    service=service.name,
                    target=self._target_name,
                    mode=ExecutionMode.LIVE,
                    duration_ms=self._elapsed_ms(t0),
                    exit_code=ExitCode.OK,
                    state=settled.to_summary_dict(),
                    data={},
                    error=None,
                )

            if service.name == "sys.preflight":
                report = self._supervisor.preflight()
                settled = self._supervisor.poll_state()
                self._last_state = settled
                return CommandEnvelope(
                    ok=report.passed,
                    service=service.name,
                    target=self._target_name,
                    mode=ExecutionMode.LIVE,
                    duration_ms=self._elapsed_ms(t0),
                    exit_code=ExitCode.OK if report.passed else ExitCode.SAFETY_DENIED,
                    state=settled.to_summary_dict(),
                    data={"readiness_report": report.to_dict()},
                    error=None
                    if report.passed
                    else ErrorDetail(
                        code=ErrorCode.E_PRECONDITION_FAILED,
                        step="sys.preflight",
                        reason="; ".join(report.blocking_reasons) or "Preflight check failed.",
                    ),
                )

            if service.name == "sys.reset_estop":
                settled = self._supervisor.reset_estop()
                self._last_state = settled
                self._evaluate_postconditions(service, coerced_args, settled)
                return CommandEnvelope(
                    ok=True,
                    service=service.name,
                    target=self._target_name,
                    mode=ExecutionMode.LIVE,
                    duration_ms=self._elapsed_ms(t0),
                    exit_code=ExitCode.OK,
                    state=settled.to_summary_dict(),
                    data={},
                    error=None,
                )

            # Domain action: submit via ActionDriver, then ALWAYS await_settled via Supervisor!
            current_state = self._supervisor.poll_state()
            telemetry = self._driver.execute(
                ActionRequest(
                    service=service.name,
                    args=coerced_args,
                    timeout_s=service.timeout_s,
                    safe_z_mm=current_state.safe_z_mm,
                    target_coords=target_coords,
                    protocol=deepcopy(service.protocol),
                )
            )
            if not telemetry.accepted:
                fail_safe_applied = self._apply_fail_safe(
                    service, "ActionDriver rejected command submission."
                )
                return self._error_envelope(
                    service_name=service.name,
                    mode=ExecutionMode.LIVE,
                    t0=t0,
                    exit_code=ExitCode.EXECUTION_ERROR,
                    state=self._safe_poll(),
                    error=ErrorDetail(
                        code=ErrorCode.E_EXECUTION_FAILED,
                        step="driver.execute",
                        reason="ActionDriver reported command was not accepted.",
                        fail_safe_applied=fail_safe_applied,
                    ),
                )

            settled = self._supervisor.await_settled(service.timeout_s)
            self._last_state = settled
            self._evaluate_postconditions(service, coerced_args, settled)

            return CommandEnvelope(
                ok=True,
                service=service.name,
                target=self._target_name,
                mode=ExecutionMode.LIVE,
                duration_ms=self._elapsed_ms(t0),
                exit_code=ExitCode.OK,
                state=settled.to_summary_dict(),
                data={"telemetry": telemetry.raw},
                error=None,
            )

        except UnsupportedCapabilityError as exc:
            return self._error_envelope(
                service_name=service.name,
                mode=ExecutionMode.LIVE,
                t0=t0,
                exit_code=ExitCode.INVALID_USAGE,
                state=self._safe_poll(),
                error=ErrorDetail(
                    code=ErrorCode.E_UNSUPPORTED_CAPABILITY,
                    step=exc.step,
                    reason=exc.reason,
                ),
            )
        except AdapterTransportError as exc:
            return self._error_envelope(
                service_name=service.name,
                mode=ExecutionMode.LIVE,
                t0=t0,
                exit_code=ExitCode.TIMEOUT,
                state=self._safe_poll(),
                error=ErrorDetail(
                    code=ErrorCode.E_TRANSPORT_ERROR,
                    step=exc.step,
                    reason=exc.reason,
                    hookup_ref=exc.hookup_ref,
                ),
            )
        except AdapterTimeoutError as exc:
            fs = self._apply_fail_safe(service, exc.reason)
            return self._error_envelope(
                service_name=service.name,
                mode=ExecutionMode.LIVE,
                t0=t0,
                exit_code=ExitCode.TIMEOUT,
                state=self._safe_poll(),
                error=ErrorDetail(
                    code=ErrorCode.E_TIMEOUT,
                    step=exc.step,
                    reason=exc.reason,
                    fail_safe_applied=fs,
                ),
            )
        except PostconditionError as exc:
            fs = self._apply_fail_safe(service, str(exc))
            return self._error_envelope(
                service_name=service.name,
                mode=ExecutionMode.LIVE,
                t0=t0,
                exit_code=ExitCode.EXECUTION_ERROR,
                state=self._safe_poll(),
                error=ErrorDetail(
                    code=ErrorCode.E_POSTCONDITION_FAILED,
                    step="postcondition_check",
                    reason=str(exc),
                    fail_safe_applied=fs,
                ),
            )
        except HardwareFaultError as exc:
            fs = self._apply_fail_safe(service, exc.reason)
            return self._error_envelope(
                service_name=service.name,
                mode=ExecutionMode.LIVE,
                t0=t0,
                exit_code=ExitCode.EXECUTION_ERROR,
                state=self._safe_poll(),
                error=ErrorDetail(
                    code=ErrorCode.E_HARDWARE_FAULT,
                    step=exc.step,
                    reason=exc.reason,
                    fail_safe_applied=fs,
                ),
            )
        except CliAnyDeviceError as exc:
            return self._error_envelope(
                service_name=service.name,
                mode=ExecutionMode.LIVE,
                t0=t0,
                exit_code=exc.exit_code,
                state=self._last_state,
                error=ErrorDetail(code=exc.code, step=exc.step, reason=exc.reason),
            )
        except Exception as exc:
            fs = self._apply_fail_safe(service, str(exc))
            return self._error_envelope(
                service_name=service.name,
                mode=ExecutionMode.LIVE,
                t0=t0,
                exit_code=ExitCode.EXECUTION_ERROR,
                state=self._safe_poll(),
                error=ErrorDetail(
                    code=ErrorCode.E_EXECUTION_FAILED,
                    step="execute",
                    reason=str(exc),
                    fail_safe_applied=fs,
                ),
            )

    def _apply_fail_safe(self, service: ServiceSpec, reason: str) -> str | None:
        if not service.fail_safe or service.fail_safe == "none":
            return None
        try:
            return self._driver.apply_fail_safe(service.fail_safe, reason)
        except CliAnyDeviceError as exc:
            return f"{service.fail_safe} failed: {exc.reason}"

    def _safe_poll(self) -> CanonicalMachineState:
        try:
            self._last_state = self._supervisor.poll_state()
        except Exception:
            return CanonicalMachineState()
        return self._last_state

    @staticmethod
    def _elapsed_ms(t0: float) -> int:
        return round((time.perf_counter() - t0) * 1000.0)

    def _error_envelope(
        self,
        *,
        service_name: str,
        mode: ExecutionMode,
        t0: float,
        exit_code: ExitCode,
        state: CanonicalMachineState,
        error: ErrorDetail,
    ) -> CommandEnvelope:
        return CommandEnvelope(
            ok=False,
            service=service_name,
            target=self._target_name,
            mode=mode,
            duration_ms=self._elapsed_ms(t0),
            exit_code=exit_code,
            state=state.to_summary_dict(),
            data={},
            error=error,
        )
