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

from ap_common import AnalysisError, collect_dataflash, ensure_dir, numeric_series, safe_float, write_json

METHODIC_101_URL = "https://ardupilot.github.io/MethodicConfigurator/TUNING_GUIDE_ArduCopter#101-windspeed-estimation-flights"
LOG_MESSAGES_URL = "https://ardupilot.org/copter/docs/logmessages.html"
MESSAGES = [
    "GPS",
    "GPS2",
    "GPA",
    "ATT",
    "RATE",
    "XKF1",
    "XKF2",
    "XKF3",
    "XKF4",
    "NKF1",
    "NKF2",
    "NKF3",
    "NKF4",
    "IMU",
    "ACC",
    "MODE",
    "RCIN",
    "BAT",
    "POWR",
    "PARM",
    "WIND",
    "POS",
    "MSG",
    "EV",
    "ERR",
    "ARM",
]
RELEVANT_PARAMETERS = [
    "EK3_DRAG_BCOEF_X",
    "EK3_DRAG_BCOEF_Y",
    "EK3_DRAG_MCOEF",
    "WPNAV_SPEED",
    "INS_HNTCH_*",
    "INS_GYRO_FILTER",
]


def rows_to_tables(rows_by_message: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    try:
        import pandas as pd
    except Exception as exc:
        raise AnalysisError("pandas is required for Methodic wind/drag review. Install dependencies with: pip install -r requirements.txt") from exc
    return {name: pd.DataFrame(rows) for name, rows in rows_by_message.items() if rows}


def analyze_wind_drag_review(
    log_path: str | Path,
    *,
    mass_kg: float | None = None,
    frontal_area_m2: float | None = None,
    side_area_m2: float | None = None,
    air_density_kg_m3: float = 1.225,
    wind_notes: str | None = None,
    plots_dir: str | Path | None = None,
) -> dict[str, Any]:
    rows, index, stats = collect_dataflash(log_path, include=MESSAGES, source=str(log_path))
    tables = rows_to_tables(rows)
    params = index.get("parameters", {}) or {}
    metadata = input_metadata(mass_kg, frontal_area_m2, side_area_m2, air_density_kg_m3, wind_notes)

    result = empty_result(params, metadata)
    result["analysis_window"]["parser_stats"] = stats
    result["analysis_window"].update(log_window(tables))
    result["missing_evidence"] = missing_evidence(tables)
    result["missing_inputs"] = missing_inputs(metadata)
    result["evidence_used"].append({"type": "messages_present", "messages": sorted(tables.keys())})

    speed = speed_evidence(tables)
    accel = acceleration_evidence(tables)
    wind = wind_evidence(tables, wind_notes)
    estimator = estimator_evidence(tables)
    segments = select_drag_segments(speed, accel)
    suitability = segment_suitability(segments, speed, accel, wind, estimator, result["missing_inputs"], result["missing_evidence"])
    coefficients = drag_coefficients(metadata, segments, speed, accel, suitability)

    result["drag_test_segments"] = segments
    result["windspeed_estimate_m_s"] = wind.get("estimate_m_s")
    result["drag_coefficients"] = coefficients
    result["confidence"] = classify_confidence(suitability, coefficients)
    result["assumptions"] = assumptions(metadata, wind, coefficients)
    result["evidence_used"].extend([
        {"type": "metadata", "value": metadata},
        {"type": "ground_speed", "value": speed},
        {"type": "acceleration", "value": accel},
        {"type": "wind", "value": wind},
        {"type": "estimator_consistency", "value": estimator},
        {"type": "segment_suitability", "value": suitability},
    ])
    result["findings"] = classify_findings(suitability, coefficients, result["missing_inputs"], result["missing_evidence"])
    result["checked_but_not_supported"] = checked_but_not_supported(tables, speed, accel, wind, estimator)
    result["result"], result["safety_gate"] = classify_result(suitability, coefficients, result["missing_inputs"], result["missing_evidence"])
    result["recommended_next_steps"] = recommended_next_steps(result)
    result["what_not_to_do"] = what_not_to_do()
    result["next_methodic_step"] = next_methodic_step(result["result"])
    result["confidence_limits"] = confidence_limits(result, wind, estimator)
    if plots_dir:
        result["plots"] = make_plots(tables, Path(plots_dir), segments)
    return result


def empty_result(params: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "methodic_step": "10.1",
        "title": "Wind estimation / drag coefficients",
        "official_reference": {"url": METHODIC_101_URL, "supporting_urls": [LOG_MESSAGES_URL]},
        "result": "inconclusive",
        "safety_gate": "repeat_step",
        "drag_test_segments": [],
        "windspeed_estimate_m_s": None,
        "drag_coefficients": {},
        "confidence": "low",
        "assumptions": [],
        "missing_inputs": [],
        "evidence_used": [],
        "missing_evidence": [],
        "manual_observations_required": ["Wind direction/speed context", "Known frontal/side area and vehicle mass", "Pilot confirms test direction and manoeuvre quality"],
        "analysis_window": {"selection": "whole_log", "preferred_window": "AltHold acceleration/deceleration tests in each direction with enough GPS speed and acceleration signal quality.", "start_s": None, "end_s": None},
        "findings": [],
        "checked_but_not_supported": [],
        "parameter_context": parameter_context(params),
        "plots": [],
        "recommended_next_steps": [],
        "what_not_to_do": [],
        "next_methodic_step": None,
        "confidence_limits": [],
        "input_metadata": metadata,
    }


def input_metadata(mass_kg: float | None, frontal_area_m2: float | None, side_area_m2: float | None, air_density_kg_m3: float, wind_notes: str | None) -> dict[str, Any]:
    return {
        "mass_kg": positive_or_none(mass_kg),
        "frontal_area_m2": positive_or_none(frontal_area_m2),
        "side_area_m2": positive_or_none(side_area_m2),
        "air_density_kg_m3": positive_or_none(air_density_kg_m3) or 1.225,
        "wind_notes": wind_notes,
    }


def positive_or_none(value: float | None) -> float | None:
    value = safe_float(value)
    return value if value is not None and value > 0 else None


def missing_inputs(metadata: dict[str, Any]) -> list[str]:
    missing = []
    for key in ("mass_kg", "frontal_area_m2", "side_area_m2"):
        if metadata.get(key) is None:
            missing.append(key)
    return missing


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
    if "GPS" not in tables and "GPS2" not in tables:
        missing.append("Missing required message: GPS/GPS2")
    if "IMU" not in tables and "ACC" not in tables:
        missing.append("Missing required message: IMU/ACC")
    for name in ("ATT", "RATE", "MODE", "RCIN", "BAT", "PARM"):
        if name not in tables:
            missing.append(f"Missing strongly recommended message: {name}")
    if not any(name in tables for name in ("XKF1", "XKF2", "XKF3", "XKF4", "NKF1", "NKF2", "NKF3", "NKF4")):
        missing.append("Missing strongly recommended message: XKF*/NKF*")
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


def speed_evidence(tables: dict[str, Any]) -> dict[str, Any]:
    gps = first_table(tables, ["GPS", "GPS2"])
    if gps is None or len(gps) == 0:
        return {"available": False, "reason": "GPS/GPS2 missing"}
    speed = series_values(gps, first_present_col(gps, ["Spd", "GSpd", "Vel", "Speed"]))
    times = time_values(gps)
    if not speed:
        vn = series_values(gps, first_present_col(gps, ["VN", "Vn"]))
        ve = series_values(gps, first_present_col(gps, ["VE", "Ve"]))
        count = min(len(vn), len(ve))
        speed = [math.sqrt(vn[i] ** 2 + ve[i] ** 2) for i in range(count)]
    return {
        "available": bool(speed),
        "samples": len(speed),
        "time_window": window_from_times(times),
        "min_m_s": min(speed) if speed else None,
        "max_m_s": max(speed) if speed else None,
        "range_m_s": max(speed) - min(speed) if speed else None,
        "p95_m_s": percentile(speed, 95),
        "series_preview": speed[:5],
    }


def acceleration_evidence(tables: dict[str, Any]) -> dict[str, Any]:
    df = first_table(tables, ["IMU", "ACC"])
    if df is None or len(df) == 0:
        return {"available": False, "reason": "IMU/ACC missing"}
    x = series_values(df, first_present_col(df, ["AccX", "AX", "X"]))
    y = series_values(df, first_present_col(df, ["AccY", "AY", "Y"]))
    times = time_values(df)
    mags = [math.sqrt(xv * xv + yv * yv) for xv, yv in zip(x, y)]
    return {
        "available": bool(mags),
        "samples": len(mags),
        "time_window": window_from_times(times),
        "accel_x_p95_abs_m_s_s": percentile([abs(v) for v in x], 95),
        "accel_y_p95_abs_m_s_s": percentile([abs(v) for v in y], 95),
        "horizontal_accel_p95_m_s_s": percentile(mags, 95),
        "horizontal_accel_max_m_s_s": max(mags) if mags else None,
    }


def wind_evidence(tables: dict[str, Any], notes: str | None) -> dict[str, Any]:
    samples = []
    for name in ("WIND", "XKF2", "NKF2"):
        df = tables.get(name)
        if df is None:
            continue
        vn = series_values(df, first_present_col(df, ["VWN", "WindN", "WN", "Vn"]))
        ve = series_values(df, first_present_col(df, ["VWE", "WindE", "WE", "Ve"]))
        if vn and ve:
            samples.extend(math.sqrt(vn[i] ** 2 + ve[i] ** 2) for i in range(min(len(vn), len(ve))))
    variability = (percentile(samples, 95) - percentile(samples, 5)) if len(samples) >= 5 else None
    return {
        "available": bool(samples),
        "estimate_m_s": median(samples) if samples else None,
        "p95_m_s": percentile(samples, 95),
        "p05_m_s": percentile(samples, 5),
        "variability_m_s": variability,
        "variable_wind": variability is not None and variability > 2.0,
        "notes": notes,
    }


def estimator_evidence(tables: dict[str, Any]) -> dict[str, Any]:
    out = {"available": False, "test_ratios": {}, "innovation_fields": {}}
    for name in ("XKF4", "NKF4", "XKF3", "NKF3"):
        df = tables.get(name)
        if df is None:
            continue
        for col in df.columns:
            lower = str(col).lower()
            values = series_values(df, col)
            if not values:
                continue
            if lower in {"sv", "sp", "sh", "sm", "svt", "spt"} or lower.startswith("s"):
                out["test_ratios"][f"{name}.{col}"] = {"max": max(values), "p95": percentile(values, 95)}
            elif "innov" in lower or lower in {"ivn", "ive", "ivd", "vd", "vn", "ve"}:
                out["innovation_fields"][f"{name}.{col}"] = {"p95_abs": percentile([abs(v) for v in values], 95), "max_abs": max(abs(v) for v in values)}
    out["available"] = bool(out["test_ratios"] or out["innovation_fields"])
    return out


def select_drag_segments(speed: dict[str, Any], accel: dict[str, Any]) -> list[dict[str, Any]]:
    if not speed.get("available") or not accel.get("available"):
        return []
    start = max((speed.get("time_window") or {}).get("start_s") or 0.0, (accel.get("time_window") or {}).get("start_s") or 0.0)
    end = min((speed.get("time_window") or {}).get("end_s") or start, (accel.get("time_window") or {}).get("end_s") or start)
    if end <= start:
        return []
    duration = end - start
    score = min((speed.get("range_m_s") or 0.0) / 4.0, 1.0) + min((accel.get("horizontal_accel_p95_m_s_s") or 0.0) / 2.5, 1.0)
    return [{
        "start_s": float(start),
        "end_s": float(end),
        "duration_s": float(duration),
        "speed_range_m_s": speed.get("range_m_s"),
        "horizontal_accel_p95_m_s_s": accel.get("horizontal_accel_p95_m_s_s"),
        "suitability_score": round(score / 2.0, 3),
        "selection": "whole overlapping GPS/IMU evidence window; agent must verify actual manoeuvre direction before using candidates",
    }]


def segment_suitability(segments: list[dict[str, Any]], speed: dict[str, Any], accel: dict[str, Any], wind: dict[str, Any], estimator: dict[str, Any], missing_inputs_: list[str], missing_evidence_: list[str]) -> dict[str, Any]:
    reasons = []
    if missing_inputs_:
        reasons.append("vehicle mass/frontal area/side area metadata is incomplete")
    if any(item.startswith("Missing required") for item in missing_evidence_):
        reasons.append("required GPS or IMU/ACC log evidence is missing")
    if not segments:
        reasons.append("no overlapping GPS speed and IMU/ACC acceleration evidence")
    if speed.get("range_m_s") is not None and speed["range_m_s"] < 2.0:
        reasons.append("ground speed variation is too small for drag estimation")
    if accel.get("horizontal_accel_p95_m_s_s") is not None and accel["horizontal_accel_p95_m_s_s"] < 0.5:
        reasons.append("horizontal acceleration is too small for drag estimation")
    if wind.get("variable_wind"):
        reasons.append("wind estimate varies too much to claim coefficient accuracy")
    estimator_bad = [
        key for key, data in (estimator.get("test_ratios") or {}).items()
        if data.get("p95") is not None and data["p95"] > 1.0
    ]
    if estimator_bad:
        reasons.append(f"EKF estimator test ratios are high: {', '.join(estimator_bad[:4])}")
    adequate = not reasons
    return {"adequate": adequate, "reasons": reasons, "segment_count": len(segments)}


def drag_coefficients(metadata: dict[str, Any], segments: list[dict[str, Any]], speed: dict[str, Any], accel: dict[str, Any], suitability: dict[str, Any]) -> dict[str, Any]:
    if not suitability.get("adequate"):
        return {"available": False, "reason": "Evidence is not adequate for candidate drag coefficients."}
    mass = metadata["mass_kg"]
    frontal = metadata["frontal_area_m2"]
    side = metadata["side_area_m2"]
    air_density = metadata["air_density_kg_m3"]
    bcoef_x = mass / frontal
    bcoef_y = mass / side
    speed_ref = max(speed.get("p95_m_s") or 0.0, 0.1)
    accel_ref = accel.get("horizontal_accel_p95_m_s_s") or 0.0
    bluff_drag_x = 0.5 * air_density * speed_ref * speed_ref / bcoef_x
    residual = max(accel_ref - bluff_drag_x, 0.0)
    mcoef = residual / max(speed_ref * speed_ref, 0.1)
    return {
        "available": True,
        "EK3_DRAG_BCOEF_X": bcoef_x,
        "EK3_DRAG_BCOEF_Y": bcoef_y,
        "EK3_DRAG_MCOEF": mcoef,
        "calculation_context": {
            "mass_kg": mass,
            "frontal_area_m2": frontal,
            "side_area_m2": side,
            "air_density_kg_m3": air_density,
            "speed_reference_m_s": speed_ref,
            "acceleration_reference_m_s_s": accel_ref,
            "bluff_drag_x_m_s_s": bluff_drag_x,
            "method": "Candidate context only; Methodic agent must inspect manoeuvre direction and validate externally before parameter use.",
        },
    }


def classify_confidence(suitability: dict[str, Any], coefficients: dict[str, Any]) -> str:
    if not coefficients.get("available"):
        return "low"
    if suitability.get("adequate") and suitability.get("segment_count", 0) >= 1:
        return "medium"
    return "low"


def assumptions(metadata: dict[str, Any], wind: dict[str, Any], coefficients: dict[str, Any]) -> list[str]:
    out = [
        "Vehicle mass and projected areas are supplied by the user and are not verified from the log.",
        f"Air density assumed {metadata['air_density_kg_m3']} kg/m^3 unless supplied.",
        "Candidate values assume the selected acceleration/deceleration segments match the Methodic wind/drag manoeuvre directions.",
    ]
    if wind.get("notes"):
        out.append(f"User wind-condition notes: {wind['notes']}")
    if coefficients.get("available"):
        out.append("EK3_DRAG_MCOEF is residual context from speed/acceleration evidence, not an automatically validated parameter.")
    return out


def classify_findings(suitability: dict[str, Any], coefficients: dict[str, Any], missing_inputs_: list[str], missing_evidence_: list[str]) -> list[dict[str, Any]]:
    findings = []
    if missing_inputs_:
        findings.append({"severity": "inconclusive", "finding": "Required mass/frontal-area/side-area inputs are missing.", "evidence": missing_inputs_})
    if any(item.startswith("Missing required") for item in missing_evidence_):
        findings.append({"severity": "inconclusive", "finding": "Required GPS or IMU/ACC evidence is missing.", "evidence": missing_evidence_})
    for reason in suitability.get("reasons") or []:
        severity = "repeat" if "wind" in reason or "small" in reason or "no overlapping" in reason else "inconclusive"
        findings.append({"severity": severity, "finding": reason, "evidence": suitability})
    if coefficients.get("available"):
        findings.append({"severity": "candidate", "finding": "Candidate drag coefficient context is available for agent review.", "evidence": coefficients})
    return findings


def classify_result(suitability: dict[str, Any], coefficients: dict[str, Any], missing_inputs_: list[str], missing_evidence_: list[str]) -> tuple[str, str]:
    if missing_inputs_ or any(item.startswith("Missing required") for item in missing_evidence_):
        return "inconclusive", "repeat_step"
    reasons = " ".join(suitability.get("reasons") or []).lower()
    if "test ratios are high" in reasons:
        return "do_not_use", "do_not_proceed"
    if "wind estimate varies" in reasons or "too small" in reasons or "no overlapping" in reasons:
        return "repeat_flight", "repeat_step"
    if coefficients.get("available"):
        return "candidate", "proceed_with_caution"
    return "inconclusive", "repeat_step"


def checked_but_not_supported(tables: dict[str, Any], speed: dict[str, Any], accel: dict[str, Any], wind: dict[str, Any], estimator: dict[str, Any]) -> list[str]:
    checked = []
    if speed.get("available"):
        checked.append("GPS ground-speed evidence was checked.")
    if accel.get("available"):
        checked.append("IMU/ACC acceleration evidence was checked.")
    if wind.get("available"):
        checked.append("EKF/logged wind evidence was checked.")
    if estimator.get("available"):
        checked.append("EKF/NKF consistency evidence was checked.")
    for name in ("ATT", "RATE", "RCIN", "BAT", "POWR", "PARM"):
        if name in tables:
            checked.append(f"{name} context was checked.")
    return checked


def recommended_next_steps(result: dict[str, Any]) -> list[str]:
    if result["result"] == "candidate":
        return [
            "Agent should inspect selected segments, speed, acceleration, attitude, wind, and EKF consistency plots before presenting candidates.",
            "Treat EK3_DRAG_BCOEF_X/Y and EK3_DRAG_MCOEF values as review candidates only; do not auto-apply them.",
            "Any externally applied EKF drag parameter change requires validation with a fresh log in comparable conditions.",
        ]
    if result["result"] == "repeat_flight":
        return [
            "Repeat the Methodic wind/drag evidence capture only when conditions are suitable and the manoeuvre can be performed safely.",
            "Use clearer acceleration/deceleration tests with stable wind, adequate GPS speed variation, and clean IMU acceleration evidence.",
        ]
    if result["result"] == "do_not_use":
        return [
            "Do not use this log for EKF drag coefficient candidates.",
            "Resolve estimator consistency, setup, or data quality problems before repeating wind/drag testing.",
        ]
    return [
        "Provide vehicle mass, frontal area, and side area, and collect a readable log with GPS/GPA, IMU/ACC, ATT/RATE, XKF/NKF, MODE, RCIN, BAT/POWR, and PARM evidence.",
        "Do not infer EKF drag coefficients from missing metadata or unsuitable manoeuvres.",
    ]


def what_not_to_do() -> list[str]:
    return [
        "Do not automatically write EK3_DRAG_BCOEF_X, EK3_DRAG_BCOEF_Y, or EK3_DRAG_MCOEF.",
        "Do not calculate or endorse coefficients without mass, frontal area, and side area.",
        "Do not claim coefficient accuracy in variable wind or poor estimator conditions.",
        "Do not skip validation after any external parameter change.",
    ]


def next_methodic_step(result: str) -> str | None:
    if result == "candidate":
        return "10.2 after external validation or with documented coefficient uncertainty"
    if result == "repeat_flight":
        return "Repeat 10.1 evidence capture in suitable conditions"
    if result == "do_not_use":
        return "Do not use this data for 10.1; resolve data/setup issues first"
    return "10.1 remains incomplete"


def confidence_limits(result: dict[str, Any], wind: dict[str, Any], estimator: dict[str, Any]) -> list[str]:
    limits = []
    if result["missing_inputs"]:
        limits.append("Mass/frontal-area/side-area metadata is required before coefficient review.")
    if result["missing_evidence"]:
        limits.append("Missing log messages limit wind/drag evidence quality.")
    if wind.get("variable_wind"):
        limits.append("Variable wind prevents an accuracy claim.")
    if not estimator.get("available"):
        limits.append("EKF/NKF estimator consistency evidence is missing.")
    limits.append("Candidate coefficients require external application and validation with a fresh log.")
    return limits


def make_plots(tables: dict[str, Any], plots_dir: Path, segments: list[dict[str, Any]]) -> list[str]:
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except Exception:
        return []
    out = ensure_dir(plots_dir)
    plots: list[str] = []
    gps = first_table(tables, ["GPS", "GPS2"])
    if gps is not None and "TimeS" in getattr(gps, "columns", []):
        fig = go.Figure()
        for col in ("Spd", "GSpd", "Vel", "Speed"):
            if col in gps.columns:
                fig.add_trace(go.Scatter(x=gps["TimeS"], y=numeric_series(gps, [col]), mode="lines", name=f"GPS.{col}"))
        add_segment_markers(fig, segments)
        fig.update_layout(title="Methodic 10.1 ground speed", template="plotly_white", hovermode="x unified")
        path = out / "methodic_10_1_ground_speed.html"
        fig.write_html(str(path), include_plotlyjs="cdn")
        plots.append(str(path))
    imu = first_table(tables, ["IMU", "ACC"])
    if imu is not None and "TimeS" in getattr(imu, "columns", []):
        fig = go.Figure()
        for col in ("AccX", "AccY", "AX", "AY", "X", "Y"):
            if col in imu.columns:
                fig.add_trace(go.Scatter(x=imu["TimeS"], y=numeric_series(imu, [col]), mode="lines", name=f"ACC.{col}"))
        add_segment_markers(fig, segments)
        fig.update_layout(title="Methodic 10.1 acceleration", template="plotly_white", hovermode="x unified")
        path = out / "methodic_10_1_acceleration.html"
        fig.write_html(str(path), include_plotlyjs="cdn")
        plots.append(str(path))
    for name, cols, title in [
        ("ATT", ("Roll", "Pitch", "Yaw"), "attitude"),
        ("XKF2", ("VWN", "VWE", "WN", "WE"), "EKF wind"),
        ("NKF2", ("VWN", "VWE", "WN", "WE"), "NKF wind"),
        ("XKF4", ("SV", "SP", "SH", "SM"), "EKF test ratios"),
    ]:
        path = plot_group(tables, name, cols, title, out, segments)
        if path:
            plots.append(path)
    return plots


def plot_group(tables: dict[str, Any], name: str, cols: tuple[str, ...], title: str, out: Path, segments: list[dict[str, Any]]) -> str | None:
    try:
        import plotly.graph_objects as go
    except Exception:
        return None
    df = tables.get(name)
    if df is None or "TimeS" not in getattr(df, "columns", []):
        return None
    fig = go.Figure()
    found = False
    for col in cols:
        if col in df.columns:
            fig.add_trace(go.Scatter(x=df["TimeS"], y=numeric_series(df, [col]), mode="lines", name=f"{name}.{col}"))
            found = True
    if not found:
        return None
    add_segment_markers(fig, segments)
    fig.update_layout(title=f"Methodic 10.1 {title}", template="plotly_white", hovermode="x unified")
    path = out / f"methodic_10_1_{title.replace(' ', '_')}.html"
    fig.write_html(str(path), include_plotlyjs="cdn")
    return str(path)


def add_segment_markers(fig: Any, segments: list[dict[str, Any]]) -> None:
    for seg in segments:
        fig.add_vrect(x0=seg["start_s"], x1=seg["end_s"], fillcolor="LightGreen", opacity=0.18, line_width=0)


def first_present_col(df: Any, candidates: list[str]) -> str | None:
    if df is None:
        return None
    lower = {str(c).lower(): c for c in df.columns}
    for name in candidates:
        if name in df.columns:
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


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * pct / 100.0))))
    return float(ordered[idx])


def window_from_times(times: list[float]) -> dict[str, Any]:
    if not times:
        return {"start_s": None, "end_s": None}
    return {"start_s": float(min(times)), "end_s": float(max(times))}


def write_summary(path: Path, result: dict[str, Any]) -> None:
    lines = [
        "# Methodic 10.1 Wind / Drag Coefficient Review",
        "",
        f"- Result: `{result['result']}`",
        f"- Safety gate: `{result['safety_gate']}`",
        f"- Confidence: `{result['confidence']}`",
        f"- Official reference: {result['official_reference']['url']}",
        "",
        "## Drag Coefficients",
    ]
    coeffs = result.get("drag_coefficients") or {}
    if coeffs.get("available"):
        for name in ("EK3_DRAG_BCOEF_X", "EK3_DRAG_BCOEF_Y", "EK3_DRAG_MCOEF"):
            lines.append(f"- {name}: `{coeffs.get(name):.6f}`")
    else:
        lines.append(f"- Not available: {coeffs.get('reason', 'insufficient evidence')}")
    lines.extend(["", "## Missing Inputs"])
    lines.extend(f"- {item}" for item in result.get("missing_inputs", []) or ["None reported by the script."])
    lines.extend(["", "## Recommended Next Steps"])
    lines.extend(f"- {item}" for item in result["recommended_next_steps"])
    lines.extend(["", "## What Not To Do"])
    lines.extend(f"- {item}" for item in result["what_not_to_do"])
    ensure_dir(path.parent)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Gather deterministic Methodic wind/drag coefficient review evidence for step 10.1.")
    parser.add_argument("log", help="ArduPilot DataFlash .BIN/.log file")
    parser.add_argument("--mass-kg", type=float, default=None, help="Vehicle mass in kg")
    parser.add_argument("--frontal-area-m2", type=float, default=None, help="Frontal projected area in square metres")
    parser.add_argument("--side-area-m2", type=float, default=None, help="Side projected area in square metres")
    parser.add_argument("--air-density-kg-m3", type=float, default=1.225, help="Air density in kg/m^3")
    parser.add_argument("--wind-notes", default=None, help="Optional user notes about wind condition")
    parser.add_argument("--out", default=None, help="Write structured JSON result")
    parser.add_argument("--summary", default=None, help="Write Markdown summary")
    parser.add_argument("--plots", default=None, help="Directory for generated plots")
    args = parser.parse_args()
    try:
        result = analyze_wind_drag_review(
            args.log,
            mass_kg=args.mass_kg,
            frontal_area_m2=args.frontal_area_m2,
            side_area_m2=args.side_area_m2,
            air_density_kg_m3=args.air_density_kg_m3,
            wind_notes=args.wind_notes,
            plots_dir=args.plots,
        )
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
