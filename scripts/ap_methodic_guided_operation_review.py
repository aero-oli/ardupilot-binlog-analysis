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

METHODIC_122_URL = "https://ardupilot.github.io/MethodicConfigurator/TUNING_GUIDE_ArduCopter#122-guided-operation-without-rc-transmitter"
LOG_MESSAGES_URL = "https://ardupilot.org/copter/docs/logmessages.html"

MESSAGES = [
    "MODE",
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
    "MSG",
    "EV",
    "ERR",
    "BAT",
    "POWR",
    "VIBE",
    "PARM",
    "POS",
    "NTUN",
    "PSC",
    "CMD",
    "MISSION",
    "MAV",
    "MAVC",
    "FMT",
]

PARAMETERS = [
    "GUID_OPTIONS",
    "FS_*",
    "GCS_*",
    "FENCE_*",
    "WPNAV_*",
    "PSC_POSXY_P",
    "PSC_VELXY_P",
    "PSC_ACCXY_P",
    "PSC_POSZ_P",
    "PSC_VELZ_P",
    "PSC_ACCZ_P",
]

FAILSAFE_WORDS = ("failsafe", "ekf", "gps glitch", "fence", "breach", "battery", "radio", "gcs failsafe", "crash", "error")
FAILSAFE_MODE_REASONS = {3: "RADIO_FAILSAFE", 4: "BATTERY_FAILSAFE", 5: "GCS_FAILSAFE", 6: "EKF_FAILSAFE", 7: "GPS_GLITCH", 10: "FENCE_BREACHED", 11: "TERRAIN_FAILSAFE"}


def rows_to_tables(rows_by_message: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    try:
        import pandas as pd
    except Exception as exc:
        raise AnalysisError("pandas is required for Methodic guided-operation review. Install dependencies with: pip install -r requirements.txt") from exc
    return {name: pd.DataFrame(rows) for name, rows in rows_by_message.items() if rows}


def analyze_guided_operation_review(log_path: str | Path, *, plots_dir: str | Path | None = None) -> dict[str, Any]:
    rows, index, stats = collect_dataflash(log_path, include=MESSAGES, source=str(log_path))
    tables = rows_to_tables(rows)
    params = index.get("parameters", {}) or {}

    result = empty_result(params)
    result["analysis_window"]["parser_stats"] = stats
    result["analysis_window"].update(log_window(tables))
    result["missing_evidence"] = missing_evidence(tables)
    result["evidence_used"].append({"type": "messages_present", "messages": sorted(tables.keys())})

    guided = analyze_guided_segments(tables)
    tracking = analyze_tracking_quality(tables, guided)
    failsafes = analyze_failsafe_context(tables)
    intervention = analyze_manual_intervention(tables, params)
    gps_ekf = analyze_gps_ekf_confidence(tables)
    confounders = analyze_confounders(tables)
    companion = analyze_companion_context(tables)

    result["guided_segments"] = guided
    result["tracking_quality"] = tracking
    result["failsafe_context"] = failsafes
    result["manual_intervention_context"] = intervention
    result["gps_ekf_confidence"] = gps_ekf
    result["companion_command_context"] = companion
    result["confounders"] = confounders
    result["evidence_used"].extend([
        {"type": "guided_segments", "value": guided},
        {"type": "tracking_quality", "value": tracking},
        {"type": "failsafe_context", "value": failsafes},
        {"type": "manual_intervention_context", "value": intervention},
        {"type": "gps_ekf_confidence", "value": gps_ekf},
        {"type": "companion_command_context", "value": companion},
        {"type": "confounders", "value": confounders},
    ])
    result["findings"] = classify_findings(result)
    result["checked_but_not_supported"] = checked_but_not_supported(tables)
    result["result"], result["safety_gate"] = classify_result(result)
    result["recommended_next_steps"] = recommended_next_steps(result)
    result["what_not_to_do"] = what_not_to_do()
    result["next_methodic_step"] = "12.3" if result["result"] == "ready_for_guided_checks" else None
    result["confidence_limits"] = confidence_limits(result)
    if plots_dir:
        result["plots"] = make_plots(tables, Path(plots_dir))
    return result


def empty_result(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "methodic_step": "12.2",
        "title": "Guided operation review",
        "official_reference": {"url": METHODIC_122_URL, "supporting_urls": [LOG_MESSAGES_URL]},
        "result": "inconclusive",
        "safety_gate": "repeat_step",
        "guided_segments": {},
        "tracking_quality": {},
        "failsafe_context": {},
        "manual_intervention_context": {},
        "gps_ekf_confidence": {},
        "companion_command_context": {},
        "confounders": {},
        "evidence_used": [],
        "missing_evidence": [],
        "manual_observations_required": [
            "Guided or companion-computer operation is an actual requirement",
            "Manual recovery path and pilot override plan were verified before the test",
            "No unexpected motion, drift, altitude change, or hard-to-control behaviour was observed",
            "Geofence, RC/GCS failsafe, battery failsafe, and companion-link behaviour were reviewed outside the log",
        ],
        "analysis_window": {"selection": "guided_mode_segments", "preferred_window": "Guided command and response segment with failsafe, RC override, GPS/EKF, power, and vibration context.", "start_s": None, "end_s": None},
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
    if "MODE" not in tables:
        missing.append("Missing required message: MODE")
    if "GPS" not in tables and "GPS2" not in tables:
        missing.append("Missing strongly recommended message: GPS/GPS2")
    if not any(name in tables for name in ("XKF1", "XKF3", "XKF4", "NKF1", "NKF3", "NKF4")):
        missing.append("Missing strongly recommended message: XKF*/NKF*")
    for name in ("ATT", "RATE", "CTUN", "RCIN", "BAT", "PARM"):
        if name not in tables:
            missing.append(f"Missing strongly recommended message: {name}")
    if "POWR" not in tables:
        missing.append("Missing strongly recommended message: POWR")
    if "VIBE" not in tables:
        missing.append("Missing strongly recommended message: VIBE")
    if not any(name in tables for name in ("MSG", "EV", "ERR")):
        missing.append("Missing event context messages: MSG/EV/ERR")
    if not any(name in tables for name in ("POS", "NTUN", "PSC", "GPS")):
        missing.append("Missing position/velocity tracking messages: POS, NTUN, PSC, or GPS")
    if not any(name in tables for name in ("CMD", "MAV", "MAVC", "MSG")):
        missing.append("Missing direct companion/MAVLink command context; review command source externally.")
    return missing


def analyze_guided_segments(tables: dict[str, Any]) -> dict[str, Any]:
    mode = tables.get("MODE")
    if mode is None or len(mode) == 0:
        return {"available": False, "present": None, "segments": [], "modes_seen": [], "reasons": ["MODE missing."]}
    rows = []
    modes_seen = []
    for _, row in mode.iterrows():
        text = mode_text(row, mode.columns)
        num = first_number(row, ("Mode", "ModeNum"))
        reason = first_number(row, ("Rsn", "Reason"))
        t = row_time(row)
        is_guided = "guided" in text.lower() or (num is not None and int(num) == 4)
        modes_seen.append(text or (f"ModeNum={int(num)}" if num is not None else "unknown"))
        rows.append({"time_s": t, "mode_text": text, "mode_num": int(num) if num is not None else None, "reason_num": int(reason) if reason is not None else None, "guided": is_guided})
    rows = sorted(rows, key=lambda item: item["time_s"] if item["time_s"] is not None else -1.0)
    segments = []
    for i, row in enumerate(rows):
        if not row["guided"]:
            continue
        start = row["time_s"]
        end = rows[i + 1]["time_s"] if i + 1 < len(rows) else None
        duration = None if start is None or end is None else max(0.0, end - start)
        segments.append({"start_s": start, "end_s": end, "duration_s": duration, "entry_reason": mode_reason(row.get("reason_num")), "mode_text": row["mode_text"], "mode_num": row["mode_num"]})
    present = bool(segments)
    return {"available": True, "present": present, "segments": segments, "modes_seen": sorted(set(modes_seen))[:30], "reasons": [] if present else ["No Guided mode segment was identified from MODE."]}


def analyze_tracking_quality(tables: dict[str, Any], guided: dict[str, Any]) -> dict[str, Any]:
    if not guided.get("present"):
        return {"available": False, "quality": "not_applicable", "reasons": ["No Guided segment to assess."]}
    detail = first_table(tables, ["PSC", "NTUN", "POS"])
    velocity = summarize_pair_errors(detail, [("DVelX", "VelX"), ("DVelY", "VelY"), ("TVX", "VX"), ("TVY", "VY"), ("VelXDes", "VelX"), ("VelYDes", "VelY")]) if detail is not None else {"available": False}
    position = summarize_pair_errors(detail, [("DPosX", "PosX"), ("DPosY", "PosY"), ("TPosX", "PosX"), ("TPosY", "PosY"), ("PDesX", "PX"), ("PDesY", "PY")]) if detail is not None else {"available": False}
    altitude = altitude_tracking(tables)
    attitude = attitude_tracking(tables)
    reasons = []
    if not velocity.get("available") and not position.get("available"):
        reasons.append("No desired-vs-actual Guided position/velocity pairs were available.")
    if velocity.get("p95_abs") is not None and velocity["p95_abs"] > 1.5:
        reasons.append("Guided velocity tracking error is high.")
    if position.get("p95_abs") is not None and position["p95_abs"] > 3.0:
        reasons.append("Guided position tracking error is high.")
    if altitude.get("altitude_error", {}).get("p95_abs") is not None and altitude["altitude_error"]["p95_abs"] > 2.0:
        reasons.append("Guided altitude tracking error is high.")
    for axis, err in (attitude.get("attitude_errors") or {}).items():
        limit = 10.0 if axis != "yaw" else 25.0
        if err.get("p95_abs") is not None and err["p95_abs"] > limit:
            reasons.append(f"Guided {axis} attitude tracking error is high.")
    available = velocity.get("available") or position.get("available") or altitude.get("available") or attitude.get("available")
    quality = "good" if available and not reasons else ("poor" if any("high" in item.lower() for item in reasons) else "marginal")
    return {"available": bool(available), "quality": quality, "velocity_tracking": velocity, "position_tracking": position, "altitude_tracking": altitude, "attitude_tracking": attitude, "reasons": reasons}


def analyze_failsafe_context(tables: dict[str, Any]) -> dict[str, Any]:
    events = []
    mode = tables.get("MODE")
    if mode is not None:
        for _, row in mode.iterrows():
            reason = first_number(row, ("Rsn", "Reason"))
            if reason is not None and int(reason) in FAILSAFE_MODE_REASONS:
                events.append({"message": "MODE", "time_s": row_time(row), "reason": FAILSAFE_MODE_REASONS[int(reason)]})
    for name in ("MSG", "EV", "ERR"):
        df = tables.get(name)
        if df is None:
            continue
        for _, row in df.iterrows():
            text = " ".join(str(row.get(col, "")) for col in df.columns if col not in {"TimeUS", "TimeMS", "TimeS", "Time", "_type"})
            lower = text.lower()
            if name == "ERR" or any(word in lower for word in FAILSAFE_WORDS):
                events.append({"message": name, "time_s": row_time(row), "text": text.strip()[:200]})
    return {"available": any(name in tables for name in ("MODE", "MSG", "EV", "ERR")), "issues_detected": bool(events), "events": events[:80]}


def analyze_manual_intervention(tables: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    rc = analyze_rc_input_contamination(tables, params)
    axes = {}
    active_axes = []
    for axis, data in (rc.get("axis_activity") or {}).items():
        pct = data.get("active_percent_default_deadband")
        axes[axis] = {"available": data.get("available"), "active_percent_default_deadband": pct}
        if pct is not None and pct > 20.0:
            active_axes.append(axis)
    likely = bool(active_axes) or rc.get("hands_off_confidence") == "low"
    reasons = []
    if active_axes:
        reasons.append(f"RC axes active during review window: {', '.join(active_axes)}.")
    if rc.get("hands_off_confidence") == "low":
        reasons.append("RC input was not hands-off enough to separate companion response from pilot intervention.")
    return {"rc_available": rc.get("available"), "pilot_intervention_likely": likely, "hands_off_confidence": rc.get("hands_off_confidence"), "axis_activity": axes, "reasons": reasons, "warnings": rc.get("warnings", [])}


def analyze_confounders(tables: dict[str, Any]) -> dict[str, Any]:
    return {"vibration": analyze_vibration(tables, None), "power": analyze_power(tables)}


def analyze_companion_context(tables: dict[str, Any]) -> dict[str, Any]:
    present = [name for name in ("CMD", "MAV", "MAVC", "MSG") if name in tables]
    snippets = []
    for name in present:
        df = tables.get(name)
        if df is None:
            continue
        for _, row in df.head(20).iterrows():
            text = " ".join(str(row.get(col, "")) for col in df.columns if col not in {"TimeUS", "TimeMS", "TimeS", "Time", "_type"})
            if text.strip():
                snippets.append({"message": name, "time_s": row_time(row), "text": text.strip()[:160]})
    return {"available": bool(present), "messages_present": present, "samples": snippets[:30], "caveat": None if present else "No direct companion/MAVLink command messages were logged; verify companion command source and failsafe behaviour externally."}


def classify_findings(result: dict[str, Any]) -> list[dict[str, Any]]:
    findings = []
    if not result["guided_segments"].get("present"):
        findings.append(finding("info", "No Guided mode segment was identified.", result["guided_segments"]))
    if result["failsafe_context"].get("issues_detected"):
        findings.append(finding("critical", "Failsafe/error context appears in the log.", result["failsafe_context"], "do_not_proceed"))
    if result["gps_ekf_confidence"].get("confidence") != "good":
        findings.append(finding("critical", "GPS/EKF confidence is not good enough for Guided operation review.", result["gps_ekf_confidence"], "do_not_proceed"))
    if result["tracking_quality"].get("quality") == "poor":
        findings.append(finding("warning", "Guided tracking quality has high-error evidence.", result["tracking_quality"]))
    elif result["tracking_quality"].get("quality") == "good":
        findings.append({"severity": "info", "finding": "Guided tracking evidence did not cross conservative blocker thresholds.", "evidence": result["tracking_quality"]})
    if result["manual_intervention_context"].get("pilot_intervention_likely"):
        findings.append(finding("warning", "RC override or pilot intervention may contaminate Guided response evidence.", result["manual_intervention_context"]))
    if vibration_severe(result["confounders"].get("vibration") or {}):
        findings.append(finding("critical", "Severe vibration or clipping blocks Guided evidence review.", result["confounders"].get("vibration"), "do_not_proceed"))
    return findings


def classify_result(result: dict[str, Any]) -> tuple[str, str]:
    if "Missing required message: MODE" in result.get("missing_evidence", []):
        return "inconclusive", "repeat_step"
    if not result["guided_segments"].get("present"):
        return "not_applicable", "proceed_with_caution"
    if result["failsafe_context"].get("issues_detected"):
        return "not_ready", "do_not_proceed"
    if result["gps_ekf_confidence"].get("confidence") != "good":
        return "not_ready", "do_not_proceed"
    if vibration_severe(result["confounders"].get("vibration") or {}):
        return "not_ready", "do_not_proceed"
    if result["tracking_quality"].get("quality") == "poor":
        return "not_ready", "repeat_step"
    if not result["tracking_quality"].get("available"):
        return "inconclusive", "repeat_step"
    return "ready_for_guided_checks", "proceed_with_caution"


def recommended_next_steps(result: dict[str, Any]) -> list[str]:
    if result["result"] == "ready_for_guided_checks":
        return [
            "Inspect Guided segment, tracking, failsafe, RC, GPS/EKF, power, and vibration plots before using this as Methodic 12.2 evidence.",
            "Treat this as readiness for further Guided checks only; verify companion command source, geofence, GCS/RC failsafes, battery failsafe, and manual recovery outside the log.",
            "Proceed to 12.3 only if precision-landing hardware and use case are in scope.",
        ]
    if result["result"] == "not_ready":
        return [
            "Do not continue Guided operation checks until GPS/EKF, failsafe/error, tracking, vibration, and power blockers are resolved.",
            "Review companion/GCS command source and pilot override behaviour using a controlled ground or restrained test where possible.",
            "Repeat 12.2 only after the vehicle has clean estimator, power, vibration, and position-control evidence.",
        ]
    if result["result"] == "not_applicable":
        return [
            "No Guided segment was found; treat Methodic 12.2 as not applicable unless the vehicle actually uses Guided or companion commands.",
            "If Guided operation is required, collect a controlled Guided check log with MODE, GPS/GPA, XKF*/NKF*, ATT/RATE, CTUN, RCIN, MSG/EV/ERR, BAT/POWR, VIBE, PARM, and command-context evidence.",
            "Do not infer Guided readiness from Loiter/Auto/manual flight alone.",
        ]
    return [
        "Collect a better Guided-operation evidence log with MODE, GPS/GPA, XKF*/NKF*, ATT/RATE, CTUN, RCIN, MSG/EV/ERR, BAT/POWR, VIBE, PARM, and relevant companion/MAVLink command context.",
        "Keep the test conservative and ensure manual recovery and failsafe behaviour have been verified before any flight test.",
        "Do not certify Guided operation from incomplete logs.",
    ]


def what_not_to_do() -> list[str]:
    return [
        "Do not certify Guided operation as safe from a log review.",
        "Do not proceed with Guided checks when GPS/EKF, failsafe, vibration, power, or tracking blockers appear.",
        "Do not treat missing companion-command messages as proof that commands were absent.",
        "Do not disable failsafes, geofence, GPS/EKF checks, or RC/GCS recovery protections to make Guided operation pass.",
    ]


def checked_but_not_supported(tables: dict[str, Any]) -> list[str]:
    checked = []
    if not any(name in tables for name in ("CMD", "MAV", "MAVC")):
        checked.append("Direct companion/MAVLink command messages were not available; command source/timing must be checked externally or from MSG/GCS evidence.")
    if not any(name in tables for name in ("POS", "NTUN", "PSC")):
        checked.append("Desired-vs-actual Guided position/velocity tracking could not be checked with POS/NTUN/PSC fields.")
    if "EV" not in tables or "ERR" not in tables:
        checked.append("Event/error timeline may be incomplete because EV or ERR messages are missing.")
    return checked


def confidence_limits(result: dict[str, Any]) -> list[str]:
    limits = list(result.get("missing_evidence") or [])
    if result["result"] == "ready_for_guided_checks":
        limits.append("Ready-for-checks does not certify operational readiness or BVLOS readiness.")
    if not result["companion_command_context"].get("available"):
        limits.append("Companion/GCS command source and failsafe design require external review.")
    return limits


def make_plots(tables: dict[str, Any], plots_dir: Path) -> list[str]:
    ensure_dir(plots_dir)
    paths = []
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except Exception:
        return paths
    mode = tables.get("MODE")
    if mode is not None:
        fig = go.Figure()
        x = time_values(mode)
        y = [first_number(row, ("Mode", "ModeNum")) or i for i, (_, row) in enumerate(mode.iterrows())]
        labels = [mode_text(row, mode.columns) for _, row in mode.iterrows()]
        fig.add_trace(go.Scatter(x=x, y=y, mode="markers+lines", text=labels, name="MODE"))
        fig.update_layout(title="Methodic 12.2 mode timeline")
        path = plots_dir / "methodic_12_2_mode_timeline.html"
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
        fig.update_layout(title="Methodic 12.2 position/velocity tracking")
        path = plots_dir / "methodic_12_2_position_velocity.html"
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
        fig.update_layout(title="Methodic 12.2 CTUN altitude/throttle")
        path = plots_dir / "methodic_12_2_ctun_altitude.html"
        fig.write_html(path)
        paths.append(str(path))
    for name, fields, filename in (
        ("ATT", ("DesRoll", "Roll", "DesPitch", "Pitch", "DesYaw", "Yaw"), "methodic_12_2_attitude.html"),
        ("RATE", ("RDes", "R", "PDes", "P", "YDes", "Y", "ROut", "POut", "YOut"), "methodic_12_2_rate.html"),
        ("GPS", ("Status", "NSats", "HDop", "HAcc", "Spd", "Alt"), "methodic_12_2_gps.html"),
        ("VIBE", ("VibeX", "VibeY", "VibeZ", "Clip0", "Clip1", "Clip2"), "methodic_12_2_vibe.html"),
    ):
        df = first_table(tables, [name, "GPS2"] if name == "GPS" else [name])
        if df is None:
            continue
        fig = go.Figure()
        x = time_values(df)
        for field in fields:
            vals = series_values(df, field)
            if vals:
                fig.add_trace(go.Scatter(x=x[: len(vals)], y=vals, name=f"{name}.{field}"))
        fig.update_layout(title=f"Methodic 12.2 {name} context")
        path = plots_dir / filename
        fig.write_html(path)
        paths.append(str(path))
    if any(name in tables for name in ("MSG", "ERR", "EV")):
        fig = go.Figure()
        y = 0
        for name in ("MSG", "ERR", "EV"):
            df = tables.get(name)
            if df is None:
                continue
            xs = time_values(df)
            fig.add_trace(go.Scatter(x=xs, y=[y] * len(xs), mode="markers", name=name))
            y += 1
        fig.update_layout(title="Methodic 12.2 MSG/ERR/EV timeline")
        path = plots_dir / "methodic_12_2_msg_err_timeline.html"
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
        f"- Guided present: `{result['guided_segments'].get('present')}`",
        f"- Tracking quality: `{result['tracking_quality'].get('quality')}`",
        f"- GPS/EKF confidence: `{result['gps_ekf_confidence'].get('confidence')}`",
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


def altitude_tracking(tables: dict[str, Any]) -> dict[str, Any]:
    ctun = tables.get("CTUN")
    if ctun is None:
        return {"available": False}
    err = paired_error(ctun, "DAlt", "Alt", wrap=False)
    return {"available": bool(err), "altitude_error": summarize_error(err), "throttle_output": summarize_values(series_values(ctun, "ThO"))}


def attitude_tracking(tables: dict[str, Any]) -> dict[str, Any]:
    att = tables.get("ATT")
    if att is None:
        return {"available": False}
    errors = {}
    for axis, fields in {"roll": ("DesRoll", "Roll"), "pitch": ("DesPitch", "Pitch"), "yaw": ("DesYaw", "Yaw")}.items():
        errors[axis] = summarize_error(paired_error(att, fields[0], fields[1], wrap=(axis == "yaw")))
    return {"available": any(v.get("available") for v in errors.values()), "attitude_errors": errors}


def vibration_severe(vibe: dict[str, Any]) -> bool:
    clips = vibe.get("clip_delta") or {}
    return any((safe_float(v) or 0.0) > 0 for v in clips.values()) or (safe_float(vibe.get("p95_axis")) or 0.0) > 30.0 or (safe_float(vibe.get("max_axis")) or 0.0) > 45.0


def mode_text(row: Any, columns: Any) -> str:
    parts = []
    for col in columns:
        if str(col).lower() in {"mode", "modename", "name", "astext"}:
            value = row.get(col, "")
            if isinstance(value, str):
                parts.append(value)
    return " ".join(parts).strip()


def first_number(row: Any, fields: tuple[str, ...]) -> float | None:
    for field in fields:
        if field in row:
            value = safe_float(row.get(field))
            if value is not None and math.isfinite(value):
                return value
    return None


def row_time(row: Any) -> float | None:
    for col, scale in (("TimeS", 1.0), ("Time", 1.0), ("TimeUS", 1e-6), ("TimeMS", 1e-3)):
        if col in row:
            value = safe_float(row.get(col))
            if value is not None:
                return value * scale
    return None


def mode_reason(reason: int | None) -> str | None:
    if reason is None:
        return None
    return {0: "UNKNOWN", 1: "RC_COMMAND", 2: "GCS_COMMAND", **FAILSAFE_MODE_REASONS}.get(reason, str(reason))


def log_window(tables: dict[str, Any]) -> dict[str, Any]:
    times = []
    for df in tables.values():
        times.extend(time_values(df))
    if not times:
        return {"selection": "none", "start_s": None, "end_s": None}
    return {"selection": "guided_mode_segments", "start_s": min(times), "end_s": max(times)}


def finding(severity: str, text: str, evidence: Any, gate: str | None = None) -> dict[str, Any]:
    out = {"severity": severity, "finding": text, "evidence": evidence}
    if gate:
        out["safety_gate"] = gate
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Review Methodic 12.2 Guided-operation evidence without certifying operational readiness.")
    parser.add_argument("log", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--plots", type=Path)
    args = parser.parse_args()
    result = analyze_guided_operation_review(args.log, plots_dir=args.plots)
    write_json(args.out, result)
    if args.summary:
        write_summary(args.summary, result)
    else:
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
