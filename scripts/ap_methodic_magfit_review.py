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

from ap_common import AnalysisError, collect_dataflash, ensure_dir, numeric_series, rows_to_dataframe, safe_float, safe_int, write_json

METHODIC_9_1_URL = "https://ardupilot.github.io/MethodicConfigurator/TUNING_GUIDE_ArduCopter#91-third-flight-magfit"
LOG_MESSAGES_URL = "https://ardupilot.org/copter/docs/logmessages.html"

MESSAGES = [
    "MAG",
    "RMGI",
    "XKF1",
    "XKF2",
    "XKF3",
    "XKF4",
    "XKY0",
    "XKY1",
    "NKF1",
    "NKF2",
    "NKF3",
    "NKF4",
    "ATT",
    "RATE",
    "GPS",
    "GPS2",
    "GPA",
    "MODE",
    "MSG",
    "ERR",
    "EV",
    "BAT",
    "POWR",
    "RCIN",
    "PARM",
    "RCOU",
    "RCO2",
    "RCO3",
    "ARM",
]

PARAMETERS = [
    "COMPASS_USE",
    "COMPASS_USE2",
    "COMPASS_USE3",
    "COMPASS_ORIENT*",
    "COMPASS_OFS*",
    "COMPASS_MOT*",
    "EK3_SRC1_YAW",
    "GPS_TYPE",
    "GPS_TYPE2",
    "GPS_AUTO_SWITCH",
    "LOG_BITMASK",
]


def rows_to_tables(rows_by_message: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    return {name: rows_to_dataframe(rows) for name, rows in rows_by_message.items() if rows}


def analyze_magfit_review(log_path: str | Path, *, plots_dir: str | Path | None = None) -> dict[str, Any]:
    rows, index, stats = collect_dataflash(log_path, include=MESSAGES, source=str(log_path))
    tables = rows_to_tables(rows)
    params = index.get("parameters", {}) or {}
    profile = analyze_flight_profile(tables)
    mag = analyze_magnetometer(tables)
    interference = analyze_magnetic_interference(tables, mag)
    ekf = analyze_ekf_yaw_mag(tables)
    yaw_source = analyze_yaw_source(params, tables)
    gps = analyze_gps(tables)
    events = analyze_events(tables)
    power = analyze_power(tables)

    result = empty_result(params)
    result["analysis_window"]["parser_stats"] = stats
    result["magfit_evidence_quality"] = evidence_quality(profile, mag, ekf, gps)
    result["magnetic_interference"] = interference
    result["ekf_yaw_mag_health"] = ekf
    result["yaw_source_context"] = yaw_source
    result["evidence_used"] = [
        {"type": "messages_present", "messages": sorted(tables.keys())},
        {"type": "flight_profile", "value": profile},
        {"type": "magnetometer", "value": mag},
        {"type": "magnetic_interference", "value": interference},
        {"type": "ekf_yaw_mag_health", "value": ekf},
        {"type": "yaw_source_context", "value": yaw_source},
        {"type": "gps_quality", "value": gps},
        {"type": "events", "value": events},
        {"type": "power", "value": power},
    ]
    result["missing_evidence"] = missing_evidence(tables, mag, ekf, gps)
    result["findings"] = classify_findings(profile, mag, interference, ekf, yaw_source, gps, events, power)
    result["checked_but_not_supported"] = checked_but_not_supported(tables, interference, ekf)
    result["result"], result["safety_gate"] = classify_result(result)
    result["next_methodic_step"] = "9.2" if result["result"] == "ready_for_magfit" else "repeat_9.1"
    result["recommended_next_steps"] = recommended_next_steps(result, profile, interference, ekf)
    result["what_not_to_do"] = [
        "Do not automatically write compass offsets, compass orientation, motor-compensation, or EKF yaw-source parameters.",
        "Do not infer compass interference from MAG field range alone; require timing/correlation or EKF evidence.",
        "Do not proceed to advanced tuning while compass, yaw-source, or EKF yaw/mag warnings persist.",
        "Do not use MagFit to mask wiring, current-loop, compass-placement, GPS-yaw, or power issues.",
    ]
    result["confidence_limits"] = confidence_limits(result["missing_evidence"], profile, interference, ekf)
    if plots_dir:
        result["plots"] = make_plots(tables, Path(plots_dir), mag)
    return result


def empty_result(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "methodic_step": "9.1",
        "title": "MagFit evidence review",
        "official_reference": {"url": METHODIC_9_1_URL, "supporting_urls": [LOG_MESSAGES_URL]},
        "result": "inconclusive",
        "safety_gate": "repeat_step",
        "magfit_evidence_quality": "poor",
        "magnetic_interference": {},
        "ekf_yaw_mag_health": {},
        "yaw_source_context": {},
        "evidence_used": [],
        "missing_evidence": [],
        "manual_observations_required": ["Obstacle clearance confirmed", "Pilot/GCS confirmed the intended MagFit pattern was flown"],
        "analysis_window": {"selection": "whole_log", "preferred_window": "Figure-eight MagFit segment, excluding takeoff and landing.", "start_s": None, "end_s": None},
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


def series_values(df: Any, names: list[str] | str) -> list[float]:
    if df is None:
        return []
    candidates = [names] if isinstance(names, str) else names
    s = numeric_series(df, candidates)
    if s is None:
        return []
    return [float(v) for v in s.dropna().tolist()]


def time_values(df: Any) -> list[float]:
    return series_values(df, "TimeS")


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * pct / 100.0))))
    return float(ordered[idx])


def summarize(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"available": False, "samples": 0}
    return {"available": True, "samples": len(values), "min": min(values), "max": max(values), "mean": mean(values), "range": max(values) - min(values), "p95_abs": percentile([abs(v) for v in values], 95)}


def circular_span_deg(values: list[float]) -> float | None:
    if not values:
        return None
    wrapped = sorted(v % 360.0 for v in values)
    if len(wrapped) == 1:
        return 0.0
    gaps = [wrapped[i + 1] - wrapped[i] for i in range(len(wrapped) - 1)]
    gaps.append(wrapped[0] + 360.0 - wrapped[-1])
    return 360.0 - max(gaps)


def analyze_flight_profile(tables: dict[str, Any]) -> dict[str, Any]:
    att = tables.get("ATT")
    rate = tables.get("RATE")
    mode = tables.get("MODE")
    yaw = series_values(att, ["Yaw", "DesYaw"]) if att is not None else []
    roll = series_values(att, ["Roll", "DesRoll"]) if att is not None else []
    rate_yaw = series_values(rate, ["Y", "YDes"]) if rate is not None else []
    times = time_values(att) or time_values(rate)
    modes = []
    if mode is not None:
        for row in mode.to_dict(orient="records"):
            modes.append(str(row.get("Mode") or row.get("ModeNum") or row.get("mode") or ""))
    auto_present = any(m.upper() in {"AUTO", "GUIDED", "LOITER"} or "AUTO" in m.upper() for m in modes)
    yaw_span = circular_span_deg(yaw)
    yaw_rate_abs = [abs(v) for v in rate_yaw]
    yaw_motion_samples = sum(1 for v in yaw_rate_abs if v > 10.0)
    roll_p95 = percentile([abs(v) for v in roll], 95) if roll else None
    duration = max(times) - min(times) if times else None
    heading_diversity = "good" if yaw_span is not None and yaw_span >= 240 else "marginal" if yaw_span is not None and yaw_span >= 120 else "poor"
    coordinated_turn_like = bool(roll_p95 is not None and roll_p95 >= 5.0 and yaw_motion_samples >= 20)
    suited = bool(duration and duration >= 40.0 and heading_diversity in {"good", "marginal"} and yaw_motion_samples >= 20)
    return {
        "available": bool(att is not None or rate is not None),
        "duration_s": duration,
        "yaw_span_deg": yaw_span,
        "heading_diversity": heading_diversity,
        "yaw_motion_samples": yaw_motion_samples,
        "roll_p95_abs_deg": roll_p95,
        "coordinated_turn_or_figure_eight_proxy": coordinated_turn_like,
        "auto_or_script_like_mode_present": auto_present,
        "modes_seen": sorted(set(m for m in modes if m)),
        "suitable_for_magfit": suited,
    }


def analyze_magnetometer(tables: dict[str, Any]) -> dict[str, Any]:
    mag = tables.get("MAG")
    if mag is None or len(mag) == 0:
        return {"available": False, "status": "missing", "instances": {}}
    instances = {}
    if "I" in mag.columns:
        grouped = mag.groupby("I", dropna=False)
    else:
        grouped = [(0, mag)]
    for inst, df in grouped:
        x = series_values(df, ["MagX", "X"])
        y = series_values(df, ["MagY", "Y"])
        z = series_values(df, ["MagZ", "Z"])
        count = min(len(x), len(y), len(z))
        mag_strength = [math.sqrt(x[i] ** 2 + y[i] ** 2 + z[i] ** 2) for i in range(count)]
        instances[str(int(inst) if safe_float(inst) is not None else inst)] = {
            "samples": count,
            "field_magnitude": summarize(mag_strength),
            "axis_ranges": {"x": summarize(x), "y": summarize(y), "z": summarize(z)},
        }
    usable = any(item["samples"] >= 50 for item in instances.values())
    return {"available": True, "status": "usable" if usable else "sparse", "instances": instances}


def analyze_magnetic_interference(tables: dict[str, Any], mag: dict[str, Any]) -> dict[str, Any]:
    if not mag.get("available"):
        return {"available": False, "assessment": "missing", "reason": "MAG missing."}
    mag_df = tables.get("MAG")
    field = mag_field_for_first_instance(mag_df)
    current = aligned_values(mag_df, tables.get("BAT"), ["Curr", "CurrTot", "I"])
    throttle = aligned_values(mag_df, tables.get("RCOU"), [f"C{i}" for i in range(1, 17)])
    current_corr = abs(correlation(field, current)) if field and current else None
    throttle_corr = abs(correlation(field, throttle)) if field and throttle else None
    assessment = "not_indicated"
    reason = "No strong timing correlation with current or throttle was found."
    if current_corr is not None and current_corr >= 0.65:
        assessment = "suspect"
        reason = "MAG field magnitude is strongly correlated with battery current."
    if throttle_corr is not None and throttle_corr >= 0.70:
        assessment = "suspect"
        reason = "MAG field magnitude is strongly correlated with motor output/throttle proxy."
    if (current_corr is not None and current_corr >= 0.82) or (throttle_corr is not None and throttle_corr >= 0.85):
        assessment = "likely"
    return {
        "available": True,
        "assessment": assessment,
        "reason": reason,
        "current_correlation_abs": current_corr,
        "throttle_correlation_abs": throttle_corr,
        "field_magnitude": summarize(field),
    }


def mag_field_for_first_instance(mag: Any) -> list[float]:
    if mag is None or len(mag) == 0:
        return []
    df = mag
    if "I" in mag.columns:
        first = sorted(v for v in mag["I"].dropna().unique())
        if first:
            df = mag[mag["I"] == first[0]]
    x = series_values(df, ["MagX", "X"])
    y = series_values(df, ["MagY", "Y"])
    z = series_values(df, ["MagZ", "Z"])
    count = min(len(x), len(y), len(z))
    return [math.sqrt(x[i] ** 2 + y[i] ** 2 + z[i] ** 2) for i in range(count)]


def aligned_values(base: Any, other: Any, fields: list[str]) -> list[float]:
    if base is None or other is None or len(base) == 0 or len(other) == 0:
        return []
    if "TimeS" not in base.columns or "TimeS" not in other.columns:
        return []
    other_times = time_values(other)
    series_by_field = [series_values(other, field) for field in fields]
    combined = []
    for idx in range(len(other_times)):
        values = [vals[idx] for vals in series_by_field if idx < len(vals)]
        if values:
            combined.append(mean(values))
    if not other_times or not combined:
        return []
    out = []
    j = 0
    for t in time_values(base):
        while j + 1 < len(other_times) and abs(other_times[j + 1] - t) <= abs(other_times[j] - t):
            j += 1
        out.append(combined[j])
    return out


def correlation(a: list[float], b: list[float]) -> float | None:
    n = min(len(a), len(b))
    if n < 20:
        return None
    aa = a[:n]
    bb = b[:n]
    ma = mean(aa)
    mb = mean(bb)
    da = [x - ma for x in aa]
    db = [x - mb for x in bb]
    denom = math.sqrt(sum(x * x for x in da) * sum(x * x for x in db))
    if denom <= 1e-9:
        return None
    return sum(da[i] * db[i] for i in range(n)) / denom


def analyze_ekf_yaw_mag(tables: dict[str, Any]) -> dict[str, Any]:
    ekf_tables = [tables.get(name) for name in ("XKF4", "NKF4") if tables.get(name) is not None]
    if not ekf_tables:
        return {"available": False, "status": "missing", "mag_test_ratio": None, "yaw_innovation": None}
    ratios = []
    yaw_innov = []
    for df in ekf_tables:
        ratios.extend(series_values(df, ["SM", "MAG", "M"])
                      or series_values(df, ["SV", "SH"]))
        yaw_innov.extend(series_values(df, ["IYAW", "Yaw", "YI", "IVN"]))
    ratio_summary = summarize(ratios)
    yaw_summary = summarize(yaw_innov)
    status = "healthy"
    max_ratio = ratio_summary.get("max")
    if max_ratio is not None and max_ratio > 1.0:
        status = "fail"
    elif max_ratio is not None and max_ratio > 0.7:
        status = "marginal"
    return {"available": True, "status": status, "mag_test_ratio": ratio_summary, "yaw_innovation": yaw_summary}


def analyze_yaw_source(params: dict[str, Any], tables: dict[str, Any]) -> dict[str, Any]:
    src = safe_int(params.get("EK3_SRC1_YAW"))
    gps_type = safe_int(params.get("GPS_TYPE"))
    gps2_type = safe_int(params.get("GPS_TYPE2"))
    moving_baseline_possible = any(v in {17, 18, 22} for v in (gps_type, gps2_type) if v is not None)
    compass_use = {name: safe_int(params.get(name)) for name in ("COMPASS_USE", "COMPASS_USE2", "COMPASS_USE3") if name in params}
    return {
        "ek3_src1_yaw": src,
        "gps_type": gps_type,
        "gps_type2": gps2_type,
        "gps_auto_switch": safe_int(params.get("GPS_AUTO_SWITCH")),
        "gps_yaw_or_moving_baseline_possible": moving_baseline_possible,
        "compass_use": compass_use,
        "rmgI_available": "RMGI" in tables,
        "assessment": "gps_yaw_context" if moving_baseline_possible or src in {2, 3, 6} else "compass_yaw_context",
    }


def analyze_gps(tables: dict[str, Any]) -> dict[str, Any]:
    gps = tables.get("GPS")
    if gps is None or len(gps) == 0:
        return {"available": False, "status": "missing"}
    status = series_values(gps, ["Status"])
    sats = series_values(gps, ["NSats", "NSat"])
    hdop = series_values(gps, ["HDop", "HDOP"])
    healthy = (not status or max(status) >= 3) and (not sats or percentile(sats, 50) >= 8) and (not hdop or percentile(hdop, 95) <= 2.5)
    return {"available": True, "status": "usable" if healthy else "suspect", "status_values": summarize(status), "satellites": summarize(sats), "hdop": summarize(hdop)}


def analyze_events(tables: dict[str, Any]) -> dict[str, Any]:
    events = []
    for name in ("MSG", "ERR", "EV"):
        df = tables.get(name)
        if df is None:
            continue
        for row in df.to_dict(orient="records")[:300]:
            text = " ".join(str(row.get(k, "")) for k in row if k not in {"TimeUS", "TimeS", "_type"})
            low = text.lower()
            if "magfit" in low or "mag fit" in low:
                continue
            if any(token in low for token in ("compass", "mag", "yaw", "ekf", "gps", "failsafe", "error")):
                events.append({"message": name, "time_s": safe_float(row.get("TimeS")), "text": text.strip()})
    return {"available": bool(events), "warnings": events}


def analyze_power(tables: dict[str, Any]) -> dict[str, Any]:
    bat = tables.get("BAT")
    powr = tables.get("POWR")
    out = {"available": bat is not None or powr is not None, "status": "unknown"}
    volts = series_values(bat, ["Volt", "V"]) if bat is not None else []
    vcc = series_values(powr, ["Vcc", "VccMin"]) if powr is not None else []
    out["battery_voltage"] = summarize(volts)
    out["vcc"] = summarize(vcc)
    out["status"] = "suspect" if (vcc and min(vcc) < 4.7) else "usable" if volts or vcc else "missing"
    return out


def evidence_quality(profile: dict[str, Any], mag: dict[str, Any], ekf: dict[str, Any], gps: dict[str, Any]) -> str:
    score = 0
    score += 2 if mag.get("status") == "usable" else 0
    score += 2 if profile.get("heading_diversity") == "good" else 1 if profile.get("heading_diversity") == "marginal" else 0
    score += 1 if profile.get("duration_s") and profile["duration_s"] >= 40 else 0
    score += 1 if ekf.get("available") else 0
    score += 1 if gps.get("status") == "usable" else 0
    return "good" if score >= 6 else "marginal" if score >= 4 else "poor"


def missing_evidence(tables: dict[str, Any], mag: dict[str, Any], ekf: dict[str, Any], gps: dict[str, Any]) -> list[str]:
    missing = []
    if not mag.get("available"):
        missing.append("Missing required evidence: MAG")
    if not ekf.get("available"):
        missing.append("Missing strongly recommended evidence: XKF3/XKF4 or NKF*")
    for name in ("ATT", "RATE", "GPS", "MODE", "PARM"):
        if name not in tables:
            missing.append(f"Missing strongly recommended evidence: {name}")
    for name in ("MSG", "ERR", "EV", "BAT", "POWR", "RCIN"):
        if name not in tables:
            missing.append(f"Missing optional/strong context: {name}")
    if gps.get("status") == "missing":
        missing.append("Missing GPS/GPA context for MagFit flight quality.")
    return missing


def classify_findings(profile: dict[str, Any], mag: dict[str, Any], interference: dict[str, Any], ekf: dict[str, Any], yaw_source: dict[str, Any], gps: dict[str, Any], events: dict[str, Any], power: dict[str, Any]) -> list[dict[str, Any]]:
    findings = []
    if not mag.get("available"):
        findings.append({"severity": "inconclusive", "finding": "MAG evidence is missing."})
    elif mag.get("status") != "usable":
        findings.append({"severity": "repeat", "finding": "MAG evidence is too sparse for a reliable MagFit review.", "evidence": mag})
    if not profile.get("suitable_for_magfit"):
        findings.append({"severity": "repeat", "finding": "Flight profile does not show enough duration, yaw movement, or heading diversity for MagFit.", "evidence": profile})
    if interference.get("assessment") == "likely":
        findings.append({"severity": "hardware", "finding": "MAG field timing strongly correlates with current/throttle proxy.", "evidence": interference})
    elif interference.get("assessment") == "suspect":
        findings.append({"severity": "repeat", "finding": "MAG field timing correlation suggests magnetic interference should be reviewed.", "evidence": interference})
    if ekf.get("status") == "fail":
        findings.append({"severity": "hardware", "finding": "EKF mag/yaw test ratio exceeds 1.0.", "evidence": ekf})
    elif ekf.get("status") == "marginal":
        findings.append({"severity": "repeat", "finding": "EKF mag/yaw evidence is marginal.", "evidence": ekf})
    if gps.get("status") == "suspect":
        findings.append({"severity": "repeat", "finding": "GPS quality context is suspect for the MagFit flight.", "evidence": gps})
    if events.get("warnings"):
        findings.append({"severity": "repeat", "finding": "Compass/yaw/EKF/GPS warning messages were present.", "evidence": events})
    if power.get("status") == "suspect":
        findings.append({"severity": "repeat", "finding": "Board power evidence is suspect.", "evidence": power})
    if yaw_source.get("gps_yaw_or_moving_baseline_possible"):
        findings.append({"severity": "info", "finding": "GPS yaw or moving-baseline context appears configured; interpret compass evidence with yaw-source configuration in mind.", "evidence": yaw_source})
    return findings


def classify_result(result: dict[str, Any]) -> tuple[str, str]:
    findings = result["findings"]
    missing = result["missing_evidence"]
    if any(item.get("severity") == "hardware" for item in findings):
        return "fix_hardware_first", "bench_check_required"
    if any("MAG" in item and "Missing required" in item for item in missing):
        return "inconclusive", "repeat_step"
    if result["magfit_evidence_quality"] == "poor":
        return "inconclusive", "repeat_step"
    if any(item.get("severity") in {"repeat", "inconclusive"} for item in findings):
        return "repeat_flight", "proceed_with_caution"
    return "ready_for_magfit", "proceed_with_caution"


def recommended_next_steps(result: dict[str, Any], profile: dict[str, Any], interference: dict[str, Any], ekf: dict[str, Any]) -> list[str]:
    if result["result"] == "ready_for_magfit":
        return [
            "Agent should inspect the MagFit segment, MAG/current plots, and EKF yaw/mag evidence before accepting readiness.",
            "If accepted, run the external MagFit calculation on the selected figure-eight segment and review generated parameters manually before upload.",
            "Continue to Methodic step 9.2 only after documenting compass confidence limits.",
        ]
    steps = []
    if result["result"] == "fix_hardware_first":
        steps.append("Fix compass placement, current-loop wiring, GPS-yaw setup, or EKF/yaw-source warnings before repeating MagFit evidence capture.")
    if not profile.get("suitable_for_magfit"):
        steps.append("Repeat the MagFit flight with a deliberate figure-eight path, enough yaw/heading diversity, and no takeoff/landing-only capture.")
    if interference.get("assessment") in {"suspect", "likely"}:
        steps.append("Review MAG timing against current/throttle; inspect compass separation from power wiring before changing compass offsets.")
    if ekf.get("status") in {"marginal", "fail"}:
        steps.append("Resolve EKF mag/yaw warnings or collect a cleaner log before proceeding to advanced tuning.")
    return steps or ["Collect missing MAG, EKF, GPS, mode, event, power, RC, and parameter evidence before assessing MagFit readiness."]


def checked_but_not_supported(tables: dict[str, Any], interference: dict[str, Any], ekf: dict[str, Any]) -> list[str]:
    out = []
    if "RMGI" not in tables:
        out.append("RMGI replay magnetometer details were not available.")
    if interference.get("current_correlation_abs") is None:
        out.append("Current correlation could not be calculated from BAT.Curr.")
    if interference.get("throttle_correlation_abs") is None:
        out.append("Throttle/output correlation could not be calculated from RCOU.")
    if not ekf.get("yaw_innovation", {}).get("available"):
        out.append("Yaw innovation fields were not available in the EKF messages used.")
    return out


def confidence_limits(missing: list[str], profile: dict[str, Any], interference: dict[str, Any], ekf: dict[str, Any]) -> list[str]:
    limits = list(missing)
    if profile.get("heading_diversity") != "good":
        limits.append("Heading diversity is not clearly good; MagFit readiness may be limited by flight path.")
    if interference.get("assessment") == "not_indicated":
        limits.append("No strong MAG/current timing correlation was found, but this does not prove absence of all compass interference.")
    if not ekf.get("available"):
        limits.append("EKF yaw/mag health could not be checked.")
    return dedupe(limits)


def dedupe(items: list[str]) -> list[str]:
    out = []
    seen = set()
    for item in items:
        if item not in seen:
            out.append(item)
            seen.add(item)
    return out


def make_plots(tables: dict[str, Any], plots_dir: Path, mag: dict[str, Any]) -> list[str]:
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except Exception:
        return []
    out = ensure_dir(plots_dir)
    plots = []
    mag_df = tables.get("MAG")
    if mag_df is not None and "TimeS" in mag_df.columns:
        field = mag_field_for_first_instance(mag_df)
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=time_values(mag_df)[: len(field)], y=field, mode="lines", name="MAG magnitude"))
        fig.update_layout(title="Methodic 9.1 MAG field magnitude", template="plotly_white", hovermode="x unified")
        plots.append(write_plot(fig, out / "methodic_9_1_mag_field_magnitude.html"))
        current = aligned_values(mag_df, tables.get("BAT"), ["Curr", "CurrTot", "I"])
        throttle = aligned_values(mag_df, tables.get("RCOU"), [f"C{i}" for i in range(1, 17)])
        if current or throttle:
            fig = make_subplots(rows=3, cols=1, shared_xaxes=True, subplot_titles=("MAG magnitude", "BAT current", "Motor output proxy"))
            fig.add_trace(go.Scatter(x=time_values(mag_df)[: len(field)], y=field, mode="lines", name="MAG magnitude"), row=1, col=1)
            if current:
                fig.add_trace(go.Scatter(x=time_values(mag_df)[: len(current)], y=current, mode="lines", name="BAT current"), row=2, col=1)
            if throttle:
                fig.add_trace(go.Scatter(x=time_values(mag_df)[: len(throttle)], y=throttle, mode="lines", name="RCOU mean"), row=3, col=1)
            fig.update_layout(title="Methodic 9.1 MAG vs current/throttle", template="plotly_white", hovermode="x unified")
            plots.append(write_plot(fig, out / "methodic_9_1_mag_vs_current_throttle.html"))
    for name, filename, fields, title in [
        ("XKF4", "methodic_9_1_ekf_yaw_mag.html", ["SM", "SH", "SV", "SP"], "XKF4 mag/height test ratios"),
        ("ATT", "methodic_9_1_att_rate_yaw.html", ["Yaw", "DesYaw"], "ATT yaw"),
        ("GPS", "methodic_9_1_gps_quality.html", ["Status", "NSats", "HDop"], "GPS quality"),
        ("MODE", "methodic_9_1_mode_timeline.html", ["ModeNum"], "Mode timeline"),
    ]:
        df = tables.get(name)
        if df is None or "TimeS" not in getattr(df, "columns", []):
            continue
        fig = go.Figure()
        for field in fields:
            vals = series_values(df, field)
            if vals:
                fig.add_trace(go.Scatter(x=time_values(df)[: len(vals)], y=vals, mode="lines", name=f"{name}.{field}"))
        if fig.data:
            fig.update_layout(title=f"Methodic 9.1 {title}", template="plotly_white", hovermode="x unified")
            plots.append(write_plot(fig, out / filename))
    rate = tables.get("RATE")
    if rate is not None and "TimeS" in getattr(rate, "columns", []):
        fig = go.Figure()
        for field in ("YDes", "Y"):
            vals = series_values(rate, field)
            if vals:
                fig.add_trace(go.Scatter(x=time_values(rate)[: len(vals)], y=vals, mode="lines", name=f"RATE.{field}"))
        if fig.data:
            fig.update_layout(title="Methodic 9.1 RATE yaw", template="plotly_white", hovermode="x unified")
            plots.append(write_plot(fig, out / "methodic_9_1_rate_yaw.html"))
    return plots


def write_plot(fig: Any, path: Path) -> str:
    fig.write_html(str(path), include_plotlyjs="cdn")
    return str(path)


def write_summary(path: Path, result: dict[str, Any]) -> None:
    lines = [
        "# Methodic 9.1 MagFit Review",
        "",
        f"- Result: `{result['result']}`",
        f"- Safety gate: `{result['safety_gate']}`",
        f"- Evidence quality: `{result['magfit_evidence_quality']}`",
        f"- Next step: `{result['next_methodic_step']}`",
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
    parser = argparse.ArgumentParser(description="Gather deterministic Methodic MagFit evidence.")
    parser.add_argument("log", help="ArduPilot DataFlash .BIN/.log file")
    parser.add_argument("--out", default="out/methodic_9_1.json")
    parser.add_argument("--summary", default=None)
    parser.add_argument("--plots", default=None)
    args = parser.parse_args()
    try:
        result = analyze_magfit_review(args.log, plots_dir=args.plots)
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
