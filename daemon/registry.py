"""Pluggable adapter registry backed by dependency injection and Python entry points."""

from __future__ import annotations

from importlib import metadata
from typing import cast

from adapter_spi import (
    ADAPTER_ENTRY_POINT_GROUP,
    AdapterConfig,
    AdapterFactory,
    AdapterPair,
    create_in_memory_adapter,
)
from core_types import CliAnyDeviceError, ErrorCode, ExitCode


class AdapterRegistry:
    """Resolves adapter_id -> AdapterFactory via explicit registration or lazy entry points."""

    def __init__(self, *, load_entry_points: bool = True) -> None:
        self._factories: dict[str, AdapterFactory] = {
            "in_memory": create_in_memory_adapter,
        }
        self._entry_points: dict[str, metadata.EntryPoint] = {}
        if load_entry_points:
            self._discover_entry_points()

    def _discover_entry_points(self) -> None:
        for ep in metadata.entry_points(group=ADAPTER_ENTRY_POINT_GROUP):
            if ep.name not in self._factories and ep.name not in self._entry_points:
                self._entry_points[ep.name] = ep

    def register(self, adapter_id: str, factory: AdapterFactory) -> None:
        """Register or override an adapter factory by ID (used for DI and tests)."""
        self._factories[adapter_id] = factory
        self._entry_points.pop(adapter_id, None)

    def available_adapters(self) -> tuple[str, ...]:
        return tuple(sorted(set(self._factories) | set(self._entry_points)))

    def create(self, adapter_id: str, config: AdapterConfig) -> AdapterPair:
        factory = self._factories.get(adapter_id)
        if factory is None and adapter_id in self._entry_points:
            ep = self._entry_points[adapter_id]
            try:
                loaded = ep.load()
            except Exception as exc:
                raise CliAnyDeviceError(
                    ErrorCode.E_TRANSPORT_ERROR,
                    f"Failed to load adapter entry point '{adapter_id}' ({ep.value}): {exc}",
                    step="adapter_load",
                    exit_code=ExitCode.EXECUTION_ERROR,
                ) from exc
            if not callable(loaded):
                raise CliAnyDeviceError(
                    ErrorCode.E_UNKNOWN_TARGET,
                    f"Adapter entry point '{adapter_id}' ({ep.value}) is not callable.",
                    step="adapter_load",
                    exit_code=ExitCode.INVALID_USAGE,
                )
            factory = cast(AdapterFactory, loaded)
            self._factories[adapter_id] = factory

        if factory is None:
            raise CliAnyDeviceError(
                ErrorCode.E_UNKNOWN_TARGET,
                f"Adapter plugin '{adapter_id}' is not installed or registered "
                f"(entry point group '{ADAPTER_ENTRY_POINT_GROUP}'). "
                f"Available: {list(self.available_adapters())}.",
                step="adapter_lookup",
                exit_code=ExitCode.INVALID_USAGE,
            )
        return factory(config)
