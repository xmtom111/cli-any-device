"""Universal safety predicates, confirmation rules, and argument validation."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, fields
from typing import Any

from core_types import (
    ArgSpec,
    CanonicalMachineState,
    DeviceSpec,
    ErrorCode,
    ExitCode,
    MachineLifecycle,
    RiskClass,
    ServiceSpec,
)


@dataclass(frozen=True, slots=True)
class GateRejection(Exception):
    """Structured rejection raised before any driver actuation occurs."""

    code: ErrorCode
    exit_code: ExitCode
    step: str
    reason: str


class PostconditionError(Exception):
    """Raised when settled machine state fails a service's declared postcondition."""


def _coerce_arg(spec: ArgSpec, raw: Any) -> Any:
    if spec.arg_type == "string":
        if isinstance(raw, (dict, list)):
            raise GateRejection(
                ErrorCode.E_INVALID_ARGS,
                ExitCode.INVALID_USAGE,
                "arg_validation",
                f"Argument '{spec.name}' expects string.",
            )
        return str(raw)
    if spec.arg_type == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str) and raw.lower() in ("true", "false", "1", "0"):
            return raw.lower() in ("true", "1")
        raise GateRejection(
            ErrorCode.E_INVALID_ARGS,
            ExitCode.INVALID_USAGE,
            "arg_validation",
            f"Argument '{spec.name}' expects boolean.",
        )
    if spec.arg_type in ("int", "float"):
        if isinstance(raw, bool):
            raise GateRejection(
                ErrorCode.E_INVALID_ARGS,
                ExitCode.INVALID_USAGE,
                "arg_validation",
                f"Argument '{spec.name}' expects {spec.arg_type}, not bool.",
            )
        try:
            return int(raw) if spec.arg_type == "int" else float(raw)
        except (TypeError, ValueError) as exc:
            raise GateRejection(
                ErrorCode.E_INVALID_ARGS,
                ExitCode.INVALID_USAGE,
                "arg_validation",
                f"Argument '{spec.name}' expects {spec.arg_type}.",
            ) from exc
    raise GateRejection(
        ErrorCode.E_INVALID_ARGS,
        ExitCode.INVALID_USAGE,
        "arg_validation",
        f"Unsupported arg_type '{spec.arg_type}' on '{spec.name}'.",
    )


def validate_and_coerce_args(
    service: ServiceSpec,
    raw_args: dict[str, Any],
    device: DeviceSpec,
) -> dict[str, Any]:
    """Validate required args, types, declared bounds, and device safety_limits."""
    spec_by_name = {a.name: a for a in service.args}
    unknown = set(raw_args) - set(spec_by_name)
    if unknown:
        raise GateRejection(
            ErrorCode.E_INVALID_ARGS,
            ExitCode.INVALID_USAGE,
            "arg_validation",
            f"Unknown argument(s) for '{service.name}': {sorted(unknown)}.",
        )

    coerced: dict[str, Any] = {}
    for arg_spec in service.args:
        if arg_spec.name not in raw_args or raw_args[arg_spec.name] is None:
            if arg_spec.required:
                raise GateRejection(
                    ErrorCode.E_INVALID_ARGS,
                    ExitCode.INVALID_USAGE,
                    "arg_validation",
                    f"Missing required argument '{arg_spec.name}' for '{service.name}'.",
                )
            if arg_spec.default is not None:
                coerced[arg_spec.name] = arg_spec.default
            continue

        val = _coerce_arg(arg_spec, raw_args[arg_spec.name])
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            if arg_spec.min_value is not None and val < arg_spec.min_value:
                raise GateRejection(
                    ErrorCode.E_OUT_OF_BOUNDS,
                    ExitCode.SAFETY_DENIED,
                    "bounds_check",
                    f"Argument '{arg_spec.name}'={val} below minimum {arg_spec.min_value}.",
                )
            if arg_spec.max_value is not None and val > arg_spec.max_value:
                raise GateRejection(
                    ErrorCode.E_OUT_OF_BOUNDS,
                    ExitCode.SAFETY_DENIED,
                    "bounds_check",
                    f"Argument '{arg_spec.name}'={val} exceeds maximum {arg_spec.max_value}.",
                )
            # Also enforce device-level generic safety_limits matching this parameter name
            if arg_spec.name in device.safety_limits:
                lim = device.safety_limits[arg_spec.name]
                if val < lim.min_value or val > lim.max_value:
                    raise GateRejection(
                        ErrorCode.E_OUT_OF_BOUNDS,
                        ExitCode.SAFETY_DENIED,
                        "safety_limit_check",
                        f"Argument '{arg_spec.name}'={val} outside device safety limit "
                        f"[{lim.min_value}, {lim.max_value}] {lim.unit}.",
                    )
        coerced[arg_spec.name] = val
    return coerced


_PROPERTY_PREDICATES = frozenset({"prop_eq", "prop_range", "prop_set", "flag"})
_CONFIRM_RULE = re.compile(r"(state|prop)\.([\w.-]+)\s*(==|!=)\s*(.+)")
_BARE_LITERAL = re.compile(r"[^\s!=\"']+")
_STATE_FIELDS = frozenset(field.name for field in fields(CanonicalMachineState))


def _in_property_range(value: Any, bounds: str) -> bool:
    lo, separator, hi = bounds.partition("..")
    if not separator or isinstance(value, bool):
        return False
    try:
        low, high = float(lo), float(hi)
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return False
    return math.isfinite(low) and math.isfinite(high) and low <= number <= high


def _property_failure(predicate: str, state: CanonicalMachineState) -> str | None:
    kind, _, expression = predicate.partition(":")
    if kind == "flag":
        if expression not in state.verified_flags:
            return f"Required verified flag '{expression}' is not present."
        return None
    if kind == "prop_set":
        if state.properties.get(expression) is None:
            return f"Required state property '{expression}' is not set."
        return None

    key, separator, expected = expression.partition("=")
    if not separator or not key:
        return f"Invalid property predicate '{predicate}'."
    actual = state.properties.get(key)
    if kind == "prop_eq":
        if str(actual).lower() != expected.lower():
            return f"Property '{key}' is {actual!r}, expected {expected!r}."
    elif not _in_property_range(actual, expected):
        return f"Property '{key}' is {actual!r}, expected a number in {expected!r}."
    return None


def evaluate_precondition(
    predicate: str, service: ServiceSpec, state: CanonicalMachineState
) -> bool:
    """Check a universal precondition and report whether it was recognized."""
    code = ErrorCode.E_PRECONDITION_FAILED
    exit_code = ExitCode.SAFETY_DENIED
    reason = None
    if predicate == "detected":
        code = ErrorCode.E_NOT_DETECTED
        if not state.detected:
            reason = "Machine has not passed sys.detect yet; run sys.detect before actuation."
        elif (
            service.risk_class != RiskClass.R0_READ
            and state.identity is not None
            and not state.identity.hookup_verified
        ):
            reason = "Hardware hookup is not verified; complete verification before actuation."
    elif predicate == "not_estop_latched":
        if state.estop_latched or state.lifecycle == MachineLifecycle.HALTED:
            code = ErrorCode.E_ESTOP_LATCHED
            exit_code = ExitCode.ESTOP_LATCHED
            reason = "Emergency stop is latched; inspect machine and run confirmed sys.reset_estop."
    elif predicate == "estop_latched":
        if not state.estop_latched:
            reason = "Expected estop_latched=True."
    elif predicate == "idle":
        if state.lifecycle != MachineLifecycle.IDLE:
            reason = f"Expected lifecycle IDLE, got {state.lifecycle.value}."
    elif predicate.partition(":")[0] in _PROPERTY_PREDICATES and ":" in predicate:
        reason = _property_failure(predicate, state)
    else:
        return False
    if reason is not None:
        raise GateRejection(code, exit_code, f"precondition:{predicate}", reason)
    return True


def evaluate_postcondition(
    predicate: str, service: ServiceSpec, state: CanonicalMachineState
) -> bool:
    """Check a universal postcondition and report whether it was recognized."""
    reason = None
    if predicate == "detected":
        if not state.detected:
            reason = "Machine state is not marked detected after sys.detect."
    elif predicate == "idle":
        if state.lifecycle != MachineLifecycle.IDLE:
            reason = f"Expected lifecycle IDLE after '{service.name}', got {state.lifecycle.value}."
    elif predicate == "estop_latched":
        if not state.estop_latched:
            reason = "Expected estop_latched=True after sys.abort."
    elif predicate == "not_estop_latched":
        if state.estop_latched:
            reason = "Expected estop_latched=False after sys.reset_estop."
    elif predicate.partition(":")[0] in _PROPERTY_PREDICATES and ":" in predicate:
        reason = _property_failure(predicate, state)
    else:
        return False
    if reason is not None:
        raise PostconditionError(reason)
    return True


def evaluate_confirm_if(expr: str, state: CanonicalMachineState) -> bool:
    """Compare a canonical field or property to null or a scalar literal."""
    match = _CONFIRM_RULE.fullmatch(expr.strip())
    if match is None:
        raise ValueError(f"Invalid confirmation rule {expr!r}.")
    namespace, key, operator, literal = match.groups()
    if namespace == "state":
        if key not in _STATE_FIELDS:
            raise ValueError(f"Unknown state field {key!r} in confirmation rule {expr!r}.")
        actual = getattr(state, key)
    else:
        actual = state.properties.get(key)

    if operator == "!=":
        if literal != "null":
            raise ValueError(f"Confirmation rule {expr!r} must compare != against null.")
        return actual is not None

    try:
        expected = json.loads(literal)
    except ValueError as exc:
        if not _BARE_LITERAL.fullmatch(literal):
            raise ValueError(f"Invalid literal in confirmation rule {expr!r}.") from exc
        expected = literal
    if isinstance(expected, (dict, list)):
        raise ValueError(f"Confirmation rule {expr!r} requires a scalar literal.")
    if isinstance(actual, bool) != isinstance(expected, bool):
        return False
    return bool(actual == expected)
