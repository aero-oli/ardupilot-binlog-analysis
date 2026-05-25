#!/usr/bin/env python3
from __future__ import annotations

from typing import Any, Dict, Optional, Sequence

from ap_common import get_col, numeric_series, params_from_tables, percentile, safe_float, safe_int, wrap_angle_deg
from ap_diag_helpers import vals


AXIS_CHANNEL_PARAMS = {
    "roll": ("RCMAP_ROLL", 1),
    "pitch": ("RCMAP_PITCH", 2),
    "throttle": ("RCMAP_THROTTLE", 3),
    "yaw": ("RCMAP_YAW", 4),
}

AXIS_FIELDS = {
    "roll": {"att_des": "DesRoll", "att": "Roll", "rate_des": "RDes", "rate": "R"},
    "pitch": {"att_des": "DesPitch", "att": "Pitch", "rate_des": "PDes", "rate": "P"},
    "yaw": {"att_des": "DesYaw", "att": "Yaw", "rate_des": "YDes", "rate": "Y"},
}


def _combined_params(tables: Dict[str, Any], index: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    params = {}
    if index:
        params.update(index.get("parameters", {}) or {})
    params.update(params_from_tables(tables))
    return params


def rcin_channel_col(rcin: Any, channel: int) -> Optional[str]:
    candidates = [
        f"C{channel}",
        f"Ch{channel}",
        f"CH{channel}",
        f"Chan{channel}",
        f"Channel{channel}",
        f"RC{channel}",
    ]
    return get_col(rcin, candidates)


def rc_channel_mapping(tables: Dict[str, Any], index: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    params = _combined_params(tables, index)
    axes = {}
    used_defaults = []
    for axis, (param, default) in AXIS_CHANNEL_PARAMS.items():
        raw = params.get(param)
        channel = safe_int(raw)
        if channel is None:
            channel = default
            source = "default_assumed"
            used_defaults.append(param)
        else:
            source = "parameter"
        trim = safe_float(params.get(f"RC{channel}_TRIM"), 1500.0)
        axes[axis] = {
            "channel": channel,
            "field": f"C{channel}",
            "trim": trim,
            "mapping_source": source,
            "parameter": param,
        }
    mapping_source = "parameters" if not used_defaults else "default_assumed"
    limitation = None
    if used_defaults:
        limitation = (
            "RC channel mapping parameters were not fully available; default ArduPilot channel order "
            "was assumed for missing RCMAP_* values."
        )
    return {"axes": axes, "mapping_source": mapping_source, "limitation": limitation, "parameters_available": not used_defaults}


def summarize_rcin(tables: Dict[str, Any], index: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    rcin = tables.get("RCIN")
    mapping = rc_channel_mapping(tables, index)
    if rcin is None or len(rcin) == 0:
        return {"available": False, "mapping": mapping, "axes": {}, "limitation": "RCIN message is not available."}
    axes = {}
    for axis, info in mapping["axes"].items():
        col = rcin_channel_col(rcin, info["channel"])
        if not col:
            axes[axis] = {**info, "available": False, "limitation": f"RCIN channel {info['channel']} field was not found."}
            continue
        s = numeric_series(rcin, [col])
        values = vals(s)
        if not values:
            axes[axis] = {**info, "field": col, "available": False, "limitation": f"RCIN.{col} has no numeric samples."}
            continue
        trim = info.get("trim") or 1500.0
        deflections = [abs(v - trim) for v in values]
        threshold = 100.0 if axis != "throttle" else 50.0
        axes[axis] = {
            **info,
            "field": col,
            "available": True,
            "samples": len(values),
            "min": min(values),
            "max": max(values),
            "trim": trim,
            "p95_abs_deflection_from_trim": percentile(deflections, 95),
            "command_threshold_us": threshold,
            "active_percent": 100.0 * sum(1 for v in deflections if v >= threshold) / len(deflections),
        }
    return {"available": True, "mapping": mapping, "axes": axes, "limitation": mapping.get("limitation")}


def _first_time(df: Any, mask) -> Optional[float]:
    if df is None or "TimeS" not in df.columns:
        return None
    subset = df.loc[mask & df["TimeS"].notna()]
    if len(subset) == 0:
        return None
    return safe_float(subset["TimeS"].iloc[0])


def _first_rc_command_time(rcin: Any, field: str, trim: float, threshold: float) -> Optional[float]:
    s = numeric_series(rcin, [field])
    if s is None:
        return None
    return _first_time(rcin, abs(s - trim) >= threshold)


def _first_desired_time(tables: Dict[str, Any], axis: str) -> Optional[float]:
    fields = AXIS_FIELDS[axis]
    rate = tables.get("RATE")
    if rate is not None and fields["rate_des"] in rate.columns:
        s = numeric_series(rate, [fields["rate_des"]])
        t = _first_time(rate, abs(s) >= 10.0)
        if t is not None:
            return t
    att = tables.get("ATT")
    if att is not None and fields["att_des"] in att.columns:
        s = numeric_series(att, [fields["att_des"]])
        if s is not None and len(s.dropna()) > 0:
            baseline = float(s.dropna().iloc[0])
            delta = s - baseline
            if axis == "yaw":
                delta = wrap_angle_deg(delta)
            return _first_time(att, abs(delta) >= 5.0)
    return None


def _first_actual_time(tables: Dict[str, Any], axis: str) -> Optional[float]:
    fields = AXIS_FIELDS[axis]
    rate = tables.get("RATE")
    if rate is not None and fields["rate"] in rate.columns:
        s = numeric_series(rate, [fields["rate"]])
        t = _first_time(rate, abs(s) >= 10.0)
        if t is not None:
            return t
    att = tables.get("ATT")
    if att is not None and fields["att"] in att.columns:
        s = numeric_series(att, [fields["att"]])
        if s is not None and len(s.dropna()) > 0:
            baseline = float(s.dropna().iloc[0])
            delta = s - baseline
            if axis == "yaw":
                delta = wrap_angle_deg(delta)
            return _first_time(att, abs(delta) >= 5.0)
    return None


def build_command_response_investigation(
    tables: Dict[str, Any],
    index: Optional[Dict[str, Any]] = None,
    axes: Sequence[str] = ("yaw", "roll", "pitch"),
) -> Dict[str, Any]:
    findings = []
    context = []
    checked = []
    summary = summarize_rcin(tables, index)
    rcin = tables.get("RCIN")
    if summary.get("limitation"):
        context.append({"source": "RCIN", "detail": summary["limitation"]})
    if rcin is None or len(rcin) == 0:
        checked.append({"check": "RCIN command context", "result": "RCIN unavailable; pilot-commanded vs uncommanded motion cannot be separated from RC input."})
        return {"findings": findings, "context": context, "checked": checked, "rcin_summary": summary}

    for axis in axes:
        if axis not in AXIS_FIELDS:
            continue
        axis_summary = summary.get("axes", {}).get(axis, {})
        if not axis_summary.get("available"):
            checked.append({"check": f"RCIN {axis} command", "result": axis_summary.get("limitation", f"RCIN {axis} channel unavailable.")})
            continue
        context.append({
            "source": "RCIN",
            "detail": (
                f"RCIN {axis} channel {axis_summary['channel']} ({axis_summary['field']}): "
                f"min={axis_summary['min']:.0f}, max={axis_summary['max']:.0f}, "
                f"p95 abs deflection={axis_summary['p95_abs_deflection_from_trim']:.0f} us, "
                f"active={axis_summary['active_percent']:.1f}%"
            ),
        })
        rc_t = _first_rc_command_time(rcin, axis_summary["field"], axis_summary["trim"], axis_summary["command_threshold_us"])
        des_t = _first_desired_time(tables, axis)
        actual_t = _first_actual_time(tables, axis)
        evidence = []
        if rc_t is not None:
            evidence.append(f"first mapped RCIN {axis} command at {rc_t:.2f}s")
        if des_t is not None:
            evidence.append(f"first desired {axis} attitude/rate change at {des_t:.2f}s")
        if actual_t is not None:
            evidence.append(f"first achieved {axis} motion at {actual_t:.2f}s")
        if rc_t is not None and actual_t is not None and rc_t <= actual_t + 0.25:
            checked.append({
                "check": f"Commanded-vs-response {axis}",
                "result": "; ".join(evidence) + ". Motion appears consistent with a commanded manoeuvre, not an uncommanded-motion finding by this heuristic.",
            })
        elif rc_t is None and des_t is not None:
            checked.append({
                "check": f"Commanded-vs-response {axis}",
                "result": "; ".join(evidence) + ". No mapped RCIN command preceded the desired response; autopilot/mode, navigation, or estimator involvement should be considered.",
            })
        elif rc_t is None and des_t is None and actual_t is not None:
            confidence = "medium" if axis_summary.get("mapping_source") == "parameter" else "low"
            findings.append({
                "rank": 2,
                "possible_cause": f"{axis} motion without RCIN or desired command",
                "severity": "likely-issue",
                "confidence": confidence,
                "evidence": evidence or [f"No mapped RCIN or desired {axis} command detected before achieved motion"],
                "interpretation": "Actual motion changed without matching pilot input or logged desired attitude/rate command. This supports uncommanded mechanical, estimator, disturbance, or logging-window hypotheses rather than a simple pilot manoeuvre.",
                "recommended_checks": ["Check actuator outputs and saturation", "Check EKF/compass evidence", "Check vibration, power, and mechanical condition", "Confirm RC channel mapping from parameters"],
            })
        else:
            checked.append({
                "check": f"Commanded-vs-response {axis}",
                "result": "; ".join(evidence) if evidence else f"No mapped RCIN command or {axis} response detected by heuristic.",
            })
    return {"findings": findings, "context": context, "checked": checked, "rcin_summary": summary}
