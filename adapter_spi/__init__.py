"""Adapter Service Provider Interface (SPI) for CLI-Any-Device."""

from adapter_spi.fake import (
    DEFAULT_FULL_CAPABILITIES,
    GRBL_SUBSET_CAPABILITIES,
    InMemoryFakeAdapter,
    create_in_memory_adapter,
)
from adapter_spi.protocols import (
    ABORT_LATENCY_SLA_MS,
    ADAPTER_ENTRY_POINT_GROUP,
    AbortResult,
    ActionDriver,
    ActionRequest,
    ActionTelemetry,
    AdapterConfig,
    AdapterFactory,
    AdapterPair,
    PreflightReport,
    SupervisorChannel,
)

__all__ = [
    "ABORT_LATENCY_SLA_MS",
    "ADAPTER_ENTRY_POINT_GROUP",
    "DEFAULT_FULL_CAPABILITIES",
    "GRBL_SUBSET_CAPABILITIES",
    "AbortResult",
    "ActionDriver",
    "ActionRequest",
    "ActionTelemetry",
    "AdapterConfig",
    "AdapterFactory",
    "AdapterPair",
    "InMemoryFakeAdapter",
    "PreflightReport",
    "SupervisorChannel",
    "create_in_memory_adapter",
]
