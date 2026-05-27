#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ap_common import AnalysisError, ensure_dir, safe_float, write_json

METHODIC_13_URL = "https://ardupilot.github.io/MethodicConfigurator/TUNING_GUIDE_ArduCopter#13-productive-configuration"
LOGGING_REFERENCE = "references/logging-configuration-for-investigation.md"

HIGH_VOLUME_LOGGING = ["INS_RAW_LOG_OPT", "INS_LOG_BAT_MASK", "INS_LOG_BAT_OPT", "LOG_DISARMED"]
NORMAL_LOGGING = ["LOG_BITMASK"]
SAFETY_PARAMETERS = [
    "ARMING_CHECK",
    "BATT_MONITOR",
    "FS_BATT_ENABLE",
    "BATT_FS_LOW_ACT",
    "BATT_FS_CRT_ACT",
    "FS_THR_ENABLE",
    "FS_EKF_ACTION",
    "FS_GCS_ENABLE",
    "FENCE_ENABLE",
    "FENCE_ACTION",
]
FILTER_PARAMETERS = ["INS_HNTCH_ENABLE", "INS_HNTCH_MODE", "INS_HNTCH_FREQ", "INS_HNTCH_BW", "INS_GYRO_FILTER"]
RELEVANT_PARAMETERS = sorted(set(HIGH_VOLUME_LOGGING + NORMAL_LOGGING + SAFETY_PARAMETERS + FILTER_PARAMETERS + ["GUID_OPTIONS", "EK3_SRC1_YAW", "GPS_TYPE", "GPS_TYPE2"]))
ACCEPTED_RESULTS = {"pass", "conditional_pass", "ready_for_guided_checks", "ready_for_further_precision_land_tests", "ready_for_operational_checks", "not_applicable"}
BLOCKING_RESULTS = {"fail", "not_ready", "do_not_proceed", "fix_ekf_gps_first", "reduce_gains", "do_not_use", "unsafe_to_attempt", "fix_hardware_first"}
REQUIRED_PROGRESS_STEPS = ["7.1", "7.1.1", "8.1", "8.2"]
OPTIONAL_PROGRESS_STEPS = ["8.3", "8.4", "8.5", "9.1", "9.2", "9.3", "9.4", "9.5", "9.6", "9.7", "10.1", "10.2", "11.1", "11.2", "12.1", "12.2", "12.3"]


def analyze_productive_config_check(
    *,
    index_path: str | Path | None,
    params_path: str | Path | None,
    methodic_progress_path: str | Path | None,
) -> dict[str, Any]:
    index = load_index(index_path)
    external = parse_param_file(params_path)
    index_params = {k: v for k, v in (index.get("parameters") or {}).items() if safe_float(v) is not None}
    params = dict(index_params)
    params.update(external)
    progress = load_progress(methodic_progress_path)

    result = empty_result()
    result["analysis_window"] = {
        "selection": "final_configuration_audit",
        "index_path": str(index_path) if index_path else None,
        "params_path": str(params_path) if params_path else None,
        "methodic_progress_path": str(methodic_progress_path) if methodic_progress_path else None,
    }
    result["parameter_context"] = parameter_context(params, index_params, external)
    result["evidence_used"] = [
        {"type": "index", "value": {"available": bool(index), "parameter_count": len(index_params), "messages": sorted((index.get("messages") or {}).keys())[:80]}},
        {"type": "external_params", "value": {"available": bool(external), "parameter_count": len(external), "path": str(params_path) if params_path else None}},
        {"type": "methodic_progress", "value": summarize_progress(progress)},
    ]

    logging = audit_logging(params)
    failsafes = audit_failsafes(params, progress)
    arming = audit_arming(params)
    filters = audit_filters(params, progress)
    prior = audit_prior_methodic_progress(progress)
    mode_rc = audit_mode_rc(params)
    battery = audit_battery(params)
    compass_gps = audit_compass_gps(params, progress)
    conflicts = audit_param_conflicts(index_params, external)

    result["productive_config_audit"] = {
        "logging": logging,
        "failsafes": failsafes,
        "arming": arming,
        "filters": filters,
        "prior_methodic_progress": prior,
        "mode_mapping_and_rc": mode_rc,
        "battery_monitor": battery,
        "compass_gps_yaw_source": compass_gps,
        "parameter_conflicts": conflicts,
    }
    result["blocking_items"] = collect_blockers(result["productive_config_audit"])
    result["warnings"] = collect_warnings(result["productive_config_audit"])
    result["cleanup_actions"] = cleanup_actions(logging)
    result["remaining_validation_actions"] = remaining_validation_actions(result["productive_config_audit"], progress)
    result["missing_evidence"] = missing_evidence(index_path, params_path, methodic_progress_path, index, params, progress)
    result["findings"] = findings(result)
    result["checked_but_not_supported"] = checked_but_not_supported(index, params, progress)
    result["productive_config_status"], result["result"], result["safety_gate"] = classify_result(result)
    result["recommended_next_steps"] = recommended_next_steps(result)
    result["what_not_to_do"] = what_not_to_do()
    result["confidence_limits"] = confidence_limits(result)
    return result


def empty_result() -> dict[str, Any]:
    return {
        "methodic_step": "13",
        "title": "Productive configuration check",
        "official_reference": {"url": METHODIC_13_URL, "supporting_references": [LOGGING_REFERENCE]},
        "productive_config_status": "inconclusive",
        "productive_config_audit": {},
        "blocking_items": [],
        "warnings": [],
        "cleanup_actions": [],
        "remaining_validation_actions": [],
        "result": "inconclusive",
        "safety_gate": "repeat_step",
        "evidence_used": [],
        "missing_evidence": [],
        "manual_observations_required": [
            "All applicable prior Methodic steps were reviewed by the agent",
            "Normal preflight and operational validation process is defined",
            "Failsafe behaviours are verified for the intended operating environment",
        ],
        "analysis_window": {"selection": "final_configuration_audit"},
        "findings": [],
        "checked_but_not_supported": [],
        "parameter_context": {"relevant_parameters": RELEVANT_PARAMETERS, "present": {}, "missing_or_not_logged": []},
        "plots": [],
        "recommended_next_steps": [],
        "what_not_to_do": [],
        "next_methodic_step": None,
        "confidence_limits": [],
    }


def load_index(path: str | Path | None) -> dict[str, Any]:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        raise AnalysisError(f"Index JSON not found: {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def parse_param_file(path: str | Path | None) -> dict[str, float]:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        raise AnalysisError(f"Parameter file not found: {p}")
    out: dict[str, float] = {}
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or text.startswith(";"):
            continue
        text = text.replace(",", " ")
        parts = [part for part in text.split() if part]
        if len(parts) < 2:
            continue
        value = safe_float(parts[1])
        if value is not None:
            out[parts[0]] = float(value)
    return out


def load_progress(path: str | Path | None) -> dict[str, Any]:
    if not path:
        return {"available": False, "steps": {}, "raw": None}
    p = Path(path)
    if not p.exists():
        raise AnalysisError(f"Methodic progress JSON not found: {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    return {"available": True, "steps": normalize_progress_steps(data), "raw": data}


def normalize_progress_steps(data: Any) -> dict[str, dict[str, Any]]:
    steps: dict[str, dict[str, Any]] = {}
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        if isinstance(data.get("steps"), list):
            items = data["steps"]
        elif isinstance(data.get("steps"), dict):
            for step_id, value in data["steps"].items():
                if isinstance(value, dict):
                    item = dict(value)
                else:
                    item = {"result": value}
                item.setdefault("methodic_step", step_id)
                steps[str(step_id)] = item
            items = []
        else:
            items = [data]
    else:
        items = []
    for item in items:
        if not isinstance(item, dict):
            continue
        step_id = str(item.get("methodic_step") or item.get("step") or item.get("step_id") or "").strip()
        if step_id:
            steps[step_id] = item
    return steps


def parameter_context(params: dict[str, Any], index_params: dict[str, Any], external: dict[str, Any]) -> dict[str, Any]:
    present = {name: params[name] for name in RELEVANT_PARAMETERS if name in params}
    return {
        "relevant_parameters": RELEVANT_PARAMETERS,
        "present": present,
        "missing_or_not_logged": [name for name in RELEVANT_PARAMETERS if name not in params],
        "source": "external .param overrides index PARM values" if external else "index PARM values",
        "index_parameter_count": len(index_params),
        "external_parameter_count": len(external),
    }


def audit_logging(params: dict[str, Any]) -> dict[str, Any]:
    warnings = []
    blockers = []
    cleanup = []
    high_volume = {}
    for name in HIGH_VOLUME_LOGGING:
        value = safe_float(params.get(name))
        high_volume[name] = value
        if value is not None and value != 0:
            warnings.append(f"{name} is non-zero; confirm diagnostic logging is still intentionally needed.")
            cleanup.append(f"Review and normally disable diagnostic high-volume logging parameter {name} for everyday use.")
    log_bitmask = safe_float(params.get("LOG_BITMASK"))
    if log_bitmask is None:
        warnings.append("LOG_BITMASK is missing; normal post-flight logging adequacy could not be checked.")
    elif log_bitmask <= 0:
        blockers.append("LOG_BITMASK appears disabled; normal post-flight logging is not adequate.")
    return {"high_volume_logging": high_volume, "normal_logging": {"LOG_BITMASK": log_bitmask}, "blockers": blockers, "warnings": warnings, "cleanup_actions": cleanup}


def audit_failsafes(params: dict[str, Any], progress: dict[str, Any]) -> dict[str, Any]:
    blockers = []
    warnings = []
    battery_fs = first_numeric(params, ["FS_BATT_ENABLE", "BATT_FS_LOW_ACT", "BATT_FS_CRT_ACT"])
    if battery_fs is None:
        warnings.append("Battery failsafe parameter evidence is missing.")
    elif battery_fs <= 0:
        blockers.append("Battery failsafe action appears disabled.")
    rc_fs = first_numeric(params, ["FS_THR_ENABLE"])
    if rc_fs is None:
        warnings.append("RC/throttle failsafe parameter evidence is missing.")
    elif rc_fs <= 0:
        blockers.append("RC/throttle failsafe appears disabled.")
    ekf_fs = first_numeric(params, ["FS_EKF_ACTION"])
    if ekf_fs is None:
        warnings.append("EKF failsafe parameter evidence is missing.")
    elif ekf_fs <= 0:
        blockers.append("EKF failsafe action appears disabled.")
    guided_relevant = progress_step_present(progress, "12.2") or safe_float(params.get("GUID_OPTIONS")) is not None
    gcs_fs = safe_float(params.get("FS_GCS_ENABLE"))
    if guided_relevant and (gcs_fs is None or gcs_fs <= 0):
        warnings.append("GCS failsafe is relevant to Guided/companion operation but is missing or disabled.")
    fence_enable = safe_float(params.get("FENCE_ENABLE"))
    fence_action = safe_float(params.get("FENCE_ACTION"))
    if fence_enable and fence_enable > 0 and (fence_action is None or fence_action <= 0):
        blockers.append("Geofence is enabled but FENCE_ACTION appears disabled or missing.")
    return {"blockers": blockers, "warnings": warnings, "parameters": {name: safe_float(params.get(name)) for name in SAFETY_PARAMETERS}}


def audit_arming(params: dict[str, Any]) -> dict[str, Any]:
    value = safe_float(params.get("ARMING_CHECK"))
    if value is None:
        return {"blockers": [], "warnings": ["ARMING_CHECK is missing; arming-check status could not be confirmed."], "ARMING_CHECK": None}
    if value == 0:
        return {"blockers": ["ARMING_CHECK is disabled."], "warnings": [], "ARMING_CHECK": value}
    return {"blockers": [], "warnings": [], "ARMING_CHECK": value}


def audit_filters(params: dict[str, Any], progress: dict[str, Any]) -> dict[str, Any]:
    blockers = []
    warnings = []
    hntch = safe_float(params.get("INS_HNTCH_ENABLE"))
    step_81 = progress_step(progress, "8.1")
    if hntch is None:
        warnings.append("INS_HNTCH_ENABLE missing; notch/filter active state could not be confirmed.")
    elif hntch <= 0:
        warnings.append("INS_HNTCH_ENABLE is disabled; confirm Methodic notch/filter review supports this for the vehicle.")
    if step_81 and progress_result(step_81) not in ACCEPTED_RESULTS:
        blockers.append("Methodic 8.1 notch/filter review is not passed/caveated.")
    elif not step_81:
        warnings.append("Methodic 8.1 notch/filter validation result is missing from progress evidence.")
    return {"blockers": blockers, "warnings": warnings, "parameters": {name: safe_float(params.get(name)) for name in FILTER_PARAMETERS}, "methodic_8_1": step_81}


def audit_prior_methodic_progress(progress: dict[str, Any]) -> dict[str, Any]:
    blockers = []
    warnings = []
    steps = progress.get("steps") or {}
    if not progress.get("available"):
        return {"available": False, "blockers": [], "warnings": ["Methodic progress JSON is missing."], "steps": {}}
    for step_id in REQUIRED_PROGRESS_STEPS:
        step = steps.get(step_id)
        if not step:
            warnings.append(f"Required Methodic prerequisite {step_id} is missing from progress evidence.")
            continue
        res = progress_result(step)
        if res in BLOCKING_RESULTS:
            blockers.append(f"Methodic prerequisite {step_id} has blocking result {res}.")
        elif res not in ACCEPTED_RESULTS:
            warnings.append(f"Methodic prerequisite {step_id} result is {res}; agent review is required.")
    for step_id in OPTIONAL_PROGRESS_STEPS:
        step = steps.get(step_id)
        if step and progress_result(step) in BLOCKING_RESULTS:
            blockers.append(f"Applicable optional Methodic step {step_id} has blocking result {progress_result(step)}.")
    return {"available": True, "blockers": blockers, "warnings": warnings, "steps": {k: progress_result(v) for k, v in sorted(steps.items())}}


def audit_mode_rc(params: dict[str, Any]) -> dict[str, Any]:
    warnings = []
    blockers = []
    mode_params = {k: params.get(k) for k in sorted(params) if k.startswith("FLTMODE") or k.startswith("MODE")}
    if not mode_params:
        warnings.append("Flight mode mapping parameters are missing; mode mapping plausibility could not be checked.")
    rcmap = {name: safe_float(params.get(name)) for name in ("RCMAP_ROLL", "RCMAP_PITCH", "RCMAP_THROTTLE", "RCMAP_YAW")}
    if any(value is None for value in rcmap.values()):
        warnings.append("RCMAP_* parameters are incomplete; RC mapping was not fully checked.")
    return {"blockers": blockers, "warnings": warnings, "mode_parameters": mode_params, "rcmap": rcmap}


def audit_battery(params: dict[str, Any]) -> dict[str, Any]:
    blockers = []
    warnings = []
    monitor = safe_float(params.get("BATT_MONITOR"))
    if monitor is None:
        warnings.append("BATT_MONITOR is missing.")
    elif monitor <= 0:
        blockers.append("Battery monitor appears disabled.")
    return {"blockers": blockers, "warnings": warnings, "BATT_MONITOR": monitor}


def audit_compass_gps(params: dict[str, Any], progress: dict[str, Any]) -> dict[str, Any]:
    blockers = []
    warnings = []
    for step_id, label in (("9.1", "MagFit/compass"), ("8.4", "EKF altitude-source")):
        step = progress_step(progress, step_id)
        if step and progress_result(step) in BLOCKING_RESULTS:
            blockers.append(f"{label} Methodic evidence has blocking result {progress_result(step)}.")
    yaw_source = safe_float(params.get("EK3_SRC1_YAW"))
    gps_type = safe_float(params.get("GPS_TYPE"))
    if yaw_source is None:
        warnings.append("EK3_SRC1_YAW missing; yaw-source context could not be checked.")
    if gps_type is None:
        warnings.append("GPS_TYPE missing; GPS configuration context could not be checked.")
    return {"blockers": blockers, "warnings": warnings, "EK3_SRC1_YAW": yaw_source, "GPS_TYPE": gps_type, "GPS_TYPE2": safe_float(params.get("GPS_TYPE2"))}


def audit_param_conflicts(index_params: dict[str, Any], external: dict[str, Any]) -> dict[str, Any]:
    conflicts = []
    for name, ext_val in sorted(external.items()):
        idx_val = safe_float(index_params.get(name))
        ext = safe_float(ext_val)
        if idx_val is not None and ext is not None and abs(idx_val - ext) > max(1e-6, abs(idx_val) * 1e-6):
            conflicts.append({"parameter": name, "index_value": idx_val, "external_param_value": ext})
    return {"available": bool(index_params and external), "conflicts": conflicts[:200], "conflict_count": len(conflicts)}


def collect_blockers(audit: dict[str, Any]) -> list[str]:
    blockers = []
    for value in audit.values():
        if isinstance(value, dict):
            blockers.extend(value.get("blockers") or [])
    conflicts = audit.get("parameter_conflicts", {})
    if conflicts.get("conflict_count"):
        blockers.append("External .param values conflict with logged/index parameter values; resolve configuration provenance before final audit.")
    return blockers


def collect_warnings(audit: dict[str, Any]) -> list[str]:
    warnings = []
    for value in audit.values():
        if isinstance(value, dict):
            warnings.extend(value.get("warnings") or [])
    return warnings


def cleanup_actions(logging: dict[str, Any]) -> list[str]:
    actions = list(logging.get("cleanup_actions") or [])
    if not actions:
        actions.append("No high-volume diagnostic logging cleanup was flagged by this audit.")
    return actions


def remaining_validation_actions(audit: dict[str, Any], progress: dict[str, Any]) -> list[str]:
    actions = []
    prior = audit.get("prior_methodic_progress", {})
    if not progress.get("available"):
        actions.append("Provide Methodic progress evidence before treating Step 13 as complete.")
    for warning in prior.get("warnings") or []:
        actions.append(f"Review Methodic prerequisite: {warning}")
    if audit.get("parameter_conflicts", {}).get("conflict_count"):
        actions.append("Resolve whether the log index or external .param file represents the intended final configuration.")
    actions.append("Run normal preflight and operational validation outside this script; this audit does not declare flight safety.")
    return actions


def missing_evidence(index_path: Any, params_path: Any, progress_path: Any, index: dict[str, Any], params: dict[str, Any], progress: dict[str, Any]) -> list[str]:
    missing = []
    if not index_path:
        missing.append("Missing input: --index out/index.json")
    elif not index:
        missing.append("Index JSON is empty or unreadable.")
    if not params_path:
        missing.append("Missing input: --params vehicle.param")
    if not progress_path or not progress.get("available"):
        missing.append("Missing input: --methodic-progress out/methodic_progress.json")
    for name in ("ARMING_CHECK", "BATT_MONITOR", "LOG_BITMASK"):
        if name not in params:
            missing.append(f"Missing parameter evidence: {name}")
    return missing


def findings(result: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for item in result["blocking_items"]:
        out.append({"severity": "critical", "finding": item, "safety_gate": "do_not_proceed"})
    for item in result["warnings"]:
        out.append({"severity": "warning", "finding": item})
    for item in result["cleanup_actions"]:
        if "No high-volume" not in item:
            out.append({"severity": "info", "finding": item})
    if not out:
        out.append({"severity": "info", "finding": "No final-configuration blocker was found by the deterministic audit."})
    return out


def checked_but_not_supported(index: dict[str, Any], params: dict[str, Any], progress: dict[str, Any]) -> list[str]:
    checked = []
    if not index.get("messages"):
        checked.append("Index message inventory was unavailable; logging adequacy was checked from parameters only.")
    if not progress.get("available"):
        checked.append("Prior Methodic result consistency could not be checked because progress JSON was missing.")
    if not any(k.startswith("FLTMODE") for k in params):
        checked.append("Flight mode mapping was not fully checked because FLTMODE* parameters were missing.")
    return checked


def classify_result(result: dict[str, Any]) -> tuple[str, str, str]:
    if result["blocking_items"]:
        return "not_ready", "not_ready", "do_not_proceed"
    if not result.get("productive_config_audit", {}).get("prior_methodic_progress", {}).get("available"):
        return "inconclusive", "inconclusive", "repeat_step"
    if any(item.startswith("Missing input") for item in result.get("missing_evidence", [])):
        return "inconclusive", "inconclusive", "repeat_step"
    return "ready_for_operational_checks", "ready_for_operational_checks", "proceed_with_caution"


def recommended_next_steps(result: dict[str, Any]) -> list[str]:
    if result["productive_config_status"] == "ready_for_operational_checks":
        return [
            "Inspect warnings, cleanup actions, and remaining validation actions before treating Step 13 as complete.",
            "Use this as readiness for normal preflight and operational validation only, not as a flight-safety declaration.",
            "Keep normal logging adequate and disable diagnostic high-volume logging unless it is intentionally needed.",
        ]
    if result["productive_config_status"] == "not_ready":
        return [
            "Resolve all blocking configuration items before everyday-use configuration is accepted.",
            "Re-run the productive configuration check after safety checks, failsafes, logging, and Methodic prerequisite evidence are corrected.",
            "Do not continue to operational validation while safety checks/failsafes are disabled or parameter provenance conflicts remain.",
        ]
    return [
        "Provide index JSON, final .param file, and Methodic progress JSON before classifying productive configuration.",
        "Resolve missing parameter evidence and repeat this audit.",
        "Do not infer final configuration readiness from partial evidence.",
    ]


def what_not_to_do() -> list[str]:
    return [
        "Do not output or imply a flight-safety signoff from this audit.",
        "Do not disable ARMING_CHECK, EKF checks, GPS checks, battery failsafes, RC/GCS failsafes, or geofence protections to clear the audit.",
        "Do not leave high-volume diagnostic logging enabled for everyday use unless it is intentionally required and storage/dropout risk is understood.",
        "Do not treat passed Methodic scripts as final truth; the agent must inspect evidence and document caveats.",
    ]


def confidence_limits(result: dict[str, Any]) -> list[str]:
    limits = list(result.get("missing_evidence") or [])
    limits.append("Configuration audit cannot prove airworthiness or flight safety.")
    if result["warnings"]:
        limits.append("Warnings require agent review before final Methodic conclusions.")
    return limits


def summarize_progress(progress: dict[str, Any]) -> dict[str, Any]:
    steps = progress.get("steps") or {}
    return {"available": progress.get("available", False), "step_count": len(steps), "results": {k: progress_result(v) for k, v in sorted(steps.items())}}


def progress_step(progress: dict[str, Any], step_id: str) -> dict[str, Any] | None:
    return (progress.get("steps") or {}).get(step_id)


def progress_step_present(progress: dict[str, Any], step_id: str) -> bool:
    return progress_step(progress, step_id) is not None


def progress_result(step: dict[str, Any]) -> str:
    return str(step.get("productive_config_status") or step.get("result") or step.get("status") or "unknown")


def first_numeric(params: dict[str, Any], names: list[str]) -> float | None:
    for name in names:
        value = safe_float(params.get(name))
        if value is not None and math.isfinite(value):
            return value
    return None


def write_summary(path: Path, result: dict[str, Any]) -> None:
    ensure_dir(path.parent)
    lines = [
        f"# Methodic {result['methodic_step']}: {result['title']}",
        "",
        f"- Productive configuration status: `{result['productive_config_status']}`",
        f"- Result: `{result['result']}`",
        f"- Safety gate: `{result['safety_gate']}`",
        "",
        "## Blocking Items",
    ]
    lines.extend(f"- {item}" for item in result["blocking_items"]) if result["blocking_items"] else lines.append("- None reported by the audit.")
    lines.extend(["", "## Warnings"])
    lines.extend(f"- {item}" for item in result["warnings"]) if result["warnings"] else lines.append("- None reported by the audit.")
    lines.extend(["", "## Cleanup Actions"])
    lines.extend(f"- {item}" for item in result["cleanup_actions"])
    lines.extend(["", "## Remaining Validation Actions"])
    lines.extend(f"- {item}" for item in result["remaining_validation_actions"])
    lines.extend(["", "## What Not To Do"])
    lines.extend(f"- {item}" for item in result["what_not_to_do"])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Methodic 13 productive-configuration audit without declaring flight safety.")
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--params", type=Path, required=True)
    parser.add_argument("--methodic-progress", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args()
    result = analyze_productive_config_check(index_path=args.index, params_path=args.params, methodic_progress_path=args.methodic_progress)
    write_json(args.out, result)
    if args.summary:
        write_summary(args.summary, result)
    else:
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
