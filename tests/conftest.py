import os
from importlib import import_module
from pathlib import Path

import pytest

_AUTODNA_SKIP = "Requires local AutoDNA checkout (CLI_ANY_DEVICE_AUTODNA_ROOT)"


@pytest.fixture(scope="session")
def autodna_targets() -> tuple[str, ...]:
    root = Path(os.getenv("CLI_ANY_DEVICE_AUTODNA_ROOT", "/workspace/autodna_sim_a"))
    if not root.is_dir():
        return ()
    try:
        import_module("mujoco")
    except ImportError:
        return ("grbl_sim",)
    return ("mujoco_twin", "grbl_sim")


@pytest.fixture
def require_autodna(autodna_targets: tuple[str, ...]) -> None:
    if not autodna_targets:
        pytest.skip(_AUTODNA_SKIP)


@pytest.fixture
def require_mujoco(require_autodna: None, autodna_targets: tuple[str, ...]) -> None:
    if "mujoco_twin" not in autodna_targets:
        pytest.skip("Requires importable mujoco for the AutoDNA MuJoCo twin")
