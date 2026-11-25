"""Install bundle skills and the device onboarding skill."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from compiler import load_bundle, render_skill_md, resolve_bundle_dir, validate_bundle

META_SKILL = "cli-any-device-onboard"


def install_skill(
    bundle_ref: str | None, *, dest: str, project_dir: Path, repo_root: Path,
) -> tuple[str, list[Path]]:
    """Install the onboarding skill if no bundle reference is provided."""
    if bundle_ref is None:
        name = META_SKILL
        source = repo_root / "skills" / name / "SKILL.md"
        if source.is_file():
            content = source.read_bytes()
        else:
            content = files("cli").joinpath(name, "SKILL.md").read_bytes()
    else:
        bundle = load_bundle(resolve_bundle_dir(bundle_ref, repo_root=repo_root))
        validate_bundle(bundle)
        name = f"cli-device-{bundle.device.device_id}"
        if Path(name).name != name:
            raise ValueError(f"Device ID {bundle.device.device_id!r} must fit in a directory name.")
        content = render_skill_md(bundle).encode("utf-8")

    project_dir = project_dir.expanduser()
    if dest in ("claude", "agents", "all"):
        hosts = ("claude", "agents") if dest == "all" else (dest,)
        roots = [project_dir / f".{host}" / "skills" for host in hosts]
    else:
        roots = [Path(dest).expanduser()]
    paths = [root / name / "SKILL.md" for root in roots]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return name, paths
