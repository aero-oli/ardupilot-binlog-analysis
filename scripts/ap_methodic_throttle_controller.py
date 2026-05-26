#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from statistics import mean, median
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ap_common import AnalysisError, clip_columns, collect_dataflash, ensure_dir, numeric_series, rows_to_dataframe, safe_float, write_json
from ap_methodic_rc import analyze_rc_input_contamination
from ap_methodic_windows import select_methodic_window

METHODIC_8_2_URL = "https://ardupilot.github.io/MethodicConfigurator/TUNING_GUIDE_ArduCopter#82-configure-the-throttle-controller"
MESSAGES = [
    "CTUN",
    "ATT",
    "RATE",
    "RCOU",
    "RCO2",
    "RCO3",
    "PARM",
    "BAT",
    "POWR",
    "BARO",
    "VIBE",
    "ESC",
    "ESCX",
    "EDT2",
    "GPS",
    "XKF4",
    "RNGF",
    "MODE",
    "RCIN",
    "ARM",
]
PARAMETERS = [
    "MOT_THST_HOVER",
    "MOT_HOVER_LEARN",
    "MOT_THST_EXPO",
    "MOT_SPIN_MIN",
    "PILOT_THR_BHV",
    "PSC_ACCZ_P",
    "PSC_ACCZ_I",
    "PSC_ACCZ_D",
    "PSC_VELZ_P",
    "PSC_VELZ_I",
    "PSC_POSZ_P",
    "BATT_*",
    "MOT_BAT_*",
]


def rows_to_tables(rows_by_message: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    return {name: rows_to_dataframe(rows) for name, rows in rows_by_message.items() if rows}


def analyze_throttle_controller(log_path: str | Path, *, plots_dir: str | Path | None = None) -> dict[str, Any]:
    rows, index, stats = collect_dataflash(log_path, include=MESSAGES, source=str(log_path))
    tables = rows_to_tables(rows)
    params = index.get("parameters", {}) or {}
    result = empty_result(params)
    result["analysis_window"]["parser_stats"] = stats
    result["evidence_used"].append({"type": "messages_present", "messages": sorted(tables.keys())})

    missing_required = required_missing(tables, params)
    result["missing_evidence"].extend(missing_required)

    try:
        hover_selection = select_methodic_window(tables, "methodic_hover", min_duration_s=5.0)
    except Exception as exc:
        hover_selection = {"selected_window": None, "candidate_windows": [], "warnings": [str(exc)], "confidence": "low"}
    hover = hover_selection.get("selected_window")
    result["analysis_window"].update({
        "selection": "methodic_hover" if hover else "none",
        "start_s": hover.get("start_s") if hover else None,
        "end_s": hover.get("end_s") if hover else None,
        "methodic_selector": hover_selection,
    })
    result["evidence_used"].append({"type": "hover_window_selection", "value": hover_selection})
    result["confidence_limits"].extend(hover_selection.get("warnings", []))
    if not hover:
        result["missing_evidence"].append("No stable hover window was selected for Methodic 8.2 throttle-controller review.")

    rc = analyze_rc_input_contamination(tables, params)
    result["evidence_used"].append({"type": "rc_input_contamination", "value": trim_rc(rc)})

    hover_throttle = analyze_hover_throttle(tables, hover, params)
    altitude = analyze_altitude_control(tables, hover)
    headroom = analyze_motor_headroom(tables, hover)
    power = analyze_power(tables, hover)
    vibration = analyze_vibration(tables, hover)
    thrust = assess_thrust_margin(hover_throttle, headroom, power)

    result["hover_throttle_assessment"] = hover_throttle
    result["altitude_control_assessment"] = altitude
    result["power_thrust_headroom_assessment"] = {"motor_output_headroom": headroom, "power": power, "thrust_margin": thrust}
    result["evidence_used"].extend([
        {"type": "hover_throttle", "value": hover_throttle},
        {"type": "altitude_control", "value": altitude},
        {"type": "motor_output_headroom", "value": headroom},
        {"type": "power", "value": power},
        {"type": "vibration", "value": vibration},
    ])

    result["findings"].extend(classify_altitude_findings(altitude))
    result["findings"].extend(classify_throttle_findings(hover_throttle, thrust))
    result["findings"].extend(classify_headroom_findings(headroom))
    result["findings"].extend(classify_power_findings(power))
    result["findings"].extend(classify_vibration_findings(vibration))
    result["findings"].extend(classify_rc_findings(rc))
    result["checked_but_not_supported"] = checked_but_not_supported(tables)

    if "Missing required message: CTUN" in missing_required or not hover:
        result["result"] = "inconclusive" if "Missing required message: CTUN" in missing_required else "fail"
        result["safety_gate"] = "repeat_step"
    else:
        result["result"], result["safety_gate"] = classify_result(result["findings"])
    result["next_methodic_step"] = "8.3" if result["result"] in {"pass", "conditional_pass"} else "repeat_8.2"
    result["recommended_next_steps"] = recommended_next_steps(result, hover_throttle, altitude, power, vibration)
    result["what_not_to_do"] = [
        "Do not write throttle-controller parameter changes automatically from this script.",
        "Do not recommend changing throttle-controller values from a poor or unsafe hover.",
        "Do not proceed if altitude/throttle behaviour is unsafe, output headroom is poor, severe vibration is present, or power sag/brownout evidence exists.",
        "Describe any PSC/MOT parameter changes only as review candidates that require agent inspection and bench/flight validation.",
    ]
    if plots_dir:
        result["plots"] = make_plots(tables, Path(plots_dir), hover)
    return result


def empty_result(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "methodic_step": "8.2",
        "title": "Throttle controller review",
        "official_reference": {"url": METHODIC_8_2_URL, "anchor": "#82-configure-the-throttle-controller"},
        "result": "inconclusive",
        "safety_gate": "repeat_step",
        "hover_throttle_assessment": {},
        "altitude_control_assessment": {},
        "power_thrust_headroom_assessment": {},
        "evidence_used": [],
        "missing_evidence": [],
        "manual_observations_required": ["Stable altitude hold without bounce or pumping", "No hard-to-control behaviour"],
        "analysis_window": {
            "selection": "none",
            "preferred_window": "Stable AltHold hover after Methodic 7.1.1 and 8.1 blockers are resolved.",
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
    present = {}
    missing = []
    for name in PARAMETERS:
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
    return {"relevant_parameters": PARAMETERS, "present": present, "missing_or_not_logged": missing, "source": "log PARM messages" if params else "no PARM messages found"}


def required_missing(tables: dict[str, Any], params: dict[str, Any]) -> list[str]:
    missing = []
    for name in ("CTUN", "ATT", "RATE"):
        if name not in tables:
            missing.append(f"Missing required message: {name}")
    if not any(name in tables for name in ("RCOU", "RCO2", "RCO3")):
        missing.append("Missing required message: RCOU/RCO2/RCO3")
    if not params:
        missing.append("Missing required message: PARM")
    return missing


def slice_table(df: Any, window: dict[str, Any] | None):
    if df is None or not window or "TimeS" not in getattr(df, "columns", []):
        return df
    start = window.get("start_s")
    end = window.get("end_s")
    if start is None or end is None:
        return df
    return df[(df["TimeS"] >= start) & (df["TimeS"] <= end)]


def series_values(df: Any, col: str | None) -> list[float]:
    if df is None or not col:
        return []
    s = numeric_series(df, [col])
    if s is None:
        return []
    return [float(v) for v in s.dropna().tolist()]


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * pct / 100.0))))
    return float(ordered[idx])


def summarize(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"available": False, "samples": 0}
    return {
        "available": True,
        "samples": len(values),
        "min": min(values),
        "max": max(values),
        "mean": mean(values),
        "median": median(values),
        "p95": percentile(values, 95),
        "p95_abs": percentile([abs(v) for v in values], 95),
        "range": max(values) - min(values),
    }


def analyze_hover_throttle(tables: dict[str, Any], window: dict[str, Any] | None, params: dict[str, Any]) -> dict[str, Any]:
    ctun = slice_table(tables.get("CTUN"), window)
    if ctun is None or len(ctun) == 0:
        return {"available": False, "reason": "CTUN missing or no CTUN samples in selected hover window."}
    tho = series_values(ctun, "ThO")
    thh = series_values(ctun, "ThH")
    mot_hover = safe_float(params.get("MOT_THST_HOVER"))
    out = {
        "available": bool(tho or thh),
        "throttle_output": summarize(tho),
        "throttle_hover_logged": summarize(thh),
        "mot_thst_hover_parameter": mot_hover,
        "assessment": "inconclusive",
    }
    hover_value = out["throttle_output"].get("median") if tho else out["throttle_hover_logged"].get("median")
    if hover_value is None:
        return out
    out["hover_throttle_median"] = hover_value
    out["delta_median_tho_minus_mot_thst_hover"] = (hover_value - mot_hover) if mot_hover is not None else None
    if hover_value < 0.20:
        out["assessment"] = "overpowered_or_hover_throttle_low"
    elif hover_value > 0.75:
        out["assessment"] = "underpowered_or_hover_throttle_high"
    elif hover_value > 0.65:
        out["assessment"] = "limited_thrust_margin"
    else:
        out["assessment"] = "plausible"
    return out


def analyze_altitude_control(tables: dict[str, Any], window: dict[str, Any] | None) -> dict[str, Any]:
    ctun = slice_table(tables.get("CTUN"), window)
    if ctun is None or len(ctun) == 0:
        return {"available": False, "reason": "CTUN missing or unavailable in selected hover window."}
    alt = series_values(ctun, "Alt")
    dalt = series_values(ctun, "DAlt")
    baro = series_values(slice_table(tables.get("BARO"), window), "Alt")
    error = [a - d for a, d in zip(alt, dalt)] if alt and dalt else []
    climb_rate = []
    if alt and "TimeS" in ctun.columns:
        times = [safe_float(v) for v in ctun["TimeS"].tolist()]
        for prev_t, t, prev_alt, cur_alt in zip(times, times[1:], alt, alt[1:]):
            if None not in (prev_t, t, prev_alt, cur_alt) and t != prev_t:
                climb_rate.append((cur_alt - prev_alt) / (t - prev_t))
    span = max(alt) - min(alt) if alt else None
    error_p95 = percentile([abs(v) for v in error], 95) if error else None
    rate_p95 = percentile([abs(v) for v in climb_rate], 95) if climb_rate else None
    assessment = "inconclusive"
    if span is not None and span > 2.0:
        assessment = "poor"
    elif error_p95 is not None and error_p95 > 1.0:
        assessment = "poor"
    elif span is not None and span > 0.75:
        assessment = "marginal"
    elif rate_p95 is not None and rate_p95 > 0.5:
        assessment = "marginal"
    elif span is not None:
        assessment = "stable"
    return {
        "available": bool(alt),
        "ctun_altitude": summarize(alt),
        "ctun_desired_altitude": summarize(dalt),
        "ctun_alt_minus_dalt": summarize(error),
        "baro_altitude": summarize(baro),
        "climb_descent_rate_m_s": summarize(climb_rate),
        "altitude_span_m": span,
        "altitude_error_p95_m": error_p95,
        "climb_descent_rate_p95_m_s": rate_p95,
        "assessment": assessment,
    }


def analyze_motor_headroom(tables: dict[str, Any], window: dict[str, Any] | None) -> dict[str, Any]:
    channels = {}
    for name in ("RCOU", "RCO2", "RCO3"):
        df = slice_table(tables.get(name), window)
        if df is None or len(df) == 0:
            continue
        for col in [c for c in df.columns if str(c).startswith("C") and str(c)[1:].isdigit()]:
            vals = series_values(df, col)
            if vals:
                channels[f"{name}.{col}"] = {
                    **summarize(vals),
                    "pct_low_le_1100": 100.0 * sum(1 for v in vals if v <= 1100) / len(vals),
                    "pct_high_ge_1900": 100.0 * sum(1 for v in vals if v >= 1900) / len(vals),
                    "unit": "PWM us",
                }
    return {"available": bool(channels), "channels": channels}


def analyze_power(tables: dict[str, Any], window: dict[str, Any] | None) -> dict[str, Any]:
    full_bat = tables.get("BAT")
    bat = slice_table(full_bat, window)
    powr = slice_table(tables.get("POWR"), window)
    out = {"available": bat is not None or powr is not None}
    if bat is not None and len(bat):
        volts = series_values(bat, "Volt") or series_values(bat, "VoltR") or series_values(bat, "V")
        full_volts = series_values(full_bat, "Volt") or series_values(full_bat, "VoltR") or series_values(full_bat, "V")
        curr = series_values(bat, "Curr") or series_values(bat, "I")
        out["voltage"] = summarize(volts)
        out["full_log_voltage"] = summarize(full_volts)
        out["current"] = summarize(curr)
        if volts:
            vmin = min(volts)
            vmax_reference = max(full_volts) if full_volts else max(volts)
            out["voltage_sag_ratio"] = vmin / vmax_reference if vmax_reference > 0 else None
            out["voltage_sag_reference"] = "hover_min_divided_by_full_log_max"
    if powr is not None and len(powr):
        out["vcc"] = summarize(series_values(powr, "Vcc") or series_values(powr, "VccMin"))
    if not out["available"]:
        out["warning"] = "BAT/POWR missing; battery sag and board power cannot be assessed."
    return out


def analyze_vibration(tables: dict[str, Any], window: dict[str, Any] | None) -> dict[str, Any]:
    vibe = slice_table(tables.get("VIBE"), window)
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
    max_axis = max((v["max"] for v in axes.values()), default=None)
    return {"available": True, "axes": axes, "max_axis": max_axis, "clip_delta": clip_delta}


def assess_thrust_margin(hover_throttle: dict[str, Any], headroom: dict[str, Any], power: dict[str, Any]) -> dict[str, Any]:
    signs = []
    hover = hover_throttle.get("hover_throttle_median")
    if hover is not None and hover > 0.75:
        signs.append("hover throttle very high")
    elif hover is not None and hover > 0.65:
        signs.append("hover throttle high")
    if hover is not None and hover < 0.20:
        signs.append("hover throttle very low")
    for channel, data in (headroom.get("channels") or {}).items():
        if data.get("pct_high_ge_1900", 0.0) > 5.0:
            signs.append(f"{channel} high saturation")
        if data.get("pct_low_le_1100", 0.0) > 10.0:
            signs.append(f"{channel} low saturation")
    sag = power.get("voltage_sag_ratio")
    if sag is not None and sag < 0.85:
        signs.append("battery sag affects thrust margin")
    assessment = "poor" if any("saturation" in s or "very high" in s for s in signs) else ("limited" if signs else "acceptable")
    return {"assessment": assessment, "signs": signs}


def classify_altitude_findings(altitude: dict[str, Any]) -> list[dict[str, Any]]:
    if not altitude.get("available"):
        return [{"severity": "inconclusive", "finding": altitude.get("reason", "Altitude control evidence missing.")}]
    if altitude.get("assessment") == "poor":
        return [{"severity": "fail", "safety_gate": "do_not_proceed", "finding": "Altitude hold tracking/stability is poor in the hover window.", "evidence": altitude}]
    if altitude.get("assessment") == "marginal":
        return [{"severity": "conditional", "finding": "Altitude hold stability is marginal in the hover window.", "evidence": altitude}]
    return [{"severity": "info", "finding": "Altitude hold evidence is stable enough for throttle-controller review.", "evidence": altitude}]


def classify_throttle_findings(hover: dict[str, Any], thrust: dict[str, Any]) -> list[dict[str, Any]]:
    if not hover.get("available"):
        return [{"severity": "inconclusive", "finding": hover.get("reason", "Hover throttle evidence missing.")}]
    if thrust.get("assessment") == "poor":
        return [{"severity": "fail", "safety_gate": "do_not_proceed", "finding": "Hover throttle/thrust margin suggests poor thrust headroom.", "evidence": {"hover_throttle": hover, "thrust": thrust}}]
    if thrust.get("assessment") == "limited" or hover.get("assessment") in {"limited_thrust_margin", "overpowered_or_hover_throttle_low"}:
        return [{"severity": "conditional", "finding": "Hover throttle is plausible but thrust margin or scaling deserves review.", "evidence": {"hover_throttle": hover, "thrust": thrust}}]
    return [{"severity": "info", "finding": "Hover throttle is in a plausible range.", "evidence": hover}]


def classify_headroom_findings(headroom: dict[str, Any]) -> list[dict[str, Any]]:
    if not headroom.get("available"):
        return [{"severity": "inconclusive", "finding": "Motor output headroom evidence is missing."}]
    for channel, data in headroom.get("channels", {}).items():
        if data.get("pct_high_ge_1900", 0.0) > 5.0 or data.get("pct_low_le_1100", 0.0) > 10.0:
            return [{"severity": "fail", "safety_gate": "do_not_proceed", "finding": f"Motor output headroom is poor on {channel}.", "evidence": data}]
    return []


def classify_power_findings(power: dict[str, Any]) -> list[dict[str, Any]]:
    if not power.get("available"):
        return [{"severity": "conditional", "finding": power.get("warning", "Power evidence missing.")}]
    findings = []
    sag = power.get("voltage_sag_ratio")
    if sag is not None and sag < 0.75:
        findings.append({"severity": "fail", "safety_gate": "bench_check_required", "finding": "Severe battery voltage sag was detected during hover.", "evidence": power})
    elif sag is not None and sag < 0.90:
        findings.append({"severity": "conditional", "finding": "Battery voltage sag limits throttle-controller confidence.", "evidence": power})
    vcc = power.get("vcc") or {}
    if vcc.get("min") is not None and vcc["min"] < 4.7:
        findings.append({"severity": "fail", "safety_gate": "bench_check_required", "finding": "Board power Vcc dropped below 4.7 V.", "evidence": power})
    return findings


def classify_vibration_findings(vibration: dict[str, Any]) -> list[dict[str, Any]]:
    if not vibration.get("available"):
        return [{"severity": "conditional", "finding": vibration.get("warning", "VIBE missing.")}]
    max_axis = vibration.get("max_axis") or 0.0
    clip_delta = vibration.get("clip_delta") or {}
    if max_axis > 60.0 or any((delta or 0.0) > 0 for delta in clip_delta.values()):
        return [{"severity": "fail", "safety_gate": "bench_check_required", "finding": "Severe vibration or clipping blocks throttle-controller review.", "evidence": vibration}]
    if max_axis > 30.0:
        return [{"severity": "conditional", "finding": "Vibration is high enough to limit throttle-controller confidence.", "evidence": vibration}]
    return []


def classify_rc_findings(rc: dict[str, Any]) -> list[dict[str, Any]]:
    if not rc.get("available"):
        return []
    if rc.get("hands_off_confidence") == "low":
        return [{"severity": "conditional", "finding": "RC input contamination limits hover/throttle-controller confidence.", "evidence": trim_rc(rc)}]
    return []


def classify_result(findings: list[dict[str, Any]]) -> tuple[str, str]:
    if any(item.get("safety_gate") == "bench_check_required" for item in findings):
        return "fail", "bench_check_required"
    if any(item.get("severity") == "fail" for item in findings):
        return "fail", "do_not_proceed"
    if any(item.get("severity") == "inconclusive" for item in findings):
        return "inconclusive", "repeat_step"
    if any(item.get("severity") == "conditional" for item in findings):
        return "conditional_pass", "proceed_with_caution"
    return "pass", "proceed"


def checked_but_not_supported(tables: dict[str, Any]) -> list[str]:
    out = []
    if "GPS" not in tables and "XKF4" not in tables and "RNGF" not in tables:
        out.append("GPS/XKF4/RNGF altitude-source context not available; CTUN/BARO evidence used where present")
    if "ESC" not in tables and "ESCX" not in tables and "EDT2" not in tables:
        out.append("ESC telemetry not available for motor temperature/current cross-check")
    return out


def recommended_next_steps(result: dict[str, Any], hover: dict[str, Any], altitude: dict[str, Any], power: dict[str, Any], vibration: dict[str, Any]) -> list[str]:
    if result["result"] == "pass":
        return [
            "Agent should inspect the hover-throttle, altitude tracking, power, and headroom evidence before accepting Methodic 8.2.",
            "If the evidence agrees, proceed to Methodic 8.3 only if notch/filter and output-oscillation gates remain satisfied.",
        ]
    steps = []
    if result["result"] == "inconclusive":
        steps.append("Collect a readable log with CTUN, ATT, RATE, RCOU/RCO2/RCO3, and PARM plus a stable AltHold hover before classifying Methodic 8.2.")
    if altitude.get("assessment") == "poor":
        steps.append("Do not change throttle-controller values from this poor hover; first resolve altitude/throttle behaviour and repeat a controlled hover capture.")
    if power.get("voltage_sag_ratio") is not None and power["voltage_sag_ratio"] < 0.90:
        steps.append("Review battery, wiring, current draw, and voltage compensation evidence before treating throttle-controller parameters as the cause.")
    if vibration.get("max_axis") and vibration["max_axis"] > 30.0:
        steps.append("Resolve vibration/clipping before throttle-controller tuning decisions.")
    if hover.get("assessment") in {"underpowered_or_hover_throttle_high", "limited_thrust_margin", "overpowered_or_hover_throttle_low"}:
        steps.append("Treat MOT_THST_HOVER, MOT_THST_EXPO, MOT_SPIN_MIN, and vertical PSC gains as review candidates only after confirming hover quality and thrust margin.")
    return steps or ["Resolve the listed conditional evidence limits before treating Methodic 8.2 as complete."]


def trim_rc(rc: dict[str, Any]) -> dict[str, Any]:
    axes = {}
    for axis, data in (rc.get("axis_activity") or {}).items():
        axes[axis] = {
            "available": data.get("available"),
            "channel": data.get("channel"),
            "active_percent_by_deadband_us": data.get("active_percent_by_deadband_us"),
            "centered_percent": data.get("centered_percent"),
        }
    return {"available": rc.get("available"), "hands_off_confidence": rc.get("hands_off_confidence"), "centered_percent": rc.get("centered_percent"), "warnings": rc.get("warnings"), "axes": axes}


def make_plots(tables: dict[str, Any], plots_dir: Path, hover_window: dict[str, Any] | None) -> list[str]:
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except Exception:
        return []
    out = ensure_dir(plots_dir)
    plots = []

    ctun = tables.get("CTUN")
    if ctun is not None and "TimeS" in ctun.columns:
        fig = make_subplots(rows=2, cols=1, shared_xaxes=True, subplot_titles=("Altitude", "Throttle"))
        for col in ("DAlt", "Alt", "BAlt"):
            if col in ctun.columns:
                fig.add_trace(go.Scatter(x=ctun["TimeS"], y=ctun[col], mode="lines", name=f"CTUN.{col}"), row=1, col=1)
        for col in ("ThO", "ThH"):
            if col in ctun.columns:
                fig.add_trace(go.Scatter(x=ctun["TimeS"], y=ctun[col], mode="lines", name=f"CTUN.{col}"), row=2, col=1)
        add_window(fig, hover_window)
        fig.update_layout(title="Methodic 8.2 CTUN altitude/throttle", template="plotly_white", hovermode="x unified")
        plots.append(write_plot(fig, out / "methodic_8_2_ctun_altitude_throttle.html"))

    for name, filename, title in [
        ("BAT", "methodic_8_2_bat_powr.html", "Methodic 8.2 BAT/POWR"),
        ("RCOU", "methodic_8_2_motor_outputs.html", "Methodic 8.2 motor outputs"),
        ("VIBE", "methodic_8_2_vibe.html", "Methodic 8.2 vibration"),
    ]:
        fig = go.Figure()
        names = ["BAT", "POWR"] if name == "BAT" else [name, "RCO2", "RCO3"] if name == "RCOU" else [name]
        for msg in names:
            df = tables.get(msg)
            if df is None or "TimeS" not in getattr(df, "columns", []):
                continue
            cols = [c for c in df.columns if c != "TimeS" and (msg != "VIBE" or c in {"VibeX", "VibeY", "VibeZ", *clip_columns(df)})]
            for col in cols[:16]:
                fig.add_trace(go.Scatter(x=df["TimeS"], y=df[col], mode="lines", name=f"{msg}.{col}"))
        if fig.data:
            add_window(fig, hover_window)
            fig.update_layout(title=title, template="plotly_white", hovermode="x unified")
            plots.append(write_plot(fig, out / filename))
    return plots


def add_window(fig: Any, window: dict[str, Any] | None) -> None:
    if window and window.get("start_s") is not None and window.get("end_s") is not None:
        fig.add_vrect(x0=window["start_s"], x1=window["end_s"], fillcolor="#dbeafe", opacity=0.20, line_width=0)


def write_plot(fig: Any, path: Path) -> str:
    fig.write_html(str(path), include_plotlyjs="cdn")
    return str(path)


def write_summary(path: Path, result: dict[str, Any]) -> None:
    lines = [
        "# Methodic 8.2 Throttle Controller Review",
        "",
        f"- Result: `{result['result']}`",
        f"- Safety gate: `{result['safety_gate']}`",
        f"- Next Methodic step: `{result['next_methodic_step']}`",
        f"- Official reference: {METHODIC_8_2_URL}",
        "",
        "## Findings",
    ]
    lines.extend(f"- {item.get('severity', 'info')}: {item.get('finding')}" for item in result["findings"]) if result["findings"] else lines.append("- No deterministic finding was produced.")
    lines.extend(["", "## Missing Evidence"])
    lines.extend(f"- {item}" for item in result["missing_evidence"]) if result["missing_evidence"] else lines.append("- None reported by deterministic checks.")
    lines.extend(["", "## Recommended Next Steps"])
    lines.extend(f"- {item}" for item in result["recommended_next_steps"])
    lines.extend(["", "## What Not To Do"])
    lines.extend(f"- {item}" for item in result["what_not_to_do"])
    ensure_dir(path.parent)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Gather deterministic Methodic 8.2 throttle-controller evidence.")
    parser.add_argument("log", help="ArduPilot DataFlash .BIN/.log file")
    parser.add_argument("--out", default="out/methodic_8_2.json")
    parser.add_argument("--summary", default=None)
    parser.add_argument("--plots", default=None)
    args = parser.parse_args()
    try:
        result = analyze_throttle_controller(args.log, plots_dir=args.plots)
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
