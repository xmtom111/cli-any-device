"""Unit tests for the generic bundle loader, drafter (ingestor), and compiler."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from compiler.builder import BuildDriftError, build_bundle
from compiler.ingestor import draft_bundle
from compiler.loader import BundleValidationError, load_bundle, validate_bundle

REPO_ROOT = Path(__file__).resolve().parent.parent
JUBILEE_BUNDLE_DIR = REPO_ROOT / "bundles" / "jubilee"
JUBILEE_GENERATED_DIR = REPO_ROOT / "docs" / "generated" / "jubilee"
RADIO_FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "open_radio"


def test_jubilee_bundle_loads_validates_and_reflects_real_machine_facts() -> None:
    bundle = load_bundle(JUBILEE_BUNDLE_DIR)
    validate_bundle(bundle)

    assert bundle.device.device_id == "jubilee"
    assert "Duet" in bundle.device.hookup.primary_interface
    assert "HTTP" in bundle.device.hookup.primary_interface
    assert "M400" in bundle.device.hookup.completion_semantics
    assert "idle" in bundle.device.hookup.completion_semantics
    assert "/rr_gcode" in bundle.device.hookup.completion_semantics
    assert "M112" in bundle.device.hookup.estop_policy
    assert "physical" in bundle.device.hookup.estop_policy.lower()

    # GRBL sim is honestly capability-limited and NOT real Jubilee
    grbl = bundle.device.targets["grbl_sim"]
    assert "tool_lock_u" not in grbl.capabilities
    assert "rrf_object_model" not in grbl.capabilities
    assert "NOT a real Jubilee" in grbl.notes

    # Default catalog omits expert escape hatches
    default_names = {svc.name for svc in bundle.catalog.default_agent_services()}
    expert_names = {svc.name for svc in bundle.catalog.expert_services()}
    assert {
        "sys.detect",
        "sys.status",
        "sys.preflight",
        "sys.abort",
        "motion.home_all",
        "tool.pickup",
        "motion.move_well",
        "tool.park",
    } <= default_names
    assert {"expert.gcode", "expert.tool_lock", "expert.tool_unlock"} <= expert_names
    assert default_names.isdisjoint(expert_names)


def test_compiler_builds_jubilee_artifacts_and_detects_drift(tmp_path: Path) -> None:
    out_dir = tmp_path / "jubilee_gen"
    result = build_bundle(JUBILEE_BUNDLE_DIR, out_dir, check=False)
    assert len(result.written_files) == 3

    skill_md = (out_dir / "SKILL.md").read_text(encoding="utf-8")
    hookup_md = (out_dir / "HOOKUP.md").read_text(encoding="utf-8")
    schema = json.loads((out_dir / "cli_schema.json").read_text(encoding="utf-8"))

    # SKILL.md stays concise and omits expert commands
    assert "DO NOT EDIT" in skill_md
    assert "sys.preflight" in skill_md or "sys preflight" in skill_md
    assert "motion.move_well" in skill_md or "move-well" in skill_md
    assert "expert.gcode" not in skill_md
    assert "expert.tool_unlock" not in skill_md
    assert "reprap" not in skill_md.lower()  # Never hardcode default password

    # HOOKUP.md preserves all real-machine wiring, completion, e-stop, and GRBL disclaimer
    assert "192.168.1.2" in hookup_md
    assert "192.168.1.3" in hookup_md
    assert "M400" in hookup_md
    assert "M112" in hookup_md
    assert "sec-troubleshooting" in hookup_md
    assert "GRBL" in hookup_md

    # cli_schema.json describes groups and services dynamically
    assert schema["device_id"] == "jubilee"
    service_names = {s["name"] for s in schema["services"]}
    assert "motion.move_well" in service_names
    assert "expert.gcode" in service_names

    # --check passes immediately after build
    check_res = build_bundle(JUBILEE_BUNDLE_DIR, out_dir, check=True)
    assert check_res.checked is True

    # Manual edit triggers drift failure under --check
    (out_dir / "SKILL.md").write_text(skill_md + "\nhand edited\n", encoding="utf-8")
    with pytest.raises(BuildDriftError):
        build_bundle(JUBILEE_BUNDLE_DIR, out_dir, check=True)


def test_checked_in_jubilee_generated_docs_match_yaml() -> None:
    """CI invariant: docs/generated/jubilee must match bundles/jubilee without drift."""
    result = build_bundle(JUBILEE_BUNDLE_DIR, JUBILEE_GENERATED_DIR, check=True)
    assert result.checked is True


def test_generic_ingestor_drafts_and_compiles_open_radio_from_manual_and_host_api(
    tmp_path: Path,
) -> None:
    """Proves the core product works for ANY open-source machine (non-motion open radio)."""
    bundle_dir = tmp_path / "bundles" / "open_radio"
    gen_dir = tmp_path / "generated" / "open_radio"

    draft_bundle(
        device_id="open_radio",
        display_name="OpenRadio-100 Transceiver",
        device_kind="radio",
        interface_type="usb_serial",
        endpoint="serial:///dev/ttyUSB0",
        manual_paths=[RADIO_FIXTURE_DIR / "manual.md"],
        host_api_paths=[RADIO_FIXTURE_DIR / "radio_host.py"],
        output_dir=bundle_dir,
    )

    bundle = load_bundle(bundle_dir)
    validate_bundle(bundle)
    assert bundle.device.device_id == "open_radio"
    assert bundle.device.device_kind == "radio"
    assert bundle.device.review_status == "draft"
    assert bundle.device.axes == {}  # Non-motion device needs zero fake axes

    svc_names = set(bundle.catalog.services)
    # Universal sys services + AST-extracted host API services
    expected_sys = {"sys.detect", "sys.status", "sys.preflight", "sys.abort", "sys.reset_estop"}
    assert expected_sys <= svc_names
    assert "radio.get_signal" in svc_names
    assert "radio.tune" in svc_names
    assert "radio.set_volume" in svc_names
    assert "radio.enable_tx" in svc_names
    assert "radio.calibrate_frontend" in svc_names
    assert "expert.send_raw_at" in svc_names
    assert bundle.catalog.services["expert.send_raw_at"].expert_only is True

    # Compiles cleanly into SKILL.md, HOOKUP.md, cli_schema.json
    build_bundle(bundle_dir, gen_dir, check=False)
    build_bundle(bundle_dir, gen_dir, check=True)
    skill = (gen_dir / "SKILL.md").read_text(encoding="utf-8")
    hookup = (gen_dir / "HOOKUP.md").read_text(encoding="utf-8")
    assert "OpenRadio-100" in skill
    assert "radio.tune" in skill
    assert "send_raw_at" not in skill  # Expert service omitted from agent SKILL.md
    assert "Jubilee" not in skill
    assert "Jubilee" not in hookup


def test_validator_rejects_hardcoded_default_password(tmp_path: Path) -> None:
    bad_dir = tmp_path / "bad_bundle"
    bad_dir.mkdir()
    device_yaml = (JUBILEE_BUNDLE_DIR / "device.yaml").read_text(encoding="utf-8")
    device_yaml = device_yaml.replace("http://192.168.1.2", "http://192.168.1.2?password=reprap")
    (bad_dir / "device.yaml").write_text(device_yaml, encoding="utf-8")
    (bad_dir / "catalog.yaml").write_text(
        (JUBILEE_BUNDLE_DIR / "catalog.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    bundle = load_bundle(bad_dir)
    with pytest.raises(BundleValidationError):
        validate_bundle(bundle)
