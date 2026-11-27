"""Contracts and registration for optional safety domains."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, TypeVar

from core_types import CanonicalMachineState, DeviceSpec


@dataclass(frozen=True, slots=True)
class PreconditionContext:
    args: dict[str, Any]
    state: CanonicalMachineState
    device: DeviceSpec
    target_coords: dict[str, float] | None


Precondition = Callable[[PreconditionContext], None]
Postcondition = Callable[[dict[str, Any], CanonicalMachineState], None]
CoordinateResolver = Callable[[dict[str, Any], CanonicalMachineState], dict[str, float] | None]


@dataclass(frozen=True, slots=True)
class DomainExtension:
    """Register predicates by name and coordinate resolvers by service name."""

    preconditions: Mapping[str, Precondition] = field(default_factory=dict)
    postconditions: Mapping[str, Postcondition] = field(default_factory=dict)
    coord_resolver: Mapping[str, CoordinateResolver] = field(default_factory=dict)


_Handler = TypeVar("_Handler")


def _merge_handlers(*maps: Mapping[str, _Handler]) -> Mapping[str, _Handler]:
    handlers: dict[str, _Handler] = {}
    for mapping in maps:
        duplicates = handlers.keys() & mapping.keys()
        if duplicates:
            raise ValueError(f"Duplicate domain registrations: {sorted(duplicates)}.")
        handlers.update(mapping)
    return MappingProxyType(handlers)


def register_domains(*extensions: DomainExtension) -> DomainExtension:
    """Combine extensions into an immutable registry for a kernel."""
    return DomainExtension(
        preconditions=_merge_handlers(*(ext.preconditions for ext in extensions)),
        postconditions=_merge_handlers(*(ext.postconditions for ext in extensions)),
        coord_resolver=_merge_handlers(*(ext.coord_resolver for ext in extensions)),
    )
