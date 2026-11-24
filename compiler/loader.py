"""Declarative device bundle loader and strict schema/safety validator."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from core_types import (
    ArgSpec,
    AxisLimit,
    CatalogSpec,
    DeviceBundle,
    DeviceSpec,
    ForbiddenZone,
    HookupSection,
    HookupSpec,
    NumericLimit,
    RiskClass,
    ServiceSpec,
    TargetProfile,
    ToolSlotSpec,
)

VALID_ARG_TYPES = frozenset({"string", "float", "int", "bool"})
SERVICE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
FORBIDDEN_SECRET_PATTERNS = (
    re.compile(r"password\s*=\s*reprap", re.IGNORECASE),
    re.compile(r"password\s*:\s*[\"']?reprap[\"']?", re.IGNORECASE),
    re.compile(r"http_password\s*:\s*[\"']?[^\s\"']+[\"']?", re.IGNORECASE),
)


class BundleValidationError(ValueError):
    """Raised when device.yaml or catalog.yaml violates schema or safety invariants."""


def resolve_bundle_dir(name_or_path: str, repo_root: Path | None = None) -> Path:
    """Resolve a bundle by directory path or by name under <repo_root>/bundles/<name>."""
    candidate = Path(name_or_path)
    if candidate.is_dir() and (candidate / "device.yaml").exists():
        return candidate.resolve()
    root = repo_root or Path(__file__).resolve().parent.parent
    by_name = root / "bundles" / name_or_path
    if by_name.is_dir() and (by_name / "device.yaml").exists():
        return by_name.resolve()
    raise BundleValidationError(
        f"Bundle {name_or_path!r} not found (checked {candidate} and {by_name})."
    )


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise BundleValidationError(f"Missing required bundle file: {path}")
    raw_text = path.read_text(encoding="utf-8")
    for pattern in FORBIDDEN_SECRET_PATTERNS:
        if pattern.search(raw_text):
            raise BundleValidationError(
                f"Hardcoded secret/default password detected in {path.name}; "
                "supply credentials via environment variables or local config only."
            )
    parsed = yaml.safe_load(raw_text)
    if not isinstance(parsed, dict):
        raise BundleValidationError(f"{path} must contain a top-level YAML mapping.")
    return parsed


def _parse_risk(raw: Any, context: str) -> RiskClass:
    try:
        return RiskClass(str(raw))
    except ValueError as exc:
        raise BundleValidationError(f"Invalid risk_class {raw!r} in {context}.") from exc


def _parse_options(raw: Any, context: str) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
        raise BundleValidationError(f"{context} must be a mapping with string keys.")
    return dict(raw)


def _parse_device_spec(raw: dict[str, Any]) -> DeviceSpec:
    try:
        device_id = str(raw["device_id"]).strip()
        display_name = str(raw["display_name"]).strip()
        version = str(raw["version"]).strip()
    except KeyError as exc:
        raise BundleValidationError(f"device.yaml missing required field: {exc}") from exc

    device_kind = str(raw.get("device_kind", "generic_machine")).strip()
    review_status = str(raw.get("review_status", "draft")).strip()
    hardware_ref = {str(k): str(v) for k, v in dict(raw.get("hardware_ref") or {}).items()}
    source_inputs = dict(raw.get("source_inputs") or {})

    axes: dict[str, AxisLimit] = {}
    for axis_name, axis_raw in dict(raw.get("axes") or {}).items():
        axes[str(axis_name)] = AxisLimit(
            name=str(axis_name),
            min_mm=float(axis_raw["min_mm"]),
            max_mm=float(axis_raw["max_mm"]),
            verified=bool(axis_raw.get("verified", False)),
            source=str(axis_raw.get("source", "unspecified")),
        )

    safety_limits: dict[str, NumericLimit] = {}
    for lim_name, lim_raw in dict(raw.get("safety_limits") or {}).items():
        safety_limits[str(lim_name)] = NumericLimit(
            name=str(lim_name),
            min_value=float(lim_raw["min_value"]),
            max_value=float(lim_raw["max_value"]),
            unit=str(lim_raw.get("unit", "")),
            verified=bool(lim_raw.get("verified", False)),
            source=str(lim_raw.get("source", "unspecified")),
        )

    default_safe_z_raw = raw.get("default_safe_z_mm")
    default_safe_z_mm = float(default_safe_z_raw) if default_safe_z_raw is not None else None
    min_safe_z_margin_mm = float(raw.get("min_safe_z_margin_mm", 0.0))

    forbidden_zones = tuple(
        ForbiddenZone(
            name=str(z["name"]),
            x_min=float(z["x_min"]),
            x_max=float(z["x_max"]),
            y_min=float(z["y_min"]),
            y_max=float(z["y_max"]),
            z_min=float(z["z_min"]),
            z_max=float(z["z_max"]),
            reason=str(z.get("reason", "")),
        )
        for z in list(raw.get("forbidden_zones") or [])
    )

    tools: dict[int, ToolSlotSpec] = {}
    for t in list(raw.get("tools") or []):
        tid = int(t["tool_id"])
        tools[tid] = ToolSlotSpec(
            tool_id=tid,
            name=str(t["name"]),
            park_x_mm=float(t["park_x_mm"]),
            park_y_mm=float(t["park_y_mm"]),
            park_z_mm=float(t["park_z_mm"]),
            length_mm=float(t["length_mm"]),
            verified=bool(t.get("verified", False)),
        )

    hookup_raw = raw.get("hookup")
    if not isinstance(hookup_raw, dict):
        raise BundleValidationError("device.yaml missing required 'hookup' mapping.")

    sections = tuple(
        HookupSection(
            section_id=str(s["section_id"]),
            title=str(s["title"]),
            summary=str(s.get("summary", "")),
            steps=tuple(str(step) for step in list(s.get("steps") or [])),
        )
        for s in list(hookup_raw.get("sections") or [])
    )
    hookup = HookupSpec(
        primary_interface=str(hookup_raw["primary_interface"]).strip(),
        default_endpoint=str(hookup_raw["default_endpoint"]).strip(),
        host_ip=str(hookup_raw["host_ip"]).strip() if hookup_raw.get("host_ip") else None,
        subnet_mask=(
            str(hookup_raw["subnet_mask"]).strip() if hookup_raw.get("subnet_mask") else None
        ),
        fallback_interface=str(hookup_raw.get("fallback_interface", "None")).strip(),
        completion_semantics=str(hookup_raw["completion_semantics"]).strip(),
        estop_policy=str(hookup_raw["estop_policy"]).strip(),
        preflight_prohibitions=tuple(
            str(item) for item in list(hookup_raw.get("preflight_prohibitions") or [])
        ),
        field_verification_required=tuple(
            str(item) for item in list(hookup_raw.get("field_verification_required") or [])
        ),
        sections=sections,
    )

    targets_raw = raw.get("targets")
    if not isinstance(targets_raw, dict) or not targets_raw:
        raise BundleValidationError("device.yaml must define at least one target profile.")
    targets: dict[str, TargetProfile] = {}
    for target_name, t_raw in targets_raw.items():
        risks = frozenset(
            _parse_risk(r, f"target '{target_name}'")
            for r in list(t_raw.get("require_confirm_for_risks") or [])
        )
        targets[str(target_name)] = TargetProfile(
            name=str(target_name),
            adapter_id=str(t_raw["adapter_id"]).strip(),
            capabilities=frozenset(str(c) for c in list(t_raw.get("capabilities") or [])),
            is_real_hardware=bool(t_raw.get("is_real_hardware", False)),
            require_confirm_for_risks=risks,
            default_endpoint=(
                str(t_raw["default_endpoint"]) if t_raw.get("default_endpoint") else None
            ),
            notes=str(t_raw.get("notes", "")).strip(),
            adapter_options=_parse_options(
                t_raw.get("adapter_options"), f"Target {target_name!r} adapter_options"
            ),
        )

    return DeviceSpec(
        device_id=device_id,
        display_name=display_name,
        version=version,
        device_kind=device_kind,
        review_status=review_status,
        hardware_ref=hardware_ref,
        axes=axes,
        safety_limits=safety_limits,
        default_safe_z_mm=default_safe_z_mm,
        min_safe_z_margin_mm=min_safe_z_margin_mm,
        forbidden_zones=forbidden_zones,
        tools=tools,
        hookup=hookup,
        targets=targets,
        source_inputs=source_inputs,
    )


def _parse_catalog_spec(raw: dict[str, Any]) -> CatalogSpec:
    try:
        device_id = str(raw["device_id"]).strip()
        version = str(raw["version"]).strip()
        services_raw = raw["services"]
    except KeyError as exc:
        raise BundleValidationError(f"catalog.yaml missing required field: {exc}") from exc

    if not isinstance(services_raw, list) or not services_raw:
        raise BundleValidationError("catalog.yaml 'services' must be a non-empty list.")

    services: dict[str, ServiceSpec] = {}
    for item in services_raw:
        name = str(item["name"]).strip()
        if name in services:
            raise BundleValidationError(f"Duplicate service definition: {name!r}")
        confirm_if = item.get("confirm_if", [])
        if not isinstance(confirm_if, list) or not all(
            isinstance(rule, str) and rule.strip() for rule in confirm_if
        ):
            raise BundleValidationError(f"Service {name!r} confirm_if must be a list of rules.")
        args = tuple(
            ArgSpec(
                name=str(a["name"]),
                arg_type=str(a.get("type", "string")),
                required=bool(a.get("required", True)),
                unit=str(a["unit"]) if a.get("unit") is not None else None,
                min_value=float(a["min_value"]) if a.get("min_value") is not None else None,
                max_value=float(a["max_value"]) if a.get("max_value") is not None else None,
                default=a.get("default"),
                description=str(a.get("description", "")),
            )
            for a in list(item.get("args") or [])
        )
        services[name] = ServiceSpec(
            name=name,
            summary=str(item.get("summary", "")).strip(),
            risk_class=_parse_risk(item.get("risk_class"), f"service '{name}'"),
            args=args,
            requires_capabilities=frozenset(
                str(c) for c in list(item.get("requires_capabilities") or [])
            ),
            preconditions=tuple(str(p) for p in list(item.get("preconditions") or [])),
            effects=tuple(str(e) for e in list(item.get("effects") or [])),
            postconditions=tuple(str(p) for p in list(item.get("postconditions") or [])),
            fail_safe=str(item.get("fail_safe", "none")).strip(),
            timeout_s=float(item.get("timeout_s", 5.0)),
            requires_confirm=bool(item.get("requires_confirm", False)),
            bypasses_action_lock=bool(item.get("bypasses_action_lock", False)),
            expert_only=bool(item.get("expert_only", False)),
            returns_summary=str(item.get("returns_summary", "")).strip(),
            confirm_if=tuple(rule.strip() for rule in confirm_if),
            protocol=_parse_options(item.get("protocol"), f"Service {name!r} protocol"),
        )
    return CatalogSpec(device_id=device_id, version=version, services=services)


def load_bundle(bundle_dir: Path) -> DeviceBundle:
    """Load and parse device.yaml and catalog.yaml from ``bundle_dir``."""
    resolved = bundle_dir.resolve()
    device_raw = _load_yaml_mapping(resolved / "device.yaml")
    catalog_raw = _load_yaml_mapping(resolved / "catalog.yaml")
    bundle = DeviceBundle(
        device=_parse_device_spec(device_raw),
        catalog=_parse_catalog_spec(catalog_raw),
        source_dir=str(resolved),
    )
    return bundle


def validate_bundle(bundle: DeviceBundle) -> None:
    """Validate cross-file consistency and safety invariants for any device bundle."""
    if not bundle.device.device_id:
        raise BundleValidationError("device.yaml device_id must be non-empty.")
    if bundle.device.device_id != bundle.catalog.device_id:
        raise BundleValidationError(
            f"device_id mismatch: device.yaml has {bundle.device.device_id!r}, "
            f"catalog.yaml has {bundle.catalog.device_id!r}."
        )
    if not bundle.device.hookup.estop_policy:
        raise BundleValidationError("device.yaml hookup.estop_policy must be documented.")
    if not bundle.device.hookup.completion_semantics:
        raise BundleValidationError("device.yaml hookup.completion_semantics must be documented.")

    for target_name, profile in bundle.device.targets.items():
        if not profile.adapter_id:
            raise BundleValidationError(f"Target {target_name!r} missing adapter_id.")

    for required_sys in ("sys.detect", "sys.status", "sys.preflight", "sys.abort"):
        if required_sys not in bundle.catalog.services:
            raise BundleValidationError(
                f"catalog.yaml missing mandatory system service {required_sys!r}."
            )
    if not bundle.catalog.services["sys.abort"].bypasses_action_lock:
        raise BundleValidationError("sys.abort MUST declare bypasses_action_lock: true.")

    for svc in bundle.catalog.services.values():
        if not SERVICE_NAME_RE.match(svc.name):
            raise BundleValidationError(
                f"Service name {svc.name!r} must match '<group>.<verb>' lowercase format."
            )
        if svc.timeout_s <= 0:
            raise BundleValidationError(f"Service {svc.name!r} timeout_s must be > 0.")
        if svc.risk_class == RiskClass.R4_EXPERT and not svc.requires_confirm:
            raise BundleValidationError(f"R4_EXPERT service {svc.name!r} must require confirm.")
        seen_args: set[str] = set()
        for arg in svc.args:
            if arg.arg_type not in VALID_ARG_TYPES:
                raise BundleValidationError(
                    f"Service {svc.name!r} arg {arg.name!r} has invalid type {arg.arg_type!r}."
                )
            if arg.name in seen_args:
                raise BundleValidationError(f"Duplicate arg {arg.name!r} on service {svc.name!r}.")
            seen_args.add(arg.name)
            if (
                arg.min_value is not None
                and arg.max_value is not None
                and arg.min_value > arg.max_value
            ):
                raise BundleValidationError(
                    f"Service {svc.name!r} arg {arg.name!r} has min_value > max_value."
                )
