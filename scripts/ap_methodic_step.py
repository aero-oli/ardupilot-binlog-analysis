#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean, pstdev
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ap_common import AnalysisError, ensure_dir, iter_dataflash_messages, message_to_dict, message_type, safe_float, write_json
from ap_methodic_711_motor_oscillation import analyze_motor_oscillation_711
from ap_methodic_ekf_altitude_source import analyze_ekf_altitude_source
from ap_methodic_first_flight import analyze_first_flight
from ap_methodic_notch_review import analyze_notch_review
from ap_methodic_pid_notch_review import analyze_pid_notch_review
from ap_methodic_quicktune_review import analyze_quicktune_review
from ap_methodic_throttle_controller import analyze_throttle_controller
from ap_methodic_oscillation import classify_oscillation
from ap_methodic_rc import analyze_rc_input_contamination
from ap_methodic_registry import MethodicRegistryError, get_step, load_registry
from ap_methodic_safety_gates import classify_from_findings, missing_manual_observations, normalize_manual_observations
from ap_methodic_windows import select_methodic_window

STANDARD_SCHEMA_KEYS = [
    "methodic_step",
    "title",
    "official_reference",
    "result",
    "safety_gate",
    "evidence_used",
    "missing_evidence",
    "manual_observations_required",
    "analysis_window",
    "findings",
    "checked_but_not_supported",
    "parameter_context",
    "plots",
    "recommended_next_steps",
    "what_not_to_do",
    "next_methodic_step",
    "confidence_limits",
]

STEP_IMPLEMENTATIONS = {
    "7.1": "analyze_7_1",
    "7.1.1": "analyze_7_1_1",
    "8.1": "analyze_8_1",
    "8.2": "analyze_8_2",
    "8.3": "analyze_8_3",
    "8.4": "analyze_8_4",
    "8.5": "analyze_8_5",
    "9.2": "analyze_9_2",
}


def empty_result(step: dict[str, Any]) -> dict[str, Any]:
    return {
        "methodic_step": step["step_id"],
        "title": step["title"],
        "official_reference": {
            "url": step["official_url"],
            "anchor": step.get("official_anchor"),
        },
        "result": "inconclusive",
        "safety_gate": "repeat_step",
        "evidence_used": [],
        "missing_evidence": [],
        "manual_observations_required": list(step.get("manual_observations_required") or []),
        "analysis_window": {
            "selection": "whole_log",
            "preferred_window": step.get("preferred_window"),
            "start_s": None,
            "end_s": None,
        },
        "findings": [],
        "checked_but_not_supported": [],
        "parameter_context": {
            "relevant_parameters": list(step.get("relevant_parameters") or []),
            "present": {},
            "missing_or_not_logged": [],
        },
        "plots": [],
        "recommended_next_steps": [],
        "what_not_to_do": [
            "Do not treat this script output as final truth; inspect the evidence before writing conclusions.",
            "Do not make blind gain changes.",
            "Do not skip safety gates or declare the aircraft safe to fly.",
        ],
        "next_methodic_step": None,
        "confidence_limits": [],
    }


def normalize_schema(result: dict[str, Any]) -> dict[str, Any]:
    for key in STANDARD_SCHEMA_KEYS:
        result.setdefault(key, [] if key in {"evidence_used", "missing_evidence", "findings", "checked_but_not_supported", "plots", "recommended_next_steps", "what_not_to_do", "confidence_limits"} else None)
    return {key: result[key] for key in STANDARD_SCHEMA_KEYS}


def not_implemented_result(step: dict[str, Any]) -> dict[str, Any]:
    result = empty_result(step)
    result["result"] = "inconclusive"
    result["safety_gate"] = "repeat_step"
    result["missing_evidence"] = [
        "No step-specific Methodic implementation is available yet; use the registry to choose existing generic scripts and manual review.",
        f"Required messages for this step: {', '.join(step.get('required_messages') or []) or 'none listed'}",
    ]
    result["checked_but_not_supported"] = ["step_specific_methodic_analysis"]
    result["recommended_next_steps"] = [
        "Run the existing validation, index, metrics, tuning, FFT, or diagnosis scripts that match this step's required messages.",
        "Inspect the listed required and strongly recommended evidence before deciding whether this Methodic step is complete.",
        "Record required manual observations before advancing the Methodic workflow.",
    ]
    result["next_methodic_step"] = step.get("next_step_if_conditional")
    result["confidence_limits"] = ["Registered Methodic step, but no deterministic step-specific analysis has been implemented."]
    return normalize_schema(result)


def analyze_7_1(log_path: Path, step: dict[str, Any], plots_dir: Path | None, manual_observations: list[str]) -> dict[str, Any]:
    first = analyze_first_flight(log_path, plots_dir=plots_dir)
    result = empty_result(step)
    result["result"] = first["result"]
    result["safety_gate"] = first["safety_gate"]
    result["evidence_used"] = [
        {"type": "first_flight_window", "value": first.get("first_flight_window")},
        {"type": "hover_quality", "value": first.get("hover_quality")},
        {"type": "detected", "value": first.get("detected")},
        {"type": "analysis", "value": first.get("analysis")},
    ]
    result["missing_evidence"] = first.get("missing_evidence", [])
    result["manual_observations_required"] = first.get("manual_observations_required", [])
    hover = ((first.get("first_flight_window") or {}).get("hover_selector") or {}).get("selected_window") or {}
    result["analysis_window"] = {
        "selection": "methodic_7.1_first_flight",
        "preferred_window": step.get("preferred_window"),
        "start_s": hover.get("start_s"),
        "end_s": hover.get("end_s"),
        "first_flight_window": first.get("first_flight_window"),
    }
    result["findings"] = first.get("safety_findings", [])
    result["checked_but_not_supported"] = []
    result["parameter_context"] = {
        "relevant_parameters": list(step.get("relevant_parameters") or []),
        "present": {},
        "missing_or_not_logged": [],
        "source": "see first-flight analysis payload",
    }
    result["plots"] = first.get("plots", [])
    result["recommended_next_steps"] = first.get("recommended_next_steps", [])
    result["what_not_to_do"] = first.get("what_not_to_do", result["what_not_to_do"])
    result["next_methodic_step"] = first.get("next_step")
    result["confidence_limits"] = first.get("confidence_limits", [])
    if manual_observations:
        result["evidence_used"].append({"type": "manual_observations_provided_to_dispatcher", "value": manual_observations})
    return normalize_schema(result)


def read_step_evidence(log_path: Path, wanted_messages: set[str]) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any], list[str]]:
    rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    params: dict[str, Any] = {}
    errors: list[str] = []
    if not log_path.exists():
        return rows, params, [f"Log file not found: {log_path}"]
    try:
        for msg in iter_dataflash_messages(log_path):
            mtype = message_type(msg)
            if mtype == "PARM":
                row = message_to_dict(msg)
                name = row.get("Name")
                if name:
                    params[str(name)] = row.get("Value")
            if mtype in wanted_messages:
                rows[mtype].append(message_to_dict(msg))
    except AnalysisError as exc:
        errors.append(str(exc))
    except Exception as exc:
        errors.append(f"Could not read DataFlash log: {exc}")
    return rows, params, errors


def time_value(row: dict[str, Any]) -> float | None:
    for key in ("TimeS", "TimeUS", "TimeMS"):
        value = safe_float(row.get(key))
        if value is None:
            continue
        if key == "TimeUS":
            return value / 1_000_000.0
        if key == "TimeMS":
            return value / 1000.0
        return value
    return None


def numeric_values(rows: list[dict[str, Any]], fields: list[str]) -> list[float]:
    values: list[float] = []
    for row in rows:
        for field in fields:
            value = safe_float(row.get(field))
            if value is not None:
                values.append(value)
                break
    return values


def p95_abs(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(abs(v) for v in values)
    idx = min(len(ordered) - 1, int(round((len(ordered) - 1) * 0.95)))
    return float(ordered[idx])


def summarize_axis(rows: list[dict[str, Any]], desired: str, actual: str, output: str) -> dict[str, Any]:
    outs = numeric_values(rows, [output])
    desired_values = numeric_values(rows, [desired])
    actual_values = numeric_values(rows, [actual])
    count = min(len(desired_values), len(actual_values))
    errors = [desired_values[i] - actual_values[i] for i in range(count)]
    return {
        "output_abs_p95": p95_abs(outs),
        "output_abs_max": max([abs(v) for v in outs], default=None),
        "output_stddev": float(pstdev(outs)) if len(outs) > 1 else None,
        "output_mean": float(mean(outs)) if outs else None,
        "tracking_error_p95_abs": p95_abs(errors),
        "samples": len(rows),
    }


def summarize_outputs(rows_by_message: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    summaries: dict[str, Any] = {}
    for message in ("RCOU", "RCO2", "RCO3"):
        rows = rows_by_message.get(message) or []
        if not rows:
            continue
        channel_summaries = {}
        for idx in range(1, 17):
            fields = [f"C{idx}", f"Chan{idx}", f"PWM{idx}"]
            values = numeric_values(rows, fields)
            if values:
                channel_summaries[f"C{idx}"] = {
                    "min": float(min(values)),
                    "max": float(max(values)),
                    "span": float(max(values) - min(values)),
                    "stddev": float(pstdev(values)) if len(values) > 1 else None,
                    "samples": len(values),
                }
        summaries[message] = channel_summaries
    return summaries


def filter_params(params: dict[str, Any], patterns: list[str]) -> tuple[dict[str, Any], list[str]]:
    present: dict[str, Any] = {}
    missing: list[str] = []
    for pattern in patterns:
        if "*" in pattern:
            prefix = pattern.split("*", 1)[0]
            matches = {k: v for k, v in params.items() if k.startswith(prefix)}
            if matches:
                present.update(matches)
            else:
                missing.append(pattern)
            continue
        if pattern in params:
            present[pattern] = params[pattern]
        else:
            missing.append(pattern)
    return present, missing


def analyze_7_1_1(log_path: Path, step: dict[str, Any], plots_dir: Path | None, manual_observations: list[str]) -> dict[str, Any]:
    try:
        result = analyze_motor_oscillation_711(log_path, plots_dir=plots_dir, manual_observations=manual_observations)
        return normalize_schema(result)
    except Exception as exc:
        result = empty_result(step)
        result["result"] = "inconclusive"
        result["safety_gate"] = "repeat_step"
        result["missing_evidence"] = [f"Step 7.1.1 evidence could not be read: {exc}"]
        result["checked_but_not_supported"] = ["methodic_711_motor_oscillation_analysis"]
        result["recommended_next_steps"] = [
            "Collect a readable DataFlash log with RATE, MODE, ATT, and RCOU/RCO2/RCO3 before classifying Methodic 7.1.1.",
            "Do not make gain changes from an unreadable or empty log.",
        ]
        result["confidence_limits"] = ["No deterministic 7.1.1 evidence was available because log parsing failed."]
        return normalize_schema(result)

    result = empty_result(step)
    wanted = set(step.get("required_messages", [])) | set(step.get("strongly_recommended_messages", [])) | set(step.get("optional_messages", []))
    rows_by_message, params, read_errors = read_step_evidence(log_path, wanted)
    present_messages = sorted(name for name, rows in rows_by_message.items() if rows)

    result["evidence_used"] = [{"type": "messages_present", "messages": present_messages}]
    missing_required = [name for name in step.get("required_messages", []) if not rows_by_message.get(name)]
    missing_strong = [name for name in step.get("strongly_recommended_messages", []) if not rows_by_message.get(name)]
    result["missing_evidence"] = [f"Missing required message: {name}" for name in missing_required]
    result["missing_evidence"].extend(f"Missing strongly recommended message: {name}" for name in missing_strong)
    result["missing_evidence"].extend(read_errors)

    times = [t for rows in rows_by_message.values() for row in rows for t in [time_value(row)] if t is not None]
    if times:
        result["analysis_window"]["start_s"] = float(min(times))
        result["analysis_window"]["end_s"] = float(max(times))
    else:
        result["confidence_limits"].append("No usable TimeS/TimeUS/TimeMS values found for Methodic window reporting.")

    present_params, missing_params = filter_params(params, step.get("relevant_parameters", []))
    result["parameter_context"] = {
        "relevant_parameters": list(step.get("relevant_parameters") or []),
        "present": present_params,
        "missing_or_not_logged": missing_params,
        "source": "log PARM messages" if params else "no PARM messages found",
    }

    tables = rows_to_tables(rows_by_message)
    if tables:
        try:
            window_result = select_methodic_window(tables, "methodic_hover", min_duration_s=5.0)
            result["analysis_window"]["methodic_selector"] = window_result
            if window_result.get("selected_window"):
                result["analysis_window"]["selection"] = "methodic_hover"
                result["analysis_window"]["start_s"] = window_result["selected_window"]["start_s"]
                result["analysis_window"]["end_s"] = window_result["selected_window"]["end_s"]
            result["confidence_limits"].extend(window_result.get("warnings", []))
        except Exception as exc:
            result["confidence_limits"].append(f"Methodic hover-window helper could not select a window: {exc}")
        try:
            rc_context = analyze_rc_input_contamination(tables, params, yaw_only_centered=False)
            result["evidence_used"].append({"type": "rc_input_contamination", "summary": _trim_rc_context(rc_context)})
            if rc_context.get("hands_off_confidence") == "low":
                result["confidence_limits"].append("RC input was not sufficiently centered; oscillation claims must be checked against a centered-stick subset.")
            elif not rc_context.get("available"):
                result["confidence_limits"].append("RCIN was unavailable; pilot stick contamination could not be ruled out before oscillation classification.")
        except Exception as exc:
            result["confidence_limits"].append(f"RC input contamination helper failed: {exc}")

    rate_rows = rows_by_message.get("RATE") or []
    if rate_rows:
        axes = {
            "roll": summarize_axis(rate_rows, "RDes", "R", "ROut"),
            "pitch": summarize_axis(rate_rows, "PDes", "P", "POut"),
            "yaw": summarize_axis(rate_rows, "YDes", "Y", "YOut"),
        }
        result["evidence_used"].append({"type": "rate_output_summary", "axes": axes})
        for axis, summary in axes.items():
            axis_fields = {"roll": "ROut", "pitch": "POut", "yaw": "YOut"}
            series = numeric_values(rate_rows, [axis_fields[axis]])
            series_times = [time_value(row) for row in rate_rows]
            osc = classify_oscillation(series, series_times, threshold=0.15)
            result["evidence_used"].append({"type": "oscillation_classification", "axis": axis, "classification": osc})
            if osc["classification"] == "oscillatory":
                result["findings"].append({
                    "severity": "safety-critical",
                    "finding": f"{axis} RATE output is classified as oscillatory",
                    "evidence_values": [{"name": key, "value": value, "unit": "normalized"} for key, value in osc.get("metrics", {}).items() if key in {"p95_abs", "highpass_residual_p95_abs", "sign_change_rate_hz"}],
                    "interpretation": "The RATE output has repeated high-pass sign changes and should be reviewed as a possible output oscillation only after RC stick contamination is checked.",
                    "recommended_checks": ["Inspect the Methodic hover window", "Use RC-centered subsets where available", "Check motor/ESC/frame health before further tuning"],
                })
            elif osc["classification"] == "steady_bias":
                result["findings"].append({
                    "severity": "worth-checking",
                    "finding": f"{axis} RATE output is classified as steady bias rather than oscillation",
                    "evidence_values": [{"name": "mean", "value": osc.get("metrics", {}).get("mean"), "unit": "normalized"}],
                    "interpretation": "A steady controller output can reflect trim, wind, CG, frame asymmetry, or sustained command; it should not be treated as oscillation without supporting evidence.",
                    "recommended_checks": ["Compare with RC input, attitude, wind, and output mapping before tuning gains"],
                })
            out95 = summary.get("output_abs_p95")
            outmax = summary.get("output_abs_max")
            err95 = summary.get("tracking_error_p95_abs")
            if out95 is not None and out95 > 0.18:
                result["findings"].append({
                    "severity": "safety-critical",
                    "finding": f"{axis} RATE output p95 is high for a first-flight motor-output oscillation check",
                    "evidence_values": [{"name": "output_abs_p95", "value": out95, "unit": "normalized"}],
                    "interpretation": "High controller output during the first-hover check can indicate output oscillation, excessive gains, actuator saturation, frame resonance, or a mechanical issue.",
                    "recommended_checks": ["Review RATE/PID/output plots", "Inspect motors, props, frame stiffness, and ESC health before further tuning"],
                })
            elif outmax is not None and outmax > 0.35:
                result["findings"].append({
                    "severity": "worth-checking",
                    "finding": f"{axis} RATE output reached a high peak",
                    "evidence_values": [{"name": "output_abs_max", "value": outmax, "unit": "normalized"}],
                    "interpretation": "A short peak may be maneuver or disturbance related, but should be checked against the hover window and pilot inputs.",
                    "recommended_checks": ["Inspect plot timing before accepting the step"],
                })
            elif err95 is not None and err95 > 30:
                result["findings"].append({
                    "severity": "worth-checking",
                    "finding": f"{axis} rate tracking error is elevated",
                    "evidence_values": [{"name": "tracking_error_p95_abs", "value": err95, "unit": "deg/s"}],
                    "interpretation": "Tracking error alone does not prove oscillation, but limits confidence in a clean pass.",
                    "recommended_checks": ["Compare desired vs actual rate and motor outputs in the same time window"],
                })
            else:
                result["checked_but_not_supported"].append(f"{axis} RATE output did not cross conservative first-flight warning thresholds")
    else:
        result["confidence_limits"].append("RATE message is required for step 7.1.1 and was not available.")

    output_summary = summarize_outputs(rows_by_message)
    if output_summary:
        result["evidence_used"].append({"type": "actuator_output_summary", "messages": output_summary})
        for message, channels in output_summary.items():
            for channel, summary in channels.items():
                span = summary.get("span")
                if span is not None and span > 700:
                    result["findings"].append({
                        "severity": "worth-checking",
                        "finding": f"{message}.{channel} output span is large during the analyzed log",
                        "evidence_values": [{"name": "pwm_span", "value": span, "unit": "us"}],
                        "interpretation": "Large output span may be normal maneuvering, but should be checked against hover-only timing before passing the oscillation check.",
                        "recommended_checks": ["Select the stable hover window and compare mapped motor outputs"],
                    })
    elif "RCOU" in step.get("strongly_recommended_messages", []):
        result["confidence_limits"].append("Motor output mapping/saturation confidence is limited because RCOU/RCO2/RCO3 were not available.")

    manual_missing = missing_manual_observations(step.get("manual_observations_required"), manual_observations)
    result["manual_observations_required"] = [
        {"observation": obs, "status": "provided" if obs not in manual_missing else "missing"}
        for obs in step.get("manual_observations_required", [])
    ]
    if manual_missing:
        result["missing_evidence"].extend(f"Missing manual observation: {obs}" for obs in manual_missing)
        result["confidence_limits"].append("Step 7.1.1 cannot be promoted to a clean pass without the required motor/ESC heat, sound, visible shaking, and control-feel observations.")

    if plots_dir and rate_rows:
        result["plots"].extend(make_7_1_1_plots(rate_rows, rows_by_message, plots_dir))

    result["result"], result["safety_gate"] = classify_from_findings(
        findings=result["findings"],
        missing_required=missing_required + read_errors,
        missing_manual=manual_missing,
    )
    if result["result"] == "pass":
        result["next_methodic_step"] = step.get("next_step_if_pass")
    elif result["result"] == "conditional_pass":
        result["next_methodic_step"] = step.get("next_step_if_conditional")
    else:
        result["next_methodic_step"] = step.get("next_step_if_fail")

    result["recommended_next_steps"] = next_steps_for_result(step, result)
    return normalize_schema(result)


def analyze_8_1(log_path: Path, step: dict[str, Any], plots_dir: Path | None, manual_observations: list[str]) -> dict[str, Any]:
    try:
        result = analyze_notch_review(log_path, plots_dir=plots_dir)
        if manual_observations:
            result["evidence_used"].append({"type": "manual_observations_provided_to_dispatcher", "value": manual_observations})
        return result
    except Exception as exc:
        result = empty_result(step)
        result["result"] = "inconclusive"
        result["safety_gate"] = "repeat_step"
        result["missing_evidence"] = [f"Step 8.1 notch/filter evidence could not be read: {exc}"]
        result["checked_but_not_supported"] = ["methodic_8_1_notch_review"]
        result["recommended_next_steps"] = [
            "Collect a readable DataFlash log with VIBE, raw/high-rate IMU or ISBH/ISBD, RATE/PID, and PARM evidence before classifying Methodic 8.1.",
            "Do not set notch parameters from an unreadable or empty log.",
        ]
        result["confidence_limits"] = ["No deterministic 8.1 evidence was available because log parsing failed."]
        return normalize_schema(result)


def analyze_8_2(log_path: Path, step: dict[str, Any], plots_dir: Path | None, manual_observations: list[str]) -> dict[str, Any]:
    try:
        result = analyze_throttle_controller(log_path, plots_dir=plots_dir)
        if manual_observations:
            result["evidence_used"].append({"type": "manual_observations_provided_to_dispatcher", "value": manual_observations})
        return result
    except Exception as exc:
        result = empty_result(step)
        result["result"] = "inconclusive"
        result["safety_gate"] = "repeat_step"
        result["missing_evidence"] = [f"Step 8.2 throttle-controller evidence could not be read: {exc}"]
        result["checked_but_not_supported"] = ["methodic_8_2_throttle_controller"]
        result["recommended_next_steps"] = [
            "Collect a readable DataFlash log with CTUN, ATT, RATE, RCOU/RCO2/RCO3, PARM, and a stable hover before classifying Methodic 8.2.",
            "Do not write throttle-controller parameter changes from an unreadable or unsafe hover log.",
        ]
        result["confidence_limits"] = ["No deterministic 8.2 evidence was available because log parsing failed."]
        return normalize_schema(result)


def analyze_8_3(log_path: Path, step: dict[str, Any], plots_dir: Path | None, manual_observations: list[str]) -> dict[str, Any]:
    try:
        result = analyze_pid_notch_review(log_path, plots_dir=plots_dir)
        if manual_observations:
            result["evidence_used"].append({"type": "manual_observations_provided_to_dispatcher", "value": manual_observations})
        return result
    except Exception as exc:
        result = empty_result(step)
        result["result"] = "inconclusive"
        result["safety_gate"] = "repeat_step"
        result["missing_evidence"] = [f"Step 8.3 PID notch/frame-resonance evidence could not be read: {exc}"]
        result["checked_but_not_supported"] = ["methodic_8_3_pid_notch_review"]
        result["recommended_next_steps"] = [
            "Collect a readable isolated-axis log with RATE, PIDR/PIDP/PIDY, VIBE, PARM, and usable frequency-domain evidence before classifying Methodic 8.3.",
            "Do not add PID notch parameters from an unreadable or inconclusive log.",
        ]
        result["confidence_limits"] = ["No deterministic 8.3 evidence was available because log parsing failed."]
        return normalize_schema(result)


def analyze_8_4(log_path: Path, step: dict[str, Any], plots_dir: Path | None, manual_observations: list[str]) -> dict[str, Any]:
    try:
        result = analyze_ekf_altitude_source(log_path, plots_dir=plots_dir)
        if manual_observations:
            result["evidence_used"].append({"type": "manual_observations_provided_to_dispatcher", "value": manual_observations})
        return result
    except Exception as exc:
        result = empty_result(step)
        result["result"] = "inconclusive"
        result["safety_gate"] = "repeat_step"
        result["missing_evidence"] = [f"Step 8.4 EKF altitude-source evidence could not be read: {exc}"]
        result["checked_but_not_supported"] = ["methodic_8_4_ekf_altitude_source"]
        result["recommended_next_steps"] = [
            "Collect a readable log with CTUN, BARO, GPS/GPA, XKF*/NKF*, VIBE, BAT/POWR, and PARM before classifying Methodic 8.4.",
            "Do not change EKF height-source parameters from an unreadable or inconclusive log.",
        ]
        result["confidence_limits"] = ["No deterministic 8.4 evidence was available because log parsing failed."]
        return normalize_schema(result)


def analyze_8_5(log_path: Path, step: dict[str, Any], plots_dir: Path | None, manual_observations: list[str]) -> dict[str, Any]:
    return analyze_quicktune_step(log_path, step, plots_dir, manual_observations, methodic_step="8.5")


def analyze_9_2(log_path: Path, step: dict[str, Any], plots_dir: Path | None, manual_observations: list[str]) -> dict[str, Any]:
    return analyze_quicktune_step(log_path, step, plots_dir, manual_observations, methodic_step="9.2")


def analyze_quicktune_step(log_path: Path, step: dict[str, Any], plots_dir: Path | None, manual_observations: list[str], *, methodic_step: str) -> dict[str, Any]:
    try:
        result = analyze_quicktune_review(log_path, plots_dir=plots_dir, methodic_step=methodic_step)
        if manual_observations:
            result["evidence_used"].append({"type": "manual_observations_provided_to_dispatcher", "value": manual_observations})
        return result
    except Exception as exc:
        result = empty_result(step)
        result["result"] = "inconclusive"
        result["safety_gate"] = "repeat_step"
        result["missing_evidence"] = [f"Step {methodic_step} QuikTune/manual PID evidence could not be read: {exc}"]
        result["checked_but_not_supported"] = [f"methodic_{methodic_step.replace('.', '_')}_quicktune_review"]
        result["recommended_next_steps"] = [
            "Collect a readable tuning log with ATT, RATE, PIDR/PIDP/PIDY, RCOU/RCO2/RCO3, VIBE, BAT, MODE, MSG, and PARM evidence.",
            "Use before/after parameter files when the log does not preserve QuikTune or manual tuning parameter changes.",
            "Do not accept tune results or proceed to later tuning from an unreadable or inconclusive review.",
        ]
        result["confidence_limits"] = [f"No deterministic {methodic_step} evidence was available because log parsing failed."]
        return normalize_schema(result)


def rows_to_tables(rows_by_message: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    try:
        import pandas as pd
    except Exception:
        return {}
    return {name: pd.DataFrame(rows) for name, rows in rows_by_message.items() if rows}


def _trim_rc_context(rc_context: dict[str, Any]) -> dict[str, Any]:
    axes = {}
    for axis, data in (rc_context.get("axis_activity") or {}).items():
        axes[axis] = {
            "available": data.get("available"),
            "channel": data.get("channel"),
            "field": data.get("field"),
            "active_percent_by_deadband_us": data.get("active_percent_by_deadband_us"),
            "centered_percent": data.get("centered_percent"),
            "mapping_source": data.get("mapping_source"),
        }
    return {
        "available": rc_context.get("available"),
        "hands_off_confidence": rc_context.get("hands_off_confidence"),
        "centered_percent": rc_context.get("centered_percent"),
        "rc_centered_windows": rc_context.get("rc_centered_windows"),
        "warnings": rc_context.get("warnings"),
        "axes": axes,
    }


def make_7_1_1_plots(rate_rows: list[dict[str, Any]], rows_by_message: dict[str, list[dict[str, Any]]], plots_dir: Path) -> list[str]:
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except Exception:
        return []

    out = ensure_dir(plots_dir)
    plots: list[str] = []
    x = [time_value(row) if time_value(row) is not None else idx for idx, row in enumerate(rate_rows)]
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, subplot_titles=("Roll output", "Pitch output", "Yaw output"))
    for row_idx, field in enumerate(["ROut", "POut", "YOut"], start=1):
        y = [safe_float(row.get(field)) for row in rate_rows]
        fig.add_trace(go.Scatter(x=x, y=y, mode="lines", name=f"RATE.{field}"), row=row_idx, col=1)
    fig.update_layout(title="Methodic 7.1.1 RATE output oscillation check", template="plotly_white", hovermode="x unified")
    rate_path = out / "rate_outputs.html"
    fig.write_html(str(rate_path), include_plotlyjs="cdn")
    plots.append(str(rate_path))

    for message in ("RCOU", "RCO2", "RCO3"):
        rows = rows_by_message.get(message) or []
        if not rows:
            continue
        fig = go.Figure()
        x_out = [time_value(row) if time_value(row) is not None else idx for idx, row in enumerate(rows)]
        for idx in range(1, 17):
            values = numeric_values(rows, [f"C{idx}", f"Chan{idx}", f"PWM{idx}"])
            if values:
                fig.add_trace(go.Scatter(x=x_out[:len(values)], y=values, mode="lines", name=f"{message}.C{idx}"))
        fig.update_layout(title=f"Methodic 7.1.1 {message} output check", template="plotly_white", hovermode="x unified")
        path = out / f"{message.lower()}_outputs.html"
        fig.write_html(str(path), include_plotlyjs="cdn")
        plots.append(str(path))
    return plots


def next_steps_for_result(step: dict[str, Any], result: dict[str, Any]) -> list[str]:
    status = result["result"]
    if status == "pass":
        return [
            f"Agent should inspect the evidence and, if it agrees, continue to Methodic step {step.get('next_step_if_pass')}.",
            "Keep the manual observations with the analysis record; do not describe the aircraft as safe to fly.",
        ]
    if status == "conditional_pass":
        return [
            "Resolve the listed missing manual observations or evidence limits before treating this step as complete.",
            f"If the agent accepts the caveats, use the conditional path: {step.get('next_step_if_conditional')}.",
        ]
    if status == "fail":
        return [
            step.get("next_step_if_fail", "Do not proceed to the next Methodic step."),
            "Perform bench/hardware/configuration checks that match the findings, then repeat the current Methodic evidence capture.",
        ]
    if status == "not_applicable":
        return ["Document why the step is not applicable before continuing."]
    return [
        "Collect the missing required log messages, parameter context, and manual observations before classifying this Methodic step.",
        "Use the preferred Methodic window listed in the registry rather than whole-log averages when possible.",
    ]


def write_summary(path: Path, result: dict[str, Any]) -> None:
    lines = [
        f"# Methodic {result['methodic_step']}: {result['title']}",
        "",
        f"- Result: `{result['result']}`",
        f"- Safety gate: `{result['safety_gate']}`",
        f"- Official reference: {result['official_reference']['url']}",
        "",
        "## Findings",
    ]
    if result["findings"]:
        for finding in result["findings"]:
            lines.append(f"- {finding.get('severity', 'info')}: {finding.get('finding')}")
    else:
        lines.append("- No step-specific blocker was found by the script.")
    lines.extend(["", "## Missing Evidence"])
    if result["missing_evidence"]:
        lines.extend(f"- {item}" for item in result["missing_evidence"])
    else:
        lines.append("- None reported by the script.")
    lines.extend(["", "## Recommended Next Steps"])
    lines.extend(f"- {item}" for item in result["recommended_next_steps"])
    lines.extend(["", "## What Not To Do"])
    lines.extend(f"- {item}" for item in result["what_not_to_do"])
    ensure_dir(path.parent)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run step-aware ArduPilot Methodic Configurator evidence analysis.")
    parser.add_argument("log", help="ArduPilot DataFlash .BIN/.log file")
    parser.add_argument("--step", required=True, help="Methodic step ID or alias, for example 7.1.1")
    parser.add_argument("--registry", default=None, help="Optional Methodic step registry YAML")
    parser.add_argument("--out", default=None, help="Write structured JSON result")
    parser.add_argument("--summary", default=None, help="Write Markdown summary")
    parser.add_argument("--plots", default=None, help="Directory for generated plots")
    parser.add_argument("--manual-observation", action="append", default=[], help="Manual observation text; repeat for multiple observations")
    args = parser.parse_args()

    try:
        registry = load_registry(args.registry)
        step = get_step(args.step, registry)
        impl_name = STEP_IMPLEMENTATIONS.get(step["step_id"])
        if impl_name:
            result = globals()[impl_name](Path(args.log), step, Path(args.plots) if args.plots else None, normalize_manual_observations(args.manual_observation))
        else:
            result = not_implemented_result(step)
        if args.out:
            write_json(args.out, result)
        if args.summary:
            write_summary(Path(args.summary), result)
        print(json.dumps(result, indent=2))
        return 0
    except MethodicRegistryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
