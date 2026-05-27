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
from ap_methodic_711_motor_oscillation import analyze_motor_outputs, analyze_vibration, summarize_values
from ap_methodic_oscillation import classify_oscillation
from ap_methodic_rc import analyze_rc_input_contamination

METHODIC_121_URL = "https://ardupilot.github.io/MethodicConfigurator/TUNING_GUIDE_ArduCopter#121-position-controller"
LOG_MESSAGES_URL = "https://ardupilot.org/copter/docs/logmessages.html"
MESSAGES = [
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
    "CTUN",
    "RCIN",
    "MODE",
    "VIBE",
    "BAT",
    "POWR",
    "PARM",
    "NTUN",
    "POS",
    "PSC",
    "RNGF",
    "RCOU",
    "RCO2",
    "RCO3",
    "MSG",
    "ERR",
    "EV",
    "ARM",
]
PARAMETERS = [
    "PSC_POSXY_P",
    "PSC_VELXY_P",
    "PSC_VELXY_I",
    "PSC_VELXY_D",
    "PSC_ACCXY_P",
    "PSC_ACCXY_I",
    "PSC_ACCXY_D",
    "PSC_POSZ_P",
    "PSC_VELZ_P",
    "PSC_VELZ_I",
    "PSC_ACCZ_P",
    "PSC_ACCZ_I",
    "PSC_ACCZ_D",
    "LOIT_*",
    "WPNAV_*",
]


def rows_to_tables(rows_by_message: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    try:
        import pandas as pd
    except Exception as exc:
        raise AnalysisError("pandas is required for Methodic position-controller review. Install dependencies with: pip install -r requirements.txt") from exc
    return {name: pd.DataFrame(rows) for name, rows in rows_by_message.items() if rows}


def analyze_position_controller_review(log_path: str | Path, *, plots_dir: str | Path | None = None) -> dict[str, Any]:
    rows, index, stats = collect_dataflash(log_path, include=MESSAGES, source=str(log_path))
    tables = rows_to_tables(rows)
    params = index.get("parameters", {}) or {}

    result = empty_result(params)
    result["analysis_window"]["parser_stats"] = stats
    result["analysis_window"].update(log_window(tables))
    result["missing_evidence"] = missing_evidence(tables)
    result["evidence_used"].append({"type": "messages_present", "messages": sorted(tables.keys())})

    modes = analyze_modes(tables)
    rc = analyze_rc_input_contamination(tables, params)
    gps_ekf = analyze_gps_ekf_confidence(tables)
    inner = analyze_inner_loop_prerequisite(tables, params)
    position = analyze_position_control_quality(tables, modes)
    altitude = analyze_altitude_context(tables)
    confounders = analyze_confounders(tables, params)

    result["position_control_quality"] = position
    result["gps_ekf_confidence"] = gps_ekf
    result["inner_loop_prerequisite_status"] = inner
    result["mode_context"] = modes
    result["altitude_control_context"] = altitude
    result["confounders"] = confounders
    result["evidence_used"].extend([
        {"type": "mode_context", "value": modes},
        {"type": "gps_ekf_confidence", "value": gps_ekf},
        {"type": "inner_loop_prerequisite_status", "value": inner},
        {"type": "position_control_quality", "value": position},
        {"type": "altitude_control_context", "value": altitude},
        {"type": "rc_input_contamination", "value": trim_rc(rc)},
        {"type": "confounders", "value": confounders},
    ])
    result["findings"] = classify_findings(result, rc)
    result["checked_but_not_supported"] = checked_but_not_supported(tables, result)
    result["result"], result["safety_gate"] = classify_result(result)
    result["recommended_next_steps"] = recommended_next_steps(result)
    result["what_not_to_do"] = what_not_to_do()
    result["next_methodic_step"] = "12.2" if result["result"] == "pass" else None
    result["confidence_limits"] = confidence_limits(result)
    if plots_dir:
        result["plots"] = make_plots(tables, Path(plots_dir))
    return result


def empty_result(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "methodic_step": "12.1",
        "title": "Position controller tuning",
        "official_reference": {"url": METHODIC_121_URL, "supporting_urls": [LOG_MESSAGES_URL]},
        "position_control_quality": {},
        "gps_ekf_confidence": {},
        "inner_loop_prerequisite_status": {},
        "result": "inconclusive",
        "safety_gate": "repeat_step",
        "evidence_used": [],
        "missing_evidence": [],
        "manual_observations_required": [
            "Position modes were controllable",
            "No toilet-bowling, runaway drift, or unexpected braking/overshoot",
            "Attitude/rate tune was already accepted before position-controller review",
        ],
        "analysis_window": {"selection": "whole_log", "preferred_window": "Loiter/PosHold position-control maneuvers after attitude/rate loops are tuned.", "start_s": None, "end_s": None},
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


def missing_evidence(tables: dict[str, Any]) -> list[str]:
    missing = []
    if "GPS" not in tables and "GPS2" not in tables:
        missing.append("Missing required message: GPS/GPS2")
    if not any(name in tables for name in ("XKF1", "XKF3", "XKF4", "NKF1", "NKF3", "NKF4")):
        missing.append("Missing required message: XKF*/NKF*")
    for name in ("ATT", "RATE", "CTUN", "RCIN", "MODE", "VIBE", "BAT", "PARM"):
        if name not in tables:
            missing.append(f"Missing strongly recommended message: {name}")
    if "POWR" not in tables:
        missing.append("Missing strongly recommended message: POWR")
    if not any(name in tables for name in ("POS", "NTUN", "PSC")):
        missing.append("Missing position-controller detail messages: POS, NTUN, or PSC")
    return missing


def analyze_modes(tables: dict[str, Any]) -> dict[str, Any]:
    mode = tables.get("MODE")
    if mode is None or len(mode) == 0:
        return {"available": False, "position_mode_detected": None, "modes_seen": []}
    modes = []
    detected = False
    for _, row in mode.iterrows():
        text = " ".join(str(row.get(col, "")) for col in mode.columns if str(col).lower() in {"mode", "modename", "name", "astext"})
        if text.strip():
            modes.append(text.strip())
        lower = text.lower()
        if any(token in lower for token in ("loiter", "poshold", "position", "brake", "auto", "guided")):
            detected = True
    return {"available": True, "position_mode_detected": detected, "modes_seen": sorted(set(modes))[:20]}


def analyze_gps_ekf_confidence(tables: dict[str, Any]) -> dict[str, Any]:
    gps = first_table(tables, ["GPS", "GPS2"])
    ekf4 = first_table(tables, ["XKF4", "NKF4"])
    gps_quality = "missing"
    gps_reasons = []
    gps_details = {}
    if gps is not None:
        status = series_values(gps, "Status")
        sats = series_values(gps, "NSats")
        hdop = series_values(gps, "HDop")
        hacc = series_values(gps, "HAcc")
        vacc = series_values(gps, "VAcc")
        gps_details = {
            "status": summarize_values(status) if status else {},
            "nsats": summarize_values(sats) if sats else {},
            "hdop": summarize_values(hdop) if hdop else {},
            "hacc": summarize_values(hacc) if hacc else {},
            "vacc": summarize_values(vacc) if vacc else {},
        }
        if status and min(status) < 3:
            gps_reasons.append("GPS status dropped below 3D fix.")
        if sats and percentile(sats, 5) is not None and percentile(sats, 5) < 8:
            gps_reasons.append("GPS satellite count is low.")
        if hdop and percentile(hdop, 95) is not None and percentile(hdop, 95) > 2.0:
            gps_reasons.append("GPS HDop is high.")
        if hacc and percentile(hacc, 95) is not None and percentile(hacc, 95) > 2.0:
            gps_reasons.append("GPS horizontal accuracy is poor.")
        gps_quality = "good" if not gps_reasons else "poor"
    ekf_quality = "missing"
    ekf_reasons = []
    ekf_details = {}
    if ekf4 is not None:
        ratios = {}
        for field in ("SP", "SV", "SH", "SM"):
            vals = series_values(ekf4, field)
            if vals:
                ratios[field] = summarize_values(vals)
                if percentile(vals, 95) is not None and percentile(vals, 95) > 1.0:
                    ekf_reasons.append(f"EKF {field} test ratio exceeds 1.0.")
        ekf_details["test_ratios"] = ratios
        ekf_quality = "good" if not ekf_reasons else "poor"
    quality = "good"
    reasons = gps_reasons + ekf_reasons
    if gps_quality == "missing" or ekf_quality == "missing":
        quality = "poor"
        reasons.append("GPS or EKF evidence is missing.")
    elif gps_quality == "poor" or ekf_quality == "poor":
        quality = "poor"
    return {"confidence": quality, "gps": {"quality": gps_quality, "details": gps_details, "reasons": gps_reasons}, "ekf": {"quality": ekf_quality, "details": ekf_details, "reasons": ekf_reasons}, "reasons": reasons}


def analyze_inner_loop_prerequisite(tables: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    rate = tables.get("RATE")
    att = tables.get("ATT")
    motor = analyze_motor_outputs(tables, None, params)
    vibe = analyze_vibration(tables, None)
    axes = {}
    blockers = []
    times = time_values(rate)
    for axis, field in {"roll": "ROut", "pitch": "POut", "yaw": "YOut"}.items():
        values = series_values(rate, field)
        if not values:
            axes[axis] = {"available": False}
            blockers.append(f"RATE.{field} missing.")
            continue
        osc = classify_oscillation(values, times[: len(values)], threshold=0.15, min_samples=20, min_duration_s=2.0)
        axes[axis] = {
            **summarize_values(values, threshold=0.15),
            "classification": osc.get("classification"),
            "classification_reason": osc.get("reason", []),
            "highpass_p95_abs": (osc.get("metrics") or {}).get("highpass_residual_p95_abs"),
        }
        if osc.get("classification") in {"oscillatory", "mixed"} and (axes[axis].get("p95_abs") or 0.0) > 0.15:
            blockers.append(f"{axis} RATE output is high and oscillatory.")
        elif (axes[axis].get("p95_abs") or 0.0) > 0.25:
            blockers.append(f"{axis} RATE output demand is high.")
    att_errors = {}
    if att is not None:
        for axis, fields in {"roll": ("DesRoll", "Roll"), "pitch": ("DesPitch", "Pitch"), "yaw": ("DesYaw", "Yaw")}.items():
            err = paired_error(att, fields[0], fields[1], wrap=(axis == "yaw"))
            att_errors[axis] = summarize_error(err)
            if (att_errors[axis].get("p95_abs") or 0.0) > (10.0 if axis != "yaw" else 20.0):
                blockers.append(f"{axis} attitude tracking error is high.")
    else:
        blockers.append("ATT missing.")
    saturated = [
        name for name, data in (motor.get("channels") or {}).items()
        if (data.get("pct_high_ge_1900") or 0.0) > 1.0 or (data.get("pct_low_le_1100") or 0.0) > 1.0 or data.get("persistent_high") or data.get("persistent_low")
    ]
    if saturated:
        blockers.append("Motor output saturation/persistent rail contact appears.")
    if vibration_severe(vibe):
        blockers.append("Vibration or clipping blocks outer-loop tuning.")
    status = "acceptable" if not blockers and rate is not None and att is not None else "poor"
    return {"status": status, "rate_outputs": axes, "attitude_errors": att_errors, "motor_output": {"available": motor.get("available"), "saturated_channels": saturated}, "vibration": vibe, "blockers": blockers}


def analyze_position_control_quality(tables: dict[str, Any], modes: dict[str, Any]) -> dict[str, Any]:
    detail = first_table(tables, ["PSC", "NTUN", "POS"])
    if detail is None:
        return {"quality": "missing", "available": False, "reasons": ["POS/NTUN/PSC messages missing; desired vs actual position/velocity cannot be reviewed."]}
    velocity = summarize_pair_errors(detail, [("DVelX", "VelX"), ("DVelY", "VelY"), ("TVX", "VX"), ("TVY", "VY"), ("VelXDes", "VelX"), ("VelYDes", "VelY")])
    position = summarize_pair_errors(detail, [("DPosX", "PosX"), ("DPosY", "PosY"), ("TPosX", "PosX"), ("TPosY", "PosY"), ("PDesX", "PX"), ("PDesY", "PY")])
    gps_radius = gps_radius_context(tables)
    desired_att = desired_attitude_oscillation(tables)
    reasons = []
    if not modes.get("position_mode_detected"):
        reasons.append("Position-control mode was not identified from MODE messages.")
    if not velocity["available"] and not position["available"]:
        reasons.append("No desired-vs-actual position or velocity pairs were available.")
    if velocity.get("p95_abs") is not None and velocity["p95_abs"] > 1.5:
        reasons.append("Position velocity tracking error is high.")
    if position.get("p95_abs") is not None and position["p95_abs"] > 3.0:
        reasons.append("Position tracking error is high.")
    if desired_att.get("oscillatory_axes"):
        reasons.append("Desired attitude appears oscillatory, which can indicate position-controller excitation of the inner loop.")
    if gps_radius.get("radius_p95_m") is not None and gps_radius["radius_p95_m"] > 5.0 and not velocity["available"] and not position["available"]:
        reasons.append("GPS position wandered widely, but detailed desired-vs-actual position messages are missing.")
    quality = "good" if not reasons else ("poor" if any("high" in item.lower() or "oscillatory" in item.lower() for item in reasons) else "marginal")
    return {"available": True, "quality": quality, "velocity_tracking": velocity, "position_tracking": position, "gps_radius": gps_radius, "desired_attitude": desired_att, "reasons": reasons}


def analyze_altitude_context(tables: dict[str, Any]) -> dict[str, Any]:
    ctun = tables.get("CTUN")
    if ctun is None:
        return {"available": False}
    err = paired_error(ctun, "DAlt", "Alt", wrap=False)
    tho = series_values(ctun, "ThO")
    thh = series_values(ctun, "ThH")
    return {"available": True, "altitude_error": summarize_error(err), "throttle_output": summarize_values(tho) if tho else {}, "hover_throttle": summarize_values(thh) if thh else {}}


def analyze_confounders(tables: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    return {"vibration": analyze_vibration(tables, None), "power": analyze_power(tables), "motor_output": analyze_motor_outputs(tables, None, params)}


def analyze_power(tables: dict[str, Any]) -> dict[str, Any]:
    out = {"available": False, "messages": {}, "warnings": []}
    for name, fields in {"BAT": ["Volt", "VoltR", "Curr"], "POWR": ["Vcc", "Vservo", "Flags"]}.items():
        df = tables.get(name)
        if df is None:
            continue
        msg = {}
        for field in fields:
            vals = series_values(df, field)
            if vals:
                msg[field] = {"min": min(vals), "max": max(vals), "mean": mean(vals), "samples": len(vals)}
                if field == "Vcc" and min(vals) < 4.7:
                    out["warnings"].append("Board Vcc below 4.7 V.")
        out["messages"][name] = msg
    out["available"] = bool(out["messages"])
    return out


def classify_findings(result: dict[str, Any], rc: dict[str, Any]) -> list[dict[str, Any]]:
    findings = []
    if result["gps_ekf_confidence"].get("confidence") != "good":
        findings.append(finding("critical", "GPS/EKF confidence is not good enough for position-controller tuning.", result["gps_ekf_confidence"], "do_not_proceed"))
    if result["inner_loop_prerequisite_status"].get("status") != "acceptable":
        findings.append(finding("critical", "Inner attitude/rate loop prerequisite is not acceptable for outer-loop tuning.", result["inner_loop_prerequisite_status"], "repeat_step"))
    position = result["position_control_quality"]
    if position.get("quality") == "missing":
        findings.append(finding("inconclusive", "Position-controller detail messages are missing.", position))
    elif position.get("quality") == "poor":
        findings.append(finding("warning", "Position-control tracking or desired-attitude behaviour needs review.", position))
    elif position.get("quality") == "good":
        findings.append({"severity": "info", "finding": "Position-control evidence did not cross conservative blocker thresholds.", "evidence": position})
    if rc.get("hands_off_confidence") == "low":
        findings.append(finding("warning", "RC input contamination may limit position-controller conclusions.", trim_rc(rc)))
    return findings


def classify_result(result: dict[str, Any]) -> tuple[str, str]:
    if result["gps_ekf_confidence"].get("confidence") != "good":
        return "fix_ekf_gps_first", "do_not_proceed"
    if result["inner_loop_prerequisite_status"].get("status") != "acceptable":
        return "collect_better_log", "repeat_step"
    quality = result["position_control_quality"].get("quality")
    if quality == "missing":
        return "inconclusive", "repeat_step"
    if quality == "poor":
        return "reduce_position_gains", "proceed_with_caution"
    if any("POS/NTUN/PSC" in item for item in result.get("missing_evidence", [])):
        return "inconclusive", "repeat_step"
    return "pass", "proceed_with_caution"


def recommended_next_steps(result: dict[str, Any]) -> list[str]:
    if result["result"] == "pass":
        return [
            "Inspect GPS/EKF, inner-loop, and position-tracking plots before treating this as evidence for Methodic 12.1 completion.",
            "Proceed to 12.2 only if guided/autonomous operation is actually in scope and manual observations match the log evidence.",
            "Keep PSC/LOIT/WPNAV changes as review candidates only; do not auto-write parameters.",
        ]
    if result["result"] == "fix_ekf_gps_first":
        return [
            "Fix GPS/EKF quality before position-controller tuning.",
            "Do not interpret position oscillation or drift as a PSC tuning issue while estimator quality is poor.",
            "Collect GPS/GPA, XKF*/NKF*, MODE, RCIN, ATT/RATE, VIBE, BAT/POWR, and POS/NTUN/PSC evidence after estimator issues are resolved.",
        ]
    if result["result"] == "collect_better_log":
        return [
            "Do not tune outer position loops until attitude/rate loop evidence is clean.",
            "Return to Methodic tune evaluation or filter/vibration review and collect a clean inner-loop validation log first.",
            "Repeat 12.1 only after RATE outputs, attitude tracking, motor saturation, and vibration are acceptable.",
        ]
    if result["result"] == "reduce_position_gains":
        return [
            "Review PSC/LOIT/WPNAV parameters as candidates only after confirming GPS/EKF and inner-loop prerequisites remain clean.",
            "If position-controller desired attitude is oscillatory, consider conservative position-gain reduction externally and validate with a controlled log.",
            "Do not make PSC changes automatically from this evidence.",
        ]
    return [
        "Collect a better position-controller review log with Loiter/PosHold, GPS/GPA, XKF*/NKF*, ATT/RATE, CTUN, RCIN, MODE, VIBE, BAT/POWR, PARM, and POS/NTUN/PSC evidence.",
        "Use position modes only when the aircraft is already stable and attitude/rate loops have been accepted.",
        "Do not infer PSC tuning needs from logs missing desired-vs-actual position or velocity messages.",
    ]


def what_not_to_do() -> list[str]:
    return [
        "Do not tune position controller if attitude/rate loops are not clean.",
        "Do not tune position controller if GPS/EKF evidence is poor.",
        "Do not auto-change PSC, LOIT, or WPNAV parameters from this review.",
        "Do not treat drift, overshoot, or toilet-bowling as position-gain evidence until estimator and inner-loop blockers are ruled out.",
    ]


def checked_but_not_supported(tables: dict[str, Any], result: dict[str, Any]) -> list[str]:
    checked = []
    if not any(name in tables for name in ("POS", "NTUN", "PSC")):
        checked.append("Desired-vs-actual position/velocity tracking could not be checked because POS/NTUN/PSC messages are missing.")
    if "GPA" not in tables:
        checked.append("GPA is missing; GPS accuracy review used GPS fields only.")
    if "RNGF" not in tables:
        checked.append("RNGF is missing; rangefinder effects on altitude/position behaviour were not reviewed.")
    return checked


def confidence_limits(result: dict[str, Any]) -> list[str]:
    limits = list(result.get("missing_evidence") or [])
    if result["inner_loop_prerequisite_status"].get("status") != "acceptable":
        limits.append("Position-controller conclusions are blocked until inner-loop health is acceptable.")
    if result["gps_ekf_confidence"].get("confidence") != "good":
        limits.append("Position-controller conclusions are blocked until GPS/EKF health is acceptable.")
    return limits


def make_plots(tables: dict[str, Any], plots_dir: Path) -> list[str]:
    ensure_dir(plots_dir)
    paths = []
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except Exception:
        return paths
    gps = first_table(tables, ["GPS", "GPS2"])
    if gps is not None:
        fig = go.Figure()
        x = time_values(gps)
        for field in ("Lat", "Lng", "Alt", "Spd"):
            vals = series_values(gps, field)
            if vals:
                fig.add_trace(go.Scatter(x=x[: len(vals)], y=vals, name=f"GPS.{field}"))
        fig.update_layout(title="Methodic 12.1 GPS/position context")
        path = plots_dir / "methodic_12_1_gps_position.html"
        fig.write_html(path)
        paths.append(str(path))
    detail = first_table(tables, ["PSC", "NTUN", "POS"])
    if detail is not None:
        fig = go.Figure()
        x = time_values(detail)
        for field in ("DVelX", "VelX", "DVelY", "VelY", "DPosX", "PosX", "DPosY", "PosY", "TVX", "VX", "TVY", "VY"):
            vals = series_values(detail, field)
            if vals:
                fig.add_trace(go.Scatter(x=x[: len(vals)], y=vals, name=field))
        fig.update_layout(title="Methodic 12.1 position/velocity tracking")
        path = plots_dir / "methodic_12_1_position_tracking.html"
        fig.write_html(path)
        paths.append(str(path))
    att = tables.get("ATT")
    if att is not None:
        fig = go.Figure()
        x = time_values(att)
        for field in ("DesRoll", "Roll", "DesPitch", "Pitch", "DesYaw", "Yaw"):
            vals = series_values(att, field)
            if vals:
                fig.add_trace(go.Scatter(x=x[: len(vals)], y=vals, name=f"ATT.{field}"))
        fig.update_layout(title="Methodic 12.1 ATT desired/actual")
        path = plots_dir / "methodic_12_1_attitude.html"
        fig.write_html(path)
        paths.append(str(path))
    rate = tables.get("RATE")
    if rate is not None:
        fig = go.Figure()
        x = time_values(rate)
        for field in ("RDes", "R", "PDes", "P", "YDes", "Y", "ROut", "POut", "YOut"):
            vals = series_values(rate, field)
            if vals:
                fig.add_trace(go.Scatter(x=x[: len(vals)], y=vals, name=f"RATE.{field}"))
        fig.update_layout(title="Methodic 12.1 RATE tracking/output")
        path = plots_dir / "methodic_12_1_rate.html"
        fig.write_html(path)
        paths.append(str(path))
    ctun = tables.get("CTUN")
    if ctun is not None:
        fig = make_subplots(specs=[[{"secondary_y": True}]])
        x = time_values(ctun)
        for field in ("DAlt", "Alt"):
            vals = series_values(ctun, field)
            if vals:
                fig.add_trace(go.Scatter(x=x[: len(vals)], y=vals, name=f"CTUN.{field}"), secondary_y=False)
        for field in ("ThO", "ThH"):
            vals = series_values(ctun, field)
            if vals:
                fig.add_trace(go.Scatter(x=x[: len(vals)], y=vals, name=f"CTUN.{field}"), secondary_y=True)
        fig.update_layout(title="Methodic 12.1 CTUN altitude/throttle")
        path = plots_dir / "methodic_12_1_ctun.html"
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
        f"- Position quality: `{result['position_control_quality'].get('quality')}`",
        f"- GPS/EKF confidence: `{result['gps_ekf_confidence'].get('confidence')}`",
        f"- Inner-loop prerequisite: `{result['inner_loop_prerequisite_status'].get('status')}`",
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


def first_table(tables: dict[str, Any], names: list[str]) -> Any | None:
    for name in names:
        df = tables.get(name)
        if df is not None and len(df):
            return df
    return None


def series_values(df: Any, col: str | None) -> list[float]:
    if df is None or not col:
        return []
    s = numeric_series(df, [col])
    if s is None:
        return []
    return [float(v) for v in s.dropna().tolist() if math.isfinite(float(v))]


def time_values(df: Any) -> list[float]:
    if df is None or len(df) == 0:
        return []
    for col, scale in (("TimeS", 1.0), ("Time", 1.0), ("TimeUS", 1e-6), ("TimeMS", 1e-3)):
        if col in df:
            return [float(v) * scale for v in numeric_series(df, [col]).dropna().tolist()]
    return [float(i) for i in range(len(df))]


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * pct / 100.0))))
    return float(ordered[idx])


def paired_error(df: Any, desired: str, actual: str, *, wrap: bool) -> list[float]:
    d = series_values(df, desired)
    a = series_values(df, actual)
    out = []
    for err in [d[i] - a[i] for i in range(min(len(d), len(a)))]:
        out.append(((err + 180.0) % 360.0) - 180.0 if wrap else err)
    return out


def summarize_error(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"available": False, "samples": 0}
    return {"available": True, "samples": len(values), "rms": math.sqrt(sum(v * v for v in values) / len(values)), "p95_abs": percentile([abs(v) for v in values], 95), "max_abs": max(abs(v) for v in values)}


def summarize_pair_errors(df: Any, pairs: list[tuple[str, str]]) -> dict[str, Any]:
    errors = []
    used = []
    for desired, actual in pairs:
        err = paired_error(df, desired, actual, wrap=False)
        if err:
            errors.extend(err)
            used.append((desired, actual))
    out = summarize_error(errors)
    out["available"] = bool(errors)
    out["pairs_used"] = used
    return out


def gps_radius_context(tables: dict[str, Any]) -> dict[str, Any]:
    gps = first_table(tables, ["GPS", "GPS2"])
    lat = series_values(gps, "Lat")
    lng = series_values(gps, "Lng")
    count = min(len(lat), len(lng))
    if count < 2:
        return {"available": False}
    lat0 = lat[0] / (1e7 if max(abs(v) for v in lat[:count]) > 1000 else 1.0)
    lng0 = lng[0] / (1e7 if max(abs(v) for v in lng[:count]) > 1000 else 1.0)
    radii = []
    for la, lo in zip(lat[:count], lng[:count]):
        la = la / (1e7 if abs(la) > 1000 else 1.0)
        lo = lo / (1e7 if abs(lo) > 1000 else 1.0)
        dy = (la - lat0) * 111_320.0
        dx = (lo - lng0) * 111_320.0 * math.cos(math.radians(lat0))
        radii.append(math.hypot(dx, dy))
    return {"available": True, "radius_p95_m": percentile(radii, 95), "radius_max_m": max(radii), "samples": count}


def desired_attitude_oscillation(tables: dict[str, Any]) -> dict[str, Any]:
    att = tables.get("ATT")
    if att is None:
        return {"available": False, "oscillatory_axes": []}
    times = time_values(att)
    axes = {}
    osc_axes = []
    for axis, field in {"roll": "DesRoll", "pitch": "DesPitch"}.items():
        vals = series_values(att, field)
        osc = classify_oscillation(vals, times[: len(vals)], threshold=5.0, min_samples=20, min_duration_s=2.0) if vals else {"classification": "inconclusive", "reason": ["field missing"]}
        axes[axis] = {"classification": osc.get("classification"), "reason": osc.get("reason", []), "summary": summarize_values(vals) if vals else {}}
        if osc.get("classification") in {"oscillatory", "mixed"} and ((osc.get("metrics") or {}).get("highpass_residual_p95_abs") or 0.0) > 2.0:
            osc_axes.append(axis)
    return {"available": True, "axes": axes, "oscillatory_axes": osc_axes}


def log_window(tables: dict[str, Any]) -> dict[str, Any]:
    times = []
    for df in tables.values():
        times.extend(time_values(df))
    if not times:
        return {"selection": "none", "start_s": None, "end_s": None}
    return {"selection": "whole_log", "start_s": min(times), "end_s": max(times)}


def vibration_severe(vibe: dict[str, Any]) -> bool:
    clips = vibe.get("clip_delta") or {}
    return any((safe_float(v) or 0.0) > 0 for v in clips.values()) or (safe_float(vibe.get("p95_axis")) or 0.0) > 30.0 or (safe_float(vibe.get("max_axis")) or 0.0) > 45.0


def finding(severity: str, text: str, evidence: Any, gate: str | None = None) -> dict[str, Any]:
    out = {"severity": severity, "finding": text, "evidence": evidence}
    if gate:
        out["safety_gate"] = gate
    return out


def trim_rc(rc: dict[str, Any]) -> dict[str, Any]:
    axes = {}
    for axis, data in (rc.get("axis_activity") or {}).items():
        axes[axis] = {"active_percent_default_deadband": data.get("active_percent_default_deadband"), "available": data.get("available")}
    return {"available": rc.get("available"), "hands_off_confidence": rc.get("hands_off_confidence"), "axis_activity": axes, "warnings": rc.get("warnings", [])}


def main() -> int:
    parser = argparse.ArgumentParser(description="Review Methodic 12.1 position-controller tuning evidence without changing PSC parameters.")
    parser.add_argument("log", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--plots", type=Path)
    args = parser.parse_args()
    result = analyze_position_controller_review(args.log, plots_dir=args.plots)
    write_json(args.out, result)
    if args.summary:
        write_summary(args.summary, result)
    else:
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
