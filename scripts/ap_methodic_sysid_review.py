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

METHODIC_111_URL = "https://ardupilot.github.io/MethodicConfigurator/TUNING_GUIDE_ArduCopter#111-system-identification-flights"
SYSID_DOC_URL = "https://ardupilot.org/copter/docs/common-systemid-mode-operation.html"
LOG_MESSAGES_URL = "https://ardupilot.org/copter/docs/logmessages.html"
MESSAGES = [
    "SID",
    "SIDD",
    "SIDS",
    "RATE",
    "ATT",
    "RCOU",
    "RCO2",
    "RCO3",
    "VIBE",
    "BAT",
    "POWR",
    "MODE",
    "PARM",
    "MSG",
    "EV",
    "ERR",
    "ARM",
]
RELEVANT_PARAMETERS = [
    "SID_AXIS",
    "SID_MAGNITUDE",
    "SID_F_START_HZ",
    "SID_F_STOP_HZ",
    "SID_T_FADE_IN",
    "SID_T_REC",
    "SID_T_FADE_OUT",
    "ATC_RATE_FF_ENAB",
    "ATC_RAT_RLL_P",
    "ATC_RAT_PIT_P",
    "ATC_RAT_YAW_P",
    "ATC_RAT_RLL_D",
    "ATC_RAT_PIT_D",
    "ATC_RAT_YAW_D",
    "INS_HNTCH_*",
    "LOG_BITMASK",
]
AXIS_MAP = {
    1: ("roll", "input_roll_angle"),
    2: ("pitch", "input_pitch_angle"),
    3: ("yaw", "input_yaw_angle"),
    4: ("roll", "recovery_roll_angle"),
    5: ("pitch", "recovery_pitch_angle"),
    6: ("yaw", "recovery_yaw_angle"),
    7: ("roll", "rate_roll"),
    8: ("pitch", "rate_pitch"),
    9: ("yaw", "rate_yaw"),
    10: ("roll", "mixer_roll"),
    11: ("pitch", "mixer_pitch"),
    12: ("yaw", "mixer_yaw"),
    13: ("thrust", "mixer_thrust"),
}
RATE_AXIS_FIELDS = {
    "roll": ("R", "RDes", "ROut"),
    "pitch": ("P", "PDes", "POut"),
    "yaw": ("Y", "YDes", "YOut"),
    "thrust": (None, None, "AOut"),
}


def rows_to_tables(rows_by_message: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    try:
        import pandas as pd
    except Exception as exc:
        raise AnalysisError("pandas is required for Methodic System ID review. Install dependencies with: pip install -r requirements.txt") from exc
    return {name: pd.DataFrame(rows) for name, rows in rows_by_message.items() if rows}


def analyze_sysid_review(log_path: str | Path, *, plots_dir: str | Path | None = None) -> dict[str, Any]:
    rows, index, stats = collect_dataflash(log_path, include=MESSAGES, source=str(log_path))
    tables = rows_to_tables(rows)
    params = index.get("parameters", {}) or {}

    result = empty_result(params)
    result["analysis_window"]["parser_stats"] = stats
    result["analysis_window"].update(log_window(tables))
    result["missing_evidence"] = missing_evidence(tables)
    result["evidence_used"].append({"type": "messages_present", "messages": sorted(tables.keys())})

    mode_context = analyze_mode_context(tables)
    axis = detect_axis(tables, params)
    excitation = analyze_excitation(tables, axis)
    response = analyze_response(tables, axis, excitation)
    motor = analyze_motor_outputs(tables, None, params)
    vibration = analyze_vibration(tables, None)
    power = analyze_power(tables)
    logging = analyze_logging(index, stats, tables)
    pid_suppression = analyze_pid_suppression(axis, excitation, response)
    wind_context = analyze_wind_context(tables)
    saturation = saturation_context(motor)

    quality = classify_data_quality(excitation, response, saturation, vibration, logging, result["missing_evidence"], pid_suppression)
    result["axis"] = axis
    result["sysid_data_quality"] = quality
    result["frequency_response_ready"] = quality == "good"
    result["saturation"] = saturation
    result["excitation_quality"] = excitation
    result["response_quality"] = response
    result["pid_suppression"] = pid_suppression
    result["vibration_noise_quality"] = vibration
    result["power_context"] = power
    result["logging_health"] = logging
    result["mode_context"] = mode_context
    result["wind_context"] = wind_context
    result["evidence_used"].extend([
        {"type": "axis_detection", "value": axis},
        {"type": "mode_context", "value": mode_context},
        {"type": "sysid_excitation", "value": excitation},
        {"type": "rate_response", "value": response},
        {"type": "mapped_motor_outputs", "value": motor},
        {"type": "vibration", "value": vibration},
        {"type": "power", "value": power},
        {"type": "logging_health", "value": logging},
        {"type": "wind_context_weak", "value": wind_context},
    ])
    result["findings"] = findings(result)
    result["checked_but_not_supported"] = checked_but_not_supported(tables, result)
    result["result"], result["safety_gate"] = classify_result(result)
    result["recommended_next_steps"] = recommended_next_steps(result)
    result["what_not_to_do"] = what_not_to_do()
    result["next_methodic_step"] = next_methodic_step(result)
    result["confidence_limits"] = confidence_limits(result)
    if plots_dir:
        result["plots"] = make_plots(tables, Path(plots_dir), axis)
    return result


def empty_result(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "methodic_step": "11.1",
        "title": "System ID flights",
        "official_reference": {"url": METHODIC_111_URL, "supporting_urls": [SYSID_DOC_URL, LOG_MESSAGES_URL]},
        "axis": {"axis": "unknown", "source": "not_detected", "raw_value": None, "injection_point": None},
        "sysid_data_quality": "poor",
        "frequency_response_ready": False,
        "saturation": {},
        "excitation_quality": {},
        "response_quality": {},
        "pid_suppression": {},
        "vibration_noise_quality": {},
        "power_context": {},
        "logging_health": {},
        "mode_context": {},
        "wind_context": {},
        "result": "inconclusive",
        "safety_gate": "repeat_step",
        "evidence_used": [],
        "missing_evidence": [],
        "manual_observations_required": [
            "No-wind condition confirmed by the operator",
            "Vehicle remained controllable during System ID excitation",
            "Axis under test is known and matches the Methodic parameter file used",
            "No visible oscillation or unsafe attitude excursion during excitation",
        ],
        "analysis_window": {"selection": "whole_log", "preferred_window": "System ID active segments for roll, pitch, yaw, or thrust in no-wind conditions.", "start_s": None, "end_s": None},
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
    if "SID" not in tables and "SIDD" not in tables:
        missing.append("Missing required message: SID or SIDD")
    if "SIDS" not in tables:
        missing.append("Missing strongly recommended message: SIDS")
    for name in ("RATE", "ATT", "RCOU", "VIBE", "BAT", "MODE", "PARM"):
        if name not in tables:
            missing.append(f"Missing strongly recommended message: {name}")
    return missing


def log_window(tables: dict[str, Any]) -> dict[str, Any]:
    times = []
    for df in tables.values():
        times.extend(time_values(df))
    if not times:
        return {"selection": "none", "start_s": None, "end_s": None}
    sysid = first_table(tables, ["SID", "SIDD"])
    sysid_times = time_values(sysid) if sysid is not None else []
    if sysid_times:
        return {"selection": "system_id_messages", "start_s": float(min(sysid_times)), "end_s": float(max(sysid_times))}
    return {"selection": "whole_log", "start_s": float(min(times)), "end_s": float(max(times))}


def analyze_mode_context(tables: dict[str, Any]) -> dict[str, Any]:
    mode = tables.get("MODE")
    if mode is None or len(mode) == 0:
        return {"systemid_mode_detected": None, "modes_seen": [], "notes": ["MODE message missing; System ID mode usage inferred from SID/SIDD only."]}
    modes = []
    detected = False
    for _, row in mode.iterrows():
        text = " ".join(str(row.get(col, "")) for col in mode.columns if col.lower() in {"mode", "modename", "name", "astext"})
        if text.strip():
            modes.append(text.strip())
        if "sys" in text.lower() and "id" in text.lower():
            detected = True
    return {"systemid_mode_detected": detected, "modes_seen": sorted(set(modes))[:20], "notes": [] if detected else ["MODE did not explicitly decode a System ID mode; SID/SIDD data is the stronger evidence."]}


def detect_axis(tables: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    raw = None
    source = None
    sids = tables.get("SIDS")
    if sids is not None and len(sids) > 0:
        values = numeric_values(sids, ["Ax", "Axis", "SID_AXIS"])
        if values:
            raw = int(round(values[-1]))
            source = "SIDS.Ax"
    if raw is None:
        value = safe_float(params.get("SID_AXIS"))
        if value is not None:
            raw = int(round(value))
            source = "PARM.SID_AXIS"
    if raw is None:
        return {"axis": "unknown", "source": "not_detected", "raw_value": None, "injection_point": None}
    axis, injection = AXIS_MAP.get(raw, ("unknown", "unsupported_or_position_axis"))
    return {"axis": axis, "source": source, "raw_value": raw, "injection_point": injection}


def analyze_excitation(tables: dict[str, Any], axis: dict[str, Any]) -> dict[str, Any]:
    sysid = first_table(tables, ["SIDD", "SID"])
    if sysid is None or len(sysid) == 0:
        return {"quality": "poor", "samples": 0, "reasons": ["SID/SIDD missing"], "duration_s": 0.0, "frequency_content": {}}
    times = time_values(sysid)
    target = numeric_values(sysid, ["Targ", "Target", "In", "U", "Cmd"])
    freq = numeric_values(sysid, ["F", "Freq", "Frq", "Hz"])
    count = min(len(times), len(target)) if target else len(times)
    duration = float(max(times) - min(times)) if len(times) > 1 else 0.0
    target_stats = summarize_values(target) if target else {"count": 0}
    fstats = frequency_stats(freq)
    reasons = []
    if count < 100:
        reasons.append("Too few SID/SIDD samples for frequency-response review.")
    if duration < 8.0:
        reasons.append("System ID excitation duration is short.")
    if not target:
        reasons.append("SID/SIDD target waveform field was not available.")
    elif value_range(target) < 0.02:
        reasons.append("Injected chirp target amplitude is very small or constant.")
    if not freq:
        reasons.append("SID/SIDD instantaneous frequency field is missing.")
    elif fstats.get("span_hz", 0.0) < 0.5:
        reasons.append("Frequency sweep span is too small for useful model fitting.")
    monotonic = monotonic_fraction(freq)
    if freq and monotonic < 0.55:
        reasons.append("Frequency field is not consistently sweeping upward.")
    clean = not reasons
    marginal = not clean and count >= 60 and duration >= 5.0 and bool(target) and value_range(target) >= 0.01
    return {
        "quality": "good" if clean else ("marginal" if marginal else "poor"),
        "samples": count,
        "duration_s": duration,
        "target": target_stats,
        "frequency_content": fstats,
        "frequency_monotonic_fraction": monotonic if freq else None,
        "axis_under_test": axis,
        "reasons": reasons,
    }


def analyze_response(tables: dict[str, Any], axis: dict[str, Any], excitation: dict[str, Any]) -> dict[str, Any]:
    rate = tables.get("RATE")
    axis_name = axis.get("axis")
    if rate is None or len(rate) == 0:
        return {"quality": "poor", "samples": 0, "reasons": ["RATE missing"], "axis": axis_name}
    actual_field, desired_field, output_field = RATE_AXIS_FIELDS.get(axis_name, (None, None, None))
    if axis_name == "unknown":
        return {"quality": "marginal", "samples": len(rate), "reasons": ["Axis unknown; response cannot be mapped confidently."], "axis": "unknown"}
    if axis_name == "thrust":
        output = numeric_values(rate, ["AOut"])
        return {
            "quality": "marginal" if output else "poor",
            "samples": len(rate),
            "axis": axis_name,
            "rate_actual": {},
            "rate_desired": {},
            "rate_output": summarize_values(output) if output else {},
            "response_to_excitation_ratio": None,
            "reasons": [] if output else ["RATE.AOut missing for thrust System ID context."],
        }
    actual = numeric_values(rate, [actual_field] if actual_field else [])
    desired = numeric_values(rate, [desired_field] if desired_field else [])
    output = numeric_values(rate, [output_field] if output_field else [])
    target_rms = rms_from_summary((excitation.get("target") or {}))
    actual_rms = rms(actual)
    output_rms = rms(output)
    reasons = []
    if len(actual) < 100:
        reasons.append(f"Too few RATE samples for {axis_name} response.")
    if actual_rms is not None and actual_rms < 1.0:
        reasons.append(f"{axis_name} RATE response is very small; excitation may be suppressed or ineffective.")
    if output_rms is not None and output_rms < 0.005:
        reasons.append(f"{axis_name} RATE output response is very small.")
    if output and max(abs(v) for v in output) > 0.9:
        reasons.append(f"{axis_name} RATE output is near actuator command limit.")
    ratio = actual_rms / target_rms if target_rms and actual_rms is not None else None
    if ratio is not None and ratio < 0.2:
        reasons.append("Measured response is weak relative to the injected chirp target; PIDs may be suppressing excitation.")
    quality = "good" if not reasons else ("marginal" if len(actual) >= 60 and actual_rms and actual_rms >= 0.5 else "poor")
    return {
        "quality": quality,
        "samples": len(rate),
        "axis": axis_name,
        "rate_actual": summarize_values(actual) if actual else {},
        "rate_desired": summarize_values(desired) if desired else {},
        "rate_output": summarize_values(output) if output else {},
        "response_to_excitation_ratio": ratio,
        "reasons": reasons,
    }


def analyze_pid_suppression(axis: dict[str, Any], excitation: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    ratio = response.get("response_to_excitation_ratio")
    axis_name = axis.get("axis")
    if ratio is None:
        return {"suspected": None, "axis": axis_name, "reason": "Insufficient target/response evidence to assess PID suppression."}
    if ratio < 0.2 and excitation.get("quality") in {"good", "marginal"}:
        return {"suspected": True, "axis": axis_name, "response_to_excitation_ratio": ratio, "reason": "Injected chirp exists, but measured rate response is weak."}
    return {"suspected": False, "axis": axis_name, "response_to_excitation_ratio": ratio, "reason": "Measured response is not obviously suppressed by closed-loop control."}


def saturation_context(motor: dict[str, Any]) -> dict[str, Any]:
    channels = motor.get("channels") or {}
    saturated = {
        name: data
        for name, data in channels.items()
        if (data.get("pct_high_ge_1900") or 0.0) > 5.0
        or (data.get("pct_low_le_1100") or 0.0) > 5.0
        or data.get("persistent_high")
        or data.get("persistent_low")
    }
    return {"present": bool(saturated), "channels": saturated, "motor_spread": motor.get("motor_spread")}


def analyze_power(tables: dict[str, Any]) -> dict[str, Any]:
    bat = tables.get("BAT")
    powr = tables.get("POWR")
    out = {"battery": {}, "board_power": {}, "warnings": []}
    if bat is not None and len(bat):
        volts = numeric_values(bat, ["Volt", "VoltR"])
        curr = numeric_values(bat, ["Curr", "CurrTot"])
        out["battery"] = {"voltage": summarize_values(volts) if volts else {}, "current": summarize_values(curr) if curr else {}}
        if volts and min(volts) < 10.0:
            out["warnings"].append("Battery voltage dropped below a generic low-voltage screening threshold; confirm pack cell count before using this as a fault.")
    else:
        out["warnings"].append("BAT missing; battery adequacy during System ID cannot be reviewed.")
    if powr is not None and len(powr):
        vcc = numeric_values(powr, ["Vcc"])
        out["board_power"] = {"vcc": summarize_values(vcc) if vcc else {}}
        if vcc and min(vcc) < 4.7:
            out["warnings"].append("Board Vcc dropped below 4.7 V.")
    return out


def analyze_logging(index: dict[str, Any], stats: dict[str, Any], tables: dict[str, Any]) -> dict[str, Any]:
    health = index.get("logging_health") or {}
    confirmed = health.get("confirmed_dropouts") or index.get("logging_dropouts") or []
    possible = health.get("possible_dropouts") or index.get("possible_logging_dropouts") or []
    sysid = first_table(tables, ["SIDD", "SID"])
    rate_hz = sample_rate_hz(sysid) if sysid is not None else None
    warnings = []
    if confirmed:
        warnings.append("Confirmed logging dropouts are present.")
    if rate_hz is not None and rate_hz < 20.0:
        warnings.append("SID/SIDD sample rate appears low for model-ready frequency-response review.")
    return {
        "confirmed_dropouts": len(confirmed),
        "possible_dropouts": len(possible),
        "sid_sample_rate_hz": rate_hz,
        "parser_stats": stats,
        "warnings": warnings,
    }


def analyze_wind_context(tables: dict[str, Any]) -> dict[str, Any]:
    groundspeeds = []
    for name in ("GPS", "GPS2"):
        df = tables.get(name)
        if df is not None and len(df):
            groundspeeds.extend(numeric_values(df, ["Spd", "GSpd", "Vel"]))
    if not groundspeeds:
        return {"available": False, "note": "Wind cannot be confirmed absent from logs; operator observation is required."}
    return {
        "available": True,
        "groundspeed": summarize_values(groundspeeds),
        "note": "Groundspeed is weak context only. This tool does not claim low or absent wind from the log alone.",
    }


def classify_data_quality(excitation: dict[str, Any], response: dict[str, Any], saturation: dict[str, Any], vibration: dict[str, Any], logging: dict[str, Any], missing: list[str], pid_suppression: dict[str, Any]) -> str:
    if any("SID or SIDD" in item for item in missing):
        return "poor"
    if saturation.get("present"):
        return "poor"
    if vibration_severe(vibration):
        return "poor"
    if logging.get("confirmed_dropouts"):
        return "poor"
    if excitation.get("quality") == "good" and response.get("quality") == "good" and not pid_suppression.get("suspected"):
        return "good"
    if excitation.get("quality") == "poor" or response.get("quality") == "poor":
        return "poor"
    return "marginal"


def classify_result(result: dict[str, Any]) -> tuple[str, str]:
    if any("SID or SIDD" in item for item in result["missing_evidence"]):
        return "inconclusive", "repeat_step"
    if result["saturation"].get("present") or vibration_severe(result["vibration_noise_quality"]) or result["logging_health"].get("confirmed_dropouts"):
        return "do_not_use", "do_not_proceed"
    if result["sysid_data_quality"] == "good":
        return "ready_for_model", "proceed_with_caution"
    if result["sysid_data_quality"] == "marginal":
        return "repeat_sysid", "repeat_step"
    return "repeat_sysid", "repeat_step"


def findings(result: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    if result["axis"].get("axis") == "unknown":
        out.append({"severity": "warning", "finding": "System ID axis could not be determined from SIDS.Ax or SID_AXIS.", "evidence": result["axis"]})
    if result["excitation_quality"].get("quality") != "good":
        out.append({"severity": "warning", "finding": "System ID excitation is not model-ready.", "evidence": result["excitation_quality"].get("reasons", [])})
    if result["response_quality"].get("quality") != "good":
        out.append({"severity": "warning", "finding": "Measured response quality is limited.", "evidence": result["response_quality"].get("reasons", [])})
    if result["pid_suppression"].get("suspected"):
        out.append({"severity": "warning", "finding": "PIDs may be suppressing the injected chirp.", "evidence": result["pid_suppression"]})
    if result["saturation"].get("present"):
        out.append({"severity": "critical", "finding": "Actuator output saturation occurred during System ID evidence window.", "evidence": result["saturation"]})
    if vibration_severe(result["vibration_noise_quality"]):
        out.append({"severity": "critical", "finding": "Severe vibration or clipping blocks System ID data use.", "evidence": result["vibration_noise_quality"]})
    if result["logging_health"].get("confirmed_dropouts"):
        out.append({"severity": "critical", "finding": "Confirmed logging dropouts make System ID evidence unsafe for model fitting.", "evidence": result["logging_health"]})
    return out


def checked_but_not_supported(tables: dict[str, Any], result: dict[str, Any]) -> list[str]:
    checked = []
    if "SIDS" not in tables:
        checked.append("SIDS settings unavailable; axis and chirp setup relied on SID_AXIS if logged.")
    if "BAT" not in tables:
        checked.append("BAT unavailable; battery state during System ID could not be reviewed.")
    if "MODE" not in tables:
        checked.append("MODE unavailable; System ID mode switch sequence could not be reviewed.")
    if not result["wind_context"].get("available"):
        checked.append("Wind condition cannot be established from log evidence alone.")
    return checked


def recommended_next_steps(result: dict[str, Any]) -> list[str]:
    if result["result"] == "ready_for_model":
        return [
            "Inspect the SID/SIDD, RATE response, actuator output, vibration, battery, and logging evidence before using this dataset for analytical optimisation.",
            "Use only the axis reported as model-ready; repeat separate System ID flights for any missing roll, pitch, yaw, or thrust axes.",
            "Proceed to Methodic 11.2 only as an evidence review step; do not generate or apply final PID values automatically.",
        ]
    if result["result"] == "do_not_use":
        return [
            "Do not use this System ID dataset for model fitting.",
            "Resolve actuator saturation, vibration/clipping, logging dropout, or control-stability blockers before repeating System ID.",
            "Repeat only a controlled System ID capture after prerequisites and safety observations are satisfied.",
        ]
    if result["result"] == "inconclusive":
        return [
            "Collect a readable System ID log with SID/SIDD/SIDS plus RATE, ATT, RCOU/RCO2/RCO3, VIBE, BAT, MODE, and PARM evidence.",
            "Confirm which Methodic System ID parameter file and axis were used.",
            "Do not infer model readiness from ordinary flight data without System ID excitation messages.",
        ]
    return [
        "Repeat the System ID capture for this axis after addressing excitation duration, frequency sweep, response strength, or logging limits.",
        "Confirm no-wind conditions from operator observation; logs alone cannot prove wind absence.",
        "Do not continue to analytical PID optimisation until data quality is good for the intended axis.",
    ]


def what_not_to_do() -> list[str]:
    return [
        "Do not derive a model or PID values from poor, saturated, noisy, or under-excited System ID data.",
        "Do not auto-apply analytical optimisation results from this review.",
        "Do not claim wind absence from the log alone; operator observation is required.",
        "Do not run System ID on an unstable aircraft or skip earlier Methodic safety gates.",
    ]


def next_methodic_step(result: dict[str, Any]) -> str | None:
    return "11.2" if result["result"] == "ready_for_model" else None


def confidence_limits(result: dict[str, Any]) -> list[str]:
    limits = []
    limits.extend(result.get("missing_evidence") or [])
    if result["axis"].get("axis") == "unknown":
        limits.append("Axis under test was not determined from the log.")
    if result["sysid_data_quality"] != "good":
        limits.append("System ID data quality is not good enough for model-ready classification.")
    if not result["wind_context"].get("available"):
        limits.append("No-wind condition remains a required manual observation.")
    return limits


def make_plots(tables: dict[str, Any], plots_dir: Path, axis: dict[str, Any]) -> list[str]:
    ensure_dir(plots_dir)
    paths: list[str] = []
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except Exception:
        return paths

    sysid = first_table(tables, ["SIDD", "SID"])
    if sysid is not None and len(sysid):
        fig = make_subplots(specs=[[{"secondary_y": True}]])
        x = time_values(sysid)
        for field in ("Targ", "Target", "In", "U", "Cmd"):
            y = numeric_values(sysid, [field])
            if y:
                fig.add_trace(go.Scatter(x=x[: len(y)], y=y, name=f"SID.{field}"), secondary_y=False)
                break
        f = numeric_values(sysid, ["F", "Freq", "Frq", "Hz"])
        if f:
            fig.add_trace(go.Scatter(x=x[: len(f)], y=f, name="SID.F"), secondary_y=True)
        fig.update_layout(title="Methodic 11.1 SID/SIDD excitation")
        path = plots_dir / "methodic_11_1_sysid_excitation.html"
        fig.write_html(path)
        paths.append(str(path))

    rate = tables.get("RATE")
    axis_name = axis.get("axis")
    if rate is not None and len(rate):
        actual_field, desired_field, output_field = RATE_AXIS_FIELDS.get(axis_name, ("R", "RDes", "ROut"))
        fig = make_subplots(specs=[[{"secondary_y": True}]])
        x = time_values(rate)
        for field in (desired_field, actual_field):
            if field:
                y = numeric_values(rate, [field])
                if y:
                    fig.add_trace(go.Scatter(x=x[: len(y)], y=y, name=f"RATE.{field}"), secondary_y=False)
        if output_field:
            y = numeric_values(rate, [output_field])
            if y:
                fig.add_trace(go.Scatter(x=x[: len(y)], y=y, name=f"RATE.{output_field}"), secondary_y=True)
        fig.update_layout(title=f"Methodic 11.1 selected axis response ({axis_name})")
        path = plots_dir / "methodic_11_1_selected_axis_response.html"
        fig.write_html(path)
        paths.append(str(path))

    rcou = first_table(tables, ["RCOU", "RCO2", "RCO3"])
    if rcou is not None and len(rcou):
        fig = go.Figure()
        x = time_values(rcou)
        for col in [c for c in rcou.columns if str(c).startswith("C")][:16]:
            y = numeric_values(rcou, [str(col)])
            if y:
                fig.add_trace(go.Scatter(x=x[: len(y)], y=y, name=f"RCOU.{col}"))
        fig.update_layout(title="Methodic 11.1 actuator output")
        path = plots_dir / "methodic_11_1_actuator_output.html"
        fig.write_html(path)
        paths.append(str(path))

    vibe = tables.get("VIBE")
    if vibe is not None and len(vibe):
        fig = make_subplots(specs=[[{"secondary_y": True}]])
        x = time_values(vibe)
        for field in ("VibeX", "VibeY", "VibeZ"):
            y = numeric_values(vibe, [field])
            if y:
                fig.add_trace(go.Scatter(x=x[: len(y)], y=y, name=f"VIBE.{field}"), secondary_y=False)
        clips = []
        for field in clip_columns(vibe):
            vals = numeric_values(vibe, [field])
            if vals:
                clips = [sum(item) for item in zip(clips, vals)] if clips else vals
        if clips:
            fig.add_trace(go.Scatter(x=x[: len(clips)], y=clips, name="clip sum"), secondary_y=True)
        fig.update_layout(title="Methodic 11.1 vibration and clipping")
        path = plots_dir / "methodic_11_1_vibration.html"
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
        f"- Axis: `{(result.get('axis') or {}).get('axis')}`",
        f"- System ID data quality: `{result.get('sysid_data_quality')}`",
        f"- Frequency-response ready: `{result.get('frequency_response_ready')}`",
        "",
        "## Findings",
    ]
    for item in result.get("findings", []):
        lines.append(f"- {item.get('severity', 'info')}: {item.get('finding')}")
    if not result.get("findings"):
        lines.append("- No blocking findings were generated by the deterministic review.")
    lines.extend(["", "## Missing Evidence"])
    for item in result.get("missing_evidence", []):
        lines.append(f"- {item}")
    if not result.get("missing_evidence"):
        lines.append("- None from the configured evidence checklist.")
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


def time_values(df: Any) -> list[float]:
    if df is None or len(df) == 0:
        return []
    for col, scale in (("TimeS", 1.0), ("Time", 1.0), ("TimeUS", 1e-6), ("TimeMS", 1e-3)):
        if col in df:
            vals = [safe_float(v) for v in df[col].tolist()]
            return [float(v) * scale for v in vals if v is not None]
    return [float(i) for i in range(len(df))]


def numeric_values(df: Any, fields: list[str]) -> list[float]:
    if df is None:
        return []
    for field in fields:
        if field and field in df:
            out = []
            for value in numeric_series(df, [field]):
                if value is not None and math.isfinite(float(value)):
                    out.append(float(value))
            if out:
                return out
    return []


def value_range(values: list[float]) -> float:
    return float(max(values) - min(values)) if values else 0.0


def rms(values: list[float]) -> float | None:
    if not values:
        return None
    return math.sqrt(sum(v * v for v in values) / len(values))


def rms_from_summary(summary: dict[str, Any]) -> float | None:
    value = summary.get("rms")
    return float(value) if value is not None else None


def frequency_stats(freq: list[float]) -> dict[str, Any]:
    if not freq:
        return {"available": False}
    return {
        "available": True,
        "min_hz": float(min(freq)),
        "max_hz": float(max(freq)),
        "span_hz": float(max(freq) - min(freq)),
        "unique_rounded_bins": len({round(v, 2) for v in freq}),
    }


def monotonic_fraction(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    diffs = [values[i] - values[i - 1] for i in range(1, len(values))]
    return sum(1 for d in diffs if d >= -1e-6) / len(diffs)


def sample_rate_hz(df: Any) -> float | None:
    times = time_values(df)
    if len(times) < 2:
        return None
    duration = max(times) - min(times)
    if duration <= 0:
        return None
    return float((len(times) - 1) / duration)


def vibration_severe(vibration: dict[str, Any]) -> bool:
    clip_delta = vibration.get("clip_delta") or {}
    if any((safe_float(v) or 0.0) > 0.0 for v in clip_delta.values()):
        return True
    if (safe_float(vibration.get("p95_axis")) or 0.0) > 60.0:
        return True
    if (safe_float(vibration.get("max_axis")) or 0.0) > 90.0:
        return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Review Methodic 11.1 System ID flight data quality without generating PID values.")
    parser.add_argument("log", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--plots", type=Path)
    args = parser.parse_args()

    result = analyze_sysid_review(args.log, plots_dir=args.plots)
    write_json(args.out, result)
    if args.summary:
        write_summary(args.summary, result)
    else:
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
