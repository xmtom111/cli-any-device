"""Pluggable adapter registry backed by dependency injection and Python entry points."""

from __future__ import annotations

from importlib import metadata

from adapter_spi import (
    ADAPTER_ENTRY_POINT_GROUP,
    AdapterConfig,
    AdapterFactory,
    AdapterPair,
    create_in_memory_adapter,
)
from core_types import CliAnyDeviceError, ErrorCode, ExitCode


class AdapterRegistry:
    """Resolves adapter_id -> AdapterFactory via explicit registration or entry points."""

    def __init__(self, *, load_entry_points: bool = True) -> None:
        self._factories: dict[str, AdapterFactory] = {
            "in_memory": create_in_memory_adapter,
        }
        if load_entry_points:
            self._discover_entry_points()

    def _discover_entry_points(self) -> None:
        eps = metadata.entry_points(group=ADAPTER_ENTRY_POINT_GROUP)
        for ep in eps:
            if ep.name not in self._factories:
                loaded = ep.load()
                if callable(loaded):
                    self._factories[ep.name] = loaded

    def register(self, adapter_id: str, factory: AdapterFactory) -> None:
        """Register or override an adapter factory by ID (used for DI and tests)."""
        self._factories[adapter_id] = factory

    def available_adapters(self) -> tuple[str, ...]:
        return tuple(sorted(self._factories))

    def create(self, adapter_id: str, config: AdapterConfig) -> AdapterPair:
        factory = self._factories.get(adapter_id)
        if factory is None:
            raise CliAnyDeviceError(
                ErrorCode.E_UNKNOWN_TARGET,
                f"Adapter plugin '{adapter_id}' is not installed or registered "
                f"(entry point group '{ADAPTER_ENTRY_POINT_GROUP}'). "
                f"Available: {sorted(self._factories)}.",
                step="adapter_lookup",
                exit_code=ExitCode.INVALID_USAGE,
            )
        return factory(config)
