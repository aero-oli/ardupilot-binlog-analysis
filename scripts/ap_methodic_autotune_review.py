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
from ap_methodic_tune_eval import analyze_axis, analyze_pid, analyze_power, percentile, series_values, time_values

METHODIC_95_URL = "https://ardupilot.github.io/MethodicConfigurator/TUNING_GUIDE_ArduCopter#95-autotune-flights"
AUTOTUNE_URL = "https://ardupilot.org/copter/docs/autotune.html"
LOG_MESSAGES_URL = "https://ardupilot.org/copter/docs/logmessages.html"
MESSAGES = [
    "ATUN",
    "ATDE",
    "ATT",
    "RATE",
    "PIDR",
    "PIDP",
    "PIDY",
    "RCOU",
    "RCO2",
    "RCO3",
    "VIBE",
    "BAT",
    "POWR",
    "MODE",
    "MSG",
    "EV",
    "ERR",
    "PARM",
    "ARM",
]
RELEVANT_PARAMETERS = [
    "AUTOTUNE_*",
    "AUTOTUNE_MIN_D",
    "ATC_RAT_*",
    "ATC_ANG_*",
    "ATC_ACCEL_*",
    "INS_HNTCH_*",
    "INS_GYRO_FILTER",
]
AXES = {
    "roll": {"pid": "PIDR", "rate_des": "RDes", "rate": "R", "out": "ROut", "att_des": "DesRoll", "att": "Roll"},
    "pitch": {"pid": "PIDP", "rate_des": "PDes", "rate": "P", "out": "POut", "att_des": "DesPitch", "att": "Pitch"},
    "yaw": {"pid": "PIDY", "rate_des": "YDes", "rate": "Y", "out": "YOut", "att_des": "DesYaw", "att": "Yaw"},
}


def rows_to_tables(rows_by_message: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    try:
        import pandas as pd
    except Exception as exc:
        raise AnalysisError("pandas is required for Methodic AutoTune review. Install dependencies with: pip install -r requirements.txt") from exc
    return {name: pd.DataFrame(rows) for name, rows in rows_by_message.items() if rows}


def analyze_autotune_review(log_path: str | Path, *, plots_dir: str | Path | None = None) -> dict[str, Any]:
    rows, index, stats = collect_dataflash(log_path, include=MESSAGES, source=str(log_path))
    tables = rows_to_tables(rows)
    params = index.get("parameters", {}) or {}

    result = empty_result(params)
    result["analysis_window"]["parser_stats"] = stats
    result["analysis_window"].update(log_window(tables))
    result["evidence_used"].append({"type": "messages_present", "messages": sorted(tables.keys())})
    result["missing_evidence"] = missing_evidence(tables)

    atune = analyze_atun(tables)
    messages = analyze_messages(tables)
    mode_sequence = analyze_modes(tables)
    parameters_changed = analyze_parameter_changes(tables)
    preconditions = analyze_preconditions(tables, params)
    post = analyze_post_tune(tables)
    poor = analyze_poor_solution_indicators(tables, params, parameters_changed, post)

    result["autotune_detected"] = atune["detected"]
    result["axis"] = atune["axis"]
    result["completion"] = completion_status(atune, messages)
    result["saved"] = saved_status(messages)
    result["parameters_changed"] = parameters_changed
    result["poor_solution_indicators"] = poor
    result["post_tune_evaluation"] = post
    result["preconditions"] = preconditions
    result["mode_sequence"] = mode_sequence
    result["evidence_used"].extend([
        {"type": "atun", "value": atune},
        {"type": "atde", "value": analyze_atde(tables)},
        {"type": "messages", "value": messages},
        {"type": "mode_sequence", "value": mode_sequence},
        {"type": "parameters_changed", "value": parameters_changed},
        {"type": "preconditions", "value": preconditions},
        {"type": "post_tune_evaluation", "value": post},
    ])

    result["findings"] = classify_findings(atune, result["completion"], result["saved"], parameters_changed, preconditions, post, poor, result["missing_evidence"])
    result["checked_but_not_supported"] = checked_but_not_supported(tables, atune, parameters_changed, preconditions, post)
    result["result"], result["safety_gate"] = classify_result(result["findings"], atune)
    result["next_methodic_step"] = next_methodic_step(result["result"])
    result["recommended_next_steps"] = recommended_next_steps(result)
    result["what_not_to_do"] = what_not_to_do()
    result["confidence_limits"] = confidence_limits(result)
    if plots_dir:
        result["plots"] = make_plots(tables, Path(plots_dir))
    return result


def empty_result(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "methodic_step": "9.5",
        "title": "AutoTune sequence",
        "official_reference": {"url": METHODIC_95_URL, "supporting_urls": [AUTOTUNE_URL, LOG_MESSAGES_URL]},
        "autotune_detected": False,
        "axis": [],
        "completion": "unknown",
        "saved": "unknown",
        "parameters_changed": {"available": False, "changes": []},
        "poor_solution_indicators": [],
        "post_tune_evaluation": {},
        "preconditions": {},
        "mode_sequence": {},
        "result": "inconclusive",
        "safety_gate": "repeat_step",
        "evidence_used": [],
        "missing_evidence": [],
        "manual_observations_required": ["Calm practical environment", "Pilot maintained safe position/height", "No instability during or after AutoTune", "No excessive twitch response after AutoTune"],
        "analysis_window": {"selection": "whole_log", "preferred_window": "AutoTune active mode segments and post-AutoTune evaluation context.", "start_s": None, "end_s": None},
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


def missing_evidence(tables: dict[str, Any]) -> list[str]:
    missing = []
    if "ATUN" not in tables:
        missing.append("Missing required message: ATUN")
    for name in ("ATT", "RATE", "PIDR", "PIDP", "PIDY", "RCOU", "VIBE", "BAT", "MODE", "MSG", "PARM"):
        if name not in tables:
            missing.append(f"Missing strongly recommended message: {name}")
    if "ATDE" not in tables:
        missing.append("Missing optional message: ATDE")
    return missing


def log_window(tables: dict[str, Any]) -> dict[str, Any]:
    times = []
    for df in tables.values():
        times.extend(time_values(df))
    if not times:
        return {"selection": "none", "start_s": None, "end_s": None}
    return {"selection": "whole_log", "start_s": float(min(times)), "end_s": float(max(times))}


def analyze_atun(tables: dict[str, Any]) -> dict[str, Any]:
    atun = tables.get("ATUN")
    if atun is None or len(atun) == 0:
        return {"detected": False, "rows": 0, "axis": [], "steps": [], "last_rows": []}
    axes = sorted({axis_label(v) for v in column_values(atun, ["Axis", "axis"]) if axis_label(v)})
    steps = sorted({str(v) for v in column_values(atun, ["TuneStep", "Step", "Stp", "State"]) if str(v) != "nan"})
    gain_fields = {}
    for col in ("RP", "RD", "RFF", "SP", "SD", "YAW_P", "YAW_D", "YP", "YD", "RPGain", "RDGain", "SPGain", "SDGain"):
        vals = series_values(atun, col)
        if vals:
            gain_fields[col] = summarize_values(vals)
    return {
        "detected": True,
        "rows": int(len(atun)),
        "axis": axes,
        "steps": steps,
        "time_window": table_window(atun),
        "gain_fields": gain_fields,
        "last_rows": safe_records(atun.tail(20)),
    }


def analyze_atde(tables: dict[str, Any]) -> dict[str, Any]:
    atde = tables.get("ATDE")
    if atde is None or len(atde) == 0:
        return {"available": False}
    return {"available": True, "rows": int(len(atde)), "time_window": table_window(atde), "last_rows": safe_records(atde.tail(20))}


def analyze_messages(tables: dict[str, Any]) -> dict[str, Any]:
    out = {"autotune_messages": [], "events": [], "errors": []}
    msg = tables.get("MSG")
    if msg is not None:
        for row in msg.to_dict(orient="records"):
            text = str(row.get("Message") or row.get("Msg") or "").strip()
            if "tune" in text.lower() or "atun" in text.lower():
                out["autotune_messages"].append({"time_s": safe_float(row.get("TimeS")), "message": text})
    for name, key in (("EV", "events"), ("ERR", "errors")):
        df = tables.get(name)
        if df is not None:
            out[key] = safe_records(df.tail(100))
    return out


def analyze_modes(tables: dict[str, Any]) -> dict[str, Any]:
    mode = tables.get("MODE")
    if mode is None or len(mode) == 0:
        return {"available": False, "autotune_mode_seen": False, "sequence": []}
    rows = []
    autotune_seen = False
    for row in mode.to_dict(orient="records"):
        label = str(row.get("Mode") or row.get("Name") or row.get("ModeNum") or "")
        if "auto" in label.lower() and "tune" in label.lower():
            autotune_seen = True
        rows.append({"time_s": safe_float(row.get("TimeS")), "mode": label})
    return {"available": True, "autotune_mode_seen": autotune_seen, "sequence": rows[:200]}


def analyze_parameter_changes(tables: dict[str, Any]) -> dict[str, Any]:
    parm = tables.get("PARM")
    if parm is None or len(parm) == 0:
        return {"available": False, "changes": []}
    history: dict[str, list[dict[str, Any]]] = {}
    for row in parm.to_dict(orient="records"):
        name = str(row.get("Name") or "").strip()
        value = safe_float(row.get("Value"))
        if name and value is not None and relevant_param(name):
            history.setdefault(name, []).append({"time_s": safe_float(row.get("TimeS")), "value": value})
    changes = []
    for name, samples in history.items():
        if len(samples) >= 2 and abs(samples[-1]["value"] - samples[0]["value"]) > 1e-9:
            changes.append({"name": name, "before": samples[0]["value"], "after": samples[-1]["value"], "delta": samples[-1]["value"] - samples[0]["value"], "time_s": samples[-1]["time_s"]})
    return {"available": bool(history), "changes": changes, "history": history}


def relevant_param(name: str) -> bool:
    return name.startswith(("ATC_RAT_", "ATC_ANG_", "ATC_ACCEL_", "AUTOTUNE_"))


def analyze_preconditions(tables: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    vibration = analyze_vibration(tables, None)
    motor = analyze_motor_outputs(tables, None, params)
    power = analyze_power(tables)
    notch = {
        "ins_hntch_enable": params.get("INS_HNTCH_ENABLE"),
        "ins_hntch_mode": params.get("INS_HNTCH_MODE"),
        "ins_hntch_freq": params.get("INS_HNTCH_FREQ"),
        "ins_gyro_filter": params.get("INS_GYRO_FILTER"),
        "context": "Parameter context only; this tool does not prove notch/filter review was completed.",
    }
    stable_tracking = analyze_post_tune(tables)
    return {
        "notch_filter_context": notch,
        "stable_hover_proxy": stable_hover_proxy(tables),
        "vibration": vibration,
        "motor_outputs": motor,
        "power": power,
        "initial_control_stability_proxy": stable_tracking,
    }


def stable_hover_proxy(tables: dict[str, Any]) -> dict[str, Any]:
    att = tables.get("ATT")
    if att is None or len(att) == 0:
        return {"available": False, "reason": "ATT missing"}
    roll = [abs(v) for v in series_values(att, "Roll")]
    pitch = [abs(v) for v in series_values(att, "Pitch")]
    return {
        "available": bool(roll or pitch),
        "roll_abs_p95_deg": percentile(roll, 95),
        "pitch_abs_p95_deg": percentile(pitch, 95),
        "roughly_stable_attitude": (percentile(roll, 95) or 999.0) < 25.0 and (percentile(pitch, 95) or 999.0) < 25.0,
        "caveat": "This is a rough log proxy, not proof that Methodic pre-AutoTune prerequisites were satisfied.",
    }


def analyze_post_tune(tables: dict[str, Any]) -> dict[str, Any]:
    rate = tables.get("RATE")
    att = tables.get("ATT")
    axes = {
        axis: analyze_axis(axis, rate, att, tables.get(spec["pid"]))
        for axis, spec in AXES.items()
    }
    pid = analyze_pid(tables)
    outputs = {}
    for axis, data in axes.items():
        output = data.get("controller_output") or {}
        tracking = data.get("rate_tracking") or {}
        outputs[axis] = {
            "output_p95_abs": output.get("p95_abs"),
            "output_max_abs": output.get("max_abs"),
            "output_classification": output.get("classification"),
            "rate_tracking_p95_abs": tracking.get("p95_abs"),
        }
    return {"available": rate is not None and len(rate) > 0, "axis_results": axes, "pid": pid, "summary": outputs}


def analyze_poor_solution_indicators(tables: dict[str, Any], params: dict[str, Any], changes: dict[str, Any], post: dict[str, Any]) -> list[dict[str, Any]]:
    indicators: list[dict[str, Any]] = []
    final_params = final_param_values(params, changes)
    for axis, prefix in (("roll", "ATC_ANG_RLL_P"), ("pitch", "ATC_ANG_PIT_P"), ("yaw", "ATC_ANG_YAW_P")):
        value = safe_float(final_params.get(prefix))
        if value is not None and value < 3.0:
            indicators.append({"indicator": "low_angle_p", "axis": axis, "parameter": prefix, "value": value, "interpretation": "Low angle P can indicate a weak or poor AutoTune solution that needs manual review."})
    min_d = safe_float(final_params.get("AUTOTUNE_MIN_D"), 0.001)
    for axis, name in (("roll", "ATC_RAT_RLL_D"), ("pitch", "ATC_RAT_PIT_D"), ("yaw", "ATC_RAT_YAW_D")):
        value = safe_float(final_params.get(name))
        if value is not None and min_d is not None and value <= max(min_d * 1.15, min_d + 0.0002):
            indicators.append({"indicator": "d_at_minimum", "axis": axis, "parameter": name, "value": value, "minimum_reference": min_d, "interpretation": "D-term at or near the configured minimum can indicate AutoTune could not find useful D damping."})
    yaw_d = safe_float(final_params.get("ATC_RAT_YAW_D"))
    if yaw_d is not None and yaw_d < 0.0015:
        indicators.append({"indicator": "yaw_d_not_meaningful", "axis": "yaw", "parameter": "ATC_RAT_YAW_D", "value": yaw_d, "interpretation": "Yaw D is not meaningfully above a very small value; review whether yaw D tuning was actually useful."})
    for axis, data in (post.get("summary") or {}).items():
        if data.get("output_classification") in {"oscillatory", "mixed"} and (data.get("output_p95_abs") or 0.0) > 0.15:
            indicators.append({"indicator": "possible_over_aggressive_result", "axis": axis, "evidence": data, "interpretation": "Post-AutoTune output looks oscillatory/high; do not blindly accept the result."})
        if data.get("output_max_abs") is not None and data["output_max_abs"] > 0.45:
            indicators.append({"indicator": "excessive_twitch_response_proxy", "axis": axis, "evidence": data, "interpretation": "Large RATE output peaks can reflect excessive twitch response or maneuver demand; inspect plots before accepting gains."})
    return indicators


def final_param_values(params: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    out = dict(params)
    for change in changes.get("changes") or []:
        out[change["name"]] = change.get("after")
    return out


def completion_status(atune: dict[str, Any], messages: dict[str, Any]) -> str:
    if not atune.get("detected"):
        return "not_detected"
    text = " ".join(m.get("message", "") for m in messages.get("autotune_messages") or []).lower()
    if any(token in text for token in ("failed", "fail", "aborted", "abort", "cancelled", "canceled")):
        return "failed"
    if any(token in text for token in ("complete", "completed", "success", "successful", "done")):
        return "completed"
    if atune.get("rows", 0) >= 5:
        return "partial"
    return "unknown"


def saved_status(messages: dict[str, Any]) -> str:
    text = " ".join(m.get("message", "") for m in messages.get("autotune_messages") or []).lower()
    if any(token in text for token in ("saved", "save gains", "gains saved")):
        return "saved"
    if any(token in text for token in ("not saved", "discard", "discarded", "cancelled", "canceled")):
        return "discarded"
    return "unknown"


def classify_findings(atune: dict[str, Any], completion: str, saved: str, changes: dict[str, Any], preconditions: dict[str, Any], post: dict[str, Any], poor: list[dict[str, Any]], missing: list[str]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if not atune.get("detected"):
        findings.append(finding("inconclusive", "ATUN is missing; AutoTune cannot be reviewed from this log.", {"missing": missing}))
        return findings
    if completion == "completed":
        findings.append({"severity": "info", "finding": "AutoTune completion was detected.", "evidence": {"completion": completion}})
    elif completion in {"failed", "not_detected"}:
        findings.append(finding("fail", "AutoTune failed or was not detected as a valid sequence.", {"completion": completion}, "do_not_proceed"))
    else:
        findings.append(finding("conditional", "AutoTune appears partial or completion is unknown.", {"completion": completion}))
    if saved == "discarded":
        findings.append(finding("fail", "AutoTune gains appear discarded or not saved.", {"saved": saved}, "do_not_proceed"))
    elif saved == "unknown":
        findings.append(finding("conditional", "AutoTune save/discard status is unknown.", {"saved": saved}))
    if not changes.get("changes"):
        findings.append(finding("conditional", "No logged AutoTune-relevant parameter changes were detected.", changes))

    vibration = preconditions.get("vibration") or {}
    if vibration.get("available"):
        clips = vibration.get("clip_delta") or {}
        if any(v > 0 for v in clips.values()) or (vibration.get("p95_axis") is not None and vibration["p95_axis"] > 30.0) or (vibration.get("max_axis") is not None and vibration["max_axis"] > 45.0):
            findings.append(finding("fail", "Severe vibration or clipping invalidates AutoTune acceptance.", vibration, "do_not_proceed"))
        elif vibration.get("p95_axis") is not None and vibration["p95_axis"] > 20.0:
            findings.append(finding("conditional", "Vibration is in a grey zone for AutoTune acceptance.", vibration))
    else:
        findings.append(finding("conditional", "VIBE is missing; vibration prerequisite cannot be confirmed.", vibration))

    motor = preconditions.get("motor_outputs") or {}
    if motor.get("available"):
        saturated = [
            name for name, data in (motor.get("channels") or {}).items()
            if data.get("pct_high_ge_1900", 0.0) > 1.0 or data.get("pct_low_le_1100", 0.0) > 1.0 or data.get("persistent_high") or data.get("persistent_low")
        ]
        if saturated:
            findings.append(finding("fail", "Motor output saturation/headroom issue appears during AutoTune review.", {"channels": saturated[:12]}, "do_not_proceed"))
    else:
        findings.append(finding("conditional", "Motor outputs are missing; AutoTune actuator headroom cannot be confirmed.", motor))

    stable = preconditions.get("stable_hover_proxy") or {}
    if stable.get("available") and not stable.get("roughly_stable_attitude"):
        findings.append(finding("fail", "Attitude stability proxy is poor; AutoTune prerequisites are not supported.", stable, "do_not_proceed"))

    for indicator in poor:
        severity = "fail" if indicator.get("indicator") in {"possible_over_aggressive_result", "excessive_twitch_response_proxy"} else "conditional"
        findings.append(finding(severity, f"Poor AutoTune solution indicator detected: {indicator.get('indicator')}.", indicator, "do_not_proceed" if severity == "fail" else None))
    return findings


def classify_result(findings: list[dict[str, Any]], atune: dict[str, Any]) -> tuple[str, str]:
    severities = {item.get("severity") for item in findings}
    if not atune.get("detected") or "inconclusive" in severities:
        return "inconclusive", "repeat_step"
    if "fail" in severities:
        return "fail", "do_not_proceed"
    if "conditional" in severities:
        return "conditional_pass", "proceed_with_caution"
    return "pass", "proceed_with_caution"


def checked_but_not_supported(tables: dict[str, Any], atune: dict[str, Any], changes: dict[str, Any], preconditions: dict[str, Any], post: dict[str, Any]) -> list[str]:
    checked = []
    if atune.get("detected"):
        checked.append("ATUN AutoTune progression was checked.")
    if "ATDE" in tables:
        checked.append("ATDE AutoTune details were checked.")
    if changes.get("available"):
        checked.append("PARM history was checked for AutoTune-relevant parameter changes.")
    if (preconditions.get("vibration") or {}).get("available"):
        checked.append("VIBE/clipping prerequisite was checked.")
    if (preconditions.get("motor_outputs") or {}).get("available"):
        checked.append("Motor output saturation/headroom prerequisite was checked.")
    if post.get("available"):
        checked.append("Post-AutoTune ATT/RATE/PID context was checked.")
    return checked


def recommended_next_steps(result: dict[str, Any]) -> list[str]:
    if result["result"] == "pass":
        return [
            "Agent should inspect ATUN, parameter-change, ATT/RATE/PID, vibration, battery, and motor-output plots before accepting the AutoTune review.",
            "If the evidence remains consistent, continue to Methodic 9.6 performance evaluation; do not describe the aircraft as safe from this result.",
        ]
    if result["result"] == "conditional_pass":
        return [
            "Resolve unknown save status, partial-axis coverage, missing parameter-change evidence, or prerequisite caveats before treating AutoTune as complete.",
            "Repeat only the incomplete or suspect AutoTune portion after the cause is understood and prerequisites are still satisfied.",
        ]
    if result["result"] == "fail":
        return [
            "Do not accept or build on this AutoTune result.",
            "Address the listed cause first: vibration/clipping, saturation, unstable control, failed/discarded AutoTune, or over-aggressive output evidence.",
            "Use controlled evaluation and possible reduction/revert review rather than blind acceptance of generated gains.",
        ]
    return [
        "Collect a readable AutoTune log with ATUN, ATT, RATE, PIDR/PIDP/PIDY, RCOU/RCO2/RCO3, VIBE, BAT, MODE, MSG/EV/ERR, and PARM evidence.",
        "Do not recommend AutoTune until initial tune, filters, vibration, actuator headroom, and control stability evidence are acceptable.",
    ]


def what_not_to_do() -> list[str]:
    return [
        "Do not automatically apply or upload AutoTune gains.",
        "Do not recommend AutoTune unless initial tune, filters, vibration, actuator headroom, and control stability are acceptable.",
        "Do not tell the user to fly aggressive AutoTune if the vehicle is not already stable.",
        "Do not blindly accept partial, failed, discarded, over-aggressive, or poor-solution AutoTune outputs.",
    ]


def next_methodic_step(result: str) -> str | None:
    if result == "pass":
        return "9.6"
    if result == "conditional_pass":
        return "Repeat incomplete AutoTune axis only after safety review"
    if result == "fail":
        return "Do not use poor or unstable AutoTune results; repeat setup after diagnosing cause"
    return "repeat 9.5"


def confidence_limits(result: dict[str, Any]) -> list[str]:
    limits = []
    if result["missing_evidence"]:
        limits.append("Missing required or strongly recommended log messages limit AutoTune review confidence.")
    if result["saved"] == "unknown":
        limits.append("AutoTune save/discard status was not confirmed from messages.")
    if result["completion"] in {"partial", "unknown"}:
        limits.append("AutoTune completion is partial or unknown; agent must inspect ATUN/ATDE details.")
    limits.append("This tool reviews evidence only; final Methodic conclusion requires agent inspection and manual observations.")
    return limits


def make_plots(tables: dict[str, Any], plots_dir: Path) -> list[str]:
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except Exception:
        return []
    out = ensure_dir(plots_dir)
    plots: list[str] = []
    atun = tables.get("ATUN")
    if atun is not None and "TimeS" in atun.columns:
        fig = make_subplots(rows=3, cols=1, shared_xaxes=True, subplot_titles=("Target / min / max", "Gains", "Axis / step"))
        for row_idx, cols in enumerate([("Targ", "Target", "Min", "Max"), ("RP", "RD", "SP", "SD", "YP", "YD", "YAW_P", "YAW_D"), ("Axis", "TuneStep", "Step")], start=1):
            for col in cols:
                if col in atun.columns:
                    fig.add_trace(go.Scatter(x=atun["TimeS"], y=numeric_series(atun, [col]), mode="lines", name=f"ATUN.{col}"), row=row_idx, col=1)
        fig.update_layout(title="Methodic 9.5 AutoTune progression", template="plotly_white", hovermode="x unified")
        path = out / "methodic_9_5_atun_progression.html"
        fig.write_html(str(path), include_plotlyjs="cdn")
        plots.append(str(path))
    for group, title in (("RATE", "rate/attitude tracking and outputs"), ("PID", "PID terms / flags / Dmod"), ("RCOU", "motor outputs"), ("VIBE", "vibration"), ("BAT", "battery")):
        path = plot_group(tables, group, title, out)
        if path:
            plots.append(path)
    return plots


def plot_group(tables: dict[str, Any], group_name: str, title: str, out: Path) -> str | None:
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except Exception:
        return None
    if group_name == "RATE":
        rate = tables.get("RATE")
        att = tables.get("ATT")
        if rate is None and att is None:
            return None
        fig = make_subplots(rows=3, cols=1, shared_xaxes=True, subplot_titles=("Roll", "Pitch", "Yaw"))
        for row_idx, cols in enumerate([("RDes", "R", "ROut", "DesRoll", "Roll"), ("PDes", "P", "POut", "DesPitch", "Pitch"), ("YDes", "Y", "YOut", "DesYaw", "Yaw")], start=1):
            for col in cols:
                df = rate if col in getattr(rate, "columns", []) else att
                if df is not None and col in df.columns and "TimeS" in df.columns:
                    fig.add_trace(go.Scatter(x=df["TimeS"], y=numeric_series(df, [col]), mode="lines", name=col), row=row_idx, col=1)
        fig.update_layout(title=f"Methodic 9.5 AutoTune {title}", template="plotly_white", hovermode="x unified")
        path = out / "methodic_9_5_rate_att_tracking.html"
        fig.write_html(str(path), include_plotlyjs="cdn")
        return str(path)

    names = ["PIDR", "PIDP", "PIDY"] if group_name == "PID" else [group_name]
    fig = go.Figure()
    found = False
    for name in names:
        df = tables.get(name)
        if df is None or "TimeS" not in getattr(df, "columns", []):
            continue
        if group_name == "PID":
            cols = [c for c in ("P", "I", "D", "FF", "DFF", "Dmod", "Flags") if c in df.columns]
        elif group_name == "RCOU":
            cols = [c for c in df.columns if str(c).startswith("C") and str(c)[1:].isdigit()]
        elif group_name == "VIBE":
            cols = [c for c in ["VibeX", "VibeY", "VibeZ", *clip_columns(df)] if c in df.columns]
        elif group_name == "BAT":
            cols = [c for c in ("Volt", "VoltR", "Curr") if c in df.columns]
        else:
            cols = []
        for col in cols:
            fig.add_trace(go.Scatter(x=df["TimeS"], y=numeric_series(df, [col]), mode="lines", name=f"{name}.{col}"))
            found = True
    if not found:
        return None
    fig.update_layout(title=f"Methodic 9.5 AutoTune {title}", template="plotly_white", hovermode="x unified")
    path = out / f"methodic_9_5_{group_name.lower()}.html"
    fig.write_html(str(path), include_plotlyjs="cdn")
    return str(path)


def write_summary(path: Path, result: dict[str, Any]) -> None:
    lines = [
        "# Methodic 9.5 AutoTune Review",
        "",
        f"- Result: `{result['result']}`",
        f"- Safety gate: `{result['safety_gate']}`",
        f"- AutoTune detected: `{result['autotune_detected']}`",
        f"- Completion: `{result['completion']}`",
        f"- Saved: `{result['saved']}`",
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


def column_values(df: Any, names: list[str]) -> list[Any]:
    for name in names:
        if df is not None and name in getattr(df, "columns", []):
            return df[name].dropna().tolist()
    return []


def axis_label(value: Any) -> str | None:
    text = str(value).strip().lower()
    if not text or text == "nan":
        return None
    mapping = {"0": "roll", "1": "pitch", "2": "yaw", "3": "yaw_d", "4": "roll_pitch_retune"}
    if text in mapping:
        return mapping[text]
    if "roll" in text and "pitch" in text:
        return "roll_pitch_retune"
    for axis in ("roll", "pitch", "yaw"):
        if axis in text:
            return axis
    return str(value)


def table_window(df: Any) -> dict[str, Any]:
    times = time_values(df)
    if not times:
        return {"start_s": None, "end_s": None}
    return {"start_s": float(min(times)), "end_s": float(max(times))}


def safe_records(df: Any) -> list[dict[str, Any]]:
    records = []
    for row in df.to_dict(orient="records"):
        clean = {}
        for key, value in row.items():
            if hasattr(value, "item"):
                try:
                    value = value.item()
                except Exception:
                    pass
            if isinstance(value, float) and math.isnan(value):
                value = None
            clean[str(key)] = value
        records.append(clean)
    return records


def finding(severity: str, text: str, evidence: Any, gate: str | None = None) -> dict[str, Any]:
    out = {"severity": severity, "finding": text, "evidence": evidence}
    if gate:
        out["safety_gate"] = gate
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Gather deterministic Methodic AutoTune review evidence for step 9.5.")
    parser.add_argument("log", help="ArduPilot DataFlash .BIN/.log file")
    parser.add_argument("--out", default=None, help="Write structured JSON result")
    parser.add_argument("--summary", default=None, help="Write Markdown summary")
    parser.add_argument("--plots", default=None, help="Directory for generated plots")
    args = parser.parse_args()
    try:
        result = analyze_autotune_review(args.log, plots_dir=args.plots)
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
