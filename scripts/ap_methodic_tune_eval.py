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

METHODIC_URLS = {
    "9.3": "https://ardupilot.github.io/MethodicConfigurator/TUNING_GUIDE_ArduCopter#93-fifth-flight-evaluate-the-aircraft-tune---part-1",
    "9.4": "https://ardupilot.github.io/MethodicConfigurator/TUNING_GUIDE_ArduCopter#94-sixth-flight-evaluate-the-aircraft-tune---part-2",
    "9.6": "https://ardupilot.github.io/MethodicConfigurator/TUNING_GUIDE_ArduCopter#96-seventh-flight-evaluate-the-aircraft-performance",
}
TITLES = {
    "9.3": "Tune evaluation with feed-forward disabled",
    "9.4": "Tune evaluation with feed-forward enabled",
    "9.6": "Performance evaluation",
}
MESSAGES = [
    "ATT",
    "RATE",
    "PIDR",
    "PIDP",
    "PIDY",
    "RCIN",
    "RCOU",
    "RCO2",
    "RCO3",
    "VIBE",
    "BAT",
    "POWR",
    "MODE",
    "PARM",
    "MSG",
    "ERR",
    "EV",
    "ARM",
]
RELEVANT_PARAMETERS = [
    "ATC_RAT_*",
    "ATC_ANG_*",
    "ATC_ACCEL_*",
    "ATC_INPUT_TC",
    "ATC_THR_MIX_MAX",
    "INS_HNTCH_*",
    "INS_GYRO_FILTER",
]
AXES = {
    "roll": {"rate_des": "RDes", "rate": "R", "out": "ROut", "att_des": "DesRoll", "att": "Roll", "pid": "PIDR"},
    "pitch": {"rate_des": "PDes", "rate": "P", "out": "POut", "att_des": "DesPitch", "att": "Pitch", "pid": "PIDP"},
    "yaw": {"rate_des": "YDes", "rate": "Y", "out": "YOut", "att_des": "DesYaw", "att": "Yaw", "pid": "PIDY"},
}


def rows_to_tables(rows_by_message: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    try:
        import pandas as pd
    except Exception as exc:
        raise AnalysisError("pandas is required for Methodic tune evaluation. Install dependencies with: pip install -r requirements.txt") from exc
    return {name: pd.DataFrame(rows) for name, rows in rows_by_message.items() if rows}


def analyze_tune_eval(
    log_path: str | Path,
    *,
    step: str = "9.3",
    plots_dir: str | Path | None = None,
) -> dict[str, Any]:
    if step not in METHODIC_URLS:
        raise AnalysisError(f"Unsupported Methodic tune evaluation step: {step}")
    rows, index, stats = collect_dataflash(log_path, include=MESSAGES, source=str(log_path))
    tables = rows_to_tables(rows)
    params = index.get("parameters", {}) or {}

    result = empty_result(step, params)
    result["analysis_window"]["parser_stats"] = stats
    result["evidence_used"].append({"type": "messages_present", "messages": sorted(tables.keys())})
    missing = missing_evidence(tables)
    result["missing_evidence"].extend(missing)

    window = evaluation_window(tables)
    result["analysis_window"].update(window)
    rc = analyze_rc_input_contamination(tables, params)
    rate = tables.get("RATE")
    att = tables.get("ATT")

    input_quality = analyze_input_quality(tables, rc)
    axis_results = {
        axis: analyze_axis(axis, rate, att, tables.get(spec["pid"]))
        for axis, spec in AXES.items()
    }
    pid = analyze_pid(tables)
    motor = analyze_motor_outputs(tables, None, params)
    vibration = analyze_vibration(tables, None)
    power = analyze_power(tables)
    tune_quality = summarize_tune_quality(step, axis_results, pid, motor, vibration, power)

    result["axis_results"] = axis_results
    result["input_quality"] = input_quality
    result["tune_quality"] = tune_quality
    result["evidence_used"].extend([
        {"type": "rc_input_contamination", "value": trim_rc(rc)},
        {"type": "tracking_and_outputs", "value": axis_results},
        {"type": "pid_terms", "value": pid},
        {"type": "mapped_motor_outputs", "value": motor},
        {"type": "vibration", "value": vibration},
        {"type": "power", "value": power},
    ])
    result["findings"] = classify_findings(step, input_quality, axis_results, pid, motor, vibration, power, missing)
    result["checked_but_not_supported"] = checked_but_not_supported(tables, input_quality, axis_results, pid, motor, vibration, power)
    result["result"], result["safety_gate"] = classify_result(result["findings"], missing, input_quality)
    result["recommended_next_steps"] = recommended_next_steps(result)
    result["what_not_to_do"] = what_not_to_do()
    result["next_methodic_step"] = next_methodic_step(step, result["result"])
    result["confidence_limits"] = confidence_limits(result, rc)
    if plots_dir:
        result["plots"] = make_plots(tables, Path(plots_dir), step)
    return result


def compare_tune_logs(before_log: str | Path, after_log: str | Path, *, plots_dir: str | Path | None = None) -> dict[str, Any]:
    before = analyze_tune_eval(before_log, step="9.6", plots_dir=None)
    after = analyze_tune_eval(after_log, step="9.6", plots_dir=plots_dir)
    comparable = compare_windows(before, after)
    deltas = compare_axis_metrics(before, after) if comparable["comparable"] else {}
    result = {
        "methodic_step": "9.6_compare",
        "title": "Before/after Methodic tune evaluation comparison",
        "official_reference": {"url": METHODIC_URLS["9.6"]},
        "result": compare_result(before, after, comparable, deltas),
        "safety_gate": "proceed_with_caution" if comparable["comparable"] else "repeat_step",
        "before": before,
        "after": after,
        "comparison": {"comparable_window_confidence": comparable, "axis_metric_deltas": deltas},
        "recommended_next_steps": compare_next_steps(comparable, deltas, after),
        "what_not_to_do": what_not_to_do() + ["Do not claim improvement when before/after excitation or window quality is not comparable."],
        "plots": after.get("plots", []),
    }
    return result


def empty_result(step: str, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "methodic_step": step,
        "title": TITLES[step],
        "official_reference": {
            "url": METHODIC_URLS[step],
            "supporting_urls": ["https://ardupilot.org/copter/docs/logmessages.html"],
        },
        "result": "inconclusive",
        "safety_gate": "repeat_step",
        "axis_results": {},
        "input_quality": {},
        "tune_quality": {},
        "evidence_used": [],
        "missing_evidence": [],
        "manual_observations_required": ["No visible or audible oscillation", "Pilot reports controllable response", "Evaluation maneuvers were intentional and within the stable envelope"],
        "analysis_window": {"selection": "whole_log", "preferred_window": preferred_window(step), "start_s": None, "end_s": None},
        "findings": [],
        "checked_but_not_supported": [],
        "parameter_context": parameter_context(params),
        "plots": [],
        "recommended_next_steps": [],
        "what_not_to_do": [],
        "next_methodic_step": None,
        "confidence_limits": [],
    }


def preferred_window(step: str) -> str:
    if step == "9.3":
        return "Isolated tune-evaluation maneuvers with feed-forward disabled, excluding takeoff, landing, and repositioning."
    if step == "9.4":
        return "Isolated tune-evaluation maneuvers with feed-forward enabled, excluding takeoff, landing, and repositioning."
    return "Controlled performance-evaluation maneuvers inside the stable flight envelope."


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
    for name in ("ATT", "RATE"):
        if name not in tables:
            missing.append(f"Missing required message: {name}")
    for name in ("PIDR", "PIDP", "PIDY", "RCIN", "RCOU", "VIBE", "BAT", "MODE", "PARM"):
        if name not in tables:
            missing.append(f"Missing strongly recommended message: {name}")
    return missing


def evaluation_window(tables: dict[str, Any]) -> dict[str, Any]:
    times = []
    for name in ("RATE", "ATT", "RCIN"):
        df = tables.get(name)
        if df is not None and "TimeS" in getattr(df, "columns", []):
            times.extend(v for v in [safe_float(t) for t in df["TimeS"].tolist()] if v is not None)
    if not times:
        return {"selection": "none", "start_s": None, "end_s": None}
    return {"selection": "whole_log_evaluation", "start_s": float(min(times)), "end_s": float(max(times))}


def analyze_input_quality(tables: dict[str, Any], rc: dict[str, Any]) -> dict[str, Any]:
    rate = tables.get("RATE")
    axes: dict[str, Any] = {}
    active_masks: dict[str, list[bool]] = {}
    samples = len(rate) if rate is not None else 0
    for axis, spec in AXES.items():
        values = series_values(rate, spec["rate_des"])
        active = [abs(v) >= (5.0 if axis != "yaw" else 3.0) for v in values]
        active_masks[axis] = active
        axes[axis] = {
            "desired_rate_p95_abs": percentile([abs(v) for v in values], 95),
            "desired_rate_max_abs": max([abs(v) for v in values], default=None),
            "active_percent": percent_true(active),
            "sufficient_excitation": percent_true(active) >= 3.0 and (max([abs(v) for v in values], default=0.0) >= (10.0 if axis != "yaw" else 6.0)),
        }
    multi_axis = 0
    isolated = {axis: 0 for axis in AXES}
    limit = min((len(m) for m in active_masks.values()), default=0)
    for idx in range(limit):
        active_axes = [axis for axis, mask in active_masks.items() if idx < len(mask) and mask[idx]]
        if len(active_axes) > 1:
            multi_axis += 1
        elif len(active_axes) == 1:
            isolated[active_axes[0]] += 1
    duration = duration_s(rate)
    sufficient_axes = [axis for axis, data in axes.items() if data["sufficient_excitation"]]
    return {
        "available": rate is not None and samples > 0,
        "samples": samples,
        "duration_s": duration,
        "enough_duration": duration is not None and duration >= 8.0,
        "axes": axes,
        "sufficient_excitation_axes": sufficient_axes,
        "has_meaningful_stick_inputs": bool(sufficient_axes),
        "multi_axis_coupling_percent": 100.0 * multi_axis / limit if limit else None,
        "isolated_axis_percent": {axis: 100.0 * count / limit if limit else None for axis, count in isolated.items()},
        "rc_hands_off_confidence": rc.get("hands_off_confidence"),
        "rc_warnings": rc.get("warnings", []),
    }


def analyze_axis(axis: str, rate: Any, att: Any, pid: Any) -> dict[str, Any]:
    spec = AXES[axis]
    rate_error = paired_error(rate, spec["rate_des"], spec["rate"], wrap=False)
    att_error = paired_error(att, spec["att_des"], spec["att"], wrap=(axis == "yaw"))
    out_values = series_values(rate, spec["out"])
    times = time_values(rate)
    osc = classify_oscillation(out_values, times[: len(out_values)], threshold=0.1, min_samples=20, min_duration_s=2.0) if out_values else {"classification": "inconclusive", "metrics": {}, "reason": ["RATE output field missing"]}
    return {
        "rate_tracking": summarize_error(rate_error, "deg/s"),
        "attitude_tracking": summarize_error(att_error, "deg"),
        "controller_output": {
            **summarize_values(out_values, threshold=0.1),
            "classification": osc.get("classification"),
            "classification_reason": osc.get("reason", []),
            "highpass_p95_abs": (osc.get("metrics") or {}).get("highpass_residual_p95_abs"),
            "sign_change_rate_hz": (osc.get("metrics") or {}).get("sign_change_rate_hz"),
        },
        "overshoot": estimate_overshoot(rate, spec["rate_des"], spec["rate"]),
        "settling_estimate": estimate_settling(rate, spec["rate_des"], spec["rate"]),
        "lag_phase_estimate": estimate_lag(rate, spec["rate_des"], spec["rate"]),
        "pid": summarize_pid_axis(pid),
    }


def paired_error(df: Any, desired: str, actual: str, *, wrap: bool) -> list[float]:
    if df is None:
        return []
    d = series_values(df, desired)
    a = series_values(df, actual)
    count = min(len(d), len(a))
    errors = []
    for idx in range(count):
        err = d[idx] - a[idx]
        if wrap:
            err = ((err + 180.0) % 360.0) - 180.0
        errors.append(err)
    return errors


def summarize_error(values: list[float], unit: str) -> dict[str, Any]:
    if not values:
        return {"available": False, "samples": 0, "unit": unit}
    abs_values = [abs(v) for v in values]
    return {
        "available": True,
        "samples": len(values),
        "rms": math.sqrt(sum(v * v for v in values) / len(values)),
        "p95_abs": percentile(abs_values, 95),
        "max_abs": max(abs_values),
        "unit": unit,
    }


def summarize_pid_axis(pid: Any) -> dict[str, Any]:
    if pid is None or len(pid) == 0:
        return {"available": False}
    terms = {}
    for col in ("P", "I", "D", "FF", "DFF", "Dmod", "SRate", "Flags"):
        values = series_values(pid, col)
        if values:
            terms[col] = summarize_values(values)
    flags = terms.get("Flags") or {}
    dmod = terms.get("Dmod") or {}
    return {
        "available": bool(terms),
        "terms": terms,
        "flags_present": bool(flags.get("max_abs") and flags.get("max_abs") > 0),
        "dmod_min": min(series_values(pid, "Dmod"), default=None),
        "i_term_p95_abs": (terms.get("I") or {}).get("p95_abs"),
        "i_term_mean": (terms.get("I") or {}).get("mean"),
        "dmod_p05": percentile(series_values(pid, "Dmod"), 5) if "Dmod" in getattr(pid, "columns", []) else None,
    }


def analyze_pid(tables: dict[str, Any]) -> dict[str, Any]:
    return {axis: summarize_pid_axis(tables.get(spec["pid"])) for axis, spec in AXES.items()}


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
                msg[field] = {
                    "min": min(values),
                    "max": max(values),
                    "mean": mean(values),
                    "range": max(values) - min(values),
                    "samples": len(values),
                }
        out["messages"][name] = msg
    out["available"] = bool(out["messages"])
    return out


def summarize_tune_quality(step: str, axis_results: dict[str, Any], pid: dict[str, Any], motor: dict[str, Any], vibration: dict[str, Any], power: dict[str, Any]) -> dict[str, Any]:
    target = 0.1 if step == "9.6" else 0.15
    axes = {}
    for axis, data in axis_results.items():
        output = data.get("controller_output") or {}
        tracking = data.get("rate_tracking") or {}
        axes[axis] = {
            "output_target": target,
            "output_p95_within_target": output.get("p95_abs") is not None and output.get("p95_abs") <= target,
            "rate_tracking_acceptable": tracking.get("p95_abs") is not None and tracking.get("p95_abs") <= (12.0 if axis != "yaw" else 18.0),
            "oscillation_classification": output.get("classification"),
        }
    return {
        "axis_assessments": axes,
        "all_outputs_within_target": all(v["output_p95_within_target"] for v in axes.values() if v),
        "motor_output_available": motor.get("available"),
        "vibration_available": vibration.get("available"),
        "power_available": power.get("available"),
        "pid_available_axes": [axis for axis, data in pid.items() if data.get("available")],
    }


def classify_findings(step: str, input_quality: dict[str, Any], axis_results: dict[str, Any], pid: dict[str, Any], motor: dict[str, Any], vibration: dict[str, Any], power: dict[str, Any], missing: list[str]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if any(item.startswith("Missing required") for item in missing):
        findings.append(finding("inconclusive", "Required ATT/RATE tune-evaluation evidence is missing.", {"missing": missing}))
    if not input_quality.get("has_meaningful_stick_inputs"):
        findings.append(finding("inconclusive", "No meaningful isolated tune-evaluation stick inputs were detected.", input_quality))
    if not input_quality.get("enough_duration"):
        findings.append(finding("repeat", "Evaluation duration is short for tune quality claims.", input_quality))
    coupling = input_quality.get("multi_axis_coupling_percent")
    if coupling is not None and coupling > 45.0:
        findings.append(finding("repeat", "Stick inputs are highly coupled across axes, limiting tune-evaluation confidence.", {"multi_axis_coupling_percent": coupling}))

    severe_vibe = False
    if vibration.get("available"):
        clips = vibration.get("clip_delta") or {}
        severe_vibe = any(v > 0 for v in clips.values()) or (vibration.get("p95_axis") is not None and vibration["p95_axis"] > 30.0) or (vibration.get("max_axis") is not None and vibration["max_axis"] > 45.0)
        if severe_vibe:
            findings.append(finding("filter_blocker", "Vibration or clipping blocks tune evaluation.", vibration, "bench_check_required"))
        elif vibration.get("p95_axis") is not None and vibration["p95_axis"] > 20.0:
            findings.append(finding("filter_caution", "Vibration is in a grey zone; tune results need filter/mechanical review.", vibration))
    else:
        findings.append(finding("conditional", "VIBE is missing, so filter/mechanical noise cannot be ruled out.", vibration))

    if motor.get("available"):
        saturated = [
            name for name, data in (motor.get("channels") or {}).items()
            if data.get("pct_high_ge_1900", 0.0) > 1.0 or data.get("pct_low_le_1100", 0.0) > 1.0 or data.get("persistent_high") or data.get("persistent_low")
        ]
        if saturated:
            findings.append(finding("gain_blocker", "Motor output saturation or persistent rail contact appears during evaluation.", {"channels": saturated[:12]}, "do_not_proceed"))
    else:
        findings.append(finding("conditional", "Motor outputs are missing; saturation/headroom cannot be assessed.", motor))

    for axis, data in axis_results.items():
        output = data.get("controller_output") or {}
        tracking = data.get("rate_tracking") or {}
        cls = output.get("classification")
        p95 = output.get("p95_abs")
        max_abs = output.get("max_abs")
        target = 0.1 if step == "9.6" else 0.15
        if cls in {"oscillatory", "mixed"} and (p95 or 0.0) > target:
            findings.append(finding("gain_blocker", f"{axis} controller output is high and oscillatory.", {"axis": axis, "output": output}, "do_not_proceed"))
        elif p95 is not None and p95 > max(0.2, target * 1.5):
            findings.append(finding("gain_blocker", f"{axis} RATE output demand is high for this Methodic evaluation.", {"axis": axis, "output": output}, "do_not_proceed"))
        elif p95 is not None and p95 > target:
            findings.append(finding("conditional", f"{axis} RATE output p95 exceeds the Methodic target for this step.", {"axis": axis, "output": output}))
        elif tracking.get("p95_abs") is not None and tracking["p95_abs"] > (20.0 if axis != "yaw" else 30.0) and (p95 or 0.0) < 0.08 and not severe_vibe:
            findings.append(finding("response_low", f"{axis} tracking error is high while controller output demand is low.", {"axis": axis, "tracking": tracking, "output": output}))
        else:
            findings.append({"severity": "info", "finding": f"{axis} tune-evaluation metrics did not cross conservative blocker thresholds.", "evidence": {"axis": axis, "tracking": tracking, "output": output}})

        if max_abs is not None and max_abs > 0.5:
            findings.append(finding("conditional", f"{axis} RATE output had a large transient peak.", {"axis": axis, "output_max_abs": max_abs}))

    for axis, pdata in pid.items():
        if not pdata.get("available"):
            continue
        if pdata.get("flags_present"):
            findings.append(finding("gain_blocker", f"{axis} PID flags are non-zero during evaluation.", pdata, "do_not_proceed"))
        if pdata.get("dmod_p05") is not None and pdata["dmod_p05"] < 0.65:
            findings.append(finding("filter_blocker", f"{axis} Dmod reduction suggests noise/filter limits.", pdata))
        if pdata.get("i_term_p95_abs") is not None and pdata["i_term_p95_abs"] > 0.25:
            findings.append(finding("conditional", f"{axis} I-term buildup is high.", pdata))

    if power.get("available"):
        bat = (power.get("messages") or {}).get("BAT") or {}
        volt = bat.get("Volt") or bat.get("VoltR") or {}
        if volt.get("range") is not None and volt["range"] > 2.0:
            findings.append(finding("conditional", "Battery voltage sag may limit comparability of tune evaluation.", volt))
        powr = (power.get("messages") or {}).get("POWR") or {}
        flags = powr.get("Flags") or {}
        if flags.get("max", 0.0) > 0:
            findings.append(finding("filter_blocker", "Board power flags are non-zero during evaluation.", flags, "bench_check_required"))
    return findings


def classify_result(findings: list[dict[str, Any]], missing: list[str], input_quality: dict[str, Any]) -> tuple[str, str]:
    severities = {f.get("severity") for f in findings}
    if "inconclusive" in severities or any(item.startswith("Missing required") for item in missing):
        return "inconclusive", "repeat_step"
    if "filter_blocker" in severities:
        return "improve_filters", "bench_check_required"
    if "gain_blocker" in severities:
        return "reduce_gains", "do_not_proceed"
    if not input_quality.get("has_meaningful_stick_inputs"):
        return "inconclusive", "repeat_step"
    if "response_low" in severities:
        return "increase_response", "proceed_with_caution"
    if "repeat" in severities:
        return "repeat_evaluation", "repeat_step"
    if "conditional" in severities or "filter_caution" in severities:
        return "conditional_pass", "proceed_with_caution"
    return "pass", "proceed"


def checked_but_not_supported(tables: dict[str, Any], input_quality: dict[str, Any], axis_results: dict[str, Any], pid: dict[str, Any], motor: dict[str, Any], vibration: dict[str, Any], power: dict[str, Any]) -> list[str]:
    checked = []
    if input_quality.get("has_meaningful_stick_inputs"):
        checked.append("Meaningful tune-evaluation excitation was detected.")
    if motor.get("available"):
        checked.append("Motor output saturation/headroom was checked.")
    if vibration.get("available"):
        checked.append("VIBE/clipping was checked.")
    if power.get("available"):
        checked.append("Battery/board power context was checked.")
    for axis, data in axis_results.items():
        if (data.get("controller_output") or {}).get("available"):
            checked.append(f"{axis} RATE output demand was checked.")
        if (data.get("rate_tracking") or {}).get("available"):
            checked.append(f"{axis} RATE desired-vs-actual tracking was checked.")
    for name in ("PIDR", "PIDP", "PIDY"):
        if name in tables:
            checked.append(f"{name} PID terms were checked.")
    return checked


def recommended_next_steps(result: dict[str, Any]) -> list[str]:
    status = result["result"]
    if status == "pass":
        return [
            f"Agent should inspect the plots and evidence; if they agree, follow the Methodic path to {result['next_methodic_step']}.",
            "Keep manual observations with the review; do not describe the aircraft as safe to fly.",
        ]
    if status == "conditional_pass":
        return [
            "Resolve listed caveats before treating the evaluation as clean.",
            "Proceed only with explicit limits for the affected axes and evidence gaps.",
        ]
    if status == "improve_filters":
        return [
            "Fix vibration, clipping, Dmod/noise, or board-power evidence before tuning from this log.",
            "Do not use notch/filtering to hide mechanical problems; inspect hardware and repeat a controlled evaluation capture.",
        ]
    if status == "reduce_gains":
        return [
            "Do not continue to later Methodic tuning from this log.",
            "Review the axis with high/oscillatory output demand and follow the Methodic gain-reduction or tune-review path before repeating a short controlled evaluation.",
        ]
    if status == "increase_response":
        return [
            "Tracking appears weak without output/noise/saturation blockers; review response parameters as candidates only after the agent confirms the plots and aircraft context.",
            "Do not increase gains if later inspection finds vibration, saturation, power sag, or pilot-input contamination.",
        ]
    if status == "repeat_evaluation":
        return [
            "Repeat the evaluation with clearer isolated-axis inputs and enough duration before drawing tune conclusions.",
            "Do not claim before/after improvement or regression from a contaminated maneuver set.",
        ]
    return [
        "Collect a readable log with meaningful isolated roll, pitch, and yaw evaluation inputs plus ATT/RATE/PID/RCIN/RCOU/VIBE/BAT/MODE/PARM evidence.",
        "Do not tune from a log that lacks meaningful stick excitation.",
    ]


def what_not_to_do() -> list[str]:
    return [
        "Do not automatically write or upload gains from this evidence tool.",
        "Do not recommend increasing gains when vibration, clipping, motor saturation, power issues, or PID noise evidence exists.",
        "Do not claim tune improvement from before/after logs unless the input windows are comparable.",
        "Do not treat RATE output targets as final truth without inspecting ATT/RATE/PID/RC/motor plots.",
    ]


def next_methodic_step(step: str, result: str) -> str | None:
    if result in {"pass", "conditional_pass"}:
        return {"9.3": "9.4", "9.4": "9.5", "9.6": "9.7"}.get(step)
    if result == "increase_response":
        return f"repeat {step} after response review"
    return f"repeat {step}"


def confidence_limits(result: dict[str, Any], rc: dict[str, Any]) -> list[str]:
    limits = []
    if result["missing_evidence"]:
        limits.append("Missing required or strongly recommended log messages limit tune-evaluation confidence.")
    if not rc.get("available"):
        limits.append("RCIN is missing; stick contamination/coupling was inferred only from RATE desired signals.")
    elif rc.get("hands_off_confidence") == "low":
        limits.append("RC stick activity is high; tune metrics must be interpreted as maneuver response rather than hands-off stability.")
    if result["result"] in {"pass", "conditional_pass"}:
        limits.append("Manual pilot observations remain required before treating the Methodic step as complete.")
    return limits


def compare_windows(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    reasons = []
    bq = before.get("input_quality") or {}
    aq = after.get("input_quality") or {}
    bd = bq.get("duration_s") or 0.0
    ad = aq.get("duration_s") or 0.0
    if min(bd, ad) <= 0:
        reasons.append("One log has no measurable evaluation duration.")
    elif max(bd, ad) / max(min(bd, ad), 0.001) > 2.0:
        reasons.append("Evaluation durations differ by more than 2x.")
    baxes = set(bq.get("sufficient_excitation_axes") or [])
    aaxes = set(aq.get("sufficient_excitation_axes") or [])
    if baxes != aaxes:
        reasons.append("Before/after logs excite different axes.")
    for label, quality in (("before", bq), ("after", aq)):
        if quality.get("multi_axis_coupling_percent") is not None and quality["multi_axis_coupling_percent"] > 60.0:
            reasons.append(f"{label} log has highly coupled stick inputs.")
    comparable = not reasons
    return {"comparable": comparable, "confidence": "high" if comparable else "low", "reasons": reasons}


def compare_axis_metrics(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for axis in AXES:
        b = ((before.get("axis_results") or {}).get(axis) or {})
        a = ((after.get("axis_results") or {}).get(axis) or {})
        out[axis] = {
            "rate_tracking_p95_delta": metric(a, "rate_tracking", "p95_abs") - metric(b, "rate_tracking", "p95_abs") if metric(a, "rate_tracking", "p95_abs") is not None and metric(b, "rate_tracking", "p95_abs") is not None else None,
            "output_p95_delta": metric(a, "controller_output", "p95_abs") - metric(b, "controller_output", "p95_abs") if metric(a, "controller_output", "p95_abs") is not None and metric(b, "controller_output", "p95_abs") is not None else None,
        }
    return out


def compare_result(before: dict[str, Any], after: dict[str, Any], comparable: dict[str, Any], deltas: dict[str, Any]) -> str:
    if not comparable.get("comparable"):
        return "repeat_evaluation"
    if after.get("result") in {"improve_filters", "reduce_gains", "inconclusive"}:
        return after["result"]
    improved = [d for d in deltas.values() if d.get("rate_tracking_p95_delta") is not None and d["rate_tracking_p95_delta"] < -2.0]
    regressed = [d for d in deltas.values() if d.get("rate_tracking_p95_delta") is not None and d["rate_tracking_p95_delta"] > 2.0]
    if regressed and not improved:
        return "repeat_evaluation"
    return "pass" if improved else "conditional_pass"


def compare_next_steps(comparable: dict[str, Any], deltas: dict[str, Any], after: dict[str, Any]) -> list[str]:
    if not comparable.get("comparable"):
        return ["Repeat comparison with matching maneuvers, duration, battery state, and axis excitation before claiming improvement.", *comparable.get("reasons", [])]
    return ["Inspect before/after plots and deltas before describing improvement.", *recommended_next_steps(after)]


def metric(axis_data: dict[str, Any], group: str, name: str) -> float | None:
    return safe_float((axis_data.get(group) or {}).get(name))


def finding(severity: str, text: str, evidence: Any, gate: str | None = None) -> dict[str, Any]:
    out = {"severity": severity, "finding": text, "evidence": evidence}
    if gate:
        out["safety_gate"] = gate
    return out


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


def duration_s(df: Any) -> float | None:
    times = time_values(df)
    if len(times) < 2:
        return None
    return float(max(times) - min(times))


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * pct / 100.0))))
    return float(ordered[idx])


def percent_true(mask: list[bool]) -> float:
    return 100.0 * sum(1 for item in mask if item) / len(mask) if mask else 0.0


def estimate_overshoot(df: Any, desired: str, actual: str) -> dict[str, Any]:
    d = series_values(df, desired)
    a = series_values(df, actual)
    count = min(len(d), len(a))
    if count < 10:
        return {"available": False}
    peaks = []
    for idx in range(count):
        if abs(d[idx]) >= 8.0 and abs(a[idx]) > abs(d[idx]) and same_sign(d[idx], a[idx]):
            peaks.append(abs(a[idx]) - abs(d[idx]))
    return {"available": bool(peaks), "p95_abs": percentile(peaks, 95), "max_abs": max(peaks) if peaks else None, "unit": "deg/s"}


def estimate_settling(df: Any, desired: str, actual: str) -> dict[str, Any]:
    errors = paired_error(df, desired, actual, wrap=False)
    if len(errors) < 20:
        return {"available": False}
    abs_err = [abs(e) for e in errors]
    p95 = percentile(abs_err, 95)
    return {"available": True, "rough_error_band_p95": p95, "method": "error-band proxy; not a formal step-response settling time"}


def estimate_lag(df: Any, desired: str, actual: str) -> dict[str, Any]:
    try:
        import numpy as np
    except Exception:
        return {"available": False, "reason": "numpy unavailable"}
    d = series_values(df, desired)
    a = series_values(df, actual)
    times = time_values(df)
    count = min(len(d), len(a), len(times))
    if count < 30:
        return {"available": False, "reason": "too few samples"}
    d_arr = np.asarray(d[:count]) - np.mean(d[:count])
    a_arr = np.asarray(a[:count]) - np.mean(a[:count])
    if float(np.std(d_arr)) < 1e-6 or float(np.std(a_arr)) < 1e-6:
        return {"available": False, "reason": "insufficient signal variation"}
    corr = np.correlate(a_arr, d_arr, mode="full")
    lag_idx = int(np.argmax(corr) - (count - 1))
    dt = (times[-1] - times[0]) / max(count - 1, 1)
    return {"available": True, "lag_s": float(lag_idx * dt), "correlation_peak": float(np.max(corr) / (np.linalg.norm(a_arr) * np.linalg.norm(d_arr)))}


def same_sign(a: float, b: float) -> bool:
    return (a >= 0 and b >= 0) or (a < 0 and b < 0)


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
    return {
        "available": rc.get("available"),
        "hands_off_confidence": rc.get("hands_off_confidence"),
        "centered_percent": rc.get("centered_percent"),
        "rc_centered_windows": rc.get("rc_centered_windows"),
        "warnings": rc.get("warnings"),
        "axes": axes,
    }


def make_plots(tables: dict[str, Any], plots_dir: Path, step: str) -> list[str]:
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except Exception:
        return []
    out = ensure_dir(plots_dir)
    plots: list[str] = []

    rate = tables.get("RATE")
    if rate is not None and "TimeS" in rate.columns:
        fig = make_subplots(rows=3, cols=1, shared_xaxes=True, subplot_titles=("Roll rate", "Pitch rate", "Yaw rate"))
        for row_idx, (des, actual, name) in enumerate([("RDes", "R", "Roll"), ("PDes", "P", "Pitch"), ("YDes", "Y", "Yaw")], start=1):
            for field, suffix in ((des, "desired"), (actual, "actual")):
                if field in rate.columns:
                    fig.add_trace(go.Scatter(x=rate["TimeS"], y=rate[field], mode="lines", name=f"{name} {suffix}"), row=row_idx, col=1)
        fig.update_layout(title=f"Methodic {step} RATE tracking", template="plotly_white", hovermode="x unified")
        path = out / "methodic_tune_eval_rate_tracking.html"
        fig.write_html(str(path), include_plotlyjs="cdn")
        plots.append(str(path))

        fig = make_subplots(rows=3, cols=1, shared_xaxes=True, subplot_titles=("ROut", "POut", "YOut"))
        for row_idx, field in enumerate(["ROut", "POut", "YOut"], start=1):
            if field in rate.columns:
                fig.add_trace(go.Scatter(x=rate["TimeS"], y=rate[field], mode="lines", name=f"RATE.{field}"), row=row_idx, col=1)
        fig.update_layout(title=f"Methodic {step} RATE outputs", template="plotly_white", hovermode="x unified")
        path = out / "methodic_tune_eval_rate_outputs.html"
        fig.write_html(str(path), include_plotlyjs="cdn")
        plots.append(str(path))

    att = tables.get("ATT")
    if att is not None and "TimeS" in att.columns:
        fig = make_subplots(rows=3, cols=1, shared_xaxes=True, subplot_titles=("Roll attitude", "Pitch attitude", "Yaw attitude"))
        for row_idx, (des, actual, name) in enumerate([("DesRoll", "Roll", "Roll"), ("DesPitch", "Pitch", "Pitch"), ("DesYaw", "Yaw", "Yaw")], start=1):
            for field, suffix in ((des, "desired"), (actual, "actual")):
                if field in att.columns:
                    fig.add_trace(go.Scatter(x=att["TimeS"], y=att[field], mode="lines", name=f"{name} {suffix}"), row=row_idx, col=1)
        fig.update_layout(title=f"Methodic {step} ATT tracking", template="plotly_white", hovermode="x unified")
        path = out / "methodic_tune_eval_att_tracking.html"
        fig.write_html(str(path), include_plotlyjs="cdn")
        plots.append(str(path))

    for group_name, title in (("PID", "PID terms / Dmod / Flags"), ("RCIN", "RCIN inputs"), ("RCOU", "motor outputs"), ("VIBE", "VIBE / clipping"), ("BAT", "battery power")):
        path = plot_group(tables, group_name, title, out)
        if path:
            plots.append(path)
    return plots


def plot_group(tables: dict[str, Any], group_name: str, title: str, out: Path) -> str | None:
    try:
        import plotly.graph_objects as go
    except Exception:
        return None
    names = ["PIDR", "PIDP", "PIDY"] if group_name == "PID" else [group_name]
    fig = go.Figure()
    found = False
    for name in names:
        df = tables.get(name)
        if df is None or "TimeS" not in getattr(df, "columns", []):
            continue
        cols = list(df.columns)
        if group_name == "PID":
            cols = [c for c in ("P", "I", "D", "FF", "DFF", "Dmod", "Flags") if c in df.columns]
        elif group_name == "RCIN":
            cols = [c for c in df.columns if str(c).startswith("C") and str(c)[1:].isdigit()]
        elif group_name == "RCOU":
            cols = [c for c in df.columns if str(c).startswith("C") and str(c)[1:].isdigit()]
        elif group_name == "VIBE":
            cols = [c for c in ["VibeX", "VibeY", "VibeZ", *clip_columns(df)] if c in df.columns]
        elif group_name == "BAT":
            cols = [c for c in ("Volt", "VoltR", "Curr") if c in df.columns]
        for col in cols:
            y = numeric_series(df, [col])
            if y is None:
                continue
            fig.add_trace(go.Scatter(x=df["TimeS"], y=y, mode="lines", name=f"{name}.{col}"))
            found = True
    if not found:
        return None
    fig.update_layout(title=f"Methodic tune evaluation {title}", template="plotly_white", hovermode="x unified")
    path = out / f"methodic_tune_eval_{group_name.lower()}.html"
    fig.write_html(str(path), include_plotlyjs="cdn")
    return str(path)


def write_summary(path: Path, result: dict[str, Any]) -> None:
    lines = [
        f"# Methodic {result.get('methodic_step')}: {result.get('title')}",
        "",
        f"- Result: `{result.get('result')}`",
        f"- Safety gate: `{result.get('safety_gate')}`",
        f"- Official reference: {(result.get('official_reference') or {}).get('url')}",
        "",
        "## Findings",
    ]
    findings = result.get("findings") or []
    if findings:
        lines.extend(f"- {item.get('severity', 'info')}: {item.get('finding')}" for item in findings)
    else:
        lines.append("- No findings reported by the script.")
    lines.extend(["", "## Recommended Next Steps"])
    lines.extend(f"- {item}" for item in result.get("recommended_next_steps", []))
    lines.extend(["", "## What Not To Do"])
    lines.extend(f"- {item}" for item in result.get("what_not_to_do", []))
    ensure_dir(path.parent)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Gather deterministic Methodic tune-evaluation evidence for steps 9.3, 9.4, and 9.6.")
    parser.add_argument("logs", nargs="+", help="One log, or BEFORE AFTER when --compare is used")
    parser.add_argument("--step", default="9.3", choices=["9.3", "9.4", "9.6"], help="Methodic tune-evaluation step")
    parser.add_argument("--compare", action="store_true", help="Compare two logs instead of evaluating one log")
    parser.add_argument("--out", default=None, help="Write structured JSON result")
    parser.add_argument("--summary", default=None, help="Write Markdown summary")
    parser.add_argument("--plots", default=None, help="Directory for generated plots")
    args = parser.parse_args()

    try:
        if args.compare:
            if len(args.logs) != 2:
                raise AnalysisError("--compare requires exactly two logs: BEFORE.BIN AFTER.BIN")
            result = compare_tune_logs(args.logs[0], args.logs[1], plots_dir=args.plots)
        else:
            if len(args.logs) != 1:
                raise AnalysisError("Single-log tune evaluation requires exactly one log unless --compare is used.")
            result = analyze_tune_eval(args.logs[0], step=args.step, plots_dir=args.plots)
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
