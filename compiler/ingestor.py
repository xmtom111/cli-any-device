"""Generic semi-automated bundle drafter (interface + manual/host API -> YAML bundle).

Uses Python AST inspection ONLY — never imports or executes untrusted vendor host code.
Produces a human-reviewable draft bundle marked ``review_status: draft`` that immediately
compiles via ``cli-device bundle build`` and runs against the generic daemon + safety_kernel.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

RANGE_RE = re.compile(
    r"([A-Za-z][A-Za-z0-9 _/-]{1,32}):\s*"
    r"(-?\d+(?:\.\d+)?)\s*(?:to|-)\s*(-?\d+(?:\.\d+)?)\s*([A-Za-z/%]+)?",
    re.IGNORECASE,
)


def _classify_method(name: str, doc: str) -> tuple[str, str, bool, bool]:
    """Return (group_override_or_empty, risk_class, requires_confirm, expert_only)."""
    low = name.lower()
    doc_low = doc.lower()
    if any(tok in low for tok in ("raw", "gcode", "send_cmd", "send_at", "unlock")):
        return ("expert", "R4_EXPERT", True, True)
    if any(tok in low for tok in ("home", "calibrate", "init", "pickup", "park", "reset")):
        return ("", "R3_PHYSICAL_INIT", True, False)
    if any(
        tok in low
        for tok in ("move", "goto", "transmit", "enable_tx", "actuate", "start", "dispense")
    ):
        needs_confirm = "confirm" in doc_low or "tx" in low
        return ("", "R2_MOTION", needs_confirm, False)
    if any(tok in low for tok in ("get_", "read_", "status", "detect", "poll", "info", "list_")):
        return ("", "R0_READ", False, False)
    return ("", "R1_CONFIG", False, False)


def _annotation_to_arg_type(annotation: ast.expr | None, default_val: Any) -> str:
    if isinstance(annotation, ast.Name):
        mapping = {"str": "string", "float": "float", "int": "int", "bool": "bool"}
        if annotation.id in mapping:
            return mapping[annotation.id]
    if isinstance(default_val, bool):
        return "bool"
    if isinstance(default_val, int):
        return "int"
    if isinstance(default_val, float):
        return "float"
    return "string"


def _literal_default(node: ast.expr | None) -> Any:
    if node is None:
        return None
    if isinstance(node, ast.Constant):
        return node.value
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.USub)
        and isinstance(node.operand, ast.Constant)
        and isinstance(node.operand.value, (int, float))
    ):
        return -node.operand.value
    return None


def _extract_host_api_services(
    host_api_paths: Sequence[Path],
    default_group: str,
    primary_capability: str,
) -> list[dict[str, Any]]:
    services: list[dict[str, Any]] = []
    seen_names: set[str] = set()

    for path in host_api_paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        func_nodes: list[ast.FunctionDef] = []
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
                func_nodes.append(node)
            elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and not item.name.startswith("_"):
                        func_nodes.append(item)

        for fn in func_nodes:
            doc = (ast.get_docstring(fn) or f"Invoke {fn.name}.").splitlines()[0].strip()
            group_override, risk_class, requires_confirm, expert_only = _classify_method(
                fn.name, doc
            )
            group = group_override or default_group
            svc_name = f"{group}.{fn.name.lower()}"
            if svc_name in seen_names:
                continue
            seen_names.add(svc_name)

            fn_args = [a for a in fn.args.args if a.arg != "self"]
            defaults_pad: list[ast.expr | None] = [None] * (
                len(fn_args) - len(fn.args.defaults)
            ) + list(fn.args.defaults)

            parsed_args: list[dict[str, Any]] = []
            for arg_node, def_node in zip(fn_args, defaults_pad, strict=True):
                def_val = _literal_default(def_node)
                arg_type = _annotation_to_arg_type(arg_node.annotation, def_val)
                arg_entry: dict[str, Any] = {
                    "name": arg_node.arg,
                    "type": arg_type,
                    "required": def_node is None,
                    "description": f"Parameter '{arg_node.arg}' extracted from {path.name}.",
                }
                if def_val is not None:
                    arg_entry["default"] = def_val
                parsed_args.append(arg_entry)

            req_caps = ["expert_raw"] if expert_only else [primary_capability]
            preconditions = ["detected", "not_estop_latched"]
            postconditions = ["idle"] if risk_class != "R0_READ" else []
            fail_safe = "abort_m112" if risk_class in ("R3_PHYSICAL_INIT", "R4_EXPERT") else "none"

            services.append(
                {
                    "name": svc_name,
                    "summary": doc,
                    "returns_summary": "result",
                    "risk_class": risk_class,
                    "args": parsed_args,
                    "requires_capabilities": req_caps,
                    "preconditions": preconditions,
                    "effects": [f"executes_{fn.name.lower()}"],
                    "postconditions": postconditions,
                    "fail_safe": fail_safe,
                    "timeout_s": 15.0 if risk_class != "R0_READ" else 5.0,
                    "requires_confirm": requires_confirm,
                    "bypasses_action_lock": False,
                    "expert_only": expert_only,
                }
            )
    return services


def _extract_manual_hints(
    manual_paths: Sequence[Path],
) -> tuple[list[str], dict[str, dict[str, Any]], list[str]]:
    """Extract safety warnings, numeric limits, and wiring notes from manuals."""
    warnings: list[str] = []
    limits: dict[str, dict[str, Any]] = {}
    wiring_notes: list[str] = []

    for path in manual_paths:
        text = path.read_text(encoding="utf-8")
        for raw_line in text.splitlines():
            line = raw_line.strip().lstrip("-* ").strip()
            if not line or line.startswith("#"):
                continue
            low = line.lower()
            if any(k in low for k in ("never", "mandatory", "warning", "physical", "do not")):
                warnings.append(line)
            wiring_keywords = ("serial", "baud", "ethernet", "ip", "connect", "port", "usb")
            if any(k in low for k in wiring_keywords):
                wiring_notes.append(line)
            match = RANGE_RE.search(line)
            if match:
                raw_label, lo, hi, unit = match.groups()
                slug = re.sub(r"[^a-z0-9]+", "_", raw_label.lower()).strip("_")
                if slug:
                    limits[slug] = {
                        "min_value": float(lo),
                        "max_value": float(hi),
                        "unit": unit or "",
                        "verified": False,
                        "source": f"Drafted from {path.name} (verify on physical hardware)",
                    }
    return warnings, limits, wiring_notes


def _base_system_services() -> list[dict[str, Any]]:
    return [
        {
            "name": "sys.detect",
            "summary": "Probe device interface, discover protocol dialect, and verify connection.",
            "returns_summary": "endpoint, api_mode, firmware",
            "risk_class": "R0_READ",
            "args": [],
            "requires_capabilities": [],
            "preconditions": [],
            "effects": ["marks_machine_detected"],
            "postconditions": ["detected"],
            "fail_safe": "none",
            "timeout_s": 5.0,
            "requires_confirm": False,
            "bypasses_action_lock": False,
            "expert_only": False,
        },
        {
            "name": "sys.status",
            "summary": "Poll canonical machine state and live telemetry properties.",
            "returns_summary": "lifecycle, estop_latched, properties",
            "risk_class": "R0_READ",
            "args": [],
            "requires_capabilities": [],
            "preconditions": [],
            "effects": [],
            "postconditions": [],
            "fail_safe": "none",
            "timeout_s": 5.0,
            "requires_confirm": False,
            "bypasses_action_lock": False,
            "expert_only": False,
        },
        {
            "name": "sys.preflight",
            "summary": "Run read-only readiness checks before issuing mutating commands.",
            "returns_summary": "readiness_report",
            "risk_class": "R0_READ",
            "args": [],
            "requires_capabilities": [],
            "preconditions": ["detected"],
            "effects": [],
            "postconditions": [],
            "fail_safe": "none",
            "timeout_s": 5.0,
            "requires_confirm": False,
            "bypasses_action_lock": False,
            "expert_only": False,
        },
        {
            "name": "sys.abort",
            "summary": "Out-of-band lock-free software emergency stop (latches HALTED).",
            "returns_summary": "ESTOP_LATCHED",
            "risk_class": "R0_READ",
            "args": [],
            "requires_capabilities": [],
            "preconditions": [],
            "effects": ["estop_latched"],
            "postconditions": ["estop_latched"],
            "fail_safe": "none",
            "timeout_s": 2.0,
            "requires_confirm": False,
            "bypasses_action_lock": True,
            "expert_only": False,
        },
        {
            "name": "sys.reset_estop",
            "summary": "Clear latched e-stop after physical inspection (requires --confirm).",
            "returns_summary": "lifecycle=IDLE",
            "risk_class": "R3_PHYSICAL_INIT",
            "args": [],
            "requires_capabilities": [],
            "preconditions": ["detected"],
            "effects": ["clears_estop_latch"],
            "postconditions": ["not_estop_latched"],
            "fail_safe": "abort_m112",
            "timeout_s": 10.0,
            "requires_confirm": True,
            "bypasses_action_lock": False,
            "expert_only": False,
        },
    ]


def draft_bundle(
    *,
    device_id: str,
    display_name: str,
    device_kind: str,
    interface_type: str,
    endpoint: str,
    manual_paths: Sequence[Path] = (),
    host_api_paths: Sequence[Path] = (),
    output_dir: Path,
) -> Path:
    """Draft device.yaml and catalog.yaml for ANY open-source machine from interface + inputs."""
    clean_id = re.sub(r"[^a-z0-9_]+", "_", device_id.lower()).strip("_")
    default_group = re.sub(r"[^a-z0-9_]+", "_", device_kind.lower()).strip("_") or clean_id
    primary_cap = f"{default_group}_control"

    warnings, limits, wiring_notes = _extract_manual_hints(manual_paths)
    extracted_services = _extract_host_api_services(host_api_paths, default_group, primary_cap)

    all_caps = [primary_cap, "abort_oob", "expert_raw"]
    prohibitions = warnings or [
        "Verify physical hardware state and wiring before issuing mutating commands.",
        "Do not run expert escape hatches without human confirmation.",
    ]
    wiring_steps = wiring_notes or [
        f"Connect host to {display_name} via {interface_type} ({endpoint}).",
        "Run `cli-device --json sys detect` and `sys preflight` before live control.",
    ]

    device_doc: dict[str, Any] = {
        "device_id": clean_id,
        "display_name": display_name,
        "version": "0.1.0-draft",
        "device_kind": device_kind,
        "interface_type": interface_type,
        "review_status": "draft",
        "hardware_ref": {
            "source_manuals": ", ".join(p.name for p in manual_paths) or "none",
            "source_host_apis": ", ".join(p.name for p in host_api_paths) or "none",
        },
        "source_inputs": {
            "manuals": [str(p) for p in manual_paths],
            "host_apis": [str(p) for p in host_api_paths],
        },
        "safety_limits": limits,
        "hookup": {
            "primary_interface": f"{interface_type} ({endpoint})",
            "default_endpoint": endpoint,
            "fallback_interface": "Diagnostic read-only console (review before production).",
            "completion_semantics": (
                "Adapter MUST wait for device acknowledgement/idle state via "
                "SupervisorChannel.await_settled() before returning exit code 0."
            ),
            "estop_policy": (
                "Software `cli-device sys abort` runs lock-free out-of-band. A physical "
                "power-cut / emergency stop switch within operator reach is MANDATORY "
                "before live hardware actuation."
            ),
            "preflight_prohibitions": prohibitions,
            "field_verification_required": [
                "Human review and sign-off on drafted safety limits, risk classes, and wiring.",
                "Verify physical emergency stop / power disconnect before live operation.",
                f"Verify actual interface address/port matches {endpoint}.",
            ],
            "sections": [
                {
                    "section_id": "sec-wiring",
                    "title": f"Connecting {display_name} ({interface_type})",
                    "summary": f"Initial connection steps for {interface_type} at {endpoint}.",
                    "steps": wiring_steps,
                },
                {
                    "section_id": "sec-physical-estop",
                    "title": "Mandatory Physical Emergency Stop & Software Abort",
                    "summary": "Software abort does not disconnect hardware supply power.",
                    "steps": [
                        "Keep a physical emergency stop / power switch within reach.",
                        "Use `cli-device --json sys abort` for immediate lock-free software halt.",
                    ],
                },
                {
                    "section_id": "sec-troubleshooting",
                    "title": "Troubleshooting `sys detect` Failures",
                    "summary": "Checks when connection detection fails.",
                    "steps": [
                        f"Verify physical cable and power for {interface_type}.",
                        f"Verify target endpoint {endpoint} and device permissions.",
                    ],
                },
            ],
        },
        "targets": {
            "in_memory": {
                "adapter_id": "in_memory",
                "is_real_hardware": False,
                "default_endpoint": f"memory://{clean_id}",
                "capabilities": all_caps,
                "require_confirm_for_risks": ["R3_PHYSICAL_INIT", "R4_EXPERT"],
                "notes": "In-memory fake target for offline verification of drafted bundle.",
            },
            "hardware_real": {
                "adapter_id": f"{clean_id}_adapter",
                "is_real_hardware": True,
                "default_endpoint": endpoint,
                "capabilities": all_caps,
                "require_confirm_for_risks": ["R2_MOTION", "R3_PHYSICAL_INIT", "R4_EXPERT"],
                "notes": (
                    "Real hardware target placeholder. Implement SupervisorChannel + ActionDriver "
                    "and complete human sign-off before live use."
                ),
            },
        },
    }

    catalog_doc: dict[str, Any] = {
        "device_id": clean_id,
        "version": "0.1.0-draft",
        "services": [*_base_system_services(), *extracted_services],
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "device.yaml").write_text(
        yaml.safe_dump(device_doc, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    (output_dir / "catalog.yaml").write_text(
        yaml.safe_dump(catalog_doc, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return output_dir
