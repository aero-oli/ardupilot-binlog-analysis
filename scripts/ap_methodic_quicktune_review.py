#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from statistics import mean
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ap_common import AnalysisError, clip_columns, collect_dataflash, ensure_dir, numeric_series, rows_to_dataframe, safe_float, write_json
from ap_methodic_oscillation import classify_oscillation
from ap_methodic_rc import analyze_rc_input_contamination

METHODIC_8_5_URL = "https://ardupilot.github.io/MethodicConfigurator/TUNING_GUIDE_ArduCopter#85-second-flight-pid-vtol-quiktune-lua-script-or-manual-pid-tune"
METHODIC_9_2_URL = "https://ardupilot.github.io/MethodicConfigurator/TUNING_GUIDE_ArduCopter#92-fourth-flight-pid-vtol-quiktune-lua-script-or-manual-pid-tune-optional"
QUIKTUNE_URL = "https://ardupilot.org/copter/docs/quiktune.html"
LOG_MESSAGES_URL = "https://ardupilot.org/copter/docs/logmessages.html"
MESSAGES = [
    "ATT",
    "ANG",
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
    "PARM",
    "RCIN",
    "ARM",
]
PARAMETERS = [
    "ATC_RAT_RLL_P",
    "ATC_RAT_RLL_I",
    "ATC_RAT_RLL_D",
    "ATC_RAT_RLL_FF",
    "ATC_RAT_PIT_P",
    "ATC_RAT_PIT_I",
    "ATC_RAT_PIT_D",
    "ATC_RAT_PIT_FF",
    "ATC_RAT_YAW_P",
    "ATC_RAT_YAW_I",
    "ATC_RAT_YAW_D",
    "ATC_RAT_YAW_FF",
    "ATC_ANG_RLL_P",
    "ATC_ANG_PIT_P",
    "ATC_ANG_YAW_P",
    "ATC_ACCEL_R_MAX",
    "ATC_ACCEL_P_MAX",
    "ATC_ACCEL_Y_MAX",
    "INS_HNTCH_*",
    "INS_GYRO_FILTER",
]


def rows_to_tables(rows_by_message: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    return {name: rows_to_dataframe(rows) for name, rows in rows_by_message.items() if rows}


def analyze_quicktune_review(
    log_path: str | Path,
    *,
    before_params: str | Path | None = None,
    after_params: str | Path | None = None,
    plots_dir: str | Path | None = None,
    methodic_step: str = "8.5",
) -> dict[str, Any]:
    rows, index, stats = collect_dataflash(log_path, include=MESSAGES, source=str(log_path))
    tables = rows_to_tables(rows)
    params = index.get("parameters", {}) or {}
    msg = analyze_messages(tables)
    external_changes = compare_external_params(before_params, after_params)
    parm_history = analyze_parm_history(tables)
    parameters_changed = merge_parameter_changes(external_changes, parm_history)
    split = choose_split_time(tables, msg, parm_history)
    tracking = analyze_tracking(tables, split)
    pid = analyze_pid_evidence(tables)
    post_health = analyze_post_tune_health(tables, split)
    rc = analyze_rc_input_contamination(tables, params)
    completion = completion_status(msg, parameters_changed)
    detected = bool(msg["quiktune_messages"] or external_changes.get("available") or parm_history.get("tuning_parameter_changes"))

    result = empty_result(params, methodic_step)
    result["analysis_window"]["parser_stats"] = stats
    result["analysis_window"]["split_time_s"] = split
    result["quicktune_detected"] = detected
    result["completion_status"] = completion
    result["parameters_changed"] = parameters_changed
    result["post_tune_health"] = post_health
    result["evidence_used"] = [
        {"type": "messages_present", "messages": sorted(tables.keys())},
        {"type": "quiktune_messages", "value": msg},
        {"type": "external_parameter_comparison", "value": external_changes},
        {"type": "parm_history", "value": parm_history},
        {"type": "tracking_comparison", "value": tracking},
        {"type": "pid_evidence", "value": pid},
        {"type": "post_tune_health", "value": post_health},
        {"type": "rc_input_contamination", "value": trim_rc(rc)},
    ]
    result["missing_evidence"] = missing_evidence(tables, before_params, after_params, pid)
    result["findings"] = classify_findings(detected, completion, parameters_changed, tracking, pid, post_health, rc)
    result["checked_but_not_supported"] = checked_but_not_supported(tables, external_changes, parm_history)
    result["result"], result["safety_gate"] = classify_result(result["findings"], detected, completion, parameters_changed, tracking, pid)
    result["next_step"] = next_step(methodic_step, result["result"])
    result["next_methodic_step"] = result["next_step"]
    result["recommended_next_steps"] = recommended_next_steps(result, tracking, post_health, completion)
    result["what_not_to_do"] = [
        "Do not automatically write or upload gains from this review.",
        "Do not proceed to AutoTune if QuikTune/manual tune did not produce stable post-tune tracking.",
        "Do not increase gains when vibration, noise, output saturation, or power sag evidence exists.",
        "If new oscillation appears, review, revert or reduce as appropriate, then retest with a short controlled log.",
        "Do not treat missing QuikTune messages as failure when before/after parameter files support manual review mode.",
    ]
    result["confidence_limits"] = confidence_limits(result["missing_evidence"], rc, msg, parameters_changed)
    if plots_dir:
        result["plots"] = make_plots(tables, Path(plots_dir), split)
    return result


def empty_result(params: dict[str, Any], methodic_step: str) -> dict[str, Any]:
    title = "QuikTune/manual PID review" if methodic_step == "8.5" else "QuikTune standard setup/results"
    official_url = METHODIC_8_5_URL if methodic_step == "8.5" else METHODIC_9_2_URL
    return {
        "methodic_step": methodic_step,
        "title": title,
        "official_reference": {"url": official_url, "supporting_urls": [QUIKTUNE_URL, LOG_MESSAGES_URL]},
        "quicktune_detected": False,
        "completion_status": "unknown",
        "parameters_changed": {"available": False, "changes": []},
        "post_tune_health": {},
        "result": "inconclusive",
        "safety_gate": "repeat_step",
        "next_step": None,
        "evidence_used": [],
        "missing_evidence": [],
        "manual_observations_required": ["No dangerous oscillation", "Pilot reports controllable response", "Appropriate conditions for QuikTune/manual tuning"],
        "analysis_window": {"selection": "whole_log", "preferred_window": "QuikTune/manual tuning flight excluding takeoff and landing transients.", "start_s": None, "end_s": None},
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


def parse_param_file(path: str | Path | None) -> dict[str, float]:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        raise AnalysisError(f"Parameter file not found: {p}")
    out = {}
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or text.startswith(";"):
            continue
        text = text.replace(",", " ")
        parts = [part for part in text.split() if part]
        if len(parts) < 2:
            continue
        value = safe_float(parts[1])
        if value is not None:
            out[parts[0]] = value
    return out


def compare_external_params(before_path: str | Path | None, after_path: str | Path | None) -> dict[str, Any]:
    if not before_path and not after_path:
        return {"available": False, "changes": []}
    before = parse_param_file(before_path)
    after = parse_param_file(after_path)
    changes = compare_params(before, after, source="external_param_files")
    return {"available": bool(before and after), "before_count": len(before), "after_count": len(after), "changes": changes}


def compare_params(before: dict[str, float], after: dict[str, float], *, source: str) -> list[dict[str, Any]]:
    changes = []
    for name in sorted(set(before) | set(after)):
        if not relevant_param(name):
            continue
        b = before.get(name)
        a = after.get(name)
        if b is None or a is None or abs(a - b) > 1e-9:
            changes.append({"name": name, "before": b, "after": a, "delta": None if b is None or a is None else a - b, "source": source})
    return changes


def relevant_param(name: str) -> bool:
    return name.startswith(("ATC_RAT_", "ATC_ANG_", "ATC_ACCEL_", "INS_HNTCH_", "INS_GYRO_FILTER"))


def analyze_parm_history(tables: dict[str, Any]) -> dict[str, Any]:
    parm = tables.get("PARM")
    if parm is None or len(parm) == 0:
        return {"available": False, "tuning_parameter_changes": []}
    history: dict[str, list[dict[str, Any]]] = {}
    for row in parm.to_dict(orient="records"):
        name = str(row.get("Name") or row.get("name") or "")
        value = safe_float(row.get("Value", row.get("value")))
        if name and value is not None and relevant_param(name):
            history.setdefault(name, []).append({"time_s": safe_float(row.get("TimeS")), "value": value})
    changes = []
    for name, samples in history.items():
        if len(samples) >= 2 and abs(samples[-1]["value"] - samples[0]["value"]) > 1e-9:
            changes.append({"name": name, "before": samples[0]["value"], "after": samples[-1]["value"], "delta": samples[-1]["value"] - samples[0]["value"], "source": "PARM_history", "time_s": samples[-1]["time_s"]})
    return {"available": bool(history), "tuning_parameter_changes": changes, "history": history}


def merge_parameter_changes(external: dict[str, Any], history: dict[str, Any]) -> dict[str, Any]:
    changes = list(external.get("changes") or [])
    seen = {c["name"] for c in changes}
    for change in history.get("tuning_parameter_changes") or []:
        if change["name"] not in seen:
            changes.append(change)
    return {"available": bool(changes), "changes": sorted(changes, key=lambda c: c["name"]), "external_available": external.get("available"), "parm_history_available": history.get("available")}


def analyze_messages(tables: dict[str, Any]) -> dict[str, Any]:
    msg = tables.get("MSG")
    matches = []
    if msg is not None:
        for row in msg.to_dict(orient="records"):
            text = str(row.get("Message") or row.get("Msg") or row.get("message") or "")
            lower = text.lower()
            if any(token in lower for token in ("quiktune", "quicktune", "quick tune", "qtune", "tune")):
                matches.append({"time_s": safe_float(row.get("TimeS")), "text": text})
    lower_text = " | ".join(item["text"].lower() for item in matches)
    return {
        "quiktune_messages": matches,
        "completed": any(token in lower_text for token in ("complete", "completed", "done")),
        "aborted": any(token in lower_text for token in ("abort", "cancel", "failed", "stopped")),
        "saved": any(token in lower_text for token in ("saved", "accept", "applied")),
        "paused_for_rc": any(token in lower_text for token in ("pause", "paused", "stick", "reposition")),
    }


def completion_status(msg: dict[str, Any], parameters_changed: dict[str, Any]) -> str:
    if msg.get("aborted"):
        return "aborted"
    if msg.get("completed") and (msg.get("saved") or parameters_changed.get("available")):
        return "completed"
    if msg.get("completed"):
        return "partial"
    if parameters_changed.get("available"):
        return "partial"
    return "unknown"


def choose_split_time(tables: dict[str, Any], msg: dict[str, Any], history: dict[str, Any]) -> float | None:
    times = [item.get("time_s") for item in msg.get("quiktune_messages") or [] if item.get("time_s") is not None and any(token in item.get("text", "").lower() for token in ("complete", "saved", "accept", "applied"))]
    if times:
        return min(times)
    change_times = [c.get("time_s") for c in history.get("tuning_parameter_changes") or [] if c.get("time_s") is not None]
    if change_times:
        return min(change_times)
    rate = tables.get("RATE")
    if rate is not None and "TimeS" in rate.columns and len(rate):
        values = [safe_float(v) for v in rate["TimeS"].tolist() if safe_float(v) is not None]
        if values:
            return (min(values) + max(values)) / 2.0
    return None


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
    return {"available": True, "samples": len(values), "mean": mean(values), "p95_abs": percentile([abs(v) for v in values], 95), "max_abs": max(abs(v) for v in values)}


def split_table(df: Any, split: float | None, post: bool):
    if df is None or split is None or "TimeS" not in getattr(df, "columns", []):
        return df
    return df[df["TimeS"] >= split] if post else df[df["TimeS"] < split]


def analyze_tracking(tables: dict[str, Any], split: float | None) -> dict[str, Any]:
    rate = tables.get("RATE")
    if rate is None or len(rate) == 0:
        return {"available": False, "reason": "RATE missing."}
    axes = {}
    for axis, des, actual in [("roll", "RDes", "R"), ("pitch", "PDes", "P"), ("yaw", "YDes", "Y")]:
        before = tracking_error(split_table(rate, split, post=False), des, actual)
        after = tracking_error(split_table(rate, split, post=True), des, actual)
        improvement = None
        if before.get("p95_abs") is not None and after.get("p95_abs") is not None and before["p95_abs"] > 1e-6:
            improvement = 100.0 * (before["p95_abs"] - after["p95_abs"]) / before["p95_abs"]
        axes[axis] = {"before": before, "after": after, "p95_improvement_percent": improvement}
    post_values = [v["after"].get("p95_abs") for v in axes.values() if v["after"].get("p95_abs") is not None]
    improvement_values = [v.get("p95_improvement_percent") for v in axes.values() if v.get("p95_improvement_percent") is not None]
    stable = bool(post_values) and max(post_values) < 25.0
    improved = bool(improvement_values) and sum(improvement_values) / len(improvement_values) >= 10.0
    return {"available": True, "split_time_s": split, "axes": axes, "post_tracking_stable": stable, "improved": improved}


def tracking_error(df: Any, des: str, actual: str) -> dict[str, Any]:
    if df is None or len(df) == 0:
        return {"available": False, "samples": 0}
    desired = series_values(df, des)
    measured = series_values(df, actual)
    err = [d - a for d, a in zip(desired, measured)]
    return summarize(err)


def analyze_pid_evidence(tables: dict[str, Any]) -> dict[str, Any]:
    messages = {}
    for name in ("PIDR", "PIDP", "PIDY"):
        df = tables.get(name)
        if df is None or len(df) == 0:
            continue
        terms = {}
        for col in ("P", "I", "D", "FF", "DFF", "Dmod", "Flags"):
            vals = series_values(df, col)
            if vals:
                terms[col] = summarize(vals)
        messages[name] = {"samples": len(df), "terms": terms}
    return {"available": bool(messages), "messages": messages}


def analyze_post_tune_health(tables: dict[str, Any], split: float | None) -> dict[str, Any]:
    post_rate = split_table(tables.get("RATE"), split, post=True)
    post_vibe = split_table(tables.get("VIBE"), split, post=True)
    post_bat = split_table(tables.get("BAT"), split, post=True)
    post_powr = split_table(tables.get("POWR"), split, post=True)
    post_outputs = {name: split_table(tables.get(name), split, post=True) for name in ("RCOU", "RCO2", "RCO3")}
    oscillation = {}
    if post_rate is not None and len(post_rate):
        times = series_values(post_rate, "TimeS")
        for axis, field in [("roll", "ROut"), ("pitch", "POut"), ("yaw", "YOut")]:
            values = series_values(post_rate, field)
            if values:
                oscillation[axis] = classify_oscillation(values, times[: len(values)], threshold=0.15)
    vibe = analyze_vibration(post_vibe)
    power = analyze_power(post_bat, post_powr)
    saturation = analyze_saturation(post_outputs)
    blockers = []
    if any(item.get("classification") in {"oscillatory", "mixed"} and ((item.get("metrics") or {}).get("p95_abs") or 0.0) > 0.15 for item in oscillation.values()):
        blockers.append("post_tune_rate_output_oscillation")
    if vibe.get("severe"):
        blockers.append("severe_vibration_or_clipping")
    if power.get("fail"):
        blockers.append("power_sag_or_brownout")
    if saturation.get("saturation"):
        blockers.append("motor_output_saturation")
    return {"oscillation": oscillation, "vibration": vibe, "power": power, "motor_outputs": saturation, "blockers": blockers}


def analyze_vibration(vibe: Any) -> dict[str, Any]:
    if vibe is None or len(vibe) == 0:
        return {"available": False, "severe": False}
    max_axis = 0.0
    for col in ("VibeX", "VibeY", "VibeZ"):
        vals = series_values(vibe, col)
        if vals:
            max_axis = max(max_axis, max(abs(v) for v in vals))
    clip_delta = {}
    for col in clip_columns(vibe):
        vals = series_values(vibe, col)
        if len(vals) > 1:
            clip_delta[col] = max(vals) - min(vals)
    return {"available": True, "max_axis": max_axis, "clip_delta": clip_delta, "severe": max_axis > 60.0 or any((v or 0.0) > 0 for v in clip_delta.values())}


def analyze_power(bat: Any, powr: Any) -> dict[str, Any]:
    out = {"available": bat is not None or powr is not None, "fail": False}
    if bat is not None and len(bat):
        volts = series_values(bat, "Volt") or series_values(bat, "VoltR") or series_values(bat, "V")
        out["voltage"] = summarize(volts)
        if volts and min(volts) < mean(volts) * 0.75:
            out["fail"] = True
    if powr is not None and len(powr):
        vcc = series_values(powr, "Vcc") or series_values(powr, "VccMin")
        out["vcc"] = summarize(vcc)
        if vcc and min(vcc) < 4.7:
            out["fail"] = True
    return out


def analyze_saturation(outputs: dict[str, Any]) -> dict[str, Any]:
    channels = {}
    saturation = False
    for name, df in outputs.items():
        if df is None or len(df) == 0:
            continue
        for col in [c for c in df.columns if str(c).startswith("C") and str(c)[1:].isdigit()]:
            vals = series_values(df, col)
            if not vals:
                continue
            high = 100.0 * sum(1 for v in vals if v >= 1900) / len(vals)
            low = 100.0 * sum(1 for v in vals if v <= 1100) / len(vals)
            channels[f"{name}.{col}"] = {"pct_high_ge_1900": high, "pct_low_le_1100": low}
            saturation = saturation or high > 5.0 or low > 10.0
    return {"available": bool(channels), "channels": channels, "saturation": saturation}


def missing_evidence(tables: dict[str, Any], before: Any, after: Any, pid: dict[str, Any]) -> list[str]:
    missing = []
    for name in ("ATT", "RATE", "VIBE", "BAT", "MODE", "MSG"):
        if name not in tables:
            missing.append(f"Missing required/strong evidence: {name}")
    if not any(name in tables for name in ("RCOU", "RCO2", "RCO3")):
        missing.append("Missing required/strong evidence: RCOU/RCO2/RCO3")
    if not pid.get("available"):
        missing.append("Missing PIDR/PIDP/PIDY; PID-term tune review is limited.")
    if "PARM" not in tables and not (before and after):
        missing.append("Missing PARM and external before/after params; parameter changes cannot be confirmed.")
    return missing


def classify_findings(detected: bool, completion: str, params_changed: dict[str, Any], tracking: dict[str, Any], pid: dict[str, Any], health: dict[str, Any], rc: dict[str, Any]) -> list[dict[str, Any]]:
    findings = []
    if not detected:
        findings.append({"severity": "inconclusive", "finding": "No QuikTune messages, PARM history changes, or before/after parameter changes were detected."})
    if completion == "aborted":
        findings.append({"severity": "fail", "finding": "QuikTune appears aborted/cancelled.", "evidence": completion})
    elif completion in {"partial", "unknown"}:
        findings.append({"severity": "conditional", "finding": f"QuikTune/manual tune completion is {completion}.", "evidence": completion})
    if not params_changed.get("available"):
        findings.append({"severity": "conditional", "finding": "No tuning parameter changes were confirmed.", "evidence": params_changed})
    if not tracking.get("available"):
        findings.append({"severity": "inconclusive", "finding": "RATE tracking evidence is unavailable.", "evidence": tracking})
    elif not tracking.get("post_tracking_stable"):
        findings.append({"severity": "fail", "finding": "Post-tune RATE tracking is not stable enough to proceed.", "evidence": tracking})
    elif not tracking.get("improved"):
        findings.append({"severity": "conditional", "finding": "Post-tune tracking did not clearly improve.", "evidence": tracking})
    if not pid.get("available"):
        findings.append({"severity": "conditional", "finding": "PID evidence is missing; tune review confidence is limited."})
    for blocker in health.get("blockers") or []:
        findings.append({"severity": "fail", "finding": f"Post-tune health blocker detected: {blocker}", "evidence": health})
    if rc.get("available") and rc.get("hands_off_confidence") == "low":
        findings.append({"severity": "conditional", "finding": "RC stick movement/repositioning may contaminate before/after tune evidence.", "evidence": trim_rc(rc)})
    return findings


def classify_result(findings: list[dict[str, Any]], detected: bool, completion: str, params_changed: dict[str, Any], tracking: dict[str, Any], pid: dict[str, Any]) -> tuple[str, str]:
    if any(item.get("severity") == "fail" for item in findings):
        return "fail", "do_not_proceed"
    if not detected or not tracking.get("available"):
        return "inconclusive", "repeat_step"
    if not pid.get("available") and not params_changed.get("available"):
        return "inconclusive", "repeat_step"
    if any(item.get("severity") in {"conditional", "inconclusive"} for item in findings):
        return "conditional_pass", "proceed_with_caution"
    return "pass", "proceed"


def next_step(methodic_step: str, result: str) -> str:
    if result == "fail":
        return "repeat_8.5_or_review_revert_reduce" if methodic_step == "8.5" else "repeat_9.2_or_review_revert_reduce"
    if result == "inconclusive":
        return methodic_step
    if methodic_step == "9.2":
        return "9.3"
    return "9.1"


def recommended_next_steps(result: dict[str, Any], tracking: dict[str, Any], health: dict[str, Any], completion: str) -> list[str]:
    if result["result"] == "pass":
        return ["Agent should inspect parameter changes and post-tune plots before accepting the step.", f"If accepted, continue to Methodic step {result['next_step']}."]
    steps = []
    if result["result"] == "fail":
        steps.append("Do not proceed to AutoTune or later tune steps; review/revert/reduce the suspect change and retest with a short controlled log.")
    if health.get("blockers"):
        steps.append("Resolve post-tune oscillation, vibration, saturation, or power blockers before any further tuning.")
    if completion in {"aborted", "partial", "unknown"}:
        steps.append("Confirm whether QuikTune completed and whether gains were saved; use before/after parameter files if log messages are incomplete.")
    if tracking.get("available") and not tracking.get("post_tracking_stable"):
        steps.append("Do not increase gains; post-tune rate tracking is not stable.")
    return steps or ["Resolve listed evidence limits before treating QuikTune/manual tuning as complete."]


def checked_but_not_supported(tables: dict[str, Any], external: dict[str, Any], history: dict[str, Any]) -> list[str]:
    out = []
    if not external.get("available"):
        out.append("External before/after parameter comparison not available")
    if not history.get("tuning_parameter_changes"):
        out.append("PARM history did not contain tuning parameter changes")
    if "MSG" in tables:
        out.append("MSG reviewed for QuikTune/script progress messages")
    return out


def confidence_limits(missing: list[str], rc: dict[str, Any], msg: dict[str, Any], params_changed: dict[str, Any]) -> list[str]:
    limits = list(missing)
    if rc.get("available") and rc.get("hands_off_confidence") == "low":
        limits.append("RC stick movement/repositioning may limit before/after comparison.")
    if not msg.get("quiktune_messages"):
        limits.append("No QuikTune script messages were logged; review may be manual/parameter-file based.")
    if not params_changed.get("available"):
        limits.append("Tuning parameter changes were not confirmed.")
    return dedupe(limits)


def dedupe(items: list[str]) -> list[str]:
    out = []
    seen = set()
    for item in items:
        if item not in seen:
            out.append(item)
            seen.add(item)
    return out


def trim_rc(rc: dict[str, Any]) -> dict[str, Any]:
    return {"available": rc.get("available"), "hands_off_confidence": rc.get("hands_off_confidence"), "centered_percent": rc.get("centered_percent"), "warnings": rc.get("warnings")}


def make_plots(tables: dict[str, Any], plots_dir: Path, split: float | None) -> list[str]:
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except Exception:
        return []
    out = ensure_dir(plots_dir)
    plots = []

    rate = tables.get("RATE")
    if rate is not None and "TimeS" in getattr(rate, "columns", []):
        fig = make_subplots(rows=3, cols=1, shared_xaxes=True, subplot_titles=("Roll", "Pitch", "Yaw"))
        for row, fields in enumerate((("RDes", "R", "ROut"), ("PDes", "P", "POut"), ("YDes", "Y", "YOut")), start=1):
            for field in fields:
                if field in rate.columns:
                    fig.add_trace(go.Scatter(x=rate["TimeS"], y=rate[field], mode="lines", name=f"RATE.{field}"), row=row, col=1)
        add_split(fig, split)
        fig.update_layout(title="Methodic QuikTune RATE tracking and outputs", template="plotly_white", hovermode="x unified")
        plots.append(write_plot(fig, out / "methodic_quicktune_rate_tracking.html"))

    pid_fig = make_subplots(rows=3, cols=1, shared_xaxes=True, subplot_titles=("PIDR", "PIDP", "PIDY"))
    has_pid = False
    for row, name in enumerate(("PIDR", "PIDP", "PIDY"), start=1):
        df = tables.get(name)
        if df is None or "TimeS" not in getattr(df, "columns", []):
            continue
        for col in ("P", "I", "D", "FF", "Dmod", "Flags"):
            if col in df.columns:
                pid_fig.add_trace(go.Scatter(x=df["TimeS"], y=df[col], mode="lines", name=f"{name}.{col}"), row=row, col=1)
                has_pid = True
    if has_pid:
        add_split(pid_fig, split)
        pid_fig.update_layout(title="Methodic QuikTune PID terms", template="plotly_white", hovermode="x unified")
        plots.append(write_plot(pid_fig, out / "methodic_quicktune_pid_terms.html"))

    for message, filename, title in [
        ("VIBE", "methodic_quicktune_vibe.html", "VIBE"),
        ("RCOU", "methodic_quicktune_motor_outputs.html", "Motor outputs"),
    ]:
        fig = go.Figure()
        messages = ["RCOU", "RCO2", "RCO3"] if message == "RCOU" else [message]
        for msg in messages:
            df = tables.get(msg)
            if df is None or "TimeS" not in getattr(df, "columns", []):
                continue
            cols = [c for c in df.columns if c != "TimeS" and (msg != "VIBE" or c in {"VibeX", "VibeY", "VibeZ", *clip_columns(df)})]
            for col in cols[:16]:
                fig.add_trace(go.Scatter(x=df["TimeS"], y=df[col], mode="lines", name=f"{msg}.{col}"))
        if fig.data:
            add_split(fig, split)
            fig.update_layout(title=f"Methodic QuikTune {title}", template="plotly_white", hovermode="x unified")
            plots.append(write_plot(fig, out / filename))

    parm = tables.get("PARM")
    if parm is not None and "TimeS" in getattr(parm, "columns", []):
        changes = []
        for row in parm.to_dict(orient="records"):
            name = str(row.get("Name") or "")
            if relevant_param(name):
                changes.append(row)
        if changes:
            fig = go.Figure()
            for row in changes[:200]:
                fig.add_trace(go.Scatter(x=[row.get("TimeS")], y=[row.get("Value")], mode="markers", name=row.get("Name")))
            fig.update_layout(title="Methodic QuikTune parameter-change timeline", template="plotly_white")
            plots.append(write_plot(fig, out / "methodic_quicktune_parameter_timeline.html"))
    return plots


def add_split(fig: Any, split: float | None) -> None:
    if split is not None:
        fig.add_vline(x=split, line_dash="dash", line_color="#dc2626")


def write_plot(fig: Any, path: Path) -> str:
    fig.write_html(str(path), include_plotlyjs="cdn")
    return str(path)


def write_summary(path: Path, result: dict[str, Any]) -> None:
    lines = [
        f"# Methodic {result['methodic_step']} QuikTune / Manual PID Review",
        "",
        f"- Result: `{result['result']}`",
        f"- Safety gate: `{result['safety_gate']}`",
        f"- QuikTune detected: `{result['quicktune_detected']}`",
        f"- Completion status: `{result['completion_status']}`",
        f"- Next step: `{result['next_step']}`",
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
    parser = argparse.ArgumentParser(description="Gather deterministic Methodic QuikTune/manual PID review evidence.")
    parser.add_argument("log", help="ArduPilot DataFlash .BIN/.log file")
    parser.add_argument("--before-params", default=None)
    parser.add_argument("--after-params", default=None)
    parser.add_argument("--methodic-step", choices=["8.5", "9.2"], default="8.5")
    parser.add_argument("--out", default="out/methodic_8_5.json")
    parser.add_argument("--summary", default=None)
    parser.add_argument("--plots", default=None)
    args = parser.parse_args()
    try:
        result = analyze_quicktune_review(args.log, before_params=args.before_params, after_params=args.after_params, plots_dir=args.plots, methodic_step=args.methodic_step)
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
