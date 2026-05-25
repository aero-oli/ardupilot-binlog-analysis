#!/usr/bin/env python3
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ap_common import (
    battery_instance_groups,
    clip_columns,
    combined_rcout_dataframe,
    ekf_instance_groups,
    get_col,
    numeric_series,
    output_channel_columns,
    percentile,
)


VIBE_WARN_THRESHOLD = 30.0


def _vals(s):
    if s is None:
        return []
    try:
        return [float(v) for v in s.dropna().tolist()]
    except Exception:
        return []


def _vibe_frame(tables):
    vibe = tables.get("VIBE")
    if vibe is None or len(vibe) == 0:
        return None
    cols = [c for c in ["VibeX", "VibeY", "VibeZ"] if c in vibe.columns]
    if not cols:
        return None
    out = vibe.copy()
    values = [numeric_series(out, [c]).abs() for c in cols]
    out["vibe_max_axis"] = values[0]
    for s in values[1:]:
        out["vibe_max_axis"] = out["vibe_max_axis"].where(out["vibe_max_axis"] >= s, s)
    return out


def _vibration_summary(tables) -> Dict[str, Any]:
    summary = {"available": "VIBE" in tables, "warnings": []}
    vibe = tables.get("VIBE")
    if vibe is None or len(vibe) == 0:
        summary["available"] = False
        summary["warnings"].append("VIBE missing; vibration contribution cannot be assessed from log data.")
        return summary
    for col in ["VibeX", "VibeY", "VibeZ"]:
        if col in vibe.columns:
            s = numeric_series(vibe, [col])
            if s is not None and len(s.dropna()) > 0:
                summary[col] = {"max": float(s.max()), "p95": percentile(_vals(s), 95)}
    clip_delta = {}
    for col in clip_columns(vibe):
        clip = numeric_series(vibe, [col])
        if clip is not None and len(clip.dropna()) > 1:
            clip_delta[col] = float(clip.max() - clip.min())
    if clip_delta:
        summary["clip_delta"] = clip_delta
    max_axis = max([v["max"] for k, v in summary.items() if isinstance(v, dict) and "max" in v] or [0.0])
    summary["max_axis"] = float(max_axis)
    summary["above_warning_threshold"] = bool(max_axis > VIBE_WARN_THRESHOLD)
    return summary


def _time_value(df, value_col, label):
    if df is None or value_col not in df.columns or "TimeS" not in df.columns:
        return None
    out = df[["TimeS", value_col]].copy().dropna()
    if len(out) == 0:
        return None
    out = out.rename(columns={value_col: label}).sort_values("TimeS")
    return out


def _attitude_rate_error(tables):
    frames = []
    att = tables.get("ATT")
    if att is not None:
        for des_col, actual_col, label in [("DesRoll", "Roll", "att_roll_error"), ("DesPitch", "Pitch", "att_pitch_error"), ("DesYaw", "Yaw", "att_yaw_error")]:
            if des_col in att.columns and actual_col in att.columns and "TimeS" in att.columns:
                des = numeric_series(att, [des_col])
                actual = numeric_series(att, [actual_col])
                err = des - actual
                if label == "att_yaw_error":
                    err = ((err + 180.0) % 360.0) - 180.0
                frame = att[["TimeS"]].copy()
                frame[label] = err.abs()
                frames.append((label, frame.dropna()))
    rate = tables.get("RATE")
    if rate is not None:
        for des_col, actual_col, label in [("RDes", "R", "rate_roll_error"), ("PDes", "P", "rate_pitch_error"), ("YDes", "Y", "rate_yaw_error")]:
            if des_col in rate.columns and actual_col in rate.columns and "TimeS" in rate.columns:
                frame = rate[["TimeS"]].copy()
                frame[label] = (numeric_series(rate, [des_col]) - numeric_series(rate, [actual_col])).abs()
                frames.append((label, frame.dropna()))
    return frames


def _altitude_error(tables):
    ctun = tables.get("CTUN")
    if ctun is None or "TimeS" not in ctun.columns:
        return []
    out = []
    if "Alt" in ctun.columns and "DAlt" in ctun.columns:
        frame = ctun[["TimeS"]].copy()
        frame["altitude_error"] = (numeric_series(ctun, ["DAlt"]) - numeric_series(ctun, ["Alt"])).abs()
        out.append(("altitude_error", frame.dropna()))
    return out


def _ekf_ratio_series(tables):
    out = []
    for group in ekf_instance_groups(tables, ("XKF4", "NKF4", "XKF3", "NKF3")):
        ekf = group["df"]
        if "TimeS" not in ekf.columns:
            continue
        label = group["label"] if group.get("instance_certain") else group["message"]
        for col in ["SV", "SP", "SH", "SM", "SVT"]:
            if col in ekf.columns:
                frame = ekf[["TimeS"]].copy()
                frame[f"{label}.{col}"] = numeric_series(ekf, [col]).abs()
                out.append((f"{label}.{col}", frame.dropna()))
    return out


def _load_series(tables):
    out = []
    ctun = tables.get("CTUN")
    if ctun is not None and "TimeS" in ctun.columns:
        col = get_col(ctun, ["ThO", "ThH", "ThI"])
        if col:
            out.append(("throttle", _time_value(ctun, col, "throttle")))
    rcou = combined_rcout_dataframe(tables)
    if rcou is not None:
        channels = output_channel_columns(rcou)
        if channels:
            frame = rcou[["TimeS", *channels]].copy() if "TimeS" in rcou.columns else rcou[channels].copy()
            if "TimeS" not in frame.columns:
                frame.insert(0, "TimeS", range(len(frame)))
            frame["motor_output_mean"] = frame[channels].mean(axis=1)
            out.append(("motor_output_mean", frame[["TimeS", "motor_output_mean"]].dropna()))
    for group in battery_instance_groups(tables):
        bat = group["df"]
        col = get_col(bat, ["Curr", "I"])
        if col:
            out.append((f"{group['label']} current", _time_value(bat, col, "current")))
    return [(name, frame) for name, frame in out if frame is not None]


def _aligned_corr(vibe, other, other_col, tolerance=0.35):
    if vibe is None or other is None or "TimeS" not in vibe.columns or "TimeS" not in other.columns:
        return None
    try:
        import pandas as pd
        left = vibe[["TimeS", "vibe_max_axis"]].dropna().copy()
        right = other[["TimeS", other_col]].dropna().copy()
        left["TimeS"] = pd.to_numeric(left["TimeS"], errors="coerce").astype(float)
        right["TimeS"] = pd.to_numeric(right["TimeS"], errors="coerce").astype(float)
        merged = pd.merge_asof(
            left.dropna().sort_values("TimeS"),
            right.dropna().sort_values("TimeS"),
            on="TimeS",
            direction="nearest",
            tolerance=tolerance,
        ).dropna()
        if len(merged) < 4:
            return None
        corr = merged["vibe_max_axis"].corr(merged[other_col])
        if corr is None or corr != corr:
            return None
        return float(corr)
    except Exception:
        return None


def build_vibration_assessment(
    full_tables: Dict[str, Any],
    symptom_class: str = "general_investigation",
    *,
    window_tables: Optional[Dict[str, Any]] = None,
    analysis_window: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    window_tables = window_tables or full_tables
    assessment = {
        "vibration_context": _vibration_summary(full_tables),
        "vibration_relevance_to_symptom": {
            "analysis_window": analysis_window or {"start_s": None, "end_s": None},
            "event_window_summary": _vibration_summary(window_tables),
            "correlations": [],
            "evidence": [],
        },
        "vibration_confidence_limits": [],
    }
    if not assessment["vibration_context"].get("available"):
        assessment["vibration_confidence_limits"].append("VIBE missing; vibration contribution cannot be assessed.")
        return assessment

    window_summary = assessment["vibration_relevance_to_symptom"]["event_window_summary"]
    window_is_specific = bool(
        analysis_window
        and (
            analysis_window.get("rule") not in {None, "whole_log"}
            or analysis_window.get("start_s") is not None
            or analysis_window.get("end_s") is not None
        )
    )
    if window_summary.get("above_warning_threshold") and (window_is_specific or symptom_class == "vibration_issue"):
        max_axis = window_summary.get("max_axis")
        assessment["vibration_relevance_to_symptom"]["evidence"].append(f"VIBE max axis {max_axis:.1f} m/s/s within analysis window")
    for col, delta in window_summary.get("clip_delta", {}).items():
        if delta > 0 and (window_is_specific or symptom_class == "vibration_issue"):
            assessment["vibration_relevance_to_symptom"]["evidence"].append(f"VIBE.{col} increased by {delta:.0f} within analysis window")

    vibe = _vibe_frame(window_tables)
    series = []
    series.extend(_attitude_rate_error(window_tables))
    series.extend(_altitude_error(window_tables))
    series.extend(_ekf_ratio_series(window_tables))
    series.extend(_load_series(window_tables))
    for label, frame in series:
        col = [c for c in frame.columns if c != "TimeS"][0]
        corr = _aligned_corr(vibe, frame, col, tolerance=0.5)
        if corr is None:
            continue
        item = {"target": label, "correlation": corr}
        assessment["vibration_relevance_to_symptom"]["correlations"].append(item)
        if abs(corr) >= 0.70 and window_summary.get("above_warning_threshold"):
            assessment["vibration_relevance_to_symptom"]["evidence"].append(f"VIBE max-axis correlates with {label} (r={corr:.2f})")

    if assessment["vibration_context"].get("above_warning_threshold") and not assessment["vibration_relevance_to_symptom"]["evidence"]:
        assessment["vibration_confidence_limits"].append("High whole-log VIBE values are present, but no analysis-window or correlation support ties them to the selected symptom.")
    return assessment


def add_vibration_assessment_findings(assessment, findings, checked, *, rank=4, symptom_class="general_investigation"):
    context = assessment.get("vibration_context", {})
    relevance = assessment.get("vibration_relevance_to_symptom", {})
    evidence = relevance.get("evidence", [])
    if not context.get("available"):
        checked.append({"check": "VIBE vibration/clipping", "result": "VIBE missing; no vibration conclusion drawn"})
        return
    if evidence:
        findings.append({
            "rank": rank,
            "possible_cause": "Vibration/clipping is relevant to the selected symptom timing",
            "severity": "safety-critical",
            "confidence": "medium",
            "evidence": evidence[:12],
            "interpretation": "Vibration is only treated as symptom-relevant because it occurs in the selected/event window or correlates with control, altitude, estimator, throttle, or current evidence. This supports correlation, not proof of causation.",
            "recommended_checks": ["Inspect props, motor bearings, frame resonance, flight-controller mounting and loose wiring", "Use raw IMU or batch-sampling FFT when frequency-domain filter review is needed"],
        })
    elif symptom_class == "vibration_issue" and context.get("above_warning_threshold"):
        findings.append({
            "rank": rank,
            "possible_cause": "High vibration reported for vibration-focused symptom",
            "severity": "likely-issue",
            "confidence": "medium",
            "evidence": [f"Whole-log VIBE max axis {context.get('max_axis'):.1f} m/s/s exceeds warning threshold"],
            "interpretation": "The selected symptom is vibration/noise itself, so whole-log VIBE warning levels are directly relevant. For other symptoms, timing/correlation support is required.",
            "recommended_checks": ["Inspect mechanical vibration sources", "Use raw IMU or batch-sampling FFT if available"],
        })
    else:
        detail = "No VIBE timing/correlation evidence supports vibration as relevant to this symptom"
        if context.get("above_warning_threshold"):
            detail += f"; whole-log max axis={context.get('max_axis'):.1f} m/s/s retained as context"
        checked.append({"check": "VIBE relevance to symptom", "result": detail})
