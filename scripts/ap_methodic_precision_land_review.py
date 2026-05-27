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

from ap_common import AnalysisError, collect_dataflash, ensure_dir, numeric_series, safe_float, write_json
from ap_methodic_711_motor_oscillation import analyze_vibration, summarize_values
from ap_methodic_guided_operation_review import analyze_failsafe_context, first_number, mode_text, row_time, vibration_severe
from ap_methodic_position_controller_review import (
    analyze_gps_ekf_confidence,
    analyze_power,
    first_table,
    paired_error,
    percentile,
    series_values,
    summarize_error,
    time_values,
)
from ap_methodic_rc import analyze_rc_input_contamination

METHODIC_123_URL = "https://ardupilot.github.io/MethodicConfigurator/TUNING_GUIDE_ArduCopter#123-precision-land"
LOG_MESSAGES_URL = "https://ardupilot.org/copter/docs/logmessages.html"

MESSAGES = [
    "MODE",
    "PL",
    "PLND",
    "PLD",
    "PLT",
    "LAND",
    "RNGF",
    "CTUN",
    "BARO",
    "GPS",
    "GPS2",
    "GPA",
    "XKF1",
    "XKF2",
    "XKF3",
    "XKF4",
    "NKF1",
    "NKF2",
    "NKF3",
    "NKF4",
    "ATT",
    "RATE",
    "RCIN",
    "MSG",
    "ERR",
    "EV",
    "BAT",
    "POWR",
    "VIBE",
    "PARM",
    "PSC",
    "POS",
    "NTUN",
]

PRECISION_MESSAGES = ("PL", "PLND", "PLD", "PLT")
PARAMETERS = ["PLND_*", "RNGFND*_*", "LAND_*", "PSC_*"]
TARGET_VALID_FIELDS = ("Heal", "Health", "TAcq", "Acq", "Found", "Valid", "Status", "Target", "HaveTarget")
TARGET_ERROR_PAIRS = (("ErrX", "ErrY"), ("X", "Y"), ("PX", "PY"), ("PosX", "PosY"), ("TPosX", "TPosY"), ("AngX", "AngY"), ("AX", "AY"))


def rows_to_tables(rows_by_message: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    try:
        import pandas as pd
    except Exception as exc:
        raise AnalysisError("pandas is required for Methodic precision-landing review. Install dependencies with: pip install -r requirements.txt") from exc
    return {name: pd.DataFrame(rows) for name, rows in rows_by_message.items() if rows}


def analyze_precision_land_review(log_path: str | Path, *, plots_dir: str | Path | None = None) -> dict[str, Any]:
    rows, index, stats = collect_dataflash(log_path, include=MESSAGES, source=str(log_path))
    tables = rows_to_tables(rows)
    params = index.get("parameters", {}) or {}

    result = empty_result(params)
    result["analysis_window"]["parser_stats"] = stats
    result["analysis_window"].update(log_window(tables))
    result["missing_evidence"] = missing_evidence(tables)
    result["evidence_used"].append({"type": "messages_present", "messages": sorted(tables.keys())})

    phase = analyze_precision_phase(tables, params)
    target = analyze_target_tracking(tables)
    rngf = analyze_rangefinder_health(tables)
    descent = analyze_descent_profile(tables, rngf)
    failsafes = analyze_failsafe_context(tables)
    intervention = analyze_manual_intervention(tables, params)
    gps_ekf = analyze_gps_ekf_confidence(tables)
    confounders = {"vibration": analyze_vibration(tables, None), "power": analyze_power(tables)}

    result["precision_landing_phase"] = phase
    result["target_tracking_quality"] = target
    result["rangefinder_health"] = rngf
    result["descent_profile"] = descent
    result["failsafe_context"] = failsafes
    result["manual_intervention_context"] = intervention
    result["gps_ekf_confidence"] = gps_ekf
    result["confounders"] = confounders
    result["evidence_used"].extend([
        {"type": "precision_landing_phase", "value": phase},
        {"type": "target_tracking_quality", "value": target},
        {"type": "rangefinder_health", "value": rngf},
        {"type": "descent_profile", "value": descent},
        {"type": "failsafe_context", "value": failsafes},
        {"type": "manual_intervention_context", "value": intervention},
        {"type": "gps_ekf_confidence", "value": gps_ekf},
        {"type": "confounders", "value": confounders},
    ])
    result["findings"] = classify_findings(result)
    result["checked_but_not_supported"] = checked_but_not_supported(tables)
    result["result"], result["safety_gate"] = classify_result(result)
    result["recommended_next_steps"] = recommended_next_steps(result)
    result["what_not_to_do"] = what_not_to_do()
    result["next_methodic_step"] = "13" if result["result"] == "ready_for_further_precision_land_tests" else None
    result["confidence_limits"] = confidence_limits(result)
    if plots_dir:
        result["plots"] = make_plots(tables, Path(plots_dir))
    return result


def empty_result(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "methodic_step": "12.3",
        "title": "Precision landing review",
        "official_reference": {"url": METHODIC_123_URL, "supporting_urls": [LOG_MESSAGES_URL]},
        "result": "inconclusive",
        "safety_gate": "repeat_step",
        "precision_landing_phase": {},
        "target_tracking_quality": {},
        "rangefinder_health": {},
        "descent_profile": {},
        "failsafe_context": {},
        "manual_intervention_context": {},
        "gps_ekf_confidence": {},
        "confounders": {},
        "evidence_used": [],
        "missing_evidence": [],
        "manual_observations_required": [
            "Precision-landing hardware is installed, aligned, and configured for the target type",
            "Landing target and lighting/environment were suitable during the test",
            "Normal landing fallback and pilot recovery path were verified",
            "No unexpected lateral motion, target hunting, hard landing, or hard-to-control behaviour was observed",
        ],
        "analysis_window": {"selection": "precision_landing_descent", "preferred_window": "Precision landing approach/descent segment with target, rangefinder, mode, failsafe, GPS/EKF, and pilot intervention context.", "start_s": None, "end_s": None},
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
    missing = []
    for pattern in PARAMETERS:
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
    return {"relevant_parameters": PARAMETERS, "present": present, "missing_or_not_logged": missing, "source": "log PARM messages" if params else "no PARM messages found"}


def missing_evidence(tables: dict[str, Any]) -> list[str]:
    missing = []
    if "MODE" not in tables:
        missing.append("Missing required message: MODE")
    if not any(name in tables for name in PRECISION_MESSAGES):
        missing.append("Missing precision landing messages: PL/PLND/PLD/PLT")
    if "RNGF" not in tables:
        missing.append("Missing strongly recommended message: RNGF")
    if "CTUN" not in tables:
        missing.append("Missing strongly recommended message: CTUN")
    if "GPS" not in tables and "GPS2" not in tables:
        missing.append("Missing strongly recommended message: GPS/GPS2")
    if not any(name in tables for name in ("XKF1", "XKF3", "XKF4", "NKF1", "NKF3", "NKF4")):
        missing.append("Missing strongly recommended message: XKF*/NKF*")
    for name in ("ATT", "RATE", "BAT", "PARM"):
        if name not in tables:
            missing.append(f"Missing strongly recommended message: {name}")
    if not any(name in tables for name in ("MSG", "ERR", "EV")):
        missing.append("Missing event context messages: MSG/ERR/EV")
    if "POWR" not in tables:
        missing.append("Missing strongly recommended message: POWR")
    return missing


def analyze_precision_phase(tables: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    mode = tables.get("MODE")
    precision_msgs = [name for name in PRECISION_MESSAGES if name in tables]
    params_present = any(name.startswith("PLND_") for name in params)
    modes_seen = []
    land_segments = []
    if mode is not None:
        rows = []
        for _, row in mode.iterrows():
            text = mode_text(row, mode.columns)
            num = first_number(row, ("Mode", "ModeNum"))
            t = row_time(row)
            modes_seen.append(text or (f"ModeNum={int(num)}" if num is not None else "unknown"))
            is_landing = "land" in text.lower() or (num is not None and int(num) == 9)
            rows.append({"time_s": t, "mode_text": text, "mode_num": int(num) if num is not None else None, "landing": is_landing})
        rows = sorted(rows, key=lambda item: item["time_s"] if item["time_s"] is not None else -1.0)
        for i, row in enumerate(rows):
            if not row["landing"]:
                continue
            end = rows[i + 1]["time_s"] if i + 1 < len(rows) else None
            duration = None if row["time_s"] is None or end is None else max(0.0, end - row["time_s"])
            land_segments.append({"start_s": row["time_s"], "end_s": end, "duration_s": duration, "mode_text": row["mode_text"], "mode_num": row["mode_num"]})
    present = bool(precision_msgs)
    if present:
        status = "present"
    elif params_present:
        status = "configured_but_no_log_evidence"
    else:
        status = "not_applicable"
    return {
        "status": status,
        "precision_messages_present": precision_msgs,
        "precision_params_present": params_present,
        "landing_segments": land_segments,
        "modes_seen": sorted(set(modes_seen))[:30],
        "reasons": [] if present else ["No precision landing target messages were identified."],
    }


def analyze_target_tracking(tables: dict[str, Any]) -> dict[str, Any]:
    df = first_table(tables, list(PRECISION_MESSAGES))
    if df is None:
        return {"available": False, "quality": "missing", "reasons": ["Precision landing target messages are missing."]}
    reasons = []
    status = target_status(df)
    errors = target_error_summary(df)
    if status.get("lost_percent") is not None and status["lost_percent"] > 5.0:
        reasons.append("Precision landing target appears lost or invalid for part of the reviewed segment.")
    if errors.get("radial_p95") is not None and errors["radial_p95"] > 1.5:
        reasons.append("Target position/angle error is high.")
    quality = "good" if not reasons else ("lost" if any("lost" in item.lower() for item in reasons) else "poor")
    return {"available": True, "quality": quality, "status": status, "target_error": errors, "reasons": reasons}


def target_status(df: Any) -> dict[str, Any]:
    for field in TARGET_VALID_FIELDS:
        vals = series_values(df, field)
        if vals:
            lost = [v for v in vals if v <= 0]
            return {"field_used": field, "samples": len(vals), "lost_samples": len(lost), "lost_percent": 100.0 * len(lost) / len(vals), "min": min(vals), "max": max(vals)}
    return {"field_used": None, "samples": len(df), "lost_percent": None, "caveat": "No explicit target-valid field was found."}


def target_error_summary(df: Any) -> dict[str, Any]:
    for xfield, yfield in TARGET_ERROR_PAIRS:
        xs = series_values(df, xfield)
        ys = series_values(df, yfield)
        count = min(len(xs), len(ys))
        if count:
            radial = [math.hypot(xs[i], ys[i]) for i in range(count)]
            return {"fields_used": [xfield, yfield], "samples": count, "radial_mean": mean(radial), "radial_p95": percentile(radial, 95), "radial_max": max(radial)}
    return {"fields_used": [], "samples": 0, "radial_p95": None, "caveat": "No target error/position pair was found."}


def analyze_rangefinder_health(tables: dict[str, Any]) -> dict[str, Any]:
    rngf = tables.get("RNGF")
    if rngf is None:
        return {"available": False, "quality": "missing", "reasons": ["RNGF missing."]}
    dist = first_present_series(rngf, ("Dist", "Range", "RangeM", "Alt"))
    status = first_present_series(rngf, ("Stat", "Status", "Health", "Heal"))
    reasons = []
    summary: dict[str, Any] = {"samples": len(rngf)}
    if dist:
        summary["distance_m"] = summarize_values(dist)
        if min(dist) <= 0.05:
            reasons.append("Rangefinder distance contains near-zero or invalid samples.")
        if percentile(dist, 95) is not None and percentile(dist, 95) > 50.0:
            reasons.append("Rangefinder distance is implausibly high for landing review.")
    else:
        reasons.append("No usable range/distance field was found in RNGF.")
    if status:
        bad = [v for v in status if v <= 0]
        summary["status"] = {"samples": len(status), "bad_percent": 100.0 * len(bad) / len(status), "min": min(status), "max": max(status)}
        if bad:
            reasons.append("Rangefinder status/health indicates invalid samples.")
    quality = "good" if dist and not reasons else ("suspect" if dist else "missing")
    return {"available": True, "quality": quality, "summary": summary, "reasons": reasons}


def analyze_descent_profile(tables: dict[str, Any], rngf: dict[str, Any]) -> dict[str, Any]:
    ctun = tables.get("CTUN")
    if ctun is None:
        return {"available": False, "quality": "missing", "reasons": ["CTUN missing."]}
    alt = series_values(ctun, "Alt")
    dalt = series_values(ctun, "DAlt")
    times = time_values(ctun)
    reasons = []
    profile: dict[str, Any] = {"altitude": summarize_values(alt) if alt else {}, "desired_altitude": summarize_values(dalt) if dalt else {}}
    rates = derivative(times, alt)
    if rates:
        profile["descent_rate_m_s"] = {"mean": mean(rates), "p95_abs": percentile([abs(v) for v in rates], 95), "min": min(rates), "max": max(rates)}
        descending = [v for v in rates if v < -0.05]
        profile["descending_percent"] = 100.0 * len(descending) / len(rates)
        if percentile([abs(v) for v in rates], 95) is not None and percentile([abs(v) for v in rates], 95) > 3.0:
            reasons.append("Descent/climb rate changes are large for a precision-landing review.")
    else:
        reasons.append("Could not estimate descent rate from CTUN.Alt.")
    alt_err = paired_error(ctun, "DAlt", "Alt", wrap=False)
    profile["altitude_error"] = summarize_error(alt_err)
    if profile["altitude_error"].get("p95_abs") is not None and profile["altitude_error"]["p95_abs"] > 2.0:
        reasons.append("Altitude target/actual error is high during landing review.")
    if rngf.get("quality") in {"missing", "suspect"}:
        reasons.append("Rangefinder evidence is missing or suspect.")
    quality = "good" if not reasons else ("poor" if any("high" in item.lower() or "large" in item.lower() for item in reasons) else "marginal")
    return {"available": True, "quality": quality, "profile": profile, "reasons": reasons}


def analyze_manual_intervention(tables: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    rc = analyze_rc_input_contamination(tables, params)
    active_axes = []
    axes = {}
    for axis, data in (rc.get("axis_activity") or {}).items():
        pct = data.get("active_percent_default_deadband")
        axes[axis] = {"available": data.get("available"), "active_percent_default_deadband": pct}
        if pct is not None and pct > 20.0:
            active_axes.append(axis)
    return {
        "rc_available": rc.get("available"),
        "pilot_intervention_likely": bool(active_axes) or rc.get("hands_off_confidence") == "low",
        "hands_off_confidence": rc.get("hands_off_confidence"),
        "axis_activity": axes,
        "reasons": [f"RC axes active during landing review: {', '.join(active_axes)}."] if active_axes else [],
        "warnings": rc.get("warnings", []),
    }


def classify_findings(result: dict[str, Any]) -> list[dict[str, Any]]:
    findings = []
    phase = result["precision_landing_phase"]
    if phase.get("status") == "not_applicable":
        findings.append(finding("info", "No precision landing feature evidence was identified.", phase))
    if result["target_tracking_quality"].get("quality") == "lost":
        findings.append(finding("critical", "Precision landing target was lost or invalid in the reviewed segment.", result["target_tracking_quality"], "bench_check_required"))
    if result["rangefinder_health"].get("quality") in {"missing", "suspect"} and phase.get("status") == "present":
        findings.append(finding("warning", "Rangefinder evidence is missing or suspect for precision landing.", result["rangefinder_health"], "bench_check_required"))
    if result["descent_profile"].get("quality") == "poor":
        findings.append(finding("warning", "Precision landing descent profile has high-error or high-rate evidence.", result["descent_profile"]))
    if result["failsafe_context"].get("issues_detected"):
        findings.append(finding("critical", "Failsafe/error context appears during precision landing review.", result["failsafe_context"], "do_not_proceed"))
    if result["gps_ekf_confidence"].get("confidence") != "good":
        findings.append(finding("warning", "GPS/EKF confidence limits precision landing conclusions.", result["gps_ekf_confidence"]))
    if result["manual_intervention_context"].get("pilot_intervention_likely"):
        findings.append(finding("warning", "RC input or pilot intervention may contaminate precision landing evidence.", result["manual_intervention_context"]))
    if vibration_severe(result["confounders"].get("vibration") or {}):
        findings.append(finding("critical", "Severe vibration or clipping blocks precision landing evidence review.", result["confounders"].get("vibration"), "do_not_proceed"))
    if result["target_tracking_quality"].get("quality") == "good" and result["rangefinder_health"].get("quality") == "good":
        findings.append({"severity": "info", "finding": "Target and rangefinder evidence did not cross conservative blocker thresholds.", "evidence": {"target": result["target_tracking_quality"], "rangefinder": result["rangefinder_health"]}})
    return findings


def classify_result(result: dict[str, Any]) -> tuple[str, str]:
    phase = result["precision_landing_phase"]
    if "Missing required message: MODE" in result.get("missing_evidence", []):
        return "inconclusive", "repeat_step"
    if phase.get("status") == "not_applicable":
        return "not_applicable", "proceed_with_caution"
    if phase.get("status") == "configured_but_no_log_evidence":
        return "inconclusive", "repeat_step"
    if result["failsafe_context"].get("issues_detected") or vibration_severe(result["confounders"].get("vibration") or {}):
        return "fail", "do_not_proceed"
    if result["target_tracking_quality"].get("quality") == "lost":
        return "needs_sensor_review", "bench_check_required"
    if result["rangefinder_health"].get("quality") in {"missing", "suspect"}:
        return "needs_sensor_review", "bench_check_required"
    if result["descent_profile"].get("quality") == "poor":
        return "fail", "repeat_step"
    if result["gps_ekf_confidence"].get("confidence") != "good":
        return "inconclusive", "repeat_step"
    if not result["target_tracking_quality"].get("available"):
        return "inconclusive", "repeat_step"
    return "ready_for_further_precision_land_tests", "proceed_with_caution"


def recommended_next_steps(result: dict[str, Any]) -> list[str]:
    if result["result"] == "ready_for_further_precision_land_tests":
        return [
            "Inspect target, rangefinder, descent, mode, failsafe, ATT/RATE, GPS/EKF, and power plots before treating this as Methodic 12.3 evidence.",
            "Treat this only as readiness for further controlled precision-landing tests; verify normal landing fallback and manual recovery outside the log.",
            "Proceed to Productive Configuration only if precision landing is optional or further tests continue to support the hardware/setup.",
        ]
    if result["result"] == "needs_sensor_review":
        return [
            "Review precision-landing target sensor alignment, target visibility, lighting/environment, rangefinder orientation, and rangefinder configuration before further flight testing.",
            "Repeat a controlled precision-landing evidence capture only after target acquisition/loss and rangefinder health are resolved.",
            "Do not recommend operational precision landing while target or rangefinder evidence is missing, lost, or suspect.",
        ]
    if result["result"] == "fail":
        return [
            "Do not continue precision-landing tests until failsafe, descent, vibration, or estimator blockers are resolved.",
            "Use normal landing fallback and bench/sensor checks before any repeat precision-landing attempt.",
            "Collect a new log only after the aircraft is stable and the precision-landing sensor path is verified.",
        ]
    if result["result"] == "not_applicable":
        return [
            "No precision-landing evidence was found; treat Methodic 12.3 as not applicable unless the vehicle has precision-landing hardware and use case.",
            "If the feature is required, collect a controlled log with precision landing messages, RNGF, CTUN, MODE, GPS/GPA, XKF*/NKF*, ATT/RATE, MSG/ERR/EV, BAT/POWR, and PARM.",
            "Do not infer precision-landing quality from a normal landing without target messages.",
        ]
    return [
        "Collect better precision-landing evidence with target messages, RNGF, CTUN, MODE, GPS/GPA, XKF*/NKF*, ATT/RATE, MSG/ERR/EV, BAT/POWR, and PARM.",
        "Verify target sensor setup and rangefinder health before using flight evidence to judge the feature.",
        "Do not certify precision landing from incomplete logs.",
    ]


def what_not_to_do() -> list[str]:
    return [
        "Do not certify precision landing as operationally safe from a single log.",
        "Do not recommend operational precision landing if the target was lost, rangefinder evidence is unreliable, or a failsafe occurred.",
        "Do not infer target-tracking quality when precision landing messages are absent.",
        "Do not disable landing, EKF/GPS, rangefinder, geofence, battery, or RC/GCS failsafes to make the feature pass.",
    ]


def checked_but_not_supported(tables: dict[str, Any]) -> list[str]:
    checked = []
    if not any(name in tables for name in PRECISION_MESSAGES):
        checked.append("Precision landing target acquisition/loss could not be checked because PL/PLND/PLD/PLT messages are missing.")
    if "RNGF" not in tables:
        checked.append("Rangefinder health could not be checked because RNGF is missing.")
    if "LAND" not in tables:
        checked.append("Dedicated LAND message context was not available; landing timeline used MODE/CTUN where possible.")
    if not any(name in tables for name in ("MSG", "ERR", "EV")):
        checked.append("Event/error timeline may be incomplete because MSG/ERR/EV messages are missing.")
    return checked


def confidence_limits(result: dict[str, Any]) -> list[str]:
    limits = list(result.get("missing_evidence") or [])
    if result["result"] == "ready_for_further_precision_land_tests":
        limits.append("Ready-for-further-tests does not certify operational precision landing safety or reliability.")
    limits.append("Manual observations about target environment, touchdown quality, and recovery path remain required.")
    return limits


def make_plots(tables: dict[str, Any], plots_dir: Path) -> list[str]:
    ensure_dir(plots_dir)
    paths = []
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except Exception:
        return paths
    ctun = tables.get("CTUN")
    rngf = tables.get("RNGF")
    if ctun is not None or rngf is not None:
        fig = make_subplots(specs=[[{"secondary_y": True}]])
        if ctun is not None:
            x = time_values(ctun)
            for field in ("DAlt", "Alt"):
                vals = series_values(ctun, field)
                if vals:
                    fig.add_trace(go.Scatter(x=x[: len(vals)], y=vals, name=f"CTUN.{field}"), secondary_y=False)
            for field in ("ThO", "ThH"):
                vals = series_values(ctun, field)
                if vals:
                    fig.add_trace(go.Scatter(x=x[: len(vals)], y=vals, name=f"CTUN.{field}"), secondary_y=True)
        if rngf is not None:
            x = time_values(rngf)
            for field in ("Dist", "Range", "RangeM", "Alt"):
                vals = series_values(rngf, field)
                if vals:
                    fig.add_trace(go.Scatter(x=x[: len(vals)], y=vals, name=f"RNGF.{field}"), secondary_y=False)
        fig.update_layout(title="Methodic 12.3 descent altitude/range")
        path = plots_dir / "methodic_12_3_descent_altitude_range.html"
        fig.write_html(path)
        paths.append(str(path))
    target = first_table(tables, list(PRECISION_MESSAGES))
    if target is not None:
        fig = go.Figure()
        x = time_values(target)
        for field in ("Heal", "Health", "TAcq", "Found", "Valid", "ErrX", "ErrY", "X", "Y", "PX", "PY", "AngX", "AngY"):
            vals = series_values(target, field)
            if vals:
                fig.add_trace(go.Scatter(x=x[: len(vals)], y=vals, name=field))
        fig.update_layout(title="Methodic 12.3 target tracking")
        path = plots_dir / "methodic_12_3_target_tracking.html"
        fig.write_html(path)
        paths.append(str(path))
    mode = tables.get("MODE")
    if mode is not None:
        fig = go.Figure()
        x = time_values(mode)
        y = [first_number(row, ("Mode", "ModeNum")) or i for i, (_, row) in enumerate(mode.iterrows())]
        labels = [mode_text(row, mode.columns) for _, row in mode.iterrows()]
        fig.add_trace(go.Scatter(x=x, y=y, mode="markers+lines", text=labels, name="MODE"))
        fig.update_layout(title="Methodic 12.3 mode timeline")
        path = plots_dir / "methodic_12_3_mode_timeline.html"
        fig.write_html(path)
        paths.append(str(path))
    for name, fields, filename in (
        ("ATT", ("DesRoll", "Roll", "DesPitch", "Pitch", "DesYaw", "Yaw"), "methodic_12_3_attitude.html"),
        ("RATE", ("RDes", "R", "PDes", "P", "YDes", "Y", "ROut", "POut", "YOut"), "methodic_12_3_rate.html"),
        ("GPS", ("Status", "NSats", "HDop", "HAcc", "Spd", "Alt"), "methodic_12_3_gps.html"),
        ("XKF4", ("SP", "SV", "SH", "SM"), "methodic_12_3_ekf.html"),
    ):
        df = first_table(tables, [name, "GPS2"] if name == "GPS" else [name, "NKF4"] if name == "XKF4" else [name])
        if df is None:
            continue
        fig = go.Figure()
        x = time_values(df)
        for field in fields:
            vals = series_values(df, field)
            if vals:
                fig.add_trace(go.Scatter(x=x[: len(vals)], y=vals, name=f"{name}.{field}"))
        fig.update_layout(title=f"Methodic 12.3 {name} context")
        path = plots_dir / filename
        fig.write_html(path)
        paths.append(str(path))
    return paths


def write_summary(path: Path, result: dict[str, Any]) -> None:
    ensure_dir(path.parent)
    lines = [
        f"# Methodic {result['methodic_step']}: {result['title']}",
        "",
        f"- Result: `{result['result']}`",
        f"- Safety gate: `{result['safety_gate']}`",
        f"- Precision phase: `{result['precision_landing_phase'].get('status')}`",
        f"- Target tracking: `{result['target_tracking_quality'].get('quality')}`",
        f"- Rangefinder: `{result['rangefinder_health'].get('quality')}`",
        f"- Descent profile: `{result['descent_profile'].get('quality')}`",
        f"- Failsafe issues: `{result['failsafe_context'].get('issues_detected')}`",
        "",
        "## Findings",
    ]
    for item in result.get("findings", []):
        lines.append(f"- {item.get('severity', 'info')}: {item.get('finding')}")
    lines.extend(["", "## Recommended Next Steps"])
    for item in result.get("recommended_next_steps", []):
        lines.append(f"- {item}")
    lines.extend(["", "## What Not To Do"])
    for item in result.get("what_not_to_do", []):
        lines.append(f"- {item}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def first_present_series(df: Any, fields: tuple[str, ...]) -> list[float]:
    for field in fields:
        vals = series_values(df, field)
        if vals:
            return vals
    return []


def derivative(times: list[float], values: list[float]) -> list[float]:
    out = []
    count = min(len(times), len(values))
    for i in range(1, count):
        dt = times[i] - times[i - 1]
        if dt > 0:
            out.append((values[i] - values[i - 1]) / dt)
    return out


def log_window(tables: dict[str, Any]) -> dict[str, Any]:
    times = []
    for df in tables.values():
        times.extend(time_values(df))
    if not times:
        return {"selection": "none", "start_s": None, "end_s": None}
    return {"selection": "precision_landing_descent", "start_s": min(times), "end_s": max(times)}


def finding(severity: str, text: str, evidence: Any, gate: str | None = None) -> dict[str, Any]:
    out = {"severity": severity, "finding": text, "evidence": evidence}
    if gate:
        out["safety_gate"] = gate
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Review Methodic 12.3 precision-landing evidence without certifying operational safety.")
    parser.add_argument("log", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--plots", type=Path)
    args = parser.parse_args()
    result = analyze_precision_land_review(args.log, plots_dir=args.plots)
    write_json(args.out, result)
    if args.summary:
        write_summary(args.summary, result)
    else:
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
