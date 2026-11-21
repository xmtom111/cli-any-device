"""Generic declarative device bundle and atomic service catalog types (no I/O)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core_types.risk import RiskClass


@dataclass(frozen=True, slots=True)
class NumericLimit:
    """Generic safe numeric operating limit for any parameter or axis."""

    name: str
    min_value: float
    max_value: float
    unit: str = ""
    verified: bool = False
    source: str = ""


@dataclass(frozen=True, slots=True)
class AxisLimit:
    """Optional soft axis bounds for motion-capable devices."""

    name: str
    min_mm: float
    max_mm: float
    verified: bool
    source: str


@dataclass(frozen=True, slots=True)
class ForbiddenZone:
    """Optional axis-aligned keep-out volume for motion-capable devices."""

    name: str
    x_min: float
    x_max: float
    y_min: float
    y_max: float
    z_min: float
    z_max: float
    reason: str

    def contains(self, x: float, y: float, z: float) -> bool:
        return (
            self.x_min <= x <= self.x_max
            and self.y_min <= y <= self.y_max
            and self.z_min <= z <= self.z_max
        )


@dataclass(frozen=True, slots=True)
class ToolSlotSpec:
    """Optional end-effector / tool dock specification."""

    tool_id: int
    name: str
    park_x_mm: float
    park_y_mm: float
    park_z_mm: float
    length_mm: float
    verified: bool


@dataclass(frozen=True, slots=True)
class HookupSection:
    """One named section in generated HOOKUP.md (addressable by anchor ID)."""

    section_id: str
    title: str
    summary: str
    steps: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class HookupSpec:
    """Human hookup, pre-flight, completion, and emergency-stop specification."""

    primary_interface: str
    default_endpoint: str
    host_ip: str | None
    subnet_mask: str | None
    fallback_interface: str
    completion_semantics: str
    estop_policy: str
    preflight_prohibitions: tuple[str, ...]
    field_verification_required: tuple[str, ...]
    sections: tuple[HookupSection, ...]


@dataclass(frozen=True, slots=True)
class TargetProfile:
    """Declarative target profile bound to an adapter plugin and capability set."""

    name: str
    adapter_id: str
    capabilities: frozenset[str]
    is_real_hardware: bool
    require_confirm_for_risks: frozenset[RiskClass] = frozenset()
    default_endpoint: str | None = None
    notes: str = ""


@dataclass(frozen=True, slots=True)
class DeviceSpec:
    """Parsed device.yaml specification for any open-source hardware device."""

    device_id: str
    display_name: str
    version: str
    device_kind: str
    hardware_ref: dict[str, str]
    hookup: HookupSpec
    targets: dict[str, TargetProfile]
    safety_limits: dict[str, NumericLimit] = field(default_factory=dict)
    axes: dict[str, AxisLimit] = field(default_factory=dict)
    default_safe_z_mm: float | None = None
    min_safe_z_margin_mm: float = 10.0
    forbidden_zones: tuple[ForbiddenZone, ...] = ()
    tools: dict[int, ToolSlotSpec] = field(default_factory=dict)
    source_inputs: dict[str, Any] = field(default_factory=dict)
    review_status: str = "verified"


@dataclass(frozen=True, slots=True)
class ArgSpec:
    """Typed argument declaration for an atomic service."""

    name: str
    arg_type: str  # "string" | "float" | "int" | "bool"
    required: bool = True
    unit: str | None = None
    min_value: float | None = None
    max_value: float | None = None
    default: Any = None
    description: str = ""


@dataclass(frozen=True, slots=True)
class ServiceSpec:
    """Single atomic service specification declared in catalog.yaml."""

    name: str
    summary: str
    risk_class: RiskClass
    args: tuple[ArgSpec, ...] = ()
    requires_capabilities: frozenset[str] = frozenset()
    preconditions: tuple[str, ...] = ()
    effects: tuple[str, ...] = ()
    postconditions: tuple[str, ...] = ()
    fail_safe: str = "none"
    timeout_s: float = 5.0
    requires_confirm: bool = False
    bypasses_action_lock: bool = False
    expert_only: bool = False
    returns_summary: str = ""

    @property
    def cli_group(self) -> str:
        group, _ = self.name.split(".", 1)
        return group

    @property
    def cli_command(self) -> str:
        _, verb = self.name.split(".", 1)
        return verb.replace("_", "-")


@dataclass(frozen=True, slots=True)
class CatalogSpec:
    """Parsed catalog.yaml specification."""

    device_id: str
    version: str
    services: dict[str, ServiceSpec] = field(default_factory=dict)

    def default_agent_services(self) -> tuple[ServiceSpec, ...]:
        return tuple(svc for svc in self.services.values() if not svc.expert_only)

    def expert_services(self) -> tuple[ServiceSpec, ...]:
        return tuple(svc for svc in self.services.values() if svc.expert_only)


@dataclass(frozen=True, slots=True)
class DeviceBundle:
    """Complete validated device bundle (device.yaml + catalog.yaml)."""

    device: DeviceSpec
    catalog: CatalogSpec
    source_dir: str = ""
