#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from statistics import mean
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ap_common import (
    AnalysisError,
    collect_dataflash,
    clip_columns,
    ensure_dir,
    get_col,
    numeric_series,
    safe_float,
    write_json,
)
from ap_methodic_rc import analyze_rc_input_contamination
from ap_methodic_windows import select_methodic_window
from ap_modes import mode_matches

METHODIC_7_1_URL = "https://ardupilot.github.io/MethodicConfigurator/TUNING_GUIDE_ArduCopter#71-first-flight"
REQUIRED_MANUAL_OBSERVATIONS = [
    "visual stability",
    "hard-to-control behaviour",
    "audible oscillation",
    "motor temperature after landing",
    "ESC temperature after landing",
]
MESSAGES = [
    "ARM",
    "MODE",
    "CTUN",
    "BARO",
    "GPS",
    "ATT",
    "RCIN",
    "RCOU",
    "RCO2",
    "RCO3",
    "VIBE",
    "BAT",
    "POWR",
    "ERR",
    "EV",
    "MSG",
    "PARM",
]


def rows_to_tables(rows_by_message: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    try:
        import pandas as pd
    except Exception as exc:
        raise AnalysisError("pandas is required for Methodic first-flight analysis. Install dependencies with: pip install -r requirements.txt") from exc
    return {name: pd.DataFrame(rows) for name, rows in rows_by_message.items() if rows}


def analyze_first_flight(log_path: str | Path, *, plots_dir: str | Path | None = None) -> dict[str, Any]:
    rows, index, stats = collect_dataflash(log_path, include=MESSAGES, source=str(log_path))
    tables = rows_to_tables(rows)
    params = index.get("parameters", {}) or {}

    result = {
        "methodic_step": "7.1",
        "title": "First flight validation",
        "official_reference": METHODIC_7_1_URL,
        "result": "inconclusive",
        "safety_gate": "repeat_step",
        "first_flight_window": {},
        "hover_quality": {},
        "safety_findings": [],
        "missing_evidence": [],
        "manual_observations_required": REQUIRED_MANUAL_OBSERVATIONS,
        "next_step": None,
        "recommended_next_steps": [],
        "detected": {},
        "analysis": {},
        "plots": [],
        "confidence_limits": [],
        "what_not_to_do": [
            "Do not declare the aircraft safe to fly from this analysis.",
            "Do not skip Methodic step 7.1.1.",
            "Do not make blind tuning changes from first-flight evidence.",
        ],
    }

    detections = detect_first_flight_events(tables, index)
    result["detected"] = detections
    result["missing_evidence"].extend(missing_core_evidence(tables))

    try:
        hover_window = select_methodic_window(tables, "methodic_hover", min_duration_s=5.0)
    except Exception as exc:
        hover_window = {"selected_window": None, "candidate_windows": [], "warnings": [str(exc)], "confidence": "low"}
    detections["hover_like_segment_exists"] = bool(hover_window.get("selected_window"))
    result["detected"] = detections
    result["first_flight_window"] = {
        "airborne": first_airborne_window(tables),
        "hover_selector": hover_window,
    }
    result["hover_quality"] = analyze_hover_quality(tables, hover_window.get("selected_window"), params)
    result["analysis"]["rc_input_contamination"] = analyze_rc_input_contamination(tables, params)
    result["analysis"]["vibration"] = analyze_vibration(tables, hover_window.get("selected_window"))
    result["analysis"]["battery_power"] = analyze_battery_power(tables, hover_window.get("selected_window"))
    result["analysis"]["motor_output_headroom"] = analyze_motor_output_headroom(tables, hover_window.get("selected_window"))
    result["analysis"]["logging_health"] = index.get("logging_health", {})
    result["analysis"]["parser_stats"] = stats

    result["safety_findings"].extend(classify_detection_findings(detections, tables))
    result["safety_findings"].extend(classify_hover_findings(result["hover_quality"]))
    result["safety_findings"].extend(classify_rc_findings(result["analysis"]["rc_input_contamination"]))
    result["safety_findings"].extend(classify_vibration_findings(result["analysis"]["vibration"]))
    result["safety_findings"].extend(classify_power_findings(result["analysis"]["battery_power"]))
    result["safety_findings"].extend(classify_motor_output_findings(result["analysis"]["motor_output_headroom"]))
    result["safety_findings"].extend(classify_logging_findings(result["analysis"]["logging_health"]))

    if not detections.get("vehicle_armed"):
        result["missing_evidence"].append("ARM evidence does not show the vehicle armed.")
    if not detections.get("takeoff_occurred"):
        result["missing_evidence"].append("No takeoff/airborne evidence was detected.")
    if not detections.get("althold_segment_exists"):
        result["missing_evidence"].append("No AltHold segment was detected.")
    if not detections.get("hover_like_segment_exists"):
        result["missing_evidence"].append("No usable hover-like segment was detected.")
    if not detections.get("landing_or_disarm_occurred"):
        result["missing_evidence"].append("Landing/disarm evidence was not detected.")

    result["result"], result["safety_gate"] = classify_result(result)
    result["next_step"] = "7.1.1" if result["result"] in {"pass", "conditional_pass"} else "repeat_7.1"
    result["recommended_next_steps"] = recommended_steps(result)

    if plots_dir:
        result["plots"] = make_plots(tables, Path(plots_dir), result)
    return result


def detect_first_flight_events(tables: dict[str, Any], index: dict[str, Any]) -> dict[str, Any]:
    armed = bool(index.get("logging_health", {}).get("first_armed_time_s") is not None)
    althold = any_mode(tables, "ALTHOLD")
    landing_disarm = detect_landing_or_disarm(tables)
    takeoff = bool(first_airborne_window(tables).get("start_s") is not None)
    failsafe_events = event_rows(tables, words=("failsafe", "fail safe", "ekf failsafe", "battery failsafe", "radio failsafe"))
    err_rows = simple_rows(tables.get("ERR"), limit=50)
    ev_rows = simple_rows(tables.get("EV"), limit=50)
    return {
        "vehicle_armed": armed,
        "first_armed_time_s": index.get("logging_health", {}).get("first_armed_time_s"),
        "takeoff_occurred": takeoff,
        "althold_segment_exists": althold,
        "hover_like_segment_exists": False,
        "landing_or_disarm_occurred": landing_disarm,
        "err_count": len(tables.get("ERR", [])) if tables.get("ERR") is not None else 0,
        "ev_count": len(tables.get("EV", [])) if tables.get("EV") is not None else 0,
        "failsafe_events": failsafe_events,
        "err_rows": err_rows,
        "ev_rows": ev_rows,
    }


def any_mode(tables: dict[str, Any], wanted: str) -> bool:
    df = tables.get("MODE")
    if df is None:
        return False
    for row in df.to_dict(orient="records"):
        candidates = [row.get(c) for c in ("Mode", "Name", "ModeNum") if c in row]
        if any(mode_matches(candidate, wanted) or str(candidate).upper().replace("_", "") == wanted for candidate in candidates):
            return True
    return False


def detect_landing_or_disarm(tables: dict[str, Any]) -> bool:
    arm = tables.get("ARM")
    if arm is not None:
        for row in arm.to_dict(orient="records"):
            text = " ".join(str(v).lower() for v in row.values())
            if "disarm" in text or "false" in text or " 0 " in f" {text} ":
                return True
    return any_mode(tables, "LAND") or bool(event_rows(tables, words=("land", "disarm")))


def first_airborne_window(tables: dict[str, Any]) -> dict[str, Any]:
    ctun = tables.get("CTUN")
    if ctun is None or "TimeS" not in getattr(ctun, "columns", []):
        return {}
    alt_col = get_col(ctun, ["Alt", "BAlt"])
    if not alt_col:
        return {}
    rows = ctun[["TimeS", alt_col]].dropna().sort_values("TimeS").to_dict(orient="records")
    if not rows:
        return {}
    base = min(safe_float(row.get(alt_col), 0.0) for row in rows[: max(3, min(30, len(rows)))])
    airborne = [(safe_float(row.get("TimeS")), safe_float(row.get(alt_col))) for row in rows if safe_float(row.get(alt_col)) is not None and safe_float(row.get(alt_col)) >= base + 0.4]
    if not airborne:
        return {}
    return {"start_s": airborne[0][0], "end_s": airborne[-1][0], "altitude_source": f"CTUN.{alt_col}", "baseline_alt_m": base}


def analyze_hover_quality(tables: dict[str, Any], window: dict[str, Any] | None, params: dict[str, Any]) -> dict[str, Any]:
    if not window:
        return {"available": False, "reason": "No usable Methodic hover window was selected."}
    ctun = slice_table(tables.get("CTUN"), window)
    if ctun is None or len(ctun) == 0:
        return {"available": False, "reason": "CTUN was unavailable inside selected hover window.", "window": window}
    alt_col = get_col(ctun, ["Alt", "BAlt"])
    tho_col = get_col(ctun, ["ThO"])
    thh_col = get_col(ctun, ["ThH"])
    alt_values = series_values(ctun, alt_col)
    tho_values = series_values(ctun, tho_col)
    thh_values = series_values(ctun, thh_col)
    hover = {
        "available": True,
        "window": window,
        "duration_s": window.get("duration_s") or (window.get("end_s", 0) - window.get("start_s", 0)),
        "altitude": summarize_values(alt_values, unit="m"),
        "throttle_output": summarize_values(tho_values, unit="normalized") if tho_values else None,
        "throttle_hover": summarize_values(thh_values, unit="normalized") if thh_values else None,
        "mot_thst_hover": safe_float(params.get("MOT_THST_HOVER")),
    }
    if alt_values:
        hover["altitude_stability"] = {
            "span_m": float(max(alt_values) - min(alt_values)),
            "mean_m": float(mean(alt_values)),
        }
    if tho_values and hover["mot_thst_hover"] is not None:
        hover["mot_thst_hover_delta_from_mean_tho"] = float(mean(tho_values) - hover["mot_thst_hover"])
    return hover


def analyze_vibration(tables: dict[str, Any], window: dict[str, Any] | None) -> dict[str, Any]:
    vibe = slice_table(tables.get("VIBE"), window) if window else tables.get("VIBE")
    if vibe is None or len(vibe) == 0:
        return {"available": False, "warning": "VIBE missing; vibration and clipping cannot be assessed."}
    out = {"available": True}
    max_axis = []
    for col in ("VibeX", "VibeY", "VibeZ"):
        vals = series_values(vibe, col)
        if vals:
            out[col] = summarize_values(vals, unit="m/s/s")
            max_axis.extend(abs(v) for v in vals)
    out["max_axis"] = max(max_axis) if max_axis else None
    out["p95_axis"] = percentile(max_axis, 95) if max_axis else None
    clip_delta = {}
    for col in clip_columns(vibe):
        vals = series_values(vibe, col)
        if len(vals) > 1:
            clip_delta[col] = float(max(vals) - min(vals))
    out["clip_delta"] = clip_delta
    return out


def analyze_battery_power(tables: dict[str, Any], window: dict[str, Any] | None) -> dict[str, Any]:
    bat = slice_table(tables.get("BAT"), window) if window else tables.get("BAT")
    powr = slice_table(tables.get("POWR"), window) if window else tables.get("POWR")
    out = {"available": bat is not None or powr is not None}
    if bat is not None and len(bat):
        for label, cols in [("voltage", ["Volt", "VoltR", "V"]), ("current", ["Curr", "I"])]:
            col = get_col(bat, cols)
            vals = series_values(bat, col)
            if vals:
                out[label] = summarize_values(vals, unit="V" if label == "voltage" else "A")
    if powr is not None and len(powr):
        col = get_col(powr, ["Vcc", "VccMin"])
        vals = series_values(powr, col)
        if vals:
            out["vcc"] = summarize_values(vals, unit="V")
    if not out["available"]:
        out["warning"] = "BAT/POWR missing; battery and board power cannot be assessed."
    return out


def analyze_motor_output_headroom(tables: dict[str, Any], window: dict[str, Any] | None) -> dict[str, Any]:
    outputs = {}
    for name in ("RCOU", "RCO2", "RCO3"):
        df = slice_table(tables.get(name), window) if window else tables.get(name)
        if df is None or len(df) == 0:
            continue
        channel_cols = [c for c in df.columns if str(c).startswith("C") and str(c)[1:].isdigit()]
        for col in channel_cols:
            vals = series_values(df, col)
            if not vals:
                continue
            outputs[f"{name}.{col}"] = {
                "min": min(vals),
                "max": max(vals),
                "mean": mean(vals),
                "pct_low_le_1100": 100.0 * sum(1 for v in vals if v <= 1100) / len(vals),
                "pct_high_ge_1900": 100.0 * sum(1 for v in vals if v >= 1900) / len(vals),
                "unit": "PWM us",
            }
    return {"available": bool(outputs), "channels": outputs, "warning": None if outputs else "RCOU/RCO2/RCO3 missing; motor output headroom cannot be assessed."}


def classify_result(result: dict[str, Any]) -> tuple[str, str]:
    severities = [f.get("severity") for f in result["safety_findings"]]
    if any(f.get("safety_gate") == "bench_check_required" for f in result["safety_findings"]):
        return "fail", "bench_check_required"
    if "fail" in severities:
        return "fail", "do_not_proceed"
    detections = result["detected"]
    if not detections.get("althold_segment_exists") or not detections.get("hover_like_segment_exists"):
        return "inconclusive", "repeat_step"
    if result["missing_evidence"]:
        return "conditional_pass", "proceed_with_caution"
    if "conditional" in severities:
        return "conditional_pass", "proceed_with_caution"
    return "pass", "proceed"


def classify_detection_findings(detections: dict[str, Any], tables: dict[str, Any]) -> list[dict[str, Any]]:
    findings = []
    if detections.get("failsafe_events"):
        findings.append({"severity": "fail", "safety_gate": "do_not_proceed", "finding": "Failsafe text/event was detected during the first-flight log.", "evidence": detections["failsafe_events"][:10]})
    if detections.get("err_count", 0) > 0:
        findings.append({"severity": "conditional", "finding": "ERR messages are present and must be decoded before accepting the first-flight step.", "evidence": detections.get("err_rows", [])[:10]})
    return findings


def classify_hover_findings(hover: dict[str, Any]) -> list[dict[str, Any]]:
    if not hover.get("available"):
        return [{"severity": "inconclusive", "finding": hover.get("reason", "Hover quality could not be assessed.")}]
    findings = []
    span = (hover.get("altitude_stability") or {}).get("span_m")
    duration = hover.get("duration_s")
    if duration is not None and duration < 20.0:
        findings.append({"severity": "conditional", "finding": f"Hover-like segment is short for Methodic first-flight review ({duration:.1f}s)."})
    if span is not None and span > 2.0:
        findings.append({"severity": "fail", "safety_gate": "do_not_proceed", "finding": f"Altitude varied excessively in hover window ({span:.2f} m span)."})
    elif span is not None and span > 0.75:
        findings.append({"severity": "conditional", "finding": f"Altitude stability is marginal in hover window ({span:.2f} m span)."})
    return findings


def classify_rc_findings(rc: dict[str, Any]) -> list[dict[str, Any]]:
    if not rc.get("available"):
        return [{"severity": "conditional", "finding": "RCIN missing; pilot stick contamination cannot be ruled out."}]
    if rc.get("hands_off_confidence") == "low":
        return [{"severity": "conditional", "finding": "RC input contamination is high enough to limit first-flight hover conclusions."}]
    return []


def classify_vibration_findings(vibration: dict[str, Any]) -> list[dict[str, Any]]:
    if not vibration.get("available"):
        return [{"severity": "conditional", "finding": vibration.get("warning", "VIBE missing.")}]
    findings = []
    max_axis = vibration.get("max_axis") or 0.0
    clip_delta = vibration.get("clip_delta") or {}
    if max_axis > 60.0 or any(delta > 0 for delta in clip_delta.values()):
        findings.append({"severity": "fail", "safety_gate": "bench_check_required", "finding": "Severe vibration or clipping was detected in the first-flight window.", "evidence": {"max_axis": max_axis, "clip_delta": clip_delta}})
    elif max_axis > 30.0:
        findings.append({"severity": "conditional", "finding": f"Vibration is in a warning range (max axis {max_axis:.1f} m/s/s)."})
    return findings


def classify_power_findings(power: dict[str, Any]) -> list[dict[str, Any]]:
    if not power.get("available"):
        return [{"severity": "conditional", "finding": power.get("warning", "Power evidence missing.")}]
    findings = []
    voltage = power.get("voltage") or {}
    if voltage.get("min") is not None and voltage.get("mean") and voltage["min"] < voltage["mean"] * 0.75:
        findings.append({"severity": "fail", "safety_gate": "bench_check_required", "finding": "Major battery voltage sag was detected.", "evidence": voltage})
    vcc = power.get("vcc") or {}
    if vcc.get("min") is not None and vcc["min"] < 4.7:
        findings.append({"severity": "fail", "safety_gate": "bench_check_required", "finding": "Board power Vcc dropped below 4.7 V.", "evidence": vcc})
    return findings


def classify_motor_output_findings(headroom: dict[str, Any]) -> list[dict[str, Any]]:
    if not headroom.get("available"):
        return [{"severity": "conditional", "finding": headroom.get("warning", "Motor output evidence missing.")}]
    for channel, summary in headroom.get("channels", {}).items():
        if summary.get("pct_high_ge_1900", 0.0) > 5.0 or summary.get("pct_low_le_1100", 0.0) > 10.0:
            return [{"severity": "fail", "safety_gate": "bench_check_required", "finding": f"Motor output saturation/headroom issue detected on {channel}.", "evidence": summary}]
    return []


def classify_logging_findings(logging_health: dict[str, Any]) -> list[dict[str, Any]]:
    if logging_health.get("limits_diagnosis"):
        return [{"severity": "conditional", "finding": "Logging health limits first-flight confidence.", "evidence": logging_health.get("confidence_impact")}]
    return []


def recommended_steps(result: dict[str, Any]) -> list[str]:
    if result["result"] == "fail":
        return [
            "Do not assess Methodic step 7.1.1 yet; resolve the safety finding first.",
            "Perform the indicated bench/hardware/configuration checks before repeating first-flight evidence collection.",
            "Repeat Methodic step 7.1 after the blocker is addressed.",
        ]
    if result["result"] == "inconclusive":
        return [
            "Repeat or re-collect the first-flight evidence with a usable AltHold hover window before assessing 7.1.1.",
            "Collect missing log messages and the required manual observations.",
        ]
    if result["result"] == "conditional_pass":
        return [
            "The first-flight evidence can gate a cautious 7.1.1 assessment only with the listed confidence limits.",
            "Resolve missing evidence, RC contamination, vibration grey-zone, or logging-health caveats before treating the step as complete.",
        ]
    return [
        "Proceed to Methodic step 7.1.1 assessment; this is not a declaration that the aircraft is safe to fly.",
        "Keep manual observations with the evidence record before final conclusions.",
    ]


def missing_core_evidence(tables: dict[str, Any]) -> list[str]:
    missing = []
    for name in ("ARM", "MODE", "CTUN", "ATT", "RCOU"):
        if name not in tables:
            missing.append(f"Missing {name}; first-flight validation confidence is reduced.")
    return missing


def event_rows(tables: dict[str, Any], words: tuple[str, ...]) -> list[dict[str, Any]]:
    matches = []
    for name in ("MSG", "EV", "ERR"):
        df = tables.get(name)
        if df is None:
            continue
        for row in df.to_dict(orient="records"):
            text = " ".join(str(v).lower() for v in row.values())
            if any(word in text for word in words):
                matches.append({"message": name, **row})
    return matches


def simple_rows(df: Any, limit: int = 20) -> list[dict[str, Any]]:
    if df is None:
        return []
    return df.head(limit).to_dict(orient="records")


def slice_table(df: Any, window: dict[str, Any] | None):
    if df is None or not window or "TimeS" not in getattr(df, "columns", []):
        return df
    start = window.get("start_s")
    end = window.get("end_s")
    if start is None or end is None:
        return df
    return df[(df["TimeS"] >= start) & (df["TimeS"] <= end)]


def series_values(df: Any, col: str | None) -> list[float]:
    if df is None or not col:
        return []
    s = numeric_series(df, [col])
    if s is None:
        return []
    return [float(v) for v in s.dropna().tolist()]


def summarize_values(values: list[float], unit: str) -> dict[str, Any]:
    if not values:
        return {}
    return {"min": min(values), "max": max(values), "mean": mean(values), "unit": unit}


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * pct / 100.0))))
    return float(ordered[idx])


def make_plots(tables: dict[str, Any], plots_dir: Path, result: dict[str, Any]) -> list[str]:
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except Exception:
        return []
    out = ensure_dir(plots_dir)
    plots = []
    window = (result.get("first_flight_window") or {}).get("airborne") or {}

    ctun = tables.get("CTUN")
    if ctun is not None and "TimeS" in ctun.columns:
        fig = make_subplots(rows=2, cols=1, shared_xaxes=True, subplot_titles=("Altitude", "Throttle"))
        for col in ["Alt", "BAlt", "DAlt"]:
            if col in ctun.columns:
                fig.add_trace(go.Scatter(x=ctun["TimeS"], y=ctun[col], mode="lines", name=f"CTUN.{col}"), row=1, col=1)
        for col in ["ThO", "ThH"]:
            if col in ctun.columns:
                fig.add_trace(go.Scatter(x=ctun["TimeS"], y=ctun[col], mode="lines", name=f"CTUN.{col}"), row=2, col=1)
        add_window_shapes(fig, window)
        fig.update_layout(title="Methodic 7.1 altitude and throttle", template="plotly_white", hovermode="x unified")
        path = out / "altitude_throttle.html"
        fig.write_html(str(path), include_plotlyjs="cdn")
        plots.append(str(path))

    mode = tables.get("MODE")
    if mode is not None and "TimeS" in mode.columns:
        fig = go.Figure()
        label_col = get_col(mode, ["Mode", "Name", "ModeNum"])
        y = [str(v) for v in mode[label_col].tolist()] if label_col else ["MODE"] * len(mode)
        fig.add_trace(go.Scatter(x=mode["TimeS"], y=y, mode="markers+lines", name="MODE"))
        add_window_shapes(fig, window)
        fig.update_layout(title="Methodic 7.1 mode timeline", template="plotly_white")
        path = out / "mode_timeline.html"
        fig.write_html(str(path), include_plotlyjs="cdn")
        plots.append(str(path))

    for name, title in [("RCIN", "RC input"), ("VIBE", "Vibration"), ("RCOU", "Motor outputs")]:
        df = tables.get(name)
        if df is None or "TimeS" not in getattr(df, "columns", []):
            continue
        fig = go.Figure()
        cols = [c for c in df.columns if c != "TimeS" and (name != "VIBE" or c in {"VibeX", "VibeY", "VibeZ", *clip_columns(df)})]
        for col in cols[:16]:
            vals = series_values(df, col)
            if vals:
                fig.add_trace(go.Scatter(x=df["TimeS"], y=df[col], mode="lines", name=f"{name}.{col}"))
        add_window_shapes(fig, window)
        fig.update_layout(title=f"Methodic 7.1 {title}", template="plotly_white", hovermode="x unified")
        path = out / f"{name.lower()}_{title.lower().replace(' ', '_')}.html"
        fig.write_html(str(path), include_plotlyjs="cdn")
        plots.append(str(path))
    return plots


def add_window_shapes(fig: Any, window: dict[str, Any]) -> None:
    if not window or window.get("start_s") is None or window.get("end_s") is None:
        return
    fig.add_vrect(x0=window["start_s"], x1=window["end_s"], fillcolor="#dbeafe", opacity=0.22, line_width=0)


def write_summary(path: Path, result: dict[str, Any]) -> None:
    lines = [
        "# Methodic 7.1 First Flight Validation",
        "",
        f"- Result: `{result['result']}`",
        f"- Safety gate: `{result['safety_gate']}`",
        f"- Next step: `{result['next_step']}`",
        f"- Official reference: {result['official_reference']}",
        "",
        "## Safety Findings",
    ]
    lines.extend(f"- {item.get('severity', 'info')}: {item.get('finding')}" for item in result["safety_findings"]) if result["safety_findings"] else lines.append("- None reported by deterministic checks.")
    lines.extend(["", "## Missing Evidence"])
    lines.extend(f"- {item}" for item in result["missing_evidence"]) if result["missing_evidence"] else lines.append("- None reported by deterministic checks.")
    lines.extend(["", "## Recommended Next Steps"])
    lines.extend(f"- {item}" for item in result["recommended_next_steps"])
    lines.extend(["", "## Manual Observations Required"])
    lines.extend(f"- {item}" for item in result["manual_observations_required"])
    ensure_dir(path.parent)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Gather deterministic Methodic 7.1 first-flight evidence.")
    parser.add_argument("log")
    parser.add_argument("--out", default="out/methodic_7_1.json")
    parser.add_argument("--summary", default=None)
    parser.add_argument("--plots", default=None)
    args = parser.parse_args()
    try:
        result = analyze_first_flight(args.log, plots_dir=args.plots)
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
