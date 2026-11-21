"""Static architecture tests: enforce import DAG, vendor isolation, and device-agnostic core."""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN_VENDOR_IMPORTS = {
    "science_jubilee",
    "mujoco",
    "serial",
    "pyserial",
    "requests",
    "httpx",
    "urllib3",
    "grbl_emulator",
}

ALLOWED_INTERNAL_IMPORTS: dict[str, set[str]] = {
    "core_types": set(),
    "adapter_spi": {"core_types"},
    "compiler": {"core_types"},
    "safety_kernel": {"core_types", "adapter_spi"},
    "daemon": {"core_types", "adapter_spi", "compiler", "safety_kernel"},
    "cli": {"core_types", "compiler", "daemon"},
}

FORBIDDEN_HARDCODED_TARGETS = {
    "duet_real",
    "mujoco_twin",
    "grbl_sim",
    "jubilee",
}


def _iter_python_files(pkg: str) -> list[Path]:
    pkg_dir = REPO_ROOT / pkg
    if not pkg_dir.exists():
        return []
    return sorted(p for p in pkg_dir.rglob("*.py") if "__pycache__" not in p.parts)


def _extract_top_level_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".", 1)[0])
    return imports


def test_import_dag_and_vendor_isolation() -> None:
    internal_pkgs = set(ALLOWED_INTERNAL_IMPORTS) | {"adapters"}
    for pkg, allowed in ALLOWED_INTERNAL_IMPORTS.items():
        for py_file in _iter_python_files(pkg):
            imported = _extract_top_level_imports(py_file)
            # No vendor / hardware libraries allowed in core/kernel/compiler/daemon/cli
            bad_vendor = imported & FORBIDDEN_VENDOR_IMPORTS
            assert not bad_vendor, f"{py_file} imports forbidden vendor libs: {sorted(bad_vendor)}"

            # Strictly respect the acyclic internal import DAG
            used_internal = (imported & internal_pkgs) - {pkg}
            illegal = used_internal - allowed
            assert not illegal, (
                f"{py_file} violates import DAG: {pkg} may only import {sorted(allowed)}, "
                f"but imported {sorted(illegal)}"
            )


def test_core_and_kernel_have_no_target_or_device_branches() -> None:
    """Kernel, core_types, and compiler must never branch on specific device/target names."""
    for pkg in ("core_types", "safety_kernel", "compiler"):
        for py_file in _iter_python_files(pkg):
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            for node in ast.walk(tree):
                if isinstance(node, ast.Compare):
                    literals = [
                        comp.value
                        for comp in [node.left, *node.comparators]
                        if isinstance(comp, ast.Constant) and isinstance(comp.value, str)
                    ]
                    for lit in literals:
                        assert lit.lower() not in FORBIDDEN_HARDCODED_TARGETS, (
                            f"{py_file}:{node.lineno} compares against hardcoded target/device "
                            f"{lit!r}; use capability flags or declarative YAML instead."
                        )
