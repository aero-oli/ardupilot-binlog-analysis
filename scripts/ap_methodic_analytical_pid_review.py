#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ap_common import AnalysisError, collect_dataflash, ensure_dir, numeric_series, safe_float, write_json
from ap_methodic_711_motor_oscillation import analyze_motor_outputs, analyze_vibration, summarize_values
from ap_methodic_oscillation import classify_oscillation

METHODIC_112_URL = "https://ardupilot.github.io/MethodicConfigurator/TUNING_GUIDE_ArduCopter#112-analytical-multicopter-flight-controller-pid-optimization"
METHODIC_111_URL = "https://ardupilot.github.io/MethodicConfigurator/TUNING_GUIDE_ArduCopter#111-system-identification-flights"
LOG_MESSAGES_URL = "https://ardupilot.org/copter/docs/logmessages.html"
VALIDATION_MESSAGES = ["RATE", "ATT", "PIDR", "PIDP", "PIDY", "RCOU", "RCO2", "RCO3", "VIBE", "BAT", "POWR", "MODE", "PARM", "MSG", "EV", "ERR", "ARM"]
TUNING_PREFIXES = ("ATC_RAT_", "ATC_ANG_", "ATC_ACCEL_")
RELEVANT_PARAMETERS = [
    "ATC_RAT_RLL_P",
    "ATC_RAT_RLL_I",
    "ATC_RAT_RLL_D",
    "ATC_RAT_RLL_FF",
    "ATC_RAT_RLL_D_FF",
    "ATC_RAT_PIT_P",
    "ATC_RAT_PIT_I",
    "ATC_RAT_PIT_D",
    "ATC_RAT_PIT_FF",
    "ATC_RAT_PIT_D_FF",
    "ATC_RAT_YAW_P",
    "ATC_RAT_YAW_I",
    "ATC_RAT_YAW_D",
    "ATC_RAT_YAW_FF",
    "ATC_RAT_YAW_D_FF",
    "ATC_ANG_RLL_P",
    "ATC_ANG_PIT_P",
    "ATC_ANG_YAW_P",
    "ATC_ACCEL_R_MAX",
    "ATC_ACCEL_P_MAX",
    "ATC_ACCEL_Y_MAX",
]


def rows_to_tables(rows_by_message: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    try:
        import pandas as pd
    except Exception as exc:
        raise AnalysisError("pandas is required for Methodic analytical PID review. Install dependencies with: pip install -r requirements.txt") from exc
    return {name: pd.DataFrame(rows) for name, rows in rows_by_message.items() if rows}


def analyze_analytical_pid_review(
    *,
    sysid_path: str | Path | None,
    proposed_params_path: str | Path | None,
    before_log: str | Path | None = None,
    after_log: str | Path | None = None,
    constraints_path: str | Path | None = None,
) -> dict[str, Any]:
    sysid = load_json_file(sysid_path) if sysid_path else {}
    proposed = parse_param_file(proposed_params_path)
    constraints = load_constraints(constraints_path)
    before_params, before_context = load_log_params(before_log)
    baseline = baseline_params(sysid, before_params)
    proposed_changes = analyze_proposed_changes(proposed, baseline, constraints)
    sysid_valid = analyze_sysid_inputs(sysid)
    conflicts = analyze_evidence_conflicts(sysid, proposed_changes)
    rollback = analyze_rollback(baseline, proposed_changes)
    validation = analyze_validation_log(after_log)

    result = empty_result()
    result["optimization_inputs_valid"] = sysid_valid
    result["proposed_param_changes"] = proposed_changes
    result["risk_flags"] = risk_flags(sysid_valid, proposed_changes, conflicts, rollback, validation)
    result["validation_required"] = True
    result["evidence_conflicts"] = conflicts
    result["rollback_values"] = rollback
    result["validation_log_review"] = validation
    result["analysis_window"] = {
        "selection": "analytical_pid_review_inputs",
        "sysid_path": str(sysid_path) if sysid_path else None,
        "proposed_params_path": str(proposed_params_path) if proposed_params_path else None,
        "before_log": str(before_log) if before_log else None,
        "after_log": str(after_log) if after_log else None,
        "constraints_path": str(constraints_path) if constraints_path else None,
        "before_log_context": before_context,
    }
    result["parameter_context"] = {
        "relevant_parameters": RELEVANT_PARAMETERS,
        "present": {name: baseline[name] for name in sorted(baseline) if is_relevant_tuning_param(name)},
        "missing_or_not_logged": [name for name in RELEVANT_PARAMETERS if name not in baseline],
        "source": "before log PARM messages and System ID review parameter_context",
    }
    result["missing_evidence"] = missing_evidence(sysid_path, proposed_params_path, sysid_valid, proposed_changes, validation)
    result["evidence_used"] = [
        {"type": "system_id_review", "value": trim_sysid(sysid)},
        {"type": "proposed_parameters", "value": {"path": str(proposed_params_path) if proposed_params_path else None, "count": len(proposed)}},
        {"type": "proposed_param_changes", "value": proposed_changes},
        {"type": "constraints", "value": constraints},
        {"type": "validation_log_review", "value": validation},
        {"type": "rollback_values", "value": rollback},
    ]
    result["findings"] = findings(result)
    result["checked_but_not_supported"] = checked_but_not_supported(sysid_path, proposed_params_path, before_log, after_log, constraints_path, result)
    result["result"], result["safety_gate"] = classify_result(result)
    result["recommended_next_steps"] = recommended_next_steps(result)
    result["what_not_to_do"] = what_not_to_do()
    result["next_methodic_step"] = "12.1" if result["result"] == "ready_for_careful_test" else None
    result["confidence_limits"] = confidence_limits(result)
    return result


def empty_result() -> dict[str, Any]:
    return {
        "methodic_step": "11.2",
        "title": "Analytical PID optimisation review",
        "official_reference": {"url": METHODIC_112_URL, "supporting_urls": [METHODIC_111_URL, LOG_MESSAGES_URL]},
        "optimization_inputs_valid": {"valid": False, "reasons": []},
        "proposed_param_changes": {"available": False, "changes": [], "summary": {}},
        "risk_flags": [],
        "validation_required": True,
        "evidence_conflicts": [],
        "rollback_values": {"available": False, "values": {}, "missing": []},
        "validation_log_review": {},
        "result": "inconclusive",
        "safety_gate": "repeat_step",
        "evidence_used": [],
        "missing_evidence": [],
        "manual_observations_required": [
            "Controlled validation-flight plan exists before applying proposed values externally",
            "Rollback parameter values are available before any external application",
            "Pilot reports no dangerous oscillation, saturation, or handling issue after validation",
        ],
        "analysis_window": {"selection": "analytical_pid_review_inputs"},
        "findings": [],
        "checked_but_not_supported": [],
        "parameter_context": {"relevant_parameters": RELEVANT_PARAMETERS, "present": {}, "missing_or_not_logged": []},
        "plots": [],
        "recommended_next_steps": [],
        "what_not_to_do": [],
        "next_methodic_step": None,
        "confidence_limits": [],
    }


def load_json_file(path: str | Path | None) -> dict[str, Any]:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        raise AnalysisError(f"System ID review JSON not found: {p}")
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


def load_constraints(path: str | Path | None) -> dict[str, Any]:
    if not path:
        return {"available": False, "constraints": {}, "notes": ["No constraints file supplied; stability margins must be documented externally."]}
    p = Path(path)
    if not p.exists():
        raise AnalysisError(f"Constraints file not found: {p}")
    text = p.read_text(encoding="utf-8", errors="ignore")
    if p.suffix.lower() == ".json":
        data = json.loads(text)
        return {"available": True, "constraints": data, "notes": []}
    constraints: dict[str, Any] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        constraints[key.strip()] = value.strip()
    return {"available": True, "constraints": constraints, "notes": []}


def load_log_params(log_path: str | Path | None) -> tuple[dict[str, float], dict[str, Any]]:
    if not log_path:
        return {}, {"available": False}
    rows, index, stats = collect_dataflash(log_path, include=["PARM"], source=str(log_path))
    params = {name: float(value) for name, value in (index.get("parameters") or {}).items() if safe_float(value) is not None}
    return params, {"available": True, "parameter_count": len(params), "parser_stats": stats, "parm_rows_collected": len(rows.get("PARM", []))}


def baseline_params(sysid: dict[str, Any], before_params: dict[str, float]) -> dict[str, float]:
    baseline: dict[str, float] = {}
    context = sysid.get("parameter_context") or {}
    for name, value in (context.get("present") or {}).items():
        numeric = safe_float(value)
        if numeric is not None:
            baseline[name] = float(numeric)
    baseline.update(before_params)
    return baseline


def analyze_sysid_inputs(sysid: dict[str, Any]) -> dict[str, Any]:
    reasons = []
    if not sysid:
        return {"valid": False, "quality": None, "result": None, "frequency_response_ready": False, "reasons": ["System ID review output missing."]}
    result = sysid.get("result")
    quality = sysid.get("sysid_data_quality")
    ready = bool(sysid.get("frequency_response_ready"))
    if result != "ready_for_model":
        reasons.append(f"System ID result is {result!r}, not 'ready_for_model'.")
    if quality != "good":
        reasons.append(f"System ID data quality is {quality!r}, not 'good'.")
    if not ready:
        reasons.append("System ID review did not mark frequency_response_ready true.")
    axis = ((sysid.get("axis") or {}).get("axis"))
    if not axis or axis == "unknown":
        reasons.append("System ID axis is unknown.")
    return {"valid": not reasons, "quality": quality, "result": result, "frequency_response_ready": ready, "axis": axis, "reasons": reasons}


def analyze_proposed_changes(proposed: dict[str, float], baseline: dict[str, float], constraints: dict[str, Any]) -> dict[str, Any]:
    changes = []
    for name, proposed_value in sorted(proposed.items()):
        if not is_relevant_tuning_param(name):
            continue
        before = baseline.get(name)
        ratio = None
        pct = None
        if before is not None and abs(before) > 1e-9:
            ratio = proposed_value / before
            pct = 100.0 * (proposed_value - before) / abs(before)
        flags = param_risk_flags(name, proposed_value, before, constraints)
        changes.append({
            "name": name,
            "before": before,
            "proposed": proposed_value,
            "delta": None if before is None else proposed_value - before,
            "ratio": ratio,
            "percent_change": pct,
            "risk_flags": flags,
        })
    changed = [item for item in changes if item["before"] is None or abs(item["proposed"] - item["before"]) > 1e-9]
    return {
        "available": bool(proposed),
        "changes": changed,
        "unchanged_relevant_count": len(changes) - len(changed),
        "summary": {
            "proposed_relevant_count": len(changes),
            "changed_relevant_count": len(changed),
            "extreme_change_count": sum(1 for item in changed if any(flag["type"] == "extreme_relative_change" for flag in item["risk_flags"])),
            "out_of_range_count": sum(1 for item in changed if any(flag["type"] in {"expected_range", "constraint_range"} for flag in item["risk_flags"])),
            "missing_baseline_count": sum(1 for item in changed if item["before"] is None),
        },
    }


def param_risk_flags(name: str, proposed: float, before: float | None, constraints: dict[str, Any]) -> list[dict[str, Any]]:
    flags: list[dict[str, Any]] = []
    expected = expected_range(name)
    if expected and not (expected[0] <= proposed <= expected[1]):
        flags.append({"type": "expected_range", "severity": "high", "message": f"{name} proposed value is outside conservative expected range {expected}.", "range": expected})
    if before is not None and abs(before) > 1e-9:
        ratio = proposed / before
        if ratio >= 2.0 or ratio <= 0.5:
            flags.append({"type": "extreme_relative_change", "severity": "high", "message": f"{name} changes by more than 2x or below half of the prior value.", "ratio": ratio})
        elif ratio >= 1.5 or ratio <= 0.67:
            flags.append({"type": "large_relative_change", "severity": "medium", "message": f"{name} changes by more than 50% relative to the prior value.", "ratio": ratio})
    elif before is None:
        flags.append({"type": "missing_baseline", "severity": "medium", "message": f"{name} has no logged rollback/baseline value for relative-risk review."})
    cflag = constraint_flag(name, proposed, constraints)
    if cflag:
        flags.append(cflag)
    return flags


def expected_range(name: str) -> tuple[float, float] | None:
    if name.endswith("_D"):
        return (0.0, 0.2)
    if name.endswith("_D_FF"):
        return (0.0, 0.2)
    if name.endswith("_FF"):
        return (0.0, 1.5)
    if name.startswith("ATC_RAT_") and (name.endswith("_P") or name.endswith("_I")):
        return (0.0, 1.5)
    if name.startswith("ATC_ANG_") and name.endswith("_P"):
        return (1.0, 36.0)
    if name.startswith("ATC_ACCEL_") and name.endswith("_MAX"):
        return (1000.0, 720000.0)
    return None


def constraint_flag(name: str, proposed: float, constraints: dict[str, Any]) -> dict[str, Any] | None:
    data = constraints.get("constraints") or {}
    item = data.get(name)
    if not item:
        return None
    min_v = max_v = None
    if isinstance(item, dict):
        min_v = safe_float(item.get("min"))
        max_v = safe_float(item.get("max"))
    if min_v is None and max_v is None:
        return None
    if (min_v is not None and proposed < min_v) or (max_v is not None and proposed > max_v):
        return {"type": "constraint_range", "severity": "high", "message": f"{name} violates supplied constraints.", "min": min_v, "max": max_v}
    return None


def analyze_evidence_conflicts(sysid: dict[str, Any], proposed_changes: dict[str, Any]) -> list[dict[str, Any]]:
    conflicts = []
    for source in ("quicktune_review", "autotune_review", "tune_evaluation"):
        review = sysid.get(source) or {}
        if review and review.get("result") in {"fail", "reduce_gains", "improve_filters", "inconclusive"}:
            conflicts.append({"source": source, "severity": "medium", "message": f"{source} result is {review.get('result')}; proposed analytical PID output needs manual reconciliation."})
    if proposed_changes["summary"].get("extreme_change_count", 0) > 0:
        conflicts.append({"source": "proposed_params", "severity": "high", "message": "One or more proposed gains changed by an extreme relative amount."})
    return conflicts


def analyze_rollback(baseline: dict[str, float], proposed_changes: dict[str, Any]) -> dict[str, Any]:
    values = {}
    missing = []
    for change in proposed_changes.get("changes", []):
        name = change["name"]
        if change.get("before") is None:
            missing.append(name)
        else:
            values[name] = change["before"]
    return {"available": bool(values) and not missing, "values": values, "missing": missing}


def analyze_validation_log(after_log: str | Path | None) -> dict[str, Any]:
    if not after_log:
        return {"available": False, "required": True, "blockers": [], "warnings": ["No validation log supplied; a controlled validation flight is still required before accepting analytical outputs."]}
    rows, index, stats = collect_dataflash(after_log, include=VALIDATION_MESSAGES, source=str(after_log))
    tables = rows_to_tables(rows)
    params = index.get("parameters", {}) or {}
    rate = analyze_rate_outputs(tables)
    motor = analyze_motor_outputs(tables, None, params)
    vibe = analyze_vibration(tables, None)
    blockers = validation_blockers(rate, motor, vibe)
    return {
        "available": True,
        "parser_stats": stats,
        "messages_present": sorted(tables.keys()),
        "rate_outputs": rate,
        "motor_outputs": summarize_motor_for_validation(motor),
        "vibration": vibe,
        "blockers": blockers,
        "warnings": [] if not blockers else ["Validation log contains blockers; do not accept the proposed analytical PID output as-is."],
    }


def analyze_rate_outputs(tables: dict[str, Any]) -> dict[str, Any]:
    rate = tables.get("RATE")
    if rate is None or len(rate) == 0:
        return {"available": False, "axes": {}, "warning": "RATE missing from validation log."}
    times = time_values(rate)
    axes = {}
    for axis, field in {"roll": "ROut", "pitch": "POut", "yaw": "YOut"}.items():
        values = series_values(rate, field)
        if not values:
            axes[axis] = {"available": False}
            continue
        osc = classify_oscillation(values, times[: len(values)], threshold=0.15, min_samples=20, min_duration_s=1.0)
        axes[axis] = {
            **summarize_values(values, threshold=0.15),
            "classification": osc.get("classification"),
            "classification_reason": osc.get("reason", []),
            "highpass_p95_abs": (osc.get("metrics") or {}).get("highpass_residual_p95_abs"),
        }
    return {"available": True, "axes": axes}


def validation_blockers(rate: dict[str, Any], motor: dict[str, Any], vibe: dict[str, Any]) -> list[dict[str, Any]]:
    blockers = []
    for axis, data in (rate.get("axes") or {}).items():
        if data.get("classification") in {"oscillatory", "mixed"} and (data.get("p95_abs") or 0.0) > 0.15:
            blockers.append({"type": "oscillation", "axis": axis, "severity": "high", "evidence": data})
        elif (data.get("p95_abs") or 0.0) > 0.25:
            blockers.append({"type": "high_rate_output", "axis": axis, "severity": "medium", "evidence": data})
    saturated = [
        name for name, data in (motor.get("channels") or {}).items()
        if (data.get("pct_high_ge_1900") or 0.0) > 1.0 or (data.get("pct_low_le_1100") or 0.0) > 1.0 or data.get("persistent_high") or data.get("persistent_low")
    ]
    if saturated:
        blockers.append({"type": "motor_saturation", "severity": "high", "channels": saturated[:12]})
    clips = vibe.get("clip_delta") or {}
    if any((safe_float(v) or 0.0) > 0 for v in clips.values()) or (safe_float(vibe.get("p95_axis")) or 0.0) > 30.0:
        blockers.append({"type": "vibration_or_clipping", "severity": "high", "evidence": vibe})
    return blockers


def summarize_motor_for_validation(motor: dict[str, Any]) -> dict[str, Any]:
    saturated = {
        name: data
        for name, data in (motor.get("channels") or {}).items()
        if (data.get("pct_high_ge_1900") or 0.0) > 1.0 or (data.get("pct_low_le_1100") or 0.0) > 1.0 or data.get("persistent_high") or data.get("persistent_low")
    }
    return {"available": motor.get("available"), "saturated_channels": saturated, "motor_spread": motor.get("motor_spread")}


def risk_flags(sysid_valid: dict[str, Any], proposed_changes: dict[str, Any], conflicts: list[dict[str, Any]], rollback: dict[str, Any], validation: dict[str, Any]) -> list[dict[str, Any]]:
    flags = []
    if not sysid_valid.get("valid"):
        flags.append({"type": "invalid_sysid_inputs", "severity": "high", "message": "System ID inputs are not valid for analytical optimisation.", "evidence": sysid_valid})
    if not proposed_changes.get("available") or not proposed_changes.get("changes"):
        flags.append({"type": "missing_proposed_changes", "severity": "high", "message": "No relevant proposed PID/attitude parameters were found."})
    for change in proposed_changes.get("changes", []):
        for flag in change.get("risk_flags", []):
            flags.append({"type": flag["type"], "severity": flag["severity"], "message": flag["message"], "parameter": change["name"], "evidence": flag})
    flags.extend(conflicts)
    if not rollback.get("available"):
        flags.append({"type": "rollback_incomplete", "severity": "high", "message": "Rollback values are missing for one or more proposed changes.", "missing": rollback.get("missing", [])})
    if validation.get("available") and validation.get("blockers"):
        flags.append({"type": "validation_blocker", "severity": "high", "message": "Validation log shows oscillation, saturation, vibration, or clipping blockers.", "evidence": validation.get("blockers")})
    return flags


def missing_evidence(sysid_path: str | Path | None, proposed_path: str | Path | None, sysid_valid: dict[str, Any], proposed_changes: dict[str, Any], validation: dict[str, Any]) -> list[str]:
    missing = []
    if not sysid_path:
        missing.append("Missing required input: System ID review output JSON.")
    if not proposed_path:
        missing.append("Missing required input: proposed .param file.")
    if not sysid_valid.get("valid"):
        missing.extend(sysid_valid.get("reasons", []))
    if not proposed_changes.get("available"):
        missing.append("No proposed parameters were parsed.")
    if not validation.get("available"):
        missing.append("No after-log validation evidence supplied; controlled validation remains required.")
    return missing


def findings(result: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for flag in result.get("risk_flags", []):
        severity = "critical" if flag.get("severity") == "high" else "warning"
        out.append({"severity": severity, "finding": flag.get("message"), "evidence": flag})
    if not out:
        out.append({"severity": "info", "finding": "Analytical PID proposal passed deterministic input and range checks; validation is still required.", "evidence": result.get("proposed_param_changes", {}).get("summary", {})})
    return out


def classify_result(result: dict[str, Any]) -> tuple[str, str]:
    flags = result.get("risk_flags") or []
    high_types = {flag.get("type") for flag in flags if flag.get("severity") == "high"}
    if "validation_blocker" in high_types:
        return "do_not_apply", "do_not_proceed"
    if "invalid_sysid_inputs" in high_types or "missing_proposed_changes" in high_types:
        return "inconclusive", "repeat_step"
    if high_types & {"expected_range", "constraint_range", "rollback_incomplete"}:
        return "do_not_apply", "do_not_proceed"
    if high_types or any(flag.get("severity") == "medium" for flag in flags):
        return "revise_model", "repeat_step"
    return "ready_for_careful_test", "proceed_with_caution"


def recommended_next_steps(result: dict[str, Any]) -> list[str]:
    if result["result"] == "ready_for_careful_test":
        return [
            "Inspect the proposed parameter deltas and keep rollback values available before any external application.",
            "Apply proposed values only through the normal Methodic/manual workflow, then perform a short controlled validation flight.",
            "Review the validation log for oscillation, motor saturation, vibration/clipping, and tracking before accepting the analytical result.",
        ]
    if result["result"] == "do_not_apply":
        return [
            "Do not apply or keep the proposed analytical PID values as-is.",
            "Resolve the high-risk flags, restore known rollback values if anything was already applied externally, and repeat model/proposal review.",
            "If a validation log exists, address oscillation, saturation, vibration, clipping, or power blockers before further tuning.",
        ]
    if result["result"] == "revise_model":
        return [
            "Revise the analytical model or constraints before considering a validation test.",
            "Reconcile extreme or large parameter changes against System ID quality, AutoTune/QuikTune evidence, and rollback values.",
            "Do not proceed to later Methodic steps until the agent has inspected the risk flags and supporting evidence.",
        ]
    return [
        "Provide a valid Methodic 11.1 System ID review JSON and a proposed analytical PID .param file.",
        "Include before values or a before-log so rollback and relative-change risk can be checked.",
        "Do not infer readiness from an incomplete analytical PID review.",
    ]


def what_not_to_do() -> list[str]:
    return [
        "Do not auto-apply analytical PID parameters from this tool.",
        "Do not accept proposed gains without rollback values and a controlled validation plan.",
        "Do not proceed when validation evidence shows oscillation, motor saturation, vibration, clipping, or power issues.",
        "Do not generate final PID values from this review; it only audits inputs, proposed outputs, and optional validation evidence.",
    ]


def checked_but_not_supported(sysid_path: Any, proposed_path: Any, before_log: Any, after_log: Any, constraints_path: Any, result: dict[str, Any]) -> list[str]:
    checked = []
    if sysid_path:
        checked.append("System ID review JSON was checked for model-readiness fields.")
    if proposed_path:
        checked.append("Proposed parameter file was parsed and screened for tuning-relevant deltas.")
    if not before_log:
        checked.append("No before-log was supplied; rollback values came only from the System ID review parameter context if available.")
    if not constraints_path:
        checked.append("No constraints file was supplied; stability margins must be documented externally.")
    if not after_log:
        checked.append("No after-log was supplied; validation safety evidence could not be checked.")
    return checked


def confidence_limits(result: dict[str, Any]) -> list[str]:
    limits = list(result.get("missing_evidence") or [])
    if not result["validation_log_review"].get("available"):
        limits.append("A controlled validation log is required before accepting analytical PID outputs.")
    if not result["rollback_values"].get("available"):
        limits.append("Rollback coverage is incomplete.")
    return limits


def trim_sysid(sysid: dict[str, Any]) -> dict[str, Any]:
    return {
        "methodic_step": sysid.get("methodic_step"),
        "result": sysid.get("result"),
        "sysid_data_quality": sysid.get("sysid_data_quality"),
        "frequency_response_ready": sysid.get("frequency_response_ready"),
        "axis": sysid.get("axis"),
        "risk_findings": sysid.get("findings", [])[:6],
    }


def is_relevant_tuning_param(name: str) -> bool:
    return name.startswith(TUNING_PREFIXES)


def time_values(df: Any) -> list[float]:
    if df is None or len(df) == 0:
        return []
    for col, scale in (("TimeS", 1.0), ("Time", 1.0), ("TimeUS", 1e-6), ("TimeMS", 1e-3)):
        if col in df:
            series = numeric_series(df, [col])
            if series is None:
                return []
            return [float(v) * scale for v in series.dropna().tolist()]
    return [float(i) for i in range(len(df))]


def series_values(df: Any, col: str) -> list[float]:
    if df is None or col not in getattr(df, "columns", []):
        return []
    series = numeric_series(df, [col])
    if series is None:
        return []
    return [float(v) for v in series.dropna().tolist() if math.isfinite(float(v))]


def write_summary(path: Path, result: dict[str, Any]) -> None:
    ensure_dir(path.parent)
    lines = [
        f"# Methodic {result['methodic_step']}: {result['title']}",
        "",
        f"- Result: `{result['result']}`",
        f"- Safety gate: `{result['safety_gate']}`",
        f"- Optimisation inputs valid: `{result['optimization_inputs_valid'].get('valid')}`",
        f"- Proposed changes: `{result['proposed_param_changes'].get('summary', {}).get('changed_relevant_count', 0)}`",
        f"- Validation required: `{result['validation_required']}`",
        "",
        "## Risk Flags",
    ]
    for flag in result.get("risk_flags", []):
        lines.append(f"- {flag.get('severity', 'info')}: {flag.get('message')}")
    if not result.get("risk_flags"):
        lines.append("- None from deterministic checks. Validation is still required.")
    lines.extend(["", "## Recommended Next Steps"])
    for item in result.get("recommended_next_steps", []):
        lines.append(f"- {item}")
    lines.extend(["", "## What Not To Do"])
    for item in result.get("what_not_to_do", []):
        lines.append(f"- {item}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Review Methodic 11.2 analytical PID optimisation inputs and proposed outputs without applying parameters.")
    parser.add_argument("--sysid", type=Path, required=True, help="Methodic 11.1 System ID review JSON.")
    parser.add_argument("--proposed-params", type=Path, required=True, help="Proposed analytical PID .param file.")
    parser.add_argument("--before-log", type=Path, help="Optional before log for rollback/baseline parameter values.")
    parser.add_argument("--after-log", type=Path, help="Optional validation log after externally applying proposed values.")
    parser.add_argument("--constraints", type=Path, help="Optional JSON or key:value constraints file.")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args()

    result = analyze_analytical_pid_review(
        sysid_path=args.sysid,
        proposed_params_path=args.proposed_params,
        before_log=args.before_log,
        after_log=args.after_log,
        constraints_path=args.constraints,
    )
    write_json(args.out, result)
    if args.summary:
        write_summary(args.summary, result)
    else:
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
