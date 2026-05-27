#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from statistics import mean
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ap_common import AnalysisError, clip_columns, collect_dataflash, ensure_dir, numeric_series, safe_float, write_json

METHODIC_102_URL = "https://ardupilot.github.io/MethodicConfigurator/TUNING_GUIDE_ArduCopter#102-baro-compensation-flights"
LOG_MESSAGES_URL = "https://ardupilot.org/copter/docs/logmessages.html"
MESSAGES = [
    "BARO",
    "CTUN",
    "GPS",
    "GPS2",
    "GPA",
    "XKF4",
    "NKF4",
    "XKF3",
    "NKF3",
    "ATT",
    "RATE",
    "MODE",
    "VIBE",
    "BAT",
    "POWR",
    "PARM",
    "RNGF",
    "WIND",
    "MSG",
    "EV",
    "ERR",
    "ARM",
]
RELEVANT_PARAMETERS = [
    "BARO*_WCF_*",
    "BARO*_GND_PRESS",
    "EK3_*",
    "RNGFND*_TYPE",
    "RNGFND*_ORIENT",
    "RNGFND*_MIN_CM",
    "RNGFND*_MAX_CM",
]


def rows_to_tables(rows_by_message: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    try:
        import pandas as pd
    except Exception as exc:
        raise AnalysisError("pandas is required for Methodic baro compensation review. Install dependencies with: pip install -r requirements.txt") from exc
    return {name: pd.DataFrame(rows) for name, rows in rows_by_message.items() if rows}


def analyze_baro_comp_review(log_path: str | Path, *, plots_dir: str | Path | None = None) -> dict[str, Any]:
    rows, index, stats = collect_dataflash(log_path, include=MESSAGES, source=str(log_path))
    tables = rows_to_tables(rows)
    params = index.get("parameters", {}) or {}

    baro = analyze_baro(tables)
    ctun = analyze_ctun(tables)
    gps = analyze_gps(tables)
    ekf = analyze_ekf_height(tables)
    segment_quality = analyze_test_segment_quality(tables, gps)
    wind_sensitivity = analyze_baro_wind_sensitivity(tables, baro, gps, ctun, segment_quality)
    correlations = analyze_correlations(tables, baro, gps, ctun)
    vibration = analyze_vibration(tables)
    power = analyze_power(tables)
    rngf = analyze_rangefinder(tables)

    result = empty_result(params)
    result["analysis_window"]["parser_stats"] = stats
    result["analysis_window"].update(log_window(tables))
    result["baro_wind_sensitivity"] = wind_sensitivity
    result["height_innovation"] = ekf
    result["baro_altitude_correlation"] = correlations
    result["test_segment_quality"] = segment_quality
    result["evidence_used"] = [
        {"type": "messages_present", "messages": sorted(tables.keys())},
        {"type": "baro", "value": baro},
        {"type": "ctun_altitude", "value": ctun},
        {"type": "gps_altitude_speed", "value": gps},
        {"type": "ekf_height", "value": ekf},
        {"type": "baro_wind_sensitivity", "value": wind_sensitivity},
        {"type": "baro_altitude_correlation", "value": correlations},
        {"type": "test_segment_quality", "value": segment_quality},
        {"type": "vibration", "value": vibration},
        {"type": "power", "value": power},
        {"type": "rangefinder", "value": rngf},
    ]
    result["missing_evidence"] = missing_evidence(tables, baro, ctun, gps, ekf, vibration, power)
    result["findings"] = classify_findings(baro, ctun, gps, ekf, segment_quality, wind_sensitivity, correlations, vibration, power, rngf, result["missing_evidence"])
    result["checked_but_not_supported"] = checked_but_not_supported(tables, baro, ctun, gps, ekf, vibration, power, rngf)
    result["result"], result["safety_gate"] = classify_result(result["findings"], result["missing_evidence"], segment_quality, wind_sensitivity, vibration)
    result["next_methodic_step"] = next_methodic_step(result["result"])
    result["recommended_next_steps"] = recommended_next_steps(result)
    result["what_not_to_do"] = what_not_to_do()
    result["confidence_limits"] = confidence_limits(result, segment_quality, wind_sensitivity, ekf)
    if plots_dir:
        result["plots"] = make_plots(tables, Path(plots_dir), segment_quality.get("segments") or [])
    return result


def empty_result(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "methodic_step": "10.2",
        "title": "Barometer compensation",
        "official_reference": {"url": METHODIC_102_URL, "supporting_urls": [LOG_MESSAGES_URL]},
        "result": "inconclusive",
        "safety_gate": "repeat_step",
        "baro_wind_sensitivity": {},
        "height_innovation": {},
        "baro_altitude_correlation": {},
        "test_segment_quality": {},
        "evidence_used": [],
        "missing_evidence": [],
        "manual_observations_required": ["Known wind exposure condition", "No unsafe altitude behaviour", "Pilot confirms forward-flight/baro-compensation test segment"],
        "analysis_window": {"selection": "whole_log", "preferred_window": "Baro compensation forward-flight/wind-exposure test segments after wind/drag setup.", "start_s": None, "end_s": None},
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


def missing_evidence(tables: dict[str, Any], baro: dict[str, Any], ctun: dict[str, Any], gps: dict[str, Any], ekf: dict[str, Any], vibration: dict[str, Any], power: dict[str, Any]) -> list[str]:
    missing = []
    if not baro.get("available"):
        missing.append("Missing required message: BARO")
    for name, available in (("CTUN", ctun.get("available")), ("GPS/GPS2", gps.get("available")), ("XKF4/NKF4", ekf.get("available")), ("VIBE", vibration.get("available")), ("BAT/POWR", power.get("available"))):
        if not available:
            missing.append(f"Missing strongly recommended message: {name}")
    for name in ("ATT", "RATE", "MODE", "PARM"):
        if name not in tables:
            missing.append(f"Missing strongly recommended message: {name}")
    return missing


def log_window(tables: dict[str, Any]) -> dict[str, Any]:
    times = []
    for df in tables.values():
        times.extend(time_values(df))
    if not times:
        return {"selection": "none", "start_s": None, "end_s": None}
    return {"selection": "whole_log", "start_s": float(min(times)), "end_s": float(max(times))}


def analyze_baro(tables: dict[str, Any]) -> dict[str, Any]:
    df = tables.get("BARO")
    if df is None or len(df) == 0:
        return {"available": False, "status": "missing"}
    alt = series_values(df, first_col(df, ["Alt", "AltAMSL", "H"]))
    press = series_values(df, first_col(df, ["Press", "PressAbs", "P"]))
    return {
        "available": bool(alt or press),
        "altitude": summarize(alt),
        "pressure": summarize(press),
        "time_window": window_from_times(time_values(df)),
    }


def analyze_ctun(tables: dict[str, Any]) -> dict[str, Any]:
    df = tables.get("CTUN")
    if df is None or len(df) == 0:
        return {"available": False, "status": "missing"}
    alt = series_values(df, first_col(df, ["Alt", "BAlt"]))
    dalt = series_values(df, first_col(df, ["DAlt", "TAlt"]))
    err = [a - d for a, d in zip(alt, dalt)] if alt and dalt else []
    return {
        "available": bool(alt),
        "altitude": summarize(alt),
        "desired_altitude": summarize(dalt),
        "altitude_error": summarize(err),
        "time_window": window_from_times(time_values(df)),
    }


def analyze_gps(tables: dict[str, Any]) -> dict[str, Any]:
    df = first_table(tables, ["GPS", "GPS2"])
    if df is None or len(df) == 0:
        return {"available": False, "status": "missing"}
    alt = series_values(df, first_col(df, ["Alt", "RelAlt", "RAlt"]))
    speed = series_values(df, first_col(df, ["Spd", "GSpd", "Vel", "Speed"]))
    if not speed:
        vn = series_values(df, first_col(df, ["VN", "Vn"]))
        ve = series_values(df, first_col(df, ["VE", "Ve"]))
        speed = [math.sqrt(vn[i] ** 2 + ve[i] ** 2) for i in range(min(len(vn), len(ve)))]
    return {
        "available": bool(alt or speed),
        "altitude": summarize(alt),
        "ground_speed": summarize(speed),
        "time_window": window_from_times(time_values(df)),
    }


def analyze_ekf_height(tables: dict[str, Any]) -> dict[str, Any]:
    out = {"available": False, "height_test_ratio": None, "innovation": {}, "status": "missing"}
    for name in ("XKF4", "NKF4"):
        df = tables.get(name)
        if df is None or len(df) == 0:
            continue
        sh = series_values(df, first_col(df, ["SH", "SV", "SP"]))
        out["available"] = True
        out["height_test_ratio"] = {"source": name, **summarize(sh)}
        if sh and max(sh) > 1.0:
            out["status"] = "fail"
        elif sh and percentile(sh, 95) and percentile(sh, 95) > 0.7:
            out["status"] = "suspect"
        else:
            out["status"] = "usable"
        break
    for name in ("XKF3", "NKF3"):
        df = tables.get(name)
        if df is None:
            continue
        for col in ("IH", "IVD", "VD", "D"):
            vals = series_values(df, col)
            if vals:
                out["innovation"][f"{name}.{col}"] = summarize(vals)
    return out


def analyze_test_segment_quality(tables: dict[str, Any], gps: dict[str, Any]) -> dict[str, Any]:
    speed = (gps.get("ground_speed") or {})
    speed_range = speed.get("range")
    speed_p95 = speed.get("p95_abs") or speed.get("max")
    att = tables.get("ATT")
    pitch = series_values(att, first_col(att, ["Pitch"])) if att is not None else []
    roll = series_values(att, first_col(att, ["Roll"])) if att is not None else []
    dynamic_attitude = max(percentile([abs(v) for v in pitch], 95) or 0.0, percentile([abs(v) for v in roll], 95) or 0.0)
    gps_tw = gps.get("time_window") or {}
    duration = None
    if gps_tw.get("start_s") is not None and gps_tw.get("end_s") is not None:
        duration = gps_tw["end_s"] - gps_tw["start_s"]
    hover_only = (speed_range is None or speed_range < 2.0) and (speed_p95 is None or speed_p95 < 3.0)
    quality = "good"
    reasons = []
    if hover_only:
        quality = "hover_only"
        reasons.append("Ground-speed variation is too small; do not infer baro compensation from hover-only data.")
    if duration is not None and duration < 10.0:
        quality = "poor"
        reasons.append("Test segment duration is too short.")
    if dynamic_attitude < 3.0 and not hover_only:
        reasons.append("Attitude variation is low; confirm the intended forward-flight/wind-exposure manoeuvre manually.")
    return {
        "quality": quality,
        "hover_only": hover_only,
        "speed_range_m_s": speed_range,
        "speed_p95_m_s": speed_p95,
        "attitude_abs_p95_deg": dynamic_attitude,
        "duration_s": duration,
        "segments": [] if duration is None else [{"start_s": gps_tw.get("start_s"), "end_s": gps_tw.get("end_s"), "duration_s": duration, "selection": "whole GPS-overlap test candidate"}],
        "reasons": reasons,
    }


def analyze_baro_wind_sensitivity(tables: dict[str, Any], baro: dict[str, Any], gps: dict[str, Any], ctun: dict[str, Any], segment_quality: dict[str, Any]) -> dict[str, Any]:
    if not baro.get("available") or not gps.get("available"):
        return {"available": False, "status": "missing"}
    baro_df = tables.get("BARO")
    gps_df = first_table(tables, ["GPS", "GPS2"])
    if baro_df is None or gps_df is None:
        return {"available": False, "status": "missing"}
    baro_alt = series_values(baro_df, first_col(baro_df, ["Alt", "AltAMSL", "H"]))
    speed = series_values(gps_df, first_col(gps_df, ["Spd", "GSpd", "Vel", "Speed"]))
    count = min(len(baro_alt), len(speed))
    corr = pearson(speed[:count], detrend(baro_alt[:count])) if count >= 5 else None
    alt_span = (baro.get("altitude") or {}).get("range")
    status = "insensitive"
    if segment_quality.get("hover_only"):
        status = "not_tested"
    elif corr is not None and abs(corr) >= 0.65 and alt_span is not None and alt_span > 0.5:
        status = "sensitive"
    elif corr is None:
        status = "inconclusive"
    return {
        "available": True,
        "status": status,
        "speed_vs_baro_alt_detrended_correlation": corr,
        "baro_altitude_span_m": alt_span,
        "interpretation": "Correlation supports review only in a valid forward-flight/wind-exposure segment; it is not an automatic compensation setting.",
    }


def analyze_correlations(tables: dict[str, Any], baro: dict[str, Any], gps: dict[str, Any], ctun: dict[str, Any]) -> dict[str, Any]:
    out = {"available": False, "items": {}}
    baro_df = tables.get("BARO")
    if baro_df is None:
        return out
    baro_alt = series_values(baro_df, first_col(baro_df, ["Alt", "AltAMSL", "H"]))
    baro_press = series_values(baro_df, first_col(baro_df, ["Press", "PressAbs", "P"]))
    gps_df = first_table(tables, ["GPS", "GPS2"])
    if gps_df is not None:
        gps_alt = series_values(gps_df, first_col(gps_df, ["Alt", "RelAlt", "RAlt"]))
        speed = series_values(gps_df, first_col(gps_df, ["Spd", "GSpd", "Vel", "Speed"]))
        out["items"]["gps_alt_vs_baro_alt"] = pearson(detrend(gps_alt), detrend(baro_alt))
        out["items"]["speed_vs_baro_alt"] = pearson(speed, detrend(baro_alt))
        out["items"]["speed_vs_baro_pressure"] = pearson(speed, detrend(baro_press))
    ctun_df = tables.get("CTUN")
    if ctun_df is not None:
        ctun_alt = series_values(ctun_df, first_col(ctun_df, ["Alt", "BAlt"]))
        out["items"]["ctun_alt_vs_baro_alt"] = pearson(detrend(ctun_alt), detrend(baro_alt))
    out["available"] = any(value is not None for value in out["items"].values())
    return out


def analyze_vibration(tables: dict[str, Any]) -> dict[str, Any]:
    df = tables.get("VIBE")
    if df is None or len(df) == 0:
        return {"available": False}
    axes = {}
    for col in ("VibeX", "VibeY", "VibeZ"):
        vals = series_values(df, col)
        if vals:
            axes[col] = summarize([abs(v) for v in vals])
    clips = {}
    for col in clip_columns(df):
        vals = series_values(df, col)
        if len(vals) > 1:
            clips[col] = max(vals) - min(vals)
    return {"available": True, "axes": axes, "p95_axis": max((a.get("p95_abs") or 0.0 for a in axes.values()), default=None), "max_axis": max((a.get("max") or 0.0 for a in axes.values()), default=None), "clip_delta": clips}


def analyze_power(tables: dict[str, Any]) -> dict[str, Any]:
    out = {"available": False, "messages": {}}
    for name, fields in {"BAT": ["Volt", "VoltR", "Curr"], "POWR": ["Vcc", "Vservo", "Flags"]}.items():
        df = tables.get(name)
        if df is None:
            continue
        msg = {}
        for field in fields:
            vals = series_values(df, field)
            if vals:
                msg[field] = summarize(vals)
        out["messages"][name] = msg
    out["available"] = bool(out["messages"])
    return out


def analyze_rangefinder(tables: dict[str, Any]) -> dict[str, Any]:
    df = tables.get("RNGF")
    if df is None or len(df) == 0:
        return {"available": False, "status": "missing"}
    dist = series_values(df, first_col(df, ["Dist", "Range", "D"]))
    status_vals = series_values(df, first_col(df, ["Status", "Stat"]))
    drop_pct = 100.0 * sum(1 for v in dist if v <= 0.0) / len(dist) if dist else None
    if status_vals:
        drop_pct = max(drop_pct or 0.0, 100.0 * sum(1 for v in status_vals if v <= 0.0) / len(status_vals))
    status = "usable" if drop_pct is None or drop_pct < 5.0 else "suspect"
    return {"available": True, "status": status, "distance": summarize(dist), "dropout_percent": drop_pct}


def classify_findings(baro: dict[str, Any], ctun: dict[str, Any], gps: dict[str, Any], ekf: dict[str, Any], segment_quality: dict[str, Any], wind_sensitivity: dict[str, Any], correlations: dict[str, Any], vibration: dict[str, Any], power: dict[str, Any], rngf: dict[str, Any], missing: list[str]) -> list[dict[str, Any]]:
    findings = []
    if any(item.startswith("Missing required") for item in missing):
        findings.append(finding("inconclusive", "Required BARO evidence is missing.", missing))
    if segment_quality.get("hover_only"):
        findings.append(finding("inconclusive", "Baro compensation cannot be inferred from hover-only data.", segment_quality))
    elif segment_quality.get("quality") == "poor":
        findings.append(finding("repeat", "Baro compensation test segment quality is poor.", segment_quality))
    if wind_sensitivity.get("status") == "sensitive":
        findings.append(finding("compensation_needed", "BARO altitude/pressure behaviour correlates with speed in the candidate test segment.", wind_sensitivity))
    elif wind_sensitivity.get("status") == "insensitive":
        findings.append({"severity": "info", "finding": "BARO evidence did not show strong speed/wind sensitivity.", "evidence": wind_sensitivity})
    if ekf.get("status") in {"fail", "suspect"}:
        findings.append(finding("repeat", "EKF height test ratio or innovation evidence limits baro compensation confidence.", ekf))
    if vibration.get("available"):
        clips = vibration.get("clip_delta") or {}
        if any(v > 0 for v in clips.values()) or (vibration.get("p95_axis") is not None and vibration["p95_axis"] > 30.0) or (vibration.get("max_axis") is not None and vibration["max_axis"] > 45.0):
            findings.append(finding("hardware", "Severe vibration or clipping requires hardware/sensor review before baro compensation.", vibration, "bench_check_required"))
    if power.get("available"):
        flags = (((power.get("messages") or {}).get("POWR") or {}).get("Flags") or {})
        if flags.get("max", 0.0) > 0:
            findings.append(finding("hardware", "Board power flags are non-zero during baro compensation review.", flags, "bench_check_required"))
    if rngf.get("available") and rngf.get("status") == "suspect":
        findings.append(finding("repeat", "Rangefinder dropout may affect altitude evidence; separate rangefinder effects before baro compensation.", rngf))
    return findings


def classify_result(findings: list[dict[str, Any]], missing: list[str], segment_quality: dict[str, Any], wind_sensitivity: dict[str, Any], vibration: dict[str, Any]) -> tuple[str, str]:
    severities = {item.get("severity") for item in findings}
    if "hardware" in severities:
        return "repeat_flight", "bench_check_required"
    if "compensation_needed" in severities:
        return "compensation_needed", "proceed_with_caution"
    if any(item.startswith("Missing required") for item in missing):
        return "inconclusive", "repeat_step"
    if segment_quality.get("hover_only"):
        return "inconclusive", "repeat_step"
    if "repeat" in severities:
        return "repeat_flight", "repeat_step"
    if wind_sensitivity.get("status") == "insensitive" and segment_quality.get("quality") == "good":
        return "pass", "proceed_with_caution"
    return "inconclusive", "repeat_step"


def checked_but_not_supported(tables: dict[str, Any], baro: dict[str, Any], ctun: dict[str, Any], gps: dict[str, Any], ekf: dict[str, Any], vibration: dict[str, Any], power: dict[str, Any], rngf: dict[str, Any]) -> list[str]:
    checked = []
    for label, data in (("BARO altitude/pressure", baro), ("CTUN DAlt/Alt", ctun), ("GPS altitude/speed", gps), ("EKF height", ekf), ("VIBE/clipping", vibration), ("BAT/POWR", power), ("RNGF", rngf)):
        if data.get("available"):
            checked.append(f"{label} evidence was checked.")
    for name in ("ATT", "RATE", "MODE", "PARM"):
        if name in tables:
            checked.append(f"{name} context was checked.")
    return checked


def recommended_next_steps(result: dict[str, Any]) -> list[str]:
    if result["result"] == "compensation_needed":
        return [
            "Agent should inspect BARO, CTUN, GPS speed/altitude, EKF height, VIBE, and power plots before proposing any compensation review.",
            "Treat barometer compensation parameters only as external review candidates; do not auto-change them.",
            "Validate any externally applied compensation with a fresh comparable wind/forward-flight log.",
        ]
    if result["result"] == "pass":
        return [
            "If the agent agrees after plot inspection, document that this log did not show strong baro wind sensitivity in the tested envelope.",
            "Proceed only with the tested-envelope caveat; do not declare the aircraft safe from baro evidence alone.",
        ]
    if result["safety_gate"] == "bench_check_required":
        return [
            "Review hardware/sensor installation, vibration, clipping, power, and static pressure exposure before repeating baro compensation testing.",
            "Do not mask hardware or sensor exposure problems with compensation parameters.",
        ]
    if result["result"] == "repeat_flight":
        return [
            "Repeat the Methodic baro compensation evidence capture with a clear forward-flight/wind-exposure test segment after resolving listed data issues.",
            "Keep altitude behaviour conservative; do not run aggressive tests if altitude control is unsafe.",
        ]
    return [
        "Collect a readable non-hover baro compensation test log with BARO, CTUN, GPS/GPA, XKF4/NKF4, ATT/RATE, MODE, VIBE, BAT/POWR, PARM, and optional RNGF evidence.",
        "Do not infer compensation from hover-only or missing-data logs.",
    ]


def what_not_to_do() -> list[str]:
    return [
        "Do not automatically write BARO*_WCF_* or related compensation parameters.",
        "Do not infer baro compensation from hover-only data.",
        "Do not use compensation to hide poor barometer placement, airflow exposure, vibration, rangefinder faults, or power problems.",
        "Do not skip validation after any external baro compensation change.",
    ]


def next_methodic_step(result: str) -> str | None:
    if result in {"pass", "compensation_needed"}:
        return "11.1 after external validation and documented caveats"
    if result == "repeat_flight":
        return "repeat 10.2 after resolving data/hardware issues"
    return "10.2 remains incomplete"


def confidence_limits(result: dict[str, Any], segment_quality: dict[str, Any], wind_sensitivity: dict[str, Any], ekf: dict[str, Any]) -> list[str]:
    limits = []
    if result["missing_evidence"]:
        limits.append("Missing log messages limit baro compensation confidence.")
    if segment_quality.get("hover_only"):
        limits.append("Hover-only evidence cannot support baro wind compensation conclusions.")
    if wind_sensitivity.get("status") in {"sensitive", "insensitive"}:
        limits.append("Correlation is evidence for review, not proof of a specific compensation parameter value.")
    if not ekf.get("available"):
        limits.append("EKF height test-ratio evidence is missing.")
    return limits


def make_plots(tables: dict[str, Any], plots_dir: Path, segments: list[dict[str, Any]]) -> list[str]:
    try:
        import plotly.graph_objects as go
    except Exception:
        return []
    out = ensure_dir(plots_dir)
    plots = []
    for name, cols, title, filename in [
        ("BARO", ("Alt", "Press", "PressAbs"), "BARO altitude / pressure", "methodic_10_2_baro.html"),
        ("CTUN", ("DAlt", "Alt", "BAlt"), "CTUN desired/actual altitude", "methodic_10_2_ctun.html"),
        ("GPS", ("Alt", "RelAlt", "RAlt", "Spd", "GSpd"), "GPS altitude and speed", "methodic_10_2_gps.html"),
        ("XKF4", ("SH", "SV", "SP", "SM"), "EKF height/test ratios", "methodic_10_2_ekf.html"),
        ("VIBE", ("VibeX", "VibeY", "VibeZ"), "VIBE", "methodic_10_2_vibe.html"),
        ("ATT", ("Roll", "Pitch", "Yaw"), "speed/attitude context", "methodic_10_2_attitude.html"),
    ]:
        path = plot_group(tables, name, cols, title, out / filename, segments)
        if path:
            plots.append(path)
    if "GPS" not in tables and "GPS2" in tables:
        path = plot_group(tables, "GPS2", ("Alt", "RelAlt", "RAlt", "Spd", "GSpd"), "GPS2 altitude and speed", out / "methodic_10_2_gps2.html", segments)
        if path:
            plots.append(path)
    if "XKF4" not in tables and "NKF4" in tables:
        path = plot_group(tables, "NKF4", ("SH", "SV", "SP", "SM"), "NKF height/test ratios", out / "methodic_10_2_nkf.html", segments)
        if path:
            plots.append(path)
    if "RNGF" in tables:
        path = plot_group(tables, "RNGF", ("Dist", "Range", "Status"), "rangefinder", out / "methodic_10_2_rngf.html", segments)
        if path:
            plots.append(path)
    return plots


def plot_group(tables: dict[str, Any], name: str, cols: tuple[str, ...], title: str, path: Path, segments: list[dict[str, Any]]) -> str | None:
    try:
        import plotly.graph_objects as go
    except Exception:
        return None
    df = tables.get(name)
    if df is None or "TimeS" not in getattr(df, "columns", []):
        return None
    fig = go.Figure()
    found = False
    all_cols = list(cols)
    if name == "VIBE":
        all_cols.extend(clip_columns(df))
    for col in all_cols:
        if col in df.columns:
            fig.add_trace(go.Scatter(x=df["TimeS"], y=numeric_series(df, [col]), mode="lines", name=f"{name}.{col}"))
            found = True
    if not found:
        return None
    for seg in segments:
        if seg.get("start_s") is not None and seg.get("end_s") is not None:
            fig.add_vrect(x0=seg["start_s"], x1=seg["end_s"], fillcolor="LightGreen", opacity=0.16, line_width=0)
    fig.update_layout(title=f"Methodic 10.2 {title}", template="plotly_white", hovermode="x unified")
    ensure_dir(path.parent)
    fig.write_html(str(path), include_plotlyjs="cdn")
    return str(path)


def first_col(df: Any, names: list[str]) -> str | None:
    if df is None:
        return None
    lower = {str(c).lower(): c for c in getattr(df, "columns", [])}
    for name in names:
        if name in getattr(df, "columns", []):
            return name
        if name.lower() in lower:
            return lower[name.lower()]
    return None


def first_table(tables: dict[str, Any], names: list[str]) -> Any:
    for name in names:
        if tables.get(name) is not None:
            return tables[name]
    return None


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


def summarize(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"available": False, "samples": 0}
    return {"available": True, "samples": len(values), "min": min(values), "max": max(values), "mean": mean(values), "range": max(values) - min(values), "p95_abs": percentile([abs(v) for v in values], 95)}


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * pct / 100.0))))
    return float(ordered[idx])


def detrend(values: list[float]) -> list[float]:
    if len(values) < 2:
        return values
    start = values[0]
    end = values[-1]
    count = len(values) - 1
    return [v - (start + (end - start) * idx / count) for idx, v in enumerate(values)]


def pearson(a: list[float], b: list[float]) -> float | None:
    count = min(len(a), len(b))
    if count < 5:
        return None
    aa = a[:count]
    bb = b[:count]
    ma = mean(aa)
    mb = mean(bb)
    da = [v - ma for v in aa]
    db = [v - mb for v in bb]
    denom = math.sqrt(sum(v * v for v in da) * sum(v * v for v in db))
    if denom <= 1e-12:
        return None
    return sum(x * y for x, y in zip(da, db)) / denom


def window_from_times(times: list[float]) -> dict[str, Any]:
    if not times:
        return {"start_s": None, "end_s": None}
    return {"start_s": float(min(times)), "end_s": float(max(times))}


def finding(severity: str, text: str, evidence: Any, gate: str | None = None) -> dict[str, Any]:
    out = {"severity": severity, "finding": text, "evidence": evidence}
    if gate:
        out["safety_gate"] = gate
    return out


def write_summary(path: Path, result: dict[str, Any]) -> None:
    lines = [
        "# Methodic 10.2 Barometer Compensation Review",
        "",
        f"- Result: `{result['result']}`",
        f"- Safety gate: `{result['safety_gate']}`",
        f"- Official reference: {result['official_reference']['url']}",
        "",
        "## Findings",
    ]
    if result["findings"]:
        lines.extend(f"- {item.get('severity', 'info')}: {item.get('finding')}" for item in result["findings"])
    else:
        lines.append("- No findings reported by the script.")
    lines.extend(["", "## Recommended Next Steps"])
    lines.extend(f"- {item}" for item in result["recommended_next_steps"])
    lines.extend(["", "## What Not To Do"])
    lines.extend(f"- {item}" for item in result["what_not_to_do"])
    ensure_dir(path.parent)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Gather deterministic Methodic barometer compensation evidence for step 10.2.")
    parser.add_argument("log", help="ArduPilot DataFlash .BIN/.log file")
    parser.add_argument("--out", default=None, help="Write structured JSON result")
    parser.add_argument("--summary", default=None, help="Write Markdown summary")
    parser.add_argument("--plots", default=None, help="Directory for generated plots")
    args = parser.parse_args()
    try:
        result = analyze_baro_comp_review(args.log, plots_dir=args.plots)
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
