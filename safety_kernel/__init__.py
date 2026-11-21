"""Device-agnostic safety kernel: R0-R4 gating, preconditions, completion, and fail-safe."""

from safety_kernel.engine import SafetyKernel
from safety_kernel.predicates import GateRejection, PostconditionError

__all__ = [
    "GateRejection",
    "PostconditionError",
    "SafetyKernel",
]
