#!/usr/bin/env python3
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ap_common import (
    AnalysisError,
    apply_active_flight_filter,
    classify_symptom,
    collect_dataflash,
    combined_rcout_dataframe,
    event_markers_from_tables,
    filter_tables_by_time,
    get_col,
    numeric_series,
    output_channel_columns,
    output_channel_label,
    output_mapping_from_tables,
    percentile,
    rows_to_dataframe,
    safe_float,
    write_json,
)
from ap_modes import decode_copter_mode, mode_label
from ap_evidence_completeness import build_control_evidence_completeness
from ap_param_context import merge_external_parameters, parse_param_file
from ap_parameters import enrich_parameter_entry
from ap_rcin import rc_channel_mapping, rcin_channel_col
from ap_symptom_map import requirement_spec


DEFAULT_COMPARE_MESSAGES = [
    "ATT", "RATE", "PIDR", "PIDP", "PIDY", "RCOU", "RCO2", "RCO3", "MODE", "MSG", "EV", "ERR", "ARM",
    "RCIN", "PARM", "VIBE", "BAT", "POWR", "GPS", "GPS2", "XKF3", "XKF4", "NKF3", "NKF4", "CTUN", "BARO",
]

PURE_MANUAL_ATTITUDE_MODES = {"STABILIZE", "ALTHOLD", "ACRO"}
MANUAL_CONFIDENCE_MODES = {"STABILIZE", "ALTHOLD"}
NAV_ASSISTED_HOLD_MODES = {"POSHOLD", "LOITER"}
POSHOLD_MANUAL_LIMITATION = (
    "POSHOLD/LOITER are not pure manual attitude modes; manual-control conclusions are limited without "
    "STABILIZE, ALTHOLD, or ACRO segments."
)
MISSION_YAW_MODES = {"AUTO", "RTL", "GUIDED", "SMART_RTL"}


def _duration(intervals: Sequence[Dict[str, Any]]) -> float:
    total = 0.0
    for interval in intervals:
        start = safe_float(interval.get("start_s"))
        end = safe_float(interval.get("end_s"))
        if start is not None and end is not None and end >= start:
            total += end - start
    return total


def _abs_values(series) -> List[float]:
    if series is None:
        return []
    try:
        return [abs(float(v)) for v in series.dropna().tolist()]
    except Exception:
        return []


def _angle_error(desired, actual):
    return ((desired - actual + 180.0) % 360.0) - 180.0


def _tracking_stats(df, desired_col, actual_col, *, angle=False):
    if df is None or not hasattr(df, "columns") or desired_col not in df.columns or actual_col not in df.columns:
        return None
    desired = numeric_series(df, [desired_col])
    actual = numeric_series(df, [actual_col])
    if desired is None or actual is None:
        return None
    if angle:
        errors = [_angle_error(float(d), float(a)) for d, a in zip(desired.tolist(), actual.tolist()) if not (math.isnan(float(d)) or math.isnan(float(a)))]
    else:
        errors = [float(d) - float(a) for d, a in zip(desired.tolist(), actual.tolist()) if not (math.isnan(float(d)) or math.isnan(float(a)))]
    abs_errors = [abs(v) for v in errors]
    if not abs_errors:
        return None
    return {
        "samples": len(abs_errors),
        "p95_abs": percentile(abs_errors, 95),
        "max_abs": max(abs_errors),
    }


def _series_abs_stats(df, col):
    if df is None or not hasattr(df, "columns") or col not in df.columns:
        return None
    values = _abs_values(numeric_series(df, [col]))
    if not values:
        return None
    return {"samples": len(values), "p95_abs": percentile(values, 95), "max_abs": max(values)}


def _parameter_value(index, name):
    params = (index or {}).get("parameters") or {}
    return safe_float(params.get(name))


def _wp_yaw_behavior_context(index):
    value = _parameter_value(index, "WP_YAW_BEHAVIOR")
    if value is None:
        return {
            "value": None,
            "meaning": None,
            "caveat": "WP_YAW_BEHAVIOR is missing from available parameter context.",
        }
    enriched = enrich_parameter_entry({"name": "WP_YAW_BEHAVIOR", "value": value})
    return {
        "value": value,
        "meaning": enriched.get("enum_value"),
        "metadata_caveat": enriched.get("metadata_caveat"),
    }


def _mission_yaw_hint(ydes_p95, y_p95, error_p95, near_rate_limit):
    high_demand = False
    if ydes_p95 is not None:
        high_demand = ydes_p95 >= 20.0 or bool(near_rate_limit)
    high_error = error_p95 is not None and error_p95 >= 10.0
    good_tracking = error_p95 is not None and ydes_p95 is not None and error_p95 <= max(5.0, ydes_p95 * 0.25)
    low_demand_actual_motion = ydes_p95 is not None and y_p95 is not None and ydes_p95 <= 5.0 and y_p95 >= 10.0
    if high_demand and high_error:
        return "High mission yaw demand and high tracking error: yaw demand may exceed aircraft response; verify actuator authority, vibration/power, and mission geometry before tuning."
    if high_demand and good_tracking:
        return "Mission yaw demand is high but tracked; review mission geometry and WP_YAW_BEHAVIOR before treating this as a tune fault."
    if low_demand_actual_motion:
        return "Low commanded yaw but actual yaw movement is present; inspect uncommanded yaw, estimator/yaw-source, mechanical, or disturbance hypotheses."
    return "Mission yaw demand context only; do not treat parameter values alone as cause or as automatic tuning advice."


def _mission_yaw_demand(tables, *, index=None, decoded_mode=None):
    if decoded_mode not in MISSION_YAW_MODES:
        return None
    rate = tables.get("RATE")
    if rate is None or not hasattr(rate, "columns") or "YDes" not in rate.columns or "Y" not in rate.columns:
        return None
    ydes = _series_abs_stats(rate, "YDes") or {}
    y_actual = _series_abs_stats(rate, "Y") or {}
    yout = _series_abs_stats(rate, "YOut") or {}
    tracking = _tracking_stats(rate, "YDes", "Y") or {}
    ydes_p95 = ydes.get("p95_abs")
    y_p95 = y_actual.get("p95_abs")
    error_p95 = tracking.get("p95_abs")
    atc_rate_y_max = _parameter_value(index, "ATC_RATE_Y_MAX")
    atc_accel_y_max = _parameter_value(index, "ATC_ACCEL_Y_MAX")
    near_rate_limit = False
    if atc_rate_y_max is not None and atc_rate_y_max > 0 and ydes_p95 is not None:
        near_rate_limit = ydes_p95 >= atc_rate_y_max * 0.9
    missing = [
        name for name, value in [
            ("ATC_RATE_Y_MAX", atc_rate_y_max),
            ("ATC_ACCEL_Y_MAX", atc_accel_y_max),
            ("WP_YAW_BEHAVIOR", _parameter_value(index, "WP_YAW_BEHAVIOR")),
        ] if value is None
    ]
    demand = {
        "RATE.YDes_p95_abs": ydes_p95,
        "RATE.YDes_max_abs": ydes.get("max_abs"),
        "RATE.Y_p95_abs": y_p95,
        "tracking_error_p95_abs": error_p95,
        "RATE.YOut_p95_abs": yout.get("p95_abs"),
        "ATC_RATE_Y_MAX": atc_rate_y_max,
        "ATC_ACCEL_Y_MAX": atc_accel_y_max,
        "WP_YAW_BEHAVIOR": _wp_yaw_behavior_context(index),
        "demand_near_configured_rate_limit": near_rate_limit,
        "interpretation_hint": _mission_yaw_hint(ydes_p95, y_p95, error_p95, near_rate_limit),
        "caveat": "This is derived mission-yaw context. It does not prove that a parameter caused the issue and must be read with mode, estimator, vibration, power, and actuator evidence.",
    }
    if missing:
        demand["parameter_caveat"] = "Mission-yaw parameter context is missing: " + ", ".join(missing) + ". Interpret demand without treating absent values as defaults."
    return demand


def _pid_context(tables, message):
    df = tables.get(message)
    if df is None or not hasattr(df, "columns") or len(df) == 0:
        return None
    out = {"rows": int(len(df))}
    flags = numeric_series(df, ["Flags"])
    if flags is not None and len(flags.dropna()) > 0:
        out["flags_nonzero_count"] = int((flags.fillna(0).astype(int) != 0).sum())
        out["flags_limit_count"] = int(((flags.fillna(0).astype(int) & 1) != 0).sum())
    dmod = numeric_series(df, ["Dmod"])
    if dmod is not None and len(dmod.dropna()) > 0:
        out["dmod_min"] = float(dmod.min())
        out["dmod_p05"] = percentile([float(v) for v in dmod.dropna().tolist()], 5)
    return out


def _motor_saturation(tables, index=None):
    rcou = combined_rcout_dataframe(tables)
    if rcou is None:
        return None
    mapping = output_mapping_from_tables(tables, index=index)
    channels = output_channel_columns(rcou)
    saturation = {}
    for channel in channels:
        s = numeric_series(rcou, [channel])
        if s is None or len(s.dropna()) == 0:
            continue
        saturation[output_channel_label(channel, mapping)] = {
            "pct_high_ge_1900": float((s >= 1900).mean() * 100),
            "pct_low_le_1100": float((s <= 1100).mean() * 100),
            "max": float(s.max()),
            "min": float(s.min()),
        }
    return {"mapping_available": bool(mapping), "channels": saturation}


def _vibe_context(tables):
    vibe = tables.get("VIBE")
    if vibe is None or not hasattr(vibe, "columns"):
        return None
    out = {}
    for col in ["VibeX", "VibeY", "VibeZ"]:
        if col in vibe.columns:
            values = _abs_values(numeric_series(vibe, [col]))
            if values:
                out[col] = {"p95": percentile(values, 95), "max": max(values)}
    return out or None


def _power_context(tables):
    out = {}
    if "BAT" in tables:
        bat = tables["BAT"]
        for col in ["Volt", "VoltR", "Curr"]:
            if col in bat.columns:
                s = numeric_series(bat, [col])
                if s is not None and len(s.dropna()) > 0:
                    out[f"BAT.{col}"] = {"min": float(s.min()), "max": float(s.max()), "mean": float(s.mean())}
    if "POWR" in tables:
        powr = tables["POWR"]
        for col in ["Vcc", "VServo"]:
            if col in powr.columns:
                s = numeric_series(powr, [col])
                if s is not None and len(s.dropna()) > 0:
                    out[f"POWR.{col}"] = {"min": float(s.min()), "max": float(s.max())}
    return out or None


def _gps_ekf_context(tables):
    out = {}
    for name in ["GPS", "GPS2"]:
        df = tables.get(name)
        if df is None or not hasattr(df, "columns"):
            continue
        for col in ["NSats", "HDop", "HAcc", "Status"]:
            if col in df.columns:
                s = numeric_series(df, [col])
                if s is not None and len(s.dropna()) > 0:
                    out[f"{name}.{col}"] = {"min": float(s.min()), "max": float(s.max())}
    for name in ["XKF4", "NKF4"]:
        df = tables.get(name)
        if df is None or not hasattr(df, "columns"):
            continue
        for col in ["SV", "SP", "SH", "SM"]:
            if col in df.columns:
                s = numeric_series(df, [col])
                if s is not None and len(s.dropna()) > 0:
                    out[f"{name}.{col}"] = {"max": float(s.max()), "gt_1_count": int((s > 1.0).sum())}
    return out or None


def _rcin_yaw_active_pct(tables, index=None):
    rcin = tables.get("RCIN")
    if rcin is None or not hasattr(rcin, "columns") or len(rcin) == 0:
        return None
    mapping = rc_channel_mapping(tables, index or {})
    yaw_channel = mapping["axes"]["yaw"]["channel"]
    col = rcin_channel_col(rcin, yaw_channel)
    if not col:
        return None
    s = numeric_series(rcin, [col])
    if s is None or len(s.dropna()) == 0:
        return None
    return float((abs(s - 1500.0) > 50.0).mean() * 100.0)


def _event_summary(tables):
    markers = event_markers_from_tables(tables, limit=1000)
    return {
        "count": len(markers),
        "by_source": {source: sum(1 for m in markers if m.get("source") == source) for source in sorted({m.get("source") for m in markers})},
        "rows": markers[:50],
    }


def _mode_metrics(symptom_class, tables, index=None, decoded_mode=None):
    metrics: Dict[str, Any] = {"events": _event_summary(tables)}
    if symptom_class == "yaw_misbehaviour":
        metrics["att_yaw_error"] = _tracking_stats(tables.get("ATT"), "DesYaw", "Yaw", angle=True)
        metrics["rate_y_error"] = _tracking_stats(tables.get("RATE"), "YDes", "Y")
        metrics["rate_yout"] = _series_abs_stats(tables.get("RATE"), "YOut")
        mission_yaw = _mission_yaw_demand(tables, index=index, decoded_mode=decoded_mode)
        if mission_yaw:
            metrics["mission_yaw_demand"] = mission_yaw
        metrics["rcin_yaw_active_pct"] = _rcin_yaw_active_pct(tables, index=index)
        metrics["pidy"] = _pid_context(tables, "PIDY")
    elif symptom_class == "attitude_rate_issue":
        metrics["att_roll_error"] = _tracking_stats(tables.get("ATT"), "DesRoll", "Roll", angle=False)
        metrics["att_pitch_error"] = _tracking_stats(tables.get("ATT"), "DesPitch", "Pitch", angle=False)
        metrics["rate_roll_error"] = _tracking_stats(tables.get("RATE"), "RDes", "R")
        metrics["rate_pitch_error"] = _tracking_stats(tables.get("RATE"), "PDes", "P")
        metrics["rate_rout"] = _series_abs_stats(tables.get("RATE"), "ROut")
        metrics["rate_pout"] = _series_abs_stats(tables.get("RATE"), "POut")
        metrics["pidr"] = _pid_context(tables, "PIDR")
        metrics["pidp"] = _pid_context(tables, "PIDP")
    metrics["motor_outputs"] = _motor_saturation(tables, index=index)
    metrics["vibration"] = _vibe_context(tables)
    metrics["power"] = _power_context(tables)
    metrics["gps_ekf"] = _gps_ekf_context(tables)
    return metrics


def _score_mode(symptom_class, metrics):
    if symptom_class == "yaw_misbehaviour":
        rate = metrics.get("rate_y_error") or {}
        att = metrics.get("att_yaw_error") or {}
        return rate.get("p95_abs") if rate.get("p95_abs") is not None else att.get("p95_abs")
    if symptom_class == "attitude_rate_issue":
        vals = []
        for key in ["rate_roll_error", "rate_pitch_error", "att_roll_error", "att_pitch_error"]:
            value = (metrics.get(key) or {}).get("p95_abs")
            if value is not None:
                vals.append(value)
        return max(vals) if vals else None
    return None


def _manual_control_assessment(modes_found):
    found = set(modes_found or [])
    limitations = []
    if found & MANUAL_CONFIDENCE_MODES:
        confidence = "high"
    elif found & PURE_MANUAL_ATTITUDE_MODES:
        confidence = "medium"
    elif found & NAV_ASSISTED_HOLD_MODES:
        confidence = "low"
        limitations.append(POSHOLD_MANUAL_LIMITATION)
    else:
        confidence = "low"
        limitations.append("No STABILIZE, ALTHOLD, or ACRO segments were compared; pure manual-control conclusions are limited.")
    return confidence, limitations


def compare_modes(
    tables,
    *,
    symptom="general_investigation",
    compare_modes=None,
    active_flight_only=False,
    airborne_only=False,
    exclude_ground_spool=False,
    min_alt=1.0,
    min_throttle=0.15,
    index=None,
):
    symptom_class = classify_symptom(symptom) if symptom not in {"yaw_misbehaviour", "attitude_rate_issue", "general_investigation"} else symptom
    modes = [m.strip() for m in (compare_modes or []) if str(m).strip()]
    if not modes:
        raise AnalysisError("--compare-modes must name at least one mode")
    log_end_s = None
    for df in tables.values():
        if df is not None and hasattr(df, "columns") and "TimeS" in df.columns and len(df["TimeS"].dropna()) > 0:
            value = float(df["TimeS"].dropna().max())
            log_end_s = value if log_end_s is None else max(log_end_s, value)
    from ap_window_select import select_analysis_window

    per_mode = []
    confidence_limits = []
    modes_found = []
    requested_modes_missing = []
    spec = requirement_spec(symptom_class)
    present = set(tables.keys())
    missing = {
        "required": [m for m in spec.get("required_messages", []) if m not in present],
        "strongly_recommended": [m for m in spec.get("strongly_recommended_messages", []) if m not in present],
        "optional_context": [m for m in spec.get("optional_context_messages", []) if m not in present],
    }
    for mode in modes:
        decoded = decode_copter_mode(mode) or mode_label(mode)
        try:
            selection = select_analysis_window(tables, mode=mode, log_end_s=log_end_s, vehicle_scope={"primary_vehicle": "Copter"})
        except AnalysisError as exc:
            per_mode.append({"query": mode, "decoded_mode": decoded, "error": str(exc), "intervals_found": [], "intervals_used": [], "metrics": {}})
            requested_modes_missing.append(decoded)
            confidence_limits.append(f"Mode {mode} could not be compared: {exc}")
            continue
        if decoded not in modes_found:
            modes_found.append(decoded)
        selected = filter_tables_by_time(tables, start_s=selection.get("start_s"), end_s=selection.get("end_s"), intervals=selection.get("intervals_used"))
        selection, profile = apply_active_flight_filter(
            selection,
            selected,
            active_flight_only=active_flight_only,
            airborne_only=airborne_only,
            exclude_ground_spool=exclude_ground_spool,
            min_alt=min_alt,
            min_throttle=min_throttle,
        )
        mode_tables = filter_tables_by_time(selected, start_s=selection.get("start_s"), end_s=selection.get("end_s"), intervals=selection.get("intervals_used"))
        metrics = _mode_metrics(symptom_class, mode_tables, index=index, decoded_mode=decoded)
        duration = _duration(selection.get("intervals_used", []))
        if duration < 2.0:
            confidence_limits.append(f"Mode {decoded} has a short comparison duration ({duration:.2f}s); do not over-interpret ranking.")
        if profile.get("warnings"):
            confidence_limits.extend(f"Mode {decoded}: {w}" for w in profile["warnings"])
        score = _score_mode(symptom_class, metrics)
        per_mode.append({
            "query": mode,
            "decoded_mode": decoded,
            "duration_s": duration,
            "intervals_found": selection.get("intervals_found", []),
            "intervals_used": selection.get("intervals_used", []),
            "active_flight_criteria": profile.get("criteria", {}),
            "window_quality": profile.get("quality", {}),
            "warnings": selection.get("warnings", []),
            "metrics": metrics,
            "ranking_score": score,
        })
    ranked = sorted(
        [m for m in per_mode if m.get("ranking_score") is not None],
        key=lambda item: item["ranking_score"],
        reverse=True,
    )
    manual_control_confidence, manual_control_limitations = _manual_control_assessment(modes_found)
    confidence_limits.extend(manual_control_limitations)
    return {
        "symptom": symptom,
        "symptom_class": symptom_class,
        "control_evidence_completeness": build_control_evidence_completeness(symptom_class, index=index, tables=tables),
        "requested_modes": modes,
        "selected_modes": modes,
        "decoded_modes": [decode_copter_mode(m) or mode_label(m) for m in modes],
        "modes_found": modes_found,
        "requested_modes_missing": requested_modes_missing,
        "manual_control_confidence": manual_control_confidence,
        "manual_control_limitations": manual_control_limitations,
        "mode_comparisons": per_mode,
        "ranking": [{"decoded_mode": m["decoded_mode"], "query": m["query"], "score": m["ranking_score"]} for m in ranked],
        "confidence_limits": list(dict.fromkeys(confidence_limits)),
        "missing_evidence": missing,
        "diagnostic_aid_note": "Mode comparison is a diagnostic aid for scoping symptoms across flight modes; it is not a final conclusion.",
    }


def _plot_series_by_mode(result, out_dir):
    try:
        import plotly.graph_objects as go
    except Exception as exc:
        raise AnalysisError("plotly is required for mode-comparison plots. Install dependencies with pip install -r requirements.txt") from exc
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    plots = []
    labels = [m["decoded_mode"] for m in result["mode_comparisons"]]
    if result["symptom_class"] == "yaw_misbehaviour":
        fig = go.Figure()
        values = [((m.get("metrics") or {}).get("rate_y_error") or {}).get("p95_abs") for m in result["mode_comparisons"]]
        fig.add_bar(x=labels, y=values, name="RATE.Y error p95")
        fig.update_layout(title="Yaw rate tracking comparison by mode", template="plotly_white", yaxis_title="abs error p95")
        path = out / "yaw_rate_comparison_by_mode.html"
        fig.write_html(str(path), include_plotlyjs="cdn")
        plots.append(str(path))
    if result["symptom_class"] in {"attitude_rate_issue", "general_investigation"}:
        fig = go.Figure()
        values = [m.get("ranking_score") for m in result["mode_comparisons"]]
        fig.add_bar(x=labels, y=values, name="tracking score")
        fig.update_layout(title="Attitude/rate comparison by mode", template="plotly_white")
        path = out / "attitude_rate_comparison_by_mode.html"
        fig.write_html(str(path), include_plotlyjs="cdn")
        plots.append(str(path))
    fig = go.Figure()
    values = []
    for m in result["mode_comparisons"]:
        vibe = (m.get("metrics") or {}).get("vibration") or {}
        values.append(max([v.get("max", 0.0) for v in vibe.values()] or [None]))
    fig.add_bar(x=labels, y=values, name="VIBE max")
    fig.update_layout(title="Vibration comparison by mode", template="plotly_white")
    path = out / "vibration_comparison_by_mode.html"
    fig.write_html(str(path), include_plotlyjs="cdn")
    plots.append(str(path))
    fig = go.Figure()
    values = []
    for m in result["mode_comparisons"]:
        motor = ((m.get("metrics") or {}).get("motor_outputs") or {}).get("channels", {})
        values.append(max([v.get("pct_high_ge_1900", 0.0) for v in motor.values()] or [None]))
    fig.add_bar(x=labels, y=values, name="max output saturation %")
    fig.update_layout(title="Motor output saturation comparison by mode", template="plotly_white", yaxis_title="% >=1900us")
    path = out / "motor_outputs_comparison_by_mode.html"
    fig.write_html(str(path), include_plotlyjs="cdn")
    plots.append(str(path))
    return plots


def main() -> int:
    p = argparse.ArgumentParser(description="Compare ArduPilot log symptoms across flight modes.")
    p.add_argument("log")
    p.add_argument("--symptom", default="general_investigation")
    p.add_argument("--compare-modes", required=True, help="Comma-separated modes, e.g. AUTO,POSHOLD,ALTHOLD,STABILIZE")
    p.add_argument("--active-flight-only", action="store_true")
    p.add_argument("--airborne-only", action="store_true")
    p.add_argument("--exclude-ground-spool", action="store_true")
    p.add_argument("--min-alt", type=float, default=1.0)
    p.add_argument("--min-throttle", type=float, default=0.15)
    p.add_argument("--json", default="mode_compare.json")
    p.add_argument("--plots", default=None)
    p.add_argument("--max-messages", type=int, default=None)
    p.add_argument("--params", help="Optional external .param file to supplement missing logged parameters")
    args = p.parse_args()
    try:
        include = list(DEFAULT_COMPARE_MESSAGES)
        rows, index, stats = collect_dataflash(args.log, include=include, max_messages=args.max_messages)
        external_parameter_context = parse_param_file(args.params) if args.params else None
        merged_params = merge_external_parameters(index, external_parameter_context)
        parameter_index = merged_params["index"]
        tables = {typ: rows_to_dataframe(data) for typ, data in rows.items() if data and typ not in {"FMT", "FMTU"}}
        result = compare_modes(
            tables,
            symptom=args.symptom,
            compare_modes=args.compare_modes.split(","),
            active_flight_only=args.active_flight_only,
            airborne_only=args.airborne_only,
            exclude_ground_spool=args.exclude_ground_spool,
            min_alt=args.min_alt,
            min_throttle=args.min_throttle,
            index=parameter_index,
        )
        result["log"] = {"file": args.log, "vehicle": index.get("vehicle"), "firmware": index.get("firmware")}
        result["parser"] = stats
        result["parameter_source_precedence"] = merged_params["parameter_source_precedence"]
        result["parameter_conflicts"] = merged_params["parameter_conflicts"]
        result["supplemented_parameters"] = merged_params["supplemented_parameters"]
        if external_parameter_context:
            result["external_parameter_context"] = external_parameter_context
        if args.plots:
            result["plots"] = _plot_series_by_mode(result, args.plots)
        write_json(args.json, result)
        print(f"Compared {len(result['mode_comparisons'])} modes; plots={len(result.get('plots', []))}")
        return 0
    except AnalysisError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
