#!/usr/bin/env python3
from __future__ import annotations

from typing import Any, Iterable

from ap_common import get_col, numeric_series, params_from_tables, safe_float, safe_int

AXIS_CHANNEL_PARAMS = {
    "roll": ("RCMAP_ROLL", 1),
    "pitch": ("RCMAP_PITCH", 2),
    "throttle": ("RCMAP_THROTTLE", 3),
    "yaw": ("RCMAP_YAW", 4),
}


def _combined_params(tables: dict[str, Any] | None = None, parameters: dict[str, Any] | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if parameters:
        params.update(parameters)
    if tables:
        params.update(params_from_tables(tables))
    return params


def rcin_channel_col(rcin: Any, channel: int) -> str | None:
    return get_col(rcin, [f"C{channel}", f"Ch{channel}", f"CH{channel}", f"Chan{channel}", f"Channel{channel}", f"RC{channel}"])


def rc_axis_mapping(tables: dict[str, Any] | None = None, parameters: dict[str, Any] | None = None) -> dict[str, Any]:
    params = _combined_params(tables, parameters)
    axes = {}
    missing = []
    for axis, (param_name, default_channel) in AXIS_CHANNEL_PARAMS.items():
        channel = safe_int(params.get(param_name))
        source = "parameter"
        if channel is None:
            channel = default_channel
            source = "default_assumed"
            missing.append(param_name)
        trim = safe_float(params.get(f"RC{channel}_TRIM"), 1500.0)
        axes[axis] = {
            "channel": channel,
            "field": f"C{channel}",
            "trim": trim,
            "mapping_source": source,
            "parameter": param_name,
        }
    caveats = []
    if missing:
        caveats.append(
            "RCMAP_* parameters were incomplete; ArduPilot default channel order was assumed for: "
            + ", ".join(missing)
            + "."
        )
    return {"axes": axes, "mapping_source": "parameters" if not missing else "default_assumed", "caveats": caveats}


def _series_values(df: Any, col: str) -> list[float]:
    s = numeric_series(df, [col])
    if s is None:
        return []
    values = []
    for value in s.tolist():
        f = safe_float(value)
        if f is not None:
            values.append(f)
    return values


def _time_values(df: Any) -> list[float]:
    if df is None or "TimeS" not in getattr(df, "columns", []):
        return []
    return [safe_float(v) for v in df["TimeS"].tolist()]


def _windows_from_mask(times: list[float | None], mask: list[bool], min_duration_s: float = 0.0) -> list[dict[str, float]]:
    windows = []
    start = None
    last_t = None
    for t, ok in zip(times, mask):
        if t is None:
            continue
        if ok and start is None:
            start = t
        if not ok and start is not None:
            end = last_t if last_t is not None else t
            if end - start >= min_duration_s:
                windows.append({"start_s": float(start), "end_s": float(end), "duration_s": float(end - start)})
            start = None
        last_t = t
    if start is not None and last_t is not None and last_t - start >= min_duration_s:
        windows.append({"start_s": float(start), "end_s": float(last_t), "duration_s": float(last_t - start)})
    return windows


def analyze_rc_input_contamination(
    tables: dict[str, Any],
    parameters: dict[str, Any] | None = None,
    *,
    deadbands_us: Iterable[float] = (30.0, 50.0),
    centered_deadband_us: float = 30.0,
    yaw_only_centered: bool = False,
    min_centered_window_s: float = 0.5,
) -> dict[str, Any]:
    mapping = rc_axis_mapping(tables, parameters)
    rcin = tables.get("RCIN")
    if rcin is None or len(rcin) == 0:
        return {
            "available": False,
            "mapping": mapping,
            "axis_activity": {},
            "hands_off_confidence": "low",
            "rc_centered_windows": [],
            "rc_centered_mask": [],
            "warnings": ["RCIN is missing; pilot stick contamination cannot be ruled out."],
        }

    axis_activity = {}
    centered_axes = ["yaw"] if yaw_only_centered else ["roll", "pitch", "yaw"]
    centered_mask = [True] * len(rcin)
    warnings = list(mapping.get("caveats") or [])

    for axis, info in mapping["axes"].items():
        col = rcin_channel_col(rcin, info["channel"])
        if not col:
            axis_activity[axis] = {**info, "available": False, "warning": f"RCIN channel {info['channel']} was not found."}
            if axis in centered_axes:
                centered_mask = [False] * len(rcin)
            continue
        values = _series_values(rcin, col)
        if len(values) != len(rcin):
            warnings.append(f"RCIN.{col} had non-numeric samples; centered mask is limited to numeric rows.")
        trim = info.get("trim") if info.get("trim") is not None else 1500.0
        deflections = [abs(v - trim) for v in values]
        active_by_deadband = {}
        for db in deadbands_us:
            active_by_deadband[str(int(db) if float(db).is_integer() else db)] = 100.0 * sum(1 for d in deflections if d > float(db)) / len(deflections) if deflections else None
        if axis in centered_axes:
            axis_mask = [d <= centered_deadband_us for d in deflections]
            centered_mask = [a and b for a, b in zip(centered_mask, axis_mask)]
        axis_activity[axis] = {
            **info,
            "field": col,
            "available": True,
            "samples": len(values),
            "active_percent_by_deadband_us": active_by_deadband,
            "centered_deadband_us": centered_deadband_us,
            "centered_percent": 100.0 * sum(1 for d in deflections if d <= centered_deadband_us) / len(deflections) if deflections else None,
            "min": min(values) if values else None,
            "max": max(values) if values else None,
            "trim": trim,
        }

    times = _time_values(rcin)
    windows = _windows_from_mask(times, centered_mask, min_duration_s=min_centered_window_s)
    centered_percent = 100.0 * sum(1 for item in centered_mask if item) / len(centered_mask) if centered_mask else 0.0
    active_axes = [
        axis for axis, data in axis_activity.items()
        if data.get("available") and (data.get("active_percent_by_deadband_us", {}).get("30") or 0.0) > 10.0
    ]
    if not mapping["caveats"] and centered_percent >= 90.0:
        confidence = "high"
    elif centered_percent >= 70.0 and len(active_axes) <= 1:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "available": True,
        "mapping": mapping,
        "axis_activity": axis_activity,
        "centered_axes": centered_axes,
        "centered_deadband_us": centered_deadband_us,
        "centered_percent": centered_percent,
        "hands_off_confidence": confidence,
        "rc_centered_windows": windows,
        "rc_centered_mask": centered_mask,
        "warnings": warnings,
    }
