#!/usr/bin/env python3
from __future__ import annotations

from typing import Any

from ap_common import get_col, numeric_series, safe_float
from ap_modes import mode_matches
from ap_methodic_rc import analyze_rc_input_contamination

SELECTORS = {
    "methodic_hover",
    "first_althold_hover",
    "post_takeoff_hover",
    "stable_hover",
    "descent_segment",
    "active_flight_only",
}


def _col(df: Any, names: list[str]) -> str | None:
    return get_col(df, names)


def _times(df: Any) -> list[float | None]:
    if df is None or "TimeS" not in getattr(df, "columns", []):
        return []
    return [safe_float(v) for v in df["TimeS"].tolist()]


def _values(df: Any, names: list[str]) -> list[float | None]:
    col = _col(df, names)
    if not col:
        return []
    s = numeric_series(df, [col])
    if s is None:
        return []
    return [safe_float(v) for v in s.tolist()]


def _intervals_from_mask(times: list[float | None], mask: list[bool], min_duration_s: float) -> list[dict[str, Any]]:
    out = []
    start = None
    last = None
    rows = 0
    start_rows = 0
    for t, ok in zip(times, mask):
        if t is None:
            continue
        if ok and start is None:
            start = t
            start_rows = rows
        if not ok and start is not None:
            end = last if last is not None else t
            if end - start >= min_duration_s:
                out.append({"start_s": float(start), "end_s": float(end), "duration_s": float(end - start), "rows": rows - start_rows})
            start = None
        last = t
        rows += 1
    if start is not None and last is not None and last - start >= min_duration_s:
        out.append({"start_s": float(start), "end_s": float(last), "duration_s": float(last - start), "rows": rows - start_rows})
    return out


def _mode_intervals(tables: dict[str, Any], mode_name: str) -> list[dict[str, float]]:
    df = tables.get("MODE")
    if df is None or "TimeS" not in getattr(df, "columns", []):
        return []
    rows = df.dropna(subset=["TimeS"]).sort_values("TimeS").to_dict(orient="records")
    intervals = []
    end_hint = _log_end(tables)
    for idx, row in enumerate(rows):
        candidates = [row.get(c) for c in ("Mode", "Name", "ModeNum") if c in row]
        if not any(mode_matches(candidate, mode_name) or str(candidate).upper().replace("_", "") == mode_name.upper().replace("_", "") for candidate in candidates):
            continue
        start = safe_float(row.get("TimeS"))
        end = safe_float(rows[idx + 1].get("TimeS")) if idx + 1 < len(rows) else end_hint
        if start is not None and end is not None and end > start:
            intervals.append({"start_s": float(start), "end_s": float(end)})
    return intervals


def _log_end(tables: dict[str, Any]) -> float | None:
    ends = []
    for df in tables.values():
        ts = _times(df)
        vals = [t for t in ts if t is not None]
        if vals:
            ends.append(max(vals))
    return max(ends) if ends else None


def _in_intervals(t: float | None, intervals: list[dict[str, float]]) -> bool:
    if t is None:
        return False
    return any(interval["start_s"] <= t <= interval["end_s"] for interval in intervals)


def _rcou_spool_mask(tables: dict[str, Any], times: list[float | None], threshold_pwm: float = 1150.0) -> tuple[list[bool] | None, bool]:
    frames = [tables.get(name) for name in ("RCOU", "RCO2", "RCO3") if tables.get(name) is not None]
    if not frames:
        return None, False
    samples = []
    for df in frames:
        if "TimeS" not in getattr(df, "columns", []):
            continue
        channel_cols = [c for c in df.columns if str(c).startswith("C") and str(c)[1:].isdigit()]
        for row in df[["TimeS"] + channel_cols].to_dict(orient="records"):
            vals = [safe_float(row.get(c)) for c in channel_cols]
            vals = [v for v in vals if v is not None]
            if vals:
                samples.append((safe_float(row.get("TimeS")), max(vals)))
    if not samples:
        return None, False
    samples.sort(key=lambda item: item[0] if item[0] is not None else -1.0)
    mask = []
    idx = 0
    last = samples[0]
    for t in times:
        if t is None:
            mask.append(False)
            continue
        while idx + 1 < len(samples) and samples[idx + 1][0] is not None and samples[idx + 1][0] <= t:
            idx += 1
            last = samples[idx]
        mask.append((last[1] or 0.0) > threshold_pwm)
    return mask, True


def _altitude_series(tables: dict[str, Any]) -> tuple[Any | None, str | None, str | None]:
    for name, cols in [("CTUN", ["Alt"]), ("BARO", ["Alt"]), ("GPS", ["Alt", "RelAlt", "RAlt"])]:
        df = tables.get(name)
        if df is None or "TimeS" not in getattr(df, "columns", []):
            continue
        col = _col(df, cols)
        if col:
            return df, col, name
    return None, None, None


def _stable_hover_candidates(
    tables: dict[str, Any],
    *,
    min_duration_s: float = 5.0,
    alt_span_max_m: float = 0.75,
    min_alt_above_ground_m: float = 0.4,
    max_attitude_abs_deg: float = 10.0,
    throttle_min: float = 0.18,
    throttle_max: float = 0.85,
    vertical_rate_max_m_s: float = 0.2,
    mode_filter: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[str], dict[str, bool]]:
    warnings = []
    df, alt_col, alt_source = _altitude_series(tables)
    if df is None or alt_col is None:
        return [], {}, ["No CTUN/BARO/GPS altitude source available for hover-window selection."], {"takeoff_landing_spool_rows_excluded": False}
    times = _times(df)
    alts = _values(df, [alt_col])
    valid_alts = [v for v in alts if v is not None]
    if not valid_alts:
        return [], {}, [f"{alt_source}.{alt_col} has no numeric altitude samples."], {"takeoff_landing_spool_rows_excluded": False}
    base_alt = min(valid_alts[: max(3, min(30, len(valid_alts)))])
    mask = []
    mode_intervals = _mode_intervals(tables, mode_filter) if mode_filter else []
    throttle = _values(tables.get("CTUN"), ["ThO", "ThH"]) if tables.get("CTUN") is df else []
    roll = _values(tables.get("ATT"), ["Roll"])
    pitch = _values(tables.get("ATT"), ["Pitch"])
    spool_mask, spool_available = _rcou_spool_mask(tables, times)

    for idx, t in enumerate(times):
        alt = alts[idx] if idx < len(alts) else None
        ok = t is not None and alt is not None and alt >= base_alt + min_alt_above_ground_m
        if ok and idx > 0 and times[idx - 1] is not None and alts[idx - 1] is not None and t != times[idx - 1]:
            vertical_rate = abs((alt - alts[idx - 1]) / (t - times[idx - 1]))
            ok = vertical_rate <= vertical_rate_max_m_s
        if ok and mode_filter:
            ok = _in_intervals(t, mode_intervals)
        if ok and throttle:
            th = throttle[min(idx, len(throttle) - 1)]
            ok = th is not None and throttle_min <= th <= throttle_max
        if ok and roll and pitch:
            r = roll[min(idx, len(roll) - 1)]
            p = pitch[min(idx, len(pitch) - 1)]
            ok = r is not None and p is not None and abs(r) <= max_attitude_abs_deg and abs(p) <= max_attitude_abs_deg
        if ok and spool_mask is not None:
            ok = spool_mask[idx]
        mask.append(bool(ok))

    raw = _intervals_from_mask(times, mask, min_duration_s=min_duration_s)
    candidates = []
    for interval in raw:
        points = [
            (t, alt) for t, alt in zip(times, alts)
            if t is not None and alt is not None and interval["start_s"] <= t <= interval["end_s"]
        ]
        vals = [alt for _t, alt in points]
        span = max(vals) - min(vals) if vals else None
        if span is not None and span <= alt_span_max_m:
            candidates.append({**interval, "altitude_span_m": float(span), "altitude_source": f"{alt_source}.{alt_col}"})
            continue
        candidates.extend(_stable_subwindows(points, min_duration_s, alt_span_max_m, f"{alt_source}.{alt_col}"))
    if not spool_available:
        warnings.append("RCOU/RCO2/RCO3 were not available; ground-spool exclusion used altitude/throttle only.")
    criteria = {
        "min_duration_s": min_duration_s,
        "alt_span_max_m": alt_span_max_m,
        "min_alt_above_ground_m": min_alt_above_ground_m,
        "max_attitude_abs_deg": max_attitude_abs_deg,
        "throttle_min": throttle_min,
        "throttle_max": throttle_max,
        "vertical_rate_max_m_s": vertical_rate_max_m_s,
        "mode_filter": mode_filter,
        "altitude_source": f"{alt_source}.{alt_col}",
    }
    excluded = {"takeoff_landing_spool_rows_excluded": any(not item for item in mask), "rcou_spool_available": spool_available}
    return candidates, criteria, warnings, excluded


def _stable_subwindows(points: list[tuple[float, float]], min_duration_s: float, alt_span_max_m: float, altitude_source: str) -> list[dict[str, Any]]:
    candidates = []
    left = 0
    for right in range(len(points)):
        while left <= right:
            values = [alt for _t, alt in points[left:right + 1]]
            if values and max(values) - min(values) <= alt_span_max_m:
                break
            left += 1
        if left <= right:
            start = points[left][0]
            end = points[right][0]
            if end - start >= min_duration_s:
                values = [alt for _t, alt in points[left:right + 1]]
                candidates.append({
                    "start_s": float(start),
                    "end_s": float(end),
                    "duration_s": float(end - start),
                    "rows": right - left + 1,
                    "altitude_span_m": float(max(values) - min(values)),
                    "altitude_source": altitude_source,
                })
    if not candidates:
        return []
    best_by_end = {}
    for candidate in candidates:
        key = round(candidate["end_s"], 3)
        if key not in best_by_end or candidate["duration_s"] > best_by_end[key]["duration_s"]:
            best_by_end[key] = candidate
    return list(best_by_end.values())


def select_methodic_window(tables: dict[str, Any], selector: str = "methodic_hover", **kwargs: Any) -> dict[str, Any]:
    if selector not in SELECTORS:
        raise ValueError(f"Unknown Methodic window selector '{selector}'. Known selectors: {', '.join(sorted(SELECTORS))}")
    warnings: list[str] = []
    confidence = "medium"

    if selector == "active_flight_only":
        candidates, criteria, candidate_warnings, excluded = _stable_hover_candidates(tables, min_duration_s=kwargs.get("min_duration_s", 1.0), alt_span_max_m=999.0)
    elif selector == "first_althold_hover":
        candidates, criteria, candidate_warnings, excluded = _stable_hover_candidates(tables, mode_filter="ALTHOLD", **kwargs)
    elif selector == "post_takeoff_hover":
        candidates, criteria, candidate_warnings, excluded = _stable_hover_candidates(tables, min_alt_above_ground_m=kwargs.get("min_alt_above_ground_m", 0.4), **{k: v for k, v in kwargs.items() if k != "min_alt_above_ground_m"})
    elif selector == "descent_segment":
        candidates, criteria, candidate_warnings, excluded = _descent_candidates(tables, **kwargs)
    else:
        mode_filter = "ALTHOLD" if selector == "methodic_hover" else None
        candidates, criteria, candidate_warnings, excluded = _stable_hover_candidates(tables, mode_filter=mode_filter, **kwargs)
        if selector == "methodic_hover" and not candidates:
            fallback, fallback_criteria, fallback_warnings, excluded = _stable_hover_candidates(tables, **kwargs)
            candidates = fallback
            criteria = {**fallback_criteria, "methodic_hover_fallback": "stable_hover_without_mode_filter"}
            candidate_warnings.extend(fallback_warnings)
            warnings.append("No ALTHOLD hover candidate was found; fell back to stable hover selection.")
            confidence = "low"

    warnings.extend(candidate_warnings)
    selected = max(candidates, key=lambda item: item.get("duration_s", 0.0), default=None)
    if selected is None:
        confidence = "low"
        warnings.append(f"No candidate window found for selector {selector}.")
    rc = analyze_rc_input_contamination(tables)
    if rc.get("available") and rc.get("hands_off_confidence") == "low":
        warnings.append("RC input contamination is likely; hands-off oscillation conclusions need a centered-stick subset.")
        confidence = "low"

    return {
        "selector": selector,
        "selected_window": selected,
        "candidate_windows": candidates,
        "criteria_used": criteria,
        "warnings": warnings,
        "confidence": confidence if selected else "low",
        "takeoff_landing_spool_rows_excluded": bool(excluded.get("takeoff_landing_spool_rows_excluded")),
        "spool_rows_excluded": bool(excluded.get("takeoff_landing_spool_rows_excluded")),
        "rc_input_context": {
            "hands_off_confidence": rc.get("hands_off_confidence"),
            "centered_percent": rc.get("centered_percent"),
            "warnings": rc.get("warnings", []),
        },
    }


def _descent_candidates(tables: dict[str, Any], *, min_duration_s: float = 2.0, descent_rate_min_m_s: float = 0.2, **_kwargs: Any):
    warnings = []
    df, alt_col, alt_source = _altitude_series(tables)
    if df is None or alt_col is None:
        return [], {}, ["No altitude source available for descent selection."], {"takeoff_landing_spool_rows_excluded": False}
    times = _times(df)
    alts = _values(df, [alt_col])
    mask = [False]
    for prev_t, t, prev_alt, alt in zip(times, times[1:], alts, alts[1:]):
        if None in (prev_t, t, prev_alt, alt) or t == prev_t:
            mask.append(False)
        else:
            mask.append(((prev_alt - alt) / (t - prev_t)) >= descent_rate_min_m_s)
    candidates = _intervals_from_mask(times, mask, min_duration_s=min_duration_s)
    criteria = {"min_duration_s": min_duration_s, "descent_rate_min_m_s": descent_rate_min_m_s, "altitude_source": f"{alt_source}.{alt_col}"}
    return candidates, criteria, warnings, {"takeoff_landing_spool_rows_excluded": False}
