#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from statistics import mean, pstdev
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ap_common import (
    AnalysisError,
    OUTPUT_FUNCTIONS,
    clip_columns,
    collect_dataflash,
    ensure_dir,
    get_col,
    numeric_series,
    safe_float,
    safe_int,
    write_json,
)
from ap_methodic_oscillation import classify_oscillation
from ap_methodic_rc import analyze_rc_input_contamination
from ap_methodic_windows import select_methodic_window

METHODIC_711_URL = "https://ardupilot.github.io/MethodicConfigurator/TUNING_GUIDE_ArduCopter#711-check-for-motor-output-oscillation"
RATE_THRESHOLD = 0.15
REQUIRED_MANUAL_OBSERVATIONS = [
    "Motors not excessively hot immediately after landing",
    "ESCs not excessively hot immediately after landing",
    "No audible oscillation",
    "No visible shaking",
    "No hard-to-control or sluggish behaviour",
]
MESSAGES = [
    "RATE",
    "RCOU",
    "RCO2",
    "RCO3",
    "MODE",
    "ATT",
    "PIDR",
    "PIDP",
    "PIDY",
    "VIBE",
    "CTUN",
    "RCIN",
    "PARM",
    "ESC",
    "ESCX",
    "EDT2",
    "BAT",
    "POWR",
    "MSG",
    "EV",
    "ERR",
    "ARM",
]
RELEVANT_PARAMETERS = [
    "ATC_RAT_PIT_D",
    "ATC_RAT_PIT_I",
    "ATC_RAT_PIT_P",
    "ATC_RAT_RLL_D",
    "ATC_RAT_RLL_I",
    "ATC_RAT_RLL_P",
    "PSC_ACCZ_P",
    "PSC_ACCZ_I",
    "SERVO*_FUNCTION",
]


def rows_to_tables(rows_by_message: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    try:
        import pandas as pd
    except Exception as exc:
        raise AnalysisError("pandas is required for Methodic 7.1.1 analysis. Install dependencies with: pip install -r requirements.txt") from exc
    return {name: pd.DataFrame(rows) for name, rows in rows_by_message.items() if rows}


def analyze_motor_oscillation_711(
    log_path: str | Path,
    *,
    plots_dir: str | Path | None = None,
    manual_observations: list[str] | None = None,
) -> dict[str, Any]:
    rows, index, stats = collect_dataflash(log_path, include=MESSAGES, source=str(log_path))
    tables = rows_to_tables(rows)
    params = index.get("parameters", {}) or {}
    manual_observations = [str(item).strip() for item in (manual_observations or []) if str(item).strip()]

    result = empty_result(params)
    result["evidence_used"].append({"type": "messages_present", "messages": sorted(tables.keys())})
    result["parameter_context"] = parameter_context(params)
    result["analysis_window"]["parser_stats"] = stats

    missing_required = required_missing(tables)
    result["missing_evidence"].extend(f"Missing required message: {name}" for name in missing_required)

    try:
        hover_selection = select_methodic_window(tables, "methodic_hover", min_duration_s=5.0)
    except Exception as exc:
        hover_selection = {"selected_window": None, "candidate_windows": [], "warnings": [str(exc)], "confidence": "low"}
    hover_window = hover_selection.get("selected_window")
    result["analysis_window"].update({
        "selection": "methodic_hover" if hover_window else "none",
        "preferred_window": "Stable AltHold hover from the first flight, excluding takeoff, landing, and ground spool.",
        "start_s": hover_window.get("start_s") if hover_window else None,
        "end_s": hover_window.get("end_s") if hover_window else None,
        "methodic_selector": hover_selection,
    })
    result["evidence_used"].append({"type": "hover_window_selection", "value": hover_selection})
    result["confidence_limits"].extend(hover_selection.get("warnings", []))
    if not hover_window:
        result["missing_evidence"].append("No usable Methodic AltHold/stable hover window was selected.")

    rc = analyze_rc_input_contamination(tables, params)
    rc_subset = rc_centered_subset_context(rc, hover_window)
    result["evidence_used"].append({"type": "rc_input_contamination", "value": trim_rc_context(rc, rc_subset)})
    if not rc.get("available"):
        result["confidence_limits"].append("RCIN is missing; pilot stick contamination cannot be ruled out.")
    elif rc.get("hands_off_confidence") == "low":
        result["confidence_limits"].append("RC input contamination limits confidence; RATE outputs were assessed on RC-centered samples when possible.")

    rate = slice_table(tables.get("RATE"), hover_window)
    if rate is not None and rc_subset.get("usable_for_rate"):
        rate = filter_by_windows(rate, rc_subset["windows"])
    rate_outputs = analyze_rate_outputs(rate)
    result["evidence_used"].append({"type": "rate_output_metrics", "value": rate_outputs})

    pid_terms = analyze_pid_terms(tables, hover_window, rc_subset)
    result["evidence_used"].append({"type": "pid_terms", "value": pid_terms})
    motor_outputs = analyze_motor_outputs(tables, hover_window, params)
    result["evidence_used"].append({"type": "mapped_motor_outputs", "value": motor_outputs})
    esc = analyze_esc_telemetry(tables, hover_window)
    result["evidence_used"].append({"type": "esc_telemetry", "value": esc})
    vibration = analyze_vibration(tables, hover_window)
    result["evidence_used"].append({"type": "vibration", "value": vibration})

    result["manual_observations_required"] = manual_observation_status(manual_observations)
    manual_findings, manual_missing = classify_manual_observations(manual_observations)
    result["missing_evidence"].extend(f"Missing manual observation: {item}" for item in manual_missing)
    result["findings"].extend(manual_findings)

    result["findings"].extend(classify_rate_findings(rate_outputs))
    result["findings"].extend(classify_pid_findings(pid_terms))
    result["findings"].extend(classify_motor_findings(motor_outputs))
    result["findings"].extend(classify_esc_findings(esc))
    result["findings"].extend(classify_vibration_findings(vibration))
    result["findings"].extend(classify_rc_findings(rc, rc_subset))

    if missing_required or not hover_window or rate_outputs.get("available") is False or motor_outputs.get("available") is False:
        result["result"] = "inconclusive"
        result["safety_gate"] = "repeat_step"
    elif rc_subset.get("too_contaminated_to_assess"):
        result["result"] = "inconclusive"
        result["safety_gate"] = "repeat_step"
    else:
        result["result"], result["safety_gate"] = classify_result(result["findings"], esc, rc, manual_missing)

    result["next_methodic_step"] = next_methodic_step(result["result"])
    result["recommended_next_steps"] = recommended_next_steps(result, rate_outputs, pid_terms, esc, vibration)
    result["what_not_to_do"] = [
        "Do not declare the aircraft safe to fly from this log analysis.",
        "Do not halve roll/pitch P/I/D if roll and pitch RATE outputs are below threshold and not oscillatory.",
        "Do not make blind gain changes without matching RATE/PID/output evidence and manual observations.",
        "Do not proceed past Methodic safety gates when hot motors/ESCs, visible shaking, audible oscillation, severe vibration, or saturation is present.",
    ]

    if plots_dir:
        result["plots"] = make_plots(tables, Path(plots_dir), hover_window, rc_subset)
    return result


def empty_result(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "methodic_step": "7.1.1",
        "title": "Motor output oscillation check",
        "official_reference": {"url": METHODIC_711_URL, "anchor": "#711-check-for-motor-output-oscillation"},
        "result": "inconclusive",
        "safety_gate": "repeat_step",
        "evidence_used": [],
        "missing_evidence": [],
        "manual_observations_required": list(REQUIRED_MANUAL_OBSERVATIONS),
        "analysis_window": {
            "selection": "none",
            "preferred_window": "Stable AltHold hover from the first flight, excluding takeoff, landing, and ground spool.",
            "start_s": None,
            "end_s": None,
        },
        "findings": [],
        "checked_but_not_supported": [],
        "parameter_context": parameter_context(params),
        "plots": [],
        "recommended_next_steps": [],
        "what_not_to_do": [],
        "next_methodic_step": None,
        "confidence_limits": [],
    }


def parameter_context(params: dict[str, Any]) -> dict[str, Any]:
    present: dict[str, Any] = {}
    missing: list[str] = []
    for name in RELEVANT_PARAMETERS:
        if "*" in name:
            prefix, suffix = name.split("*", 1)
            matches = {k: v for k, v in params.items() if k.startswith(prefix) and k.endswith(suffix)}
            if matches:
                present.update(matches)
            else:
                missing.append(name)
        elif name in params:
            present[name] = params[name]
        else:
            missing.append(name)
    return {"relevant_parameters": RELEVANT_PARAMETERS, "present": present, "missing_or_not_logged": missing, "source": "log PARM messages" if params else "no PARM messages found"}


def required_missing(tables: dict[str, Any]) -> list[str]:
    missing = [name for name in ("RATE", "MODE", "ATT") if name not in tables]
    if not any(name in tables for name in ("RCOU", "RCO2", "RCO3")):
        missing.append("RCOU/RCO2/RCO3")
    return missing


def slice_table(df: Any, window: dict[str, Any] | None):
    if df is None or not window or "TimeS" not in getattr(df, "columns", []):
        return df
    start = window.get("start_s")
    end = window.get("end_s")
    if start is None or end is None:
        return df
    return df[(df["TimeS"] >= start) & (df["TimeS"] <= end)]


def filter_by_windows(df: Any, windows: list[dict[str, Any]]):
    if df is None or not windows or "TimeS" not in getattr(df, "columns", []):
        return df
    mask = [any(window["start_s"] <= safe_float(t, -1e9) <= window["end_s"] for window in windows) for t in df["TimeS"].tolist()]
    return df[mask]


def series_values(df: Any, col: str | None) -> list[float]:
    if df is None or col is None:
        return []
    s = numeric_series(df, [col])
    if s is None:
        return []
    return [float(v) for v in s.dropna().tolist()]


def time_values(df: Any) -> list[float]:
    if df is None or "TimeS" not in getattr(df, "columns", []):
        return []
    return [safe_float(v) for v in df["TimeS"].tolist() if safe_float(v) is not None]


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * pct / 100.0))))
    return float(ordered[idx])


def summarize_values(values: list[float], *, threshold: float | None = None) -> dict[str, Any]:
    if not values:
        return {"available": False, "samples": 0}
    abs_values = [abs(v) for v in values]
    out = {
        "available": True,
        "samples": len(values),
        "mean": float(mean(values)),
        "rms": math.sqrt(sum(v * v for v in values) / len(values)),
        "p95_abs": percentile(abs_values, 95),
        "p99_abs": percentile(abs_values, 99),
        "max_abs": max(abs_values),
    }
    if threshold is not None:
        out["percent_abs_above_threshold"] = 100.0 * sum(1 for v in abs_values if v > threshold) / len(abs_values)
    return out


def rc_centered_subset_context(rc: dict[str, Any], hover_window: dict[str, Any] | None) -> dict[str, Any]:
    if not rc.get("available") or not hover_window:
        return {"available": bool(rc.get("available")), "windows": [], "usable_for_rate": False, "duration_s": 0.0, "too_contaminated_to_assess": False}
    start = hover_window.get("start_s")
    end = hover_window.get("end_s")
    windows = []
    for window in rc.get("rc_centered_windows") or []:
        lo = max(float(start), float(window["start_s"]))
        hi = min(float(end), float(window["end_s"]))
        if hi > lo:
            windows.append({"start_s": lo, "end_s": hi, "duration_s": hi - lo})
    duration = sum(w["duration_s"] for w in windows)
    hover_duration = max(float(end) - float(start), 0.0) if start is not None and end is not None else 0.0
    too_contaminated = rc.get("hands_off_confidence") == "low" and duration < min(3.0, hover_duration * 0.25)
    return {
        "available": True,
        "windows": windows,
        "duration_s": duration,
        "hover_duration_s": hover_duration,
        "usable_for_rate": duration >= 1.0,
        "too_contaminated_to_assess": too_contaminated,
    }


def trim_rc_context(rc: dict[str, Any], subset: dict[str, Any]) -> dict[str, Any]:
    axes = {}
    for axis, data in (rc.get("axis_activity") or {}).items():
        axes[axis] = {
            "available": data.get("available"),
            "channel": data.get("channel"),
            "active_percent_by_deadband_us": data.get("active_percent_by_deadband_us"),
            "centered_percent": data.get("centered_percent"),
            "mapping_source": data.get("mapping_source"),
        }
    return {
        "available": rc.get("available"),
        "hands_off_confidence": rc.get("hands_off_confidence"),
        "centered_percent": rc.get("centered_percent"),
        "centered_subset": subset,
        "warnings": rc.get("warnings"),
        "axes": axes,
    }


def analyze_rate_outputs(rate: Any) -> dict[str, Any]:
    if rate is None or len(rate) == 0:
        return {"available": False, "reason": "RATE missing or no RATE samples in the selected hover/RC-centered subset."}
    times = time_values(rate)
    axes = {}
    for axis, field in [("roll", "ROut"), ("pitch", "POut"), ("yaw", "YOut"), ("altitude", "AOut")]:
        if field not in getattr(rate, "columns", []):
            continue
        values = series_values(rate, field)
        osc = classify_oscillation(values, times[: len(values)], threshold=RATE_THRESHOLD, min_samples=20, min_duration_s=1.0)
        metrics = dict(osc.get("metrics") or {})
        axes[axis] = {
            "field": field,
            **summarize_values(values, threshold=RATE_THRESHOLD),
            "highpass_p95_abs": metrics.get("highpass_residual_p95_abs"),
            "sign_change_rate": metrics.get("sign_change_rate_hz"),
            "classification": osc.get("classification"),
            "classification_reason": osc.get("reason", []),
        }
    return {"available": bool(axes), "threshold_abs": RATE_THRESHOLD, "axes": axes, "samples": len(rate)}


def analyze_pid_terms(tables: dict[str, Any], hover_window: dict[str, Any] | None, rc_subset: dict[str, Any]) -> dict[str, Any]:
    out = {"available": False, "messages": {}, "yaw_i_term_steady_bias": None}
    for name, axis in [("PIDR", "roll"), ("PIDP", "pitch"), ("PIDY", "yaw")]:
        df = slice_table(tables.get(name), hover_window)
        if df is not None and rc_subset.get("usable_for_rate"):
            df = filter_by_windows(df, rc_subset["windows"])
        if df is None or len(df) == 0:
            continue
        msg = {"axis": axis, "samples": len(df), "terms": {}}
        for col in ("P", "I", "D", "FF", "DFF", "Dmod", "SRate", "Flags"):
            if col not in df.columns:
                continue
            vals = series_values(df, col)
            if vals:
                msg["terms"][col] = summarize_values(vals)
        out["messages"][name] = msg
    if out["messages"]:
        out["available"] = True
    yaw_i = ((out["messages"].get("PIDY") or {}).get("terms") or {}).get("I")
    if yaw_i and yaw_i.get("available"):
        out["yaw_i_term_steady_bias"] = {
            "mean": yaw_i.get("mean"),
            "p95_abs": yaw_i.get("p95_abs"),
            "interpretation": "High steady PIDY.I can indicate sustained yaw torque imbalance or alignment/asymmetry rather than output oscillation.",
        }
    return out


def servo_function_label(value: Any) -> tuple[str | None, str | None]:
    code = safe_int(value)
    if code is None:
        return None, None
    label = OUTPUT_FUNCTIONS.get(code)
    if label:
        return label
    return f"function_{code}", "other"


def output_index(message: str, channel: int) -> int:
    if message == "RCOU":
        return channel
    if message == "RCO2":
        return 16 + channel
    if message == "RCO3":
        return 32 + channel
    return channel


def analyze_motor_outputs(tables: dict[str, Any], hover_window: dict[str, Any] | None, params: dict[str, Any]) -> dict[str, Any]:
    channels = {}
    motor_values_by_time: dict[float, list[float]] = {}
    for message in ("RCOU", "RCO2", "RCO3"):
        df = slice_table(tables.get(message), hover_window)
        if df is None or len(df) == 0:
            continue
        for col in [c for c in df.columns if str(c).startswith("C") and str(c)[1:].isdigit()]:
            ch = safe_int(str(col)[1:])
            if ch is None:
                continue
            servo_idx = output_index(message, ch)
            function_name, function_group = servo_function_label(params.get(f"SERVO{servo_idx}_FUNCTION"))
            if function_group != "motor" and message == "RCOU" and servo_idx <= 8 and f"SERVO{servo_idx}_FUNCTION" not in params:
                function_name, function_group = f"motor{servo_idx}_assumed", "motor_assumed"
            vals = series_values(df, col)
            if not vals:
                continue
            summary = {
                "message": message,
                "channel": col,
                "servo_output": servo_idx,
                "servo_function": function_name,
                "function_group": function_group,
                "samples": len(vals),
                "min": min(vals),
                "max": max(vals),
                "mean": mean(vals),
                "spread_us": max(vals) - min(vals),
                "pct_low_le_1100": 100.0 * sum(1 for v in vals if v <= 1100) / len(vals),
                "pct_high_ge_1900": 100.0 * sum(1 for v in vals if v >= 1900) / len(vals),
                "persistent_high": 100.0 * sum(1 for v in vals if v >= 1850) / len(vals) > 10.0,
                "persistent_low": 100.0 * sum(1 for v in vals if v <= 1150) / len(vals) > 10.0,
            }
            channels[f"{message}.{col}"] = summary
            if function_group in {"motor", "motor_assumed"} and "TimeS" in df.columns:
                for t, value in zip(df["TimeS"].tolist(), vals):
                    tf = safe_float(t)
                    if tf is not None:
                        motor_values_by_time.setdefault(tf, []).append(value)
    spreads = [max(vals) - min(vals) for vals in motor_values_by_time.values() if len(vals) >= 2]
    return {
        "available": bool(channels),
        "channels": channels,
        "motor_spread": {
            "samples": len(spreads),
            "p95_us": percentile(spreads, 95),
            "max_us": max(spreads) if spreads else None,
        },
        "spool_exclusion": "analysis is limited to the selected hover window when available; ground spool rows are not used for saturation decisions.",
    }


def analyze_esc_telemetry(tables: dict[str, Any], hover_window: dict[str, Any] | None) -> dict[str, Any]:
    messages = {}
    for name in ("ESC", "ESCX", "EDT2"):
        df = slice_table(tables.get(name), hover_window)
        if df is None or len(df) == 0:
            continue
        fields = {}
        for col in df.columns:
            lower = str(col).lower()
            if lower in {"timeus", "times", "timems"}:
                continue
            if any(token in lower for token in ("temp", "mottemp", "err", "status", "rpm", "curr", "current")):
                vals = series_values(df, col)
                if vals:
                    fields[col] = summarize_values(vals)
        messages[name] = {"samples": len(df), "fields": fields}
    return {
        "available": bool(messages),
        "log_can_confirm_esc_temp": any(
            any("temp" in str(field).lower() for field in msg.get("fields", {}))
            for msg in messages.values()
        ),
        "messages": messages,
        "manual_user_checks_required": [] if messages else ["motor/ESC heat immediately after landing"],
    }


def analyze_vibration(tables: dict[str, Any], hover_window: dict[str, Any] | None) -> dict[str, Any]:
    vibe = slice_table(tables.get("VIBE"), hover_window)
    if vibe is None or len(vibe) == 0:
        return {"available": False, "warning": "VIBE missing; vibration and clipping cannot be assessed."}
    axes = {}
    for col in ("VibeX", "VibeY", "VibeZ"):
        vals = series_values(vibe, col)
        if vals:
            axes[col] = {"p95": percentile([abs(v) for v in vals], 95), "max": max(abs(v) for v in vals), "unit": "m/s/s"}
    clip_delta = {}
    for col in clip_columns(vibe):
        vals = series_values(vibe, col)
        if len(vals) > 1:
            clip_delta[col] = max(vals) - min(vals)
    max_axis = max((axis["max"] for axis in axes.values()), default=None)
    p95_axis = max((axis["p95"] for axis in axes.values() if axis.get("p95") is not None), default=None)
    return {"available": True, "axes": axes, "max_axis": max_axis, "p95_axis": p95_axis, "clip_delta": clip_delta}


def classify_rate_findings(rate: dict[str, Any]) -> list[dict[str, Any]]:
    if not rate.get("available"):
        return [{"severity": "inconclusive", "finding": rate.get("reason", "RATE output evidence is unavailable.")}]
    findings = []
    for axis, data in (rate.get("axes") or {}).items():
        cls = data.get("classification")
        p95 = data.get("p95_abs") or 0.0
        highpass = data.get("highpass_p95_abs") or 0.0
        pct_above = data.get("percent_abs_above_threshold") or 0.0
        if axis in {"roll", "pitch"} and cls in {"oscillatory", "mixed"} and (p95 > RATE_THRESHOLD or highpass > RATE_THRESHOLD * 0.45):
            findings.append(finding("fail", f"{axis} RATE output is oscillatory/high in RC-centered hover.", data, "do_not_proceed"))
        elif axis == "yaw" and cls in {"oscillatory", "mixed"} and (p95 > RATE_THRESHOLD or highpass > RATE_THRESHOLD * 0.45):
            findings.append(finding("fail", "yaw RATE output is oscillatory/high in RC-centered hover.", data, "do_not_proceed"))
        elif axis == "yaw" and cls == "steady_bias" and p95 > RATE_THRESHOLD:
            findings.append(finding("conditional", "yaw RATE output shows high steady bias rather than clean oscillation.", data))
        elif pct_above > 10.0 and cls == "steady_bias":
            findings.append(finding("conditional", f"{axis} RATE output spends time above threshold as steady bias, not clean oscillation.", data))
        else:
            findings.append({"severity": "info", "finding": f"{axis} RATE output did not support a Methodic output-oscillation failure.", "evidence": data})
    return findings


def classify_pid_findings(pid: dict[str, Any]) -> list[dict[str, Any]]:
    findings = []
    yaw = pid.get("yaw_i_term_steady_bias")
    if yaw and (yaw.get("p95_abs") or 0.0) > 0.15:
        findings.append({
            "severity": "conditional",
            "finding": "PIDY.I shows sustained yaw I-term bias.",
            "evidence": yaw,
            "interpretation": "Treat as yaw torque/alignment/asymmetry evidence before treating it as roll/pitch output oscillation.",
        })
    return findings


def classify_motor_findings(outputs: dict[str, Any]) -> list[dict[str, Any]]:
    if not outputs.get("available"):
        return [{"severity": "inconclusive", "finding": "RCOU/RCO2/RCO3 motor output evidence is unavailable."}]
    findings = []
    for name, data in (outputs.get("channels") or {}).items():
        if data.get("pct_high_ge_1900", 0.0) > 5.0 or data.get("pct_low_le_1100", 0.0) > 10.0:
            findings.append(finding("fail", f"Motor/output saturation detected on {name} in hover.", data, "bench_check_required"))
        elif data.get("persistent_high") or data.get("persistent_low"):
            findings.append(finding("conditional", f"Persistent high/low motor output tendency detected on {name}.", data))
    return findings


def classify_esc_findings(esc: dict[str, Any]) -> list[dict[str, Any]]:
    findings = []
    if not esc.get("available"):
        findings.append({"severity": "conditional", "finding": "ESC telemetry is absent; log cannot confirm ESC or motor temperatures.", "evidence": {"log_can_confirm_esc_temp": False}})
        return findings
    for message, data in (esc.get("messages") or {}).items():
        for field, summary in (data.get("fields") or {}).items():
            lower = str(field).lower()
            if ("err" in lower or "status" in lower) and (summary.get("max_abs") or 0.0) > 0:
                findings.append(finding("fail", f"{message}.{field} reports non-zero ESC error/status evidence.", summary, "bench_check_required"))
            if "temp" in lower and (summary.get("max_abs") or 0.0) >= 85.0:
                findings.append(finding("fail", f"{message}.{field} reports high ESC/motor temperature.", summary, "bench_check_required"))
    return findings


def classify_vibration_findings(vibration: dict[str, Any]) -> list[dict[str, Any]]:
    if not vibration.get("available"):
        return [{"severity": "conditional", "finding": vibration.get("warning", "VIBE missing.")}]
    findings = []
    max_axis = vibration.get("max_axis") or 0.0
    p95_axis = vibration.get("p95_axis") or 0.0
    clip_delta = vibration.get("clip_delta") or {}
    if max_axis > 60.0 or any((delta or 0.0) > 0 for delta in clip_delta.values()):
        findings.append(finding("fail", "Severe vibration or clipping detected in hover.", vibration, "bench_check_required"))
    elif max_axis > 30.0 or p95_axis > 20.0:
        findings.append(finding("conditional", "Vibration is in a grey-zone range; hardware inspection is needed before treating the step as clean.", vibration))
    return findings


def classify_rc_findings(rc: dict[str, Any], subset: dict[str, Any]) -> list[dict[str, Any]]:
    if not rc.get("available"):
        return [{"severity": "conditional", "finding": "RCIN missing; RC stick contamination cannot be ruled out."}]
    if subset.get("too_contaminated_to_assess"):
        return [{"severity": "inconclusive", "finding": "Too much RC input contamination remains to assess output oscillation from this hover window.", "evidence": subset}]
    if rc.get("hands_off_confidence") == "low":
        return [{"severity": "conditional", "finding": "RC input contamination limits confidence in the oscillation assessment.", "evidence": subset}]
    return []


def finding(severity: str, text: str, evidence: Any, safety_gate: str | None = None) -> dict[str, Any]:
    out = {"severity": severity, "finding": text, "evidence": evidence}
    if safety_gate:
        out["safety_gate"] = safety_gate
    return out


def manual_observation_status(observations: list[str]) -> list[dict[str, Any]]:
    missing = set(classify_manual_observations(observations)[1])
    return [{"observation": item, "status": "missing" if item in missing else "provided"} for item in REQUIRED_MANUAL_OBSERVATIONS]


def classify_manual_observations(observations: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    text = " | ".join(observations).lower()
    missing = []
    for item in REQUIRED_MANUAL_OBSERVATIONS:
        tokens = [token for token in item.lower().replace("/", " ").split() if len(token) >= 4]
        if not text or not any(token in text for token in tokens):
            missing.append(item)
    findings = []
    negators = ("no ", "not ", "none", "normal", "cool", "cold", "acceptable")
    bad_patterns = [
        ("hot", "Motors or ESCs were reported hot after landing."),
        ("excessive heat", "Excessive motor/ESC heat was reported after landing."),
        ("audible oscill", "Audible oscillation was reported."),
        ("visible shak", "Visible shaking was reported."),
        ("hard to control", "Hard-to-control behaviour was reported."),
        ("sluggish", "Sluggish behaviour was reported."),
    ]
    for pattern, message in bad_patterns:
        if pattern in text and not any(neg in text for neg in negators):
            findings.append({"severity": "fail", "safety_gate": "bench_check_required", "finding": message, "evidence": observations})
    return findings, missing


def classify_result(findings: list[dict[str, Any]], esc: dict[str, Any], rc: dict[str, Any], manual_missing: list[str]) -> tuple[str, str]:
    if any(item.get("safety_gate") == "bench_check_required" for item in findings):
        return "fail", "bench_check_required"
    if any(item.get("severity") == "fail" for item in findings):
        return "fail", "do_not_proceed"
    if any(item.get("severity") == "inconclusive" for item in findings):
        return "inconclusive", "repeat_step"
    conditional = any(item.get("severity") == "conditional" for item in findings)
    if not esc.get("log_can_confirm_esc_temp"):
        conditional = True
    if rc.get("hands_off_confidence") == "low":
        conditional = True
    if manual_missing:
        conditional = True
    return ("conditional_pass", "proceed_with_caution") if conditional else ("pass", "proceed")


def next_methodic_step(result: str) -> str:
    if result == "pass":
        return "8.1"
    if result == "conditional_pass":
        return "8.1 with caution or repeat 7.1.1 after resolving caveats"
    return "repeat_7.1.1"


def recommended_next_steps(result: dict[str, Any], rate: dict[str, Any], pid: dict[str, Any], esc: dict[str, Any], vibration: dict[str, Any]) -> list[str]:
    if result["result"] == "pass":
        return ["Agent should inspect the evidence and plots; if it agrees, proceed to Methodic 8.1 notch/filter review."]
    steps = []
    if result["result"] == "inconclusive":
        steps.append("Do not classify Methodic 7.1.1 from this log; collect a usable AltHold hover with RATE, MODE, ATT, and RCOU/RCO2/RCO3 evidence.")
    if any("roll" in f.get("finding", "").lower() or "pitch" in f.get("finding", "").lower() for f in result["findings"] if f.get("severity") == "fail"):
        steps.append("Do not continue tuning; follow the Methodic gain-reduction investigation path for the affected roll/pitch axis and repeat a short hover check.")
    if any("yaw" in f.get("finding", "").lower() and "steady bias" in f.get("finding", "").lower() for f in result["findings"]):
        steps.append("Inspect motor alignment, motor verticality, frame twist, prop condition/mismatch, motor order/direction, yaw torque imbalance, coaxial interference if applicable, and motor/ESC temperatures.")
    if not esc.get("log_can_confirm_esc_temp"):
        steps.append("Manually record motor and ESC heat immediately after landing; this log cannot confirm ESC temperature.")
    if vibration.get("available") and ((vibration.get("max_axis") or 0.0) > 30.0 or (vibration.get("p95_axis") or 0.0) > 20.0):
        steps.append("Inspect hardware for vibration sources; proceed to notch/filter review only with explicit caution if no clipping/severe vibration is present.")
    if not steps:
        steps.append("Resolve the listed conditional evidence limits before treating 7.1.1 as complete.")
    return steps


def rolling_highpass(values: list[float], window: int = 21) -> list[float]:
    if not values:
        return []
    half = max(1, window // 2)
    out = []
    for idx, value in enumerate(values):
        lo = max(0, idx - half)
        hi = min(len(values), idx + half + 1)
        out.append(value - mean(values[lo:hi]))
    return out


def make_plots(tables: dict[str, Any], plots_dir: Path, hover_window: dict[str, Any] | None, rc_subset: dict[str, Any]) -> list[str]:
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except Exception:
        return []
    out = ensure_dir(plots_dir)
    plots: list[str] = []

    rate = slice_table(tables.get("RATE"), hover_window)
    if rate is not None and len(rate):
        fig = make_subplots(rows=4, cols=1, shared_xaxes=True, subplot_titles=("Roll", "Pitch", "Yaw", "Altitude"))
        for row, field in enumerate(("ROut", "POut", "YOut", "AOut"), start=1):
            if field in rate.columns:
                fig.add_trace(go.Scatter(x=rate["TimeS"], y=rate[field], mode="lines", name=f"RATE.{field}"), row=row, col=1)
        add_window_shapes(fig, hover_window, rc_subset)
        fig.update_layout(title="Methodic 7.1.1 RATE outputs", template="plotly_white", hovermode="x unified")
        plots.append(write_plot(fig, out / "methodic_711_rate_outputs.html"))

        fig = make_subplots(rows=4, cols=1, shared_xaxes=True, subplot_titles=("Roll HP", "Pitch HP", "Yaw HP", "Altitude HP"))
        for row, field in enumerate(("ROut", "POut", "YOut", "AOut"), start=1):
            if field in rate.columns:
                vals = series_values(rate, field)
                fig.add_trace(go.Scatter(x=rate["TimeS"].tolist()[: len(vals)], y=rolling_highpass(vals), mode="lines", name=f"RATE.{field} high-pass"), row=row, col=1)
        add_window_shapes(fig, hover_window, rc_subset)
        fig.update_layout(title="Methodic 7.1.1 RATE output high-pass residuals", template="plotly_white", hovermode="x unified")
        plots.append(write_plot(fig, out / "methodic_711_rate_outputs_highpass.html"))

    motor_fig = go.Figure()
    for name in ("RCOU", "RCO2", "RCO3"):
        df = slice_table(tables.get(name), hover_window)
        if df is None or "TimeS" not in getattr(df, "columns", []):
            continue
        for col in [c for c in df.columns if str(c).startswith("C") and str(c)[1:].isdigit()][:16]:
            motor_fig.add_trace(go.Scatter(x=df["TimeS"], y=df[col], mode="lines", name=f"{name}.{col}"))
    if motor_fig.data:
        add_window_shapes(motor_fig, hover_window, rc_subset)
        motor_fig.update_layout(title="Methodic 7.1.1 motor outputs", template="plotly_white", hovermode="x unified")
        plots.append(write_plot(motor_fig, out / "methodic_711_motor_outputs.html"))

    pid_fig = make_subplots(rows=3, cols=1, shared_xaxes=True, subplot_titles=("PIDR", "PIDP", "PIDY"))
    pid_has_data = False
    for row, name in enumerate(("PIDR", "PIDP", "PIDY"), start=1):
        df = slice_table(tables.get(name), hover_window)
        if df is None or "TimeS" not in getattr(df, "columns", []):
            continue
        for col in ("P", "I", "D", "FF", "DFF"):
            if col in df.columns:
                pid_fig.add_trace(go.Scatter(x=df["TimeS"], y=df[col], mode="lines", name=f"{name}.{col}"), row=row, col=1)
                pid_has_data = True
    if pid_has_data:
        add_window_shapes(pid_fig, hover_window, rc_subset)
        pid_fig.update_layout(title="Methodic 7.1.1 PID terms", template="plotly_white", hovermode="x unified")
        plots.append(write_plot(pid_fig, out / "methodic_711_pid_terms.html"))

    for message, filename, title in [
        ("RCIN", "methodic_711_rc_input.html", "Methodic 7.1.1 RC input"),
        ("VIBE", "methodic_711_vibration.html", "Methodic 7.1.1 vibration"),
    ]:
        df = slice_table(tables.get(message), hover_window)
        if df is None or "TimeS" not in getattr(df, "columns", []):
            continue
        fig = go.Figure()
        cols = [c for c in df.columns if c != "TimeS" and (message != "VIBE" or c in {"VibeX", "VibeY", "VibeZ", *clip_columns(df)})]
        for col in cols[:16]:
            fig.add_trace(go.Scatter(x=df["TimeS"], y=df[col], mode="lines", name=f"{message}.{col}"))
        add_window_shapes(fig, hover_window, rc_subset)
        fig.update_layout(title=title, template="plotly_white", hovermode="x unified")
        plots.append(write_plot(fig, out / filename))

    hover_fig = go.Figure()
    for message, cols in [("CTUN", ("Alt", "ThO", "ThH")), ("ATT", ("Roll", "Pitch"))]:
        df = tables.get(message)
        if df is None or "TimeS" not in getattr(df, "columns", []):
            continue
        for col in cols:
            if col in df.columns:
                hover_fig.add_trace(go.Scatter(x=df["TimeS"], y=df[col], mode="lines", name=f"{message}.{col}"))
    if hover_fig.data:
        add_window_shapes(hover_fig, hover_window, rc_subset)
        hover_fig.update_layout(title="Methodic 7.1.1 hover window", template="plotly_white", hovermode="x unified")
        plots.append(write_plot(hover_fig, out / "methodic_711_hover_window.html"))
    return plots


def add_window_shapes(fig: Any, hover_window: dict[str, Any] | None, rc_subset: dict[str, Any]) -> None:
    if hover_window and hover_window.get("start_s") is not None and hover_window.get("end_s") is not None:
        fig.add_vrect(x0=hover_window["start_s"], x1=hover_window["end_s"], fillcolor="#dbeafe", opacity=0.20, line_width=0)
    for window in rc_subset.get("windows") or []:
        fig.add_vrect(x0=window["start_s"], x1=window["end_s"], fillcolor="#dcfce7", opacity=0.16, line_width=0)


def write_plot(fig: Any, path: Path) -> str:
    fig.write_html(str(path), include_plotlyjs="cdn")
    return str(path)


def write_summary(path: Path, result: dict[str, Any]) -> None:
    lines = [
        "# Methodic 7.1.1 Motor Output Oscillation",
        "",
        f"- Result: `{result['result']}`",
        f"- Safety gate: `{result['safety_gate']}`",
        f"- Next Methodic step: `{result['next_methodic_step']}`",
        f"- Official reference: {METHODIC_711_URL}",
        "",
        "## Findings",
    ]
    if result["findings"]:
        lines.extend(f"- {item.get('severity', 'info')}: {item.get('finding')}" for item in result["findings"])
    else:
        lines.append("- No deterministic finding was produced.")
    lines.extend(["", "## Missing Evidence"])
    lines.extend(f"- {item}" for item in result["missing_evidence"]) if result["missing_evidence"] else lines.append("- None reported by deterministic checks.")
    lines.extend(["", "## Recommended Next Steps"])
    lines.extend(f"- {item}" for item in result["recommended_next_steps"])
    lines.extend(["", "## What Not To Do"])
    lines.extend(f"- {item}" for item in result["what_not_to_do"])
    ensure_dir(path.parent)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Gather deterministic Methodic 7.1.1 motor-output oscillation evidence.")
    parser.add_argument("log", help="ArduPilot DataFlash .BIN/.log file")
    parser.add_argument("--out", default="out/methodic_711.json")
    parser.add_argument("--summary", default=None)
    parser.add_argument("--plots", default=None)
    parser.add_argument("--manual-observation", action="append", default=[], help="Manual observation text; repeat for multiple observations")
    args = parser.parse_args()
    try:
        result = analyze_motor_oscillation_711(args.log, plots_dir=args.plots, manual_observations=args.manual_observation)
        write_json(args.out, result)
        if args.summary:
            write_summary(Path(args.summary), result)
        print(json.dumps(result, indent=2))
        return 0
    except AnalysisError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
