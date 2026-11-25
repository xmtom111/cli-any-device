"""Generic DeviceDaemon managing any DeviceBundle, pluggable adapters, and SafetyKernel."""

from __future__ import annotations

import threading
import time
from typing import Any

from adapter_spi import AdapterConfig, AdapterPair
from core_types import (
    CliAnyDeviceError,
    CommandEnvelope,
    DeviceBundle,
    ErrorCode,
    ErrorDetail,
    ExecutionMode,
    ExitCode,
)
from daemon.registry import AdapterRegistry
from safety_kernel import SafetyKernel


class DeviceDaemon:
    """Hosts a loaded DeviceBundle and routes target invocations to per-target SafetyKernels."""

    def __init__(
        self,
        *,
        bundle: DeviceBundle,
        registry: AdapterRegistry | None = None,
        endpoint_overrides: dict[str, str] | None = None,
        allow_live_hardware: bool = False,
    ) -> None:
        self._bundle = bundle
        self._registry = registry or AdapterRegistry()
        self._endpoint_overrides = dict(endpoint_overrides or {})
        self._allow_live_hardware = allow_live_hardware
        self._session_lock = threading.Lock()
        self._kernels: dict[str, SafetyKernel] = {}
        self._pairs: dict[str, AdapterPair] = {}

    @property
    def bundle(self) -> DeviceBundle:
        return self._bundle

    def _get_or_create_kernel(self, target_name: str) -> SafetyKernel:
        with self._session_lock:
            existing = self._kernels.get(target_name)
            if existing is not None:
                return existing

            profile = self._bundle.device.targets.get(target_name)
            if profile is None:
                raise CliAnyDeviceError(
                    ErrorCode.E_UNKNOWN_TARGET,
                    f"Target '{target_name}' is not declared in bundle "
                    f"'{self._bundle.device.device_id}'. "
                    f"Available targets: {sorted(self._bundle.device.targets)}.",
                    step="target_lookup",
                    exit_code=ExitCode.INVALID_USAGE,
                )

            endpoint = self._endpoint_overrides.get(target_name, profile.default_endpoint)
            cfg = AdapterConfig(
                target=target_name,
                endpoint=endpoint,
                options={
                    "safe_z_mm": self._bundle.device.default_safe_z_mm,
                    **profile.adapter_options,
                    "capabilities": sorted(profile.capabilities),
                    "is_real_hardware": profile.is_real_hardware,
                    "device_id": self._bundle.device.device_id,
                    "device_kind": self._bundle.device.device_kind,
                    "allow_live_hardware": self._allow_live_hardware,
                },
            )
            pair = self._registry.create(profile.adapter_id, cfg)
            kernel = SafetyKernel(
                bundle=self._bundle,
                target_name=target_name,
                supervisor=pair.supervisor,
                driver=pair.driver,
                allow_live_hardware=self._allow_live_hardware,
            )
            self._pairs[target_name] = pair
            self._kernels[target_name] = kernel
            return kernel

    def dispatch(
        self,
        *,
        target: str,
        service: str,
        args: dict[str, Any] | None = None,
        dry_run: bool = False,
        confirm: bool = False,
    ) -> CommandEnvelope:
        """Dispatch a command to the target's SafetyKernel (lock-free for sys.abort)."""
        t0 = time.perf_counter()
        mode = ExecutionMode.DRY_RUN if dry_run else ExecutionMode.LIVE
        try:
            kernel = self._get_or_create_kernel(target)
        except CliAnyDeviceError as exc:
            return CommandEnvelope(
                ok=False,
                service=service,
                target=target,
                mode=mode,
                duration_ms=round((time.perf_counter() - t0) * 1000.0),
                exit_code=exc.exit_code,
                state={},
                data={},
                error=ErrorDetail(
                    code=exc.code,
                    step=exc.step,
                    reason=exc.reason,
                    hookup_ref=exc.hookup_ref,
                ),
            )
        return kernel.invoke(service, args=args, dry_run=dry_run, confirm=confirm)

    def close(self) -> None:
        with self._session_lock:
            for pair in self._pairs.values():
                if pair.close is not None:
                    pair.close()
            self._pairs.clear()
            self._kernels.clear()
