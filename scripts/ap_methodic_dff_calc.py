#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from statistics import mean, median
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ap_common import AnalysisError, clip_columns, collect_dataflash, ensure_dir, numeric_series, safe_float, write_json
from ap_methodic_711_motor_oscillation import analyze_motor_outputs, analyze_vibration, summarize_values
from ap_methodic_rc import analyze_rc_input_contamination

METHODIC_97_URL = "https://ardupilot.github.io/MethodicConfigurator/TUNING_GUIDE_ArduCopter#97-angle-rate-derivative-feed-forward-calculation"
LOG_MESSAGES_URL = "https://ardupilot.org/copter/docs/logmessages.html"
MESSAGES = [
    "RATE",
    "PIDR",
    "PIDP",
    "PIDY",
    "RCIN",
    "RCOU",
    "RCO2",
    "RCO3",
    "ATT",
    "VIBE",
    "BAT",
    "POWR",
    "PARM",
    "MODE",
    "MSG",
    "EV",
    "ERR",
    "ARM",
]
AXES = {
    "roll": {"rate": "R", "rate_des": "RDes", "out": "ROut", "pid": "PIDR", "dff": "ATC_RAT_RLL_D_FF", "rc": "roll"},
    "pitch": {"rate": "P", "rate_des": "PDes", "out": "POut", "pid": "PIDP", "dff": "ATC_RAT_PIT_D_FF", "rc": "pitch"},
    "yaw": {"rate": "Y", "rate_des": "YDes", "out": "YOut", "pid": "PIDY", "dff": "ATC_RAT_YAW_D_FF", "rc": "yaw"},
}
RELEVANT_PARAMETERS = [
    "ATC_RAT_RLL_D_FF",
    "ATC_RAT_PIT_D_FF",
    "ATC_RAT_YAW_D_FF",
    "ATC_RAT_RLL_D",
    "ATC_RAT_PIT_D",
    "ATC_RAT_YAW_D",
    "INS_HNTCH_*",
    "INS_GYRO_FILTER",
]


def rows_to_tables(rows_by_message: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    try:
        import pandas as pd
    except Exception as exc:
        raise AnalysisError("pandas is required for Methodic D_FF review. Install dependencies with: pip install -r requirements.txt") from exc
    return {name: pd.DataFrame(rows) for name, rows in rows_by_message.items() if rows}


def analyze_dff_calc(
    log_path: str | Path,
    *,
    axes: list[str] | None = None,
    plots_dir: str | Path | None = None,
) -> dict[str, Any]:
    requested_axes = normalize_axes(axes)
    rows, index, stats = collect_dataflash(log_path, include=MESSAGES, source=str(log_path))
    tables = rows_to_tables(rows)
    params = index.get("parameters", {}) or {}

    result = empty_result(params, requested_axes)
    result["analysis_window"]["parser_stats"] = stats
    result["analysis_window"].update(log_window(tables))
    result["missing_evidence"] = missing_evidence(tables)
    result["evidence_used"].append({"type": "messages_present", "messages": sorted(tables.keys())})

    rc = analyze_rc_input_contamination(tables, params)
    motor = analyze_motor_outputs(tables, None, params)
    vibration = analyze_vibration(tables, None)
    power = analyze_power(tables)
    sample_rate = sample_rate_context(tables.get("RATE"))
    axis_results = {axis: analyze_axis_candidate(axis, tables, rc, params) for axis in requested_axes}
    blockers = safety_blockers(tables, motor, vibration, sample_rate)

    result["candidate_dff"] = {
        axis: data["candidate_dff"]
        for axis, data in axis_results.items()
        if data.get("candidate_dff") is not None
    }
    result["axis_results"] = axis_results
    result["confidence"] = confidence(axis_results, blockers, result["missing_evidence"])
    result["reason_not_recommended"] = reason_not_recommended(axis_results, blockers, result["missing_evidence"])
    result["validation_required"] = True
    result["evidence_used"].extend([
        {"type": "rc_input_contamination", "value": trim_rc(rc)},
        {"type": "axis_dff_evidence", "value": axis_results},
        {"type": "mapped_motor_outputs", "value": motor},
        {"type": "vibration", "value": vibration},
        {"type": "power", "value": power},
        {"type": "sample_rate", "value": sample_rate},
    ])
    result["findings"] = classify_findings(axis_results, blockers, result["missing_evidence"])
    result["checked_but_not_supported"] = checked_but_not_supported(tables, axis_results, motor, vibration, power)
    result["result"], result["safety_gate"] = classify_result(axis_results, blockers, result["missing_evidence"])
    result["recommended_next_steps"] = recommended_next_steps(result)
    result["what_not_to_do"] = what_not_to_do()
    result["next_methodic_step"] = next_methodic_step(result["result"])
    result["confidence_limits"] = confidence_limits(result, rc, sample_rate)
    if plots_dir:
        result["plots"] = make_plots(tables, Path(plots_dir), requested_axes, axis_results)
    return result


def empty_result(params: dict[str, Any], axes: list[str]) -> dict[str, Any]:
    return {
        "methodic_step": "9.7",
        "title": "Derivative feed-forward calculation",
        "official_reference": {"url": METHODIC_97_URL, "supporting_urls": [LOG_MESSAGES_URL]},
        "candidate_dff": {},
        "axis_results": {},
        "confidence": "low",
        "reason_not_recommended": [],
        "validation_required": True,
        "result": "inconclusive",
        "safety_gate": "repeat_step",
        "evidence_used": [],
        "missing_evidence": [],
        "manual_observations_required": ["Vehicle already passed performance evaluation", "Frame resonance is under control", "AutoTune is complete", "No oscillation during aggressive inputs"],
        "analysis_window": {"selection": "whole_log", "preferred_window": "Aggressive isolated rate changes after frame resonance is controlled and AutoTune is complete.", "start_s": None, "end_s": None, "requested_axes": axes},
        "findings": [],
        "checked_but_not_supported": [],
        "parameter_context": parameter_context(params),
        "plots": [],
        "recommended_next_steps": [],
        "what_not_to_do": [],
        "next_methodic_step": None,
        "confidence_limits": [],
    }


def normalize_axes(axes: list[str] | None) -> list[str]:
    if not axes:
        return ["roll", "pitch", "yaw"]
    out = []
    for item in axes:
        for part in str(item).split(","):
            axis = part.strip().lower()
            if axis:
                if axis not in AXES:
                    raise AnalysisError(f"Unsupported D_FF axis: {axis}")
                out.append(axis)
    return sorted(set(out), key=["roll", "pitch", "yaw"].index)


def parameter_context(params: dict[str, Any]) -> dict[str, Any]:
    present: dict[str, Any] = {}
    missing: list[str] = []
    for pattern in RELEVANT_PARAMETERS:
        if "*" in pattern:
            prefix, suffix = pattern.split("*", 1)
            matches = {k: v for k, v in params.items() if k.startswith(prefix) and k.endswith(suffix)}
            if matches:
                present.update(matches)
            else:
                missing.append(pattern)
        elif pattern in params:
            present[pattern] = params[pattern]
        else:
            missing.append(pattern)
    return {"relevant_parameters": RELEVANT_PARAMETERS, "present": present, "missing_or_not_logged": missing, "source": "log PARM messages" if params else "no PARM messages found"}


def missing_evidence(tables: dict[str, Any]) -> list[str]:
    missing = []
    if "RATE" not in tables:
        missing.append("Missing required message: RATE")
    for name in ("PIDR", "PIDP", "PIDY", "RCIN", "RCOU", "ATT", "VIBE", "BAT", "PARM"):
        if name not in tables:
            missing.append(f"Missing strongly recommended message: {name}")
    if "POWR" not in tables:
        missing.append("Missing strongly recommended message: POWR")
    return missing


def log_window(tables: dict[str, Any]) -> dict[str, Any]:
    times = []
    for df in tables.values():
        times.extend(time_values(df))
    if not times:
        return {"selection": "none", "start_s": None, "end_s": None}
    return {"selection": "whole_log", "start_s": float(min(times)), "end_s": float(max(times))}


def analyze_axis_candidate(axis: str, tables: dict[str, Any], rc: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    rate = tables.get("RATE")
    spec = AXES[axis]
    if rate is None or len(rate) == 0:
        return {"axis": axis, "candidate_dff": None, "confidence": "low", "reason_not_recommended": ["RATE missing"], "samples_used": 0}
    times = time_values(rate)
    actual = series_values(rate, spec["rate"])
    desired = series_values(rate, spec["rate_des"])
    output = series_values(rate, spec["out"])
    count = min(len(times), len(actual), len(desired), len(output))
    if count < 20:
        return {"axis": axis, "candidate_dff": None, "confidence": "low", "reason_not_recommended": ["Too few RATE samples"], "samples_used": count}

    accel = angular_acceleration_rad_s2(times[:count], actual[:count])
    axis_active = active_mask(desired[:count], threshold=12.0 if axis != "yaw" else 7.0)
    other_active = other_axes_active_mask(tables, axis, count)
    rc_quality = axis_rc_quality(axis, rc)
    samples = []
    for idx in range(1, count):
        if not axis_active[idx] or other_active[idx]:
            continue
        if abs(accel[idx]) < (2.5 if axis != "yaw" else 1.5):
            continue
        out = output[idx]
        if abs(out) < 0.005 or abs(out) > 0.45:
            continue
        candidate = out / accel[idx]
        if candidate > 0 and candidate < 0.08:
            samples.append(candidate)
    current = safe_float(params.get(spec["dff"]))
    stats = summarize_values(samples) if samples else {"available": False, "samples": 0}
    variability = robust_variability(samples)
    isolation_percent = 100.0 * sum(1 for idx in range(1, count) if axis_active[idx] and not other_active[idx]) / max(sum(axis_active), 1)
    reasons = []
    if not samples:
        reasons.append("No clean isolated high-acceleration samples survived D_FF filtering.")
    if isolation_percent < 65.0:
        reasons.append("RC/rate command isolation is poor for this axis.")
    if rc_quality == "poor":
        reasons.append("RCIN axis isolation/coupling is poor.")
    if variability is not None and variability > 0.8:
        reasons.append("Candidate D_FF samples vary too much for a stable recommendation.")
    confidence_value = "high" if samples and len(samples) >= 20 and isolation_percent >= 80.0 and rc_quality != "poor" and (variability is None or variability <= 0.45) else "medium" if samples and len(samples) >= 8 and not reasons else "low"
    candidate_dff = median(samples) if confidence_value in {"medium", "high"} else None
    return {
        "axis": axis,
        "candidate_dff": candidate_dff,
        "current_dff": current,
        "confidence": confidence_value,
        "samples_used": len(samples),
        "candidate_stats": stats,
        "candidate_variability_ratio": variability,
        "angular_acceleration": summarize_values(accel[1:]),
        "rate_output_response": summarize_values(output[:count]),
        "isolation_percent": isolation_percent,
        "rc_axis_quality": rc_quality,
        "reason_not_recommended": reasons,
        "formula": "ATC_RAT_*_D_FF = RATE.*Out / angular_acceleration_rad_s_s",
    }


def angular_acceleration_rad_s2(times: list[float], rate_deg_s: list[float]) -> list[float]:
    accel = [0.0]
    for idx in range(1, min(len(times), len(rate_deg_s))):
        dt = times[idx] - times[idx - 1]
        if dt <= 0:
            accel.append(0.0)
        else:
            accel.append((rate_deg_s[idx] - rate_deg_s[idx - 1]) * math.pi / (180.0 * dt))
    return accel


def active_mask(values: list[float], threshold: float) -> list[bool]:
    return [abs(v) >= threshold for v in values]


def other_axes_active_mask(tables: dict[str, Any], axis: str, count: int) -> list[bool]:
    rate = tables.get("RATE")
    masks = []
    for other, spec in AXES.items():
        if other == axis:
            continue
        values = series_values(rate, spec["rate_des"])
        masks.append(active_mask(values[:count], threshold=12.0 if other != "yaw" else 7.0))
    out = []
    for idx in range(count):
        out.append(any(idx < len(mask) and mask[idx] for mask in masks))
    return out


def axis_rc_quality(axis: str, rc: dict[str, Any]) -> str:
    data = (rc.get("axis_activity") or {}).get(axis) or {}
    if not data.get("available"):
        return "unknown"
    active = ((data.get("active_percent_by_deadband_us") or {}).get("50") or (data.get("active_percent_by_deadband_us") or {}).get(50) or 0.0)
    other_axes = [item for name, item in (rc.get("axis_activity") or {}).items() if name != axis]
    other_active = max([
        ((item.get("active_percent_by_deadband_us") or {}).get("50") or (item.get("active_percent_by_deadband_us") or {}).get(50) or 0.0)
        for item in other_axes
    ] or [0.0])
    if active >= 5.0 and other_active <= active * 0.75 + 2.0:
        return "good"
    if active >= 2.0:
        return "marginal"
    return "poor"


def sample_rate_context(rate: Any) -> dict[str, Any]:
    times = time_values(rate)
    if len(times) < 3:
        return {"available": False, "sample_rate_hz": None, "sufficient": False}
    duration = max(times) - min(times)
    hz = (len(times) - 1) / duration if duration > 0 else None
    return {"available": True, "samples": len(times), "duration_s": duration, "sample_rate_hz": hz, "sufficient": hz is not None and hz >= 20.0}


def safety_blockers(tables: dict[str, Any], motor: dict[str, Any], vibration: dict[str, Any], sample_rate: dict[str, Any]) -> list[dict[str, Any]]:
    blockers = []
    if "RATE" not in tables:
        blockers.append({"type": "missing_rate", "severity": "inconclusive", "reason": "RATE is required for D_FF calculation."})
    if sample_rate.get("available") and not sample_rate.get("sufficient"):
        blockers.append({"type": "sample_rate", "severity": "inconclusive", "reason": "RATE sample rate is too low for angular acceleration estimation.", "evidence": sample_rate})
    if vibration.get("available"):
        clips = vibration.get("clip_delta") or {}
        if any(v > 0 for v in clips.values()) or (vibration.get("p95_axis") is not None and vibration["p95_axis"] > 30.0) or (vibration.get("max_axis") is not None and vibration["max_axis"] > 45.0):
            blockers.append({"type": "vibration", "severity": "do_not_use", "reason": "Vibration or clipping blocks D_FF calculation.", "evidence": vibration})
    else:
        blockers.append({"type": "missing_vibe", "severity": "inconclusive", "reason": "VIBE missing; vibration prerequisite cannot be confirmed."})
    if motor.get("available"):
        saturated = [
            name for name, data in (motor.get("channels") or {}).items()
            if data.get("pct_high_ge_1900", 0.0) > 1.0 or data.get("pct_low_le_1100", 0.0) > 1.0 or data.get("persistent_high") or data.get("persistent_low")
        ]
        if saturated:
            blockers.append({"type": "saturation", "severity": "do_not_use", "reason": "Motor output saturation/headroom issue blocks D_FF calculation.", "channels": saturated[:12]})
    else:
        blockers.append({"type": "missing_outputs", "severity": "inconclusive", "reason": "RCOU/RCO2/RCO3 missing; actuator saturation cannot be ruled out."})
    return blockers


def classify_findings(axis_results: dict[str, Any], blockers: list[dict[str, Any]], missing: list[str]) -> list[dict[str, Any]]:
    findings = []
    for blocker in blockers:
        findings.append({"severity": blocker["severity"], "finding": blocker["reason"], "evidence": blocker})
    if any(item.startswith("Missing required") for item in missing):
        findings.append({"severity": "inconclusive", "finding": "Required D_FF evidence is missing.", "evidence": missing})
    for axis, data in axis_results.items():
        if data.get("candidate_dff") is not None:
            findings.append({"severity": "candidate", "finding": f"{axis} has a D_FF candidate from isolated high-acceleration samples.", "evidence": data})
        else:
            findings.append({"severity": "inconclusive", "finding": f"{axis} does not have enough clean evidence for a D_FF candidate.", "evidence": data})
    return findings


def classify_result(axis_results: dict[str, Any], blockers: list[dict[str, Any]], missing: list[str]) -> tuple[str, str]:
    if any(blocker.get("severity") == "do_not_use" for blocker in blockers):
        return "do_not_use", "do_not_proceed"
    if any(item.startswith("Missing required") for item in missing):
        return "inconclusive", "repeat_step"
    if any(blocker.get("severity") == "inconclusive" for blocker in blockers):
        return "inconclusive", "repeat_step"
    if any(data.get("candidate_dff") is not None for data in axis_results.values()):
        return "candidate", "proceed_with_caution"
    return "inconclusive", "repeat_step"


def confidence(axis_results: dict[str, Any], blockers: list[dict[str, Any]], missing: list[str]) -> str:
    if blockers or any(item.startswith("Missing required") for item in missing):
        return "low"
    values = [data.get("confidence") for data in axis_results.values()]
    if values and all(v == "high" for v in values):
        return "high"
    if any(v in {"high", "medium"} for v in values):
        return "medium"
    return "low"


def reason_not_recommended(axis_results: dict[str, Any], blockers: list[dict[str, Any]], missing: list[str]) -> list[str]:
    reasons = [item for item in missing if item.startswith("Missing required")]
    reasons.extend(blocker["reason"] for blocker in blockers)
    for axis, data in axis_results.items():
        if data.get("candidate_dff") is None:
            for reason in data.get("reason_not_recommended") or ["Insufficient clean manoeuvre evidence."]:
                reasons.append(f"{axis}: {reason}")
    return reasons


def checked_but_not_supported(tables: dict[str, Any], axis_results: dict[str, Any], motor: dict[str, Any], vibration: dict[str, Any], power: dict[str, Any]) -> list[str]:
    checked = []
    if "RATE" in tables:
        checked.append("RATE angular acceleration and output response were checked.")
    if "RCIN" in tables:
        checked.append("RC input axis isolation was checked.")
    if motor.get("available"):
        checked.append("Motor output saturation/headroom was checked.")
    if vibration.get("available"):
        checked.append("VIBE/clipping was checked.")
    if power.get("available"):
        checked.append("Battery/board power context was checked.")
    for axis, data in axis_results.items():
        checked.append(f"{axis} D_FF candidate evidence was checked with {data.get('samples_used', 0)} retained samples.")
    return checked


def recommended_next_steps(result: dict[str, Any]) -> list[str]:
    if result["result"] == "candidate":
        return [
            "Agent should inspect the RATE, angular acceleration, RATE output, RCIN, motor output, and VIBE plots before presenting any D_FF candidate.",
            "Treat candidate values as review candidates only; any external parameter application requires a controlled validation flight and fresh log review.",
            "Apply candidates only outside this tool after Methodic prerequisites are confirmed: frame resonance controlled, AutoTune complete, and performance evaluation acceptable.",
        ]
    if result["result"] == "do_not_use":
        return [
            "Do not compute or apply D_FF from this log.",
            "Fix vibration, clipping, actuator saturation, or logging quality first, then repeat controlled isolated manoeuvres if Methodic prerequisites remain satisfied.",
        ]
    return [
        "Repeat evidence capture only after ensuring clean isolated manoeuvres, adequate angular acceleration, sufficient RATE sample rate, no saturation, and acceptable vibration.",
        "Do not infer D_FF from whole-log averages or coupled stick inputs.",
    ]


def what_not_to_do() -> list[str]:
    return [
        "Do not automatically write or upload ATC_RAT_*_D_FF or PSC_ACCZ_D_FF parameters.",
        "Do not compute/apply D_FF if vibration, clipping, actuator saturation, poor RC isolation, or poor logging is present.",
        "Do not skip validation; D_FF changes actuator demand and pilot feel.",
        "Do not present candidates as final tuning conclusions without agent inspection of the evidence.",
    ]


def next_methodic_step(result: str) -> str | None:
    if result == "candidate":
        return "10.1 or Section 13 only after external D_FF validation, if D_FF work is accepted"
    if result == "do_not_use":
        return "Do not calculate/apply D_FF from unsuitable data"
    return "10.1 only if D_FF work is optional/deferred"


def confidence_limits(result: dict[str, Any], rc: dict[str, Any], sample_rate: dict[str, Any]) -> list[str]:
    limits = []
    if result["missing_evidence"]:
        limits.append("Missing log messages limit D_FF evidence quality.")
    if not rc.get("available"):
        limits.append("RCIN is missing; axis isolation depends only on RATE desired signals.")
    if not sample_rate.get("sufficient"):
        limits.append("RATE sample rate may be too low for robust angular acceleration estimates.")
    limits.append("Candidate values require external application and validation flight before acceptance.")
    return limits


def analyze_power(tables: dict[str, Any]) -> dict[str, Any]:
    out = {"available": False, "messages": {}}
    for name, fields in {"BAT": ["Volt", "VoltR", "Curr"], "POWR": ["Vcc", "Vservo", "Flags"]}.items():
        df = tables.get(name)
        if df is None or len(df) == 0:
            continue
        msg = {}
        for field in fields:
            values = series_values(df, field)
            if values:
                msg[field] = {"min": min(values), "max": max(values), "mean": mean(values), "range": max(values) - min(values), "samples": len(values)}
        out["messages"][name] = msg
    out["available"] = bool(out["messages"])
    return out


def make_plots(tables: dict[str, Any], plots_dir: Path, axes: list[str], axis_results: dict[str, Any]) -> list[str]:
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except Exception:
        return []
    out = ensure_dir(plots_dir)
    plots: list[str] = []
    rate = tables.get("RATE")
    if rate is not None and "TimeS" in getattr(rate, "columns", []):
        fig = make_subplots(rows=len(axes), cols=1, shared_xaxes=True, subplot_titles=[f"{axis} RATE target/actual/output" for axis in axes])
        for row_idx, axis in enumerate(axes, start=1):
            spec = AXES[axis]
            for col in (spec["rate_des"], spec["rate"], spec["out"]):
                if col in rate.columns:
                    fig.add_trace(go.Scatter(x=rate["TimeS"], y=numeric_series(rate, [col]), mode="lines", name=f"{axis}.{col}"), row=row_idx, col=1)
        fig.update_layout(title="Methodic 9.7 RATE target/actual/output", template="plotly_white", hovermode="x unified")
        path = out / "methodic_9_7_rate_target_actual_output.html"
        fig.write_html(str(path), include_plotlyjs="cdn")
        plots.append(str(path))

        fig = make_subplots(rows=len(axes), cols=1, shared_xaxes=True, subplot_titles=[f"{axis} angular acceleration" for axis in axes])
        times = time_values(rate)
        for row_idx, axis in enumerate(axes, start=1):
            actual = series_values(rate, AXES[axis]["rate"])
            accel = angular_acceleration_rad_s2(times[: len(actual)], actual)
            fig.add_trace(go.Scatter(x=times[: len(accel)], y=accel, mode="lines", name=f"{axis} accel rad/s/s"), row=row_idx, col=1)
        fig.update_layout(title="Methodic 9.7 angular acceleration estimate", template="plotly_white", hovermode="x unified")
        path = out / "methodic_9_7_angular_acceleration.html"
        fig.write_html(str(path), include_plotlyjs="cdn")
        plots.append(str(path))

    for group_name, title in (("RCIN", "RCIN input"), ("RCOU", "motor outputs"), ("VIBE", "VIBE / clipping")):
        path = plot_group(tables, group_name, title, out)
        if path:
            plots.append(path)
    return plots


def plot_group(tables: dict[str, Any], group_name: str, title: str, out: Path) -> str | None:
    try:
        import plotly.graph_objects as go
    except Exception:
        return None
    df = tables.get(group_name)
    if df is None or "TimeS" not in getattr(df, "columns", []):
        return None
    if group_name in {"RCIN", "RCOU"}:
        cols = [c for c in df.columns if str(c).startswith("C") and str(c)[1:].isdigit()]
    elif group_name == "VIBE":
        cols = [c for c in ["VibeX", "VibeY", "VibeZ", *clip_columns(df)] if c in df.columns]
    else:
        cols = []
    if not cols:
        return None
    fig = go.Figure()
    for col in cols:
        fig.add_trace(go.Scatter(x=df["TimeS"], y=numeric_series(df, [col]), mode="lines", name=f"{group_name}.{col}"))
    fig.update_layout(title=f"Methodic 9.7 {title}", template="plotly_white", hovermode="x unified")
    path = out / f"methodic_9_7_{group_name.lower()}.html"
    fig.write_html(str(path), include_plotlyjs="cdn")
    return str(path)


def trim_rc(rc: dict[str, Any]) -> dict[str, Any]:
    axes = {}
    for axis, data in (rc.get("axis_activity") or {}).items():
        axes[axis] = {
            "available": data.get("available"),
            "channel": data.get("channel"),
            "active_percent_by_deadband_us": data.get("active_percent_by_deadband_us"),
            "centered_percent": data.get("centered_percent"),
            "mapping_source": data.get("mapping_source"),
        }
    return {"available": rc.get("available"), "hands_off_confidence": rc.get("hands_off_confidence"), "axes": axes, "warnings": rc.get("warnings")}


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


def robust_variability(values: list[float]) -> float | None:
    if len(values) < 4:
        return None
    med = median(values)
    if abs(med) < 1e-9:
        return None
    deviations = [abs(v - med) for v in values]
    return median(deviations) / abs(med)


def write_summary(path: Path, result: dict[str, Any]) -> None:
    lines = [
        "# Methodic 9.7 Derivative Feed-Forward Review",
        "",
        f"- Result: `{result['result']}`",
        f"- Safety gate: `{result['safety_gate']}`",
        f"- Confidence: `{result['confidence']}`",
        f"- Validation required: `{result['validation_required']}`",
        f"- Official reference: {result['official_reference']['url']}",
        "",
        "## Candidate D_FF",
    ]
    if result["candidate_dff"]:
        lines.extend(f"- {axis}: `{value:.6f}`" for axis, value in result["candidate_dff"].items())
    else:
        lines.append("- No candidate values produced.")
    lines.extend(["", "## Reasons Not Recommended"])
    if result["reason_not_recommended"]:
        lines.extend(f"- {item}" for item in result["reason_not_recommended"])
    else:
        lines.append("- None reported by the script.")
    lines.extend(["", "## Recommended Next Steps"])
    lines.extend(f"- {item}" for item in result["recommended_next_steps"])
    lines.extend(["", "## What Not To Do"])
    lines.extend(f"- {item}" for item in result["what_not_to_do"])
    ensure_dir(path.parent)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Gather deterministic Methodic derivative feed-forward calculation evidence for step 9.7.")
    parser.add_argument("log", help="ArduPilot DataFlash .BIN/.log file")
    parser.add_argument("--axis", default="roll,pitch,yaw", help="Comma-separated axes: roll,pitch,yaw")
    parser.add_argument("--out", default=None, help="Write structured JSON result")
    parser.add_argument("--summary", default=None, help="Write Markdown summary")
    parser.add_argument("--plots", default=None, help="Directory for generated plots")
    args = parser.parse_args()
    try:
        result = analyze_dff_calc(args.log, axes=[args.axis], plots_dir=args.plots)
        if args.out:
            write_json(args.out, result)
        if args.summary:
            write_summary(Path(args.summary), result)
        print(json.dumps(result, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
