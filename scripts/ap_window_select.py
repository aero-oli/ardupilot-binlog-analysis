from __future__ import annotations

from collections import deque

from ap_common import AnalysisError, get_col, parse_time_window, safe_float
from ap_modes import first_present, mode_decoding_note, mode_label, mode_matches


def _table_time_bounds(tables):
    starts = []
    ends = []
    for df in tables.values():
        if df is None or not hasattr(df, "columns") or "TimeS" not in df.columns:
            continue
        times = df["TimeS"].dropna()
        if len(times):
            starts.append(float(times.min()))
            ends.append(float(times.max()))
    return (min(starts), max(ends)) if starts and ends else (None, None)


def _window(start, end, rule, source=None, intervals=None, warnings=None):
    if start is None or end is None:
        raise AnalysisError(f"Window selector '{rule}' could not determine a valid time range")
    start = max(0.0, float(start))
    end = float(end)
    if end < start:
        raise AnalysisError(f"Window selector '{rule}' produced an invalid range")
    selected_interval = {"start_s": start, "end_s": end}
    normalized_intervals = intervals or [selected_interval]
    return {
        "start_s": start,
        "end_s": end,
        "rule": rule,
        "source": source,
        "intervals": normalized_intervals,
        "intervals_found": normalized_intervals,
        "intervals_used": [selected_interval],
        "non_matching_gaps_excluded": False,
        "warnings": warnings or [],
    }


def _first_text_match(df, pattern, columns):
    text = str(pattern).lower()
    for row in df.sort_values("TimeS").to_dict(orient="records"):
        haystack = " ".join(str(row.get(c, "")) for c in columns).lower()
        if text in haystack:
            return row
    return None


def _around(center, radius, rule, source):
    radius = float(radius)
    return _window(center - radius, center + radius, rule, source=source)


def _mode_window(tables, mode, log_end_s=None, vehicle_scope=None):
    if "MODE" not in tables:
        raise AnalysisError("--mode requested but MODE data is missing")
    df = tables["MODE"]
    if "TimeS" not in df.columns:
        raise AnalysisError("--mode requested but MODE.TimeS is missing")
    mode_col = get_col(df, ["Mode", "Name", "ModeNum"])
    if not mode_col:
        raise AnalysisError("--mode requested but MODE has no Mode/Name/ModeNum field")
    rows = df.dropna(subset=["TimeS"]).sort_values("TimeS").to_dict(orient="records")
    intervals = []
    interval_modes = []
    for i, row in enumerate(rows):
        candidates = [row.get(c) for c in ["Mode", "Name", "ModeNum"] if c in row]
        if not any(mode_matches(candidate, mode, vehicle_scope=vehicle_scope) for candidate in candidates):
            continue
        start = safe_float(row.get("TimeS"))
        end = safe_float(rows[i + 1].get("TimeS")) if i + 1 < len(rows) else log_end_s
        if end is None:
            _start, end = _table_time_bounds(tables)
        if start is not None and end is not None and end >= start:
            raw_mode = first_present(row, ["Mode", "ModeNum", "Name"])
            intervals.append({"start_s": start, "end_s": end})
            interval_modes.append({"start_s": start, "end_s": end, "raw_mode": raw_mode, "decoded_mode": mode_label(raw_mode)})
    if not intervals:
        raise AnalysisError(f"--mode {mode!r} did not match any MODE intervals")
    selection = _window(intervals[0]["start_s"], intervals[-1]["end_s"], "mode", source=str(mode), intervals=intervals)
    selection["decoded_source"] = mode_label(mode)
    selection["mode_decoding"] = mode_decoding_note(vehicle_scope)
    selection["mode_intervals"] = interval_modes
    selection["intervals_found"] = intervals
    selection["intervals_used"] = intervals
    selection["non_matching_gaps_excluded"] = len(intervals) > 1
    if len(intervals) > 1:
        selection["warnings"].append(
            f"--mode {mode!r} matched {len(intervals)} separate intervals; non-matching gaps were excluded."
        )
    return selection


def _around_msg_window(tables, text, radius):
    if "MSG" not in tables:
        raise AnalysisError("--around-msg requested but MSG data is missing")
    df = tables["MSG"]
    if "TimeS" not in df.columns:
        raise AnalysisError("--around-msg requested but MSG.TimeS is missing")
    cols = [c for c in ["Message", "Msg", "Text"] if c in df.columns]
    if not cols:
        raise AnalysisError("--around-msg requested but MSG has no text field")
    row = _first_text_match(df, text, cols)
    if not row:
        raise AnalysisError(f"--around-msg {text!r} did not match any MSG rows")
    return _around(float(row["TimeS"]), radius, "around_msg", source=str(text))


def _around_event_window(tables, text, radius):
    for name in ["EV", "MSG", "MODE"]:
        if name not in tables:
            continue
        df = tables[name]
        if "TimeS" not in df.columns:
            continue
        cols = [c for c in df.columns if c != "TimeS"]
        row = _first_text_match(df, text, cols)
        if row:
            return _around(float(row["TimeS"]), radius, "around_event", source=f"{name}:{text}")
    raise AnalysisError(f"--around-event {text!r} did not match EV/MSG/MODE rows")


def _around_error_window(tables, radius):
    if "ERR" not in tables:
        raise AnalysisError("--around-error requested but ERR data is missing")
    df = tables["ERR"]
    if "TimeS" not in df.columns or len(df.dropna(subset=["TimeS"])) == 0:
        raise AnalysisError("--around-error requested but ERR has no timed rows")
    first = float(df.dropna(subset=["TimeS"]).sort_values("TimeS").iloc[0]["TimeS"])
    return _around(first, radius, "around_error", source="ERR")


def _takeoff_window(tables):
    for name in ["CTUN", "BARO", "GPS"]:
        if name not in tables:
            continue
        df = tables[name]
        if "TimeS" not in df.columns:
            continue
        alt_col = get_col(df, ["Alt", "BAlt", "RelAlt"])
        if not alt_col:
            continue
        data = df[["TimeS", alt_col]].dropna().sort_values("TimeS")
        if len(data) < 2:
            continue
        base = float(data[alt_col].iloc[0])
        moving = data[data[alt_col] > base + 0.5]
        climbed = data[data[alt_col] > base + 3.0]
        if len(moving) and len(climbed):
            return _window(float(moving.iloc[0]["TimeS"]), float(climbed.iloc[0]["TimeS"]), "takeoff_only", source=name)
    raise AnalysisError("--takeoff-only requested but no takeoff-like altitude rise was detected")


def _interval_numeric_values(df, start_s, end_s, columns):
    if df is None or not hasattr(df, "columns") or "TimeS" not in df.columns:
        return []
    col = get_col(df, columns)
    if not col:
        return []
    data = df[["TimeS", col]].dropna().sort_values("TimeS")
    values = []
    for row in data.to_dict(orient="records"):
        t = safe_float(row.get("TimeS"))
        v = safe_float(row.get(col))
        if t is None or v is None or t < start_s or t > end_s:
            continue
        values.append(v)
    return values


def _hover_window(
    tables,
    min_duration_s=5.0,
    alt_span_max_m=0.75,
    throttle_min=0.25,
    throttle_max=0.75,
):
    if "CTUN" not in tables:
        raise AnalysisError("--hover-candidates requested but CTUN data is missing")
    min_duration_s = float(min_duration_s)
    alt_span_max_m = float(alt_span_max_m)
    throttle_min = float(throttle_min)
    throttle_max = float(throttle_max)
    if min_duration_s <= 0:
        raise AnalysisError("--hover-min-duration must be greater than zero")
    if alt_span_max_m < 0:
        raise AnalysisError("--hover-alt-span-max must be non-negative")
    if throttle_min > throttle_max:
        raise AnalysisError("--hover-throttle-min must be less than or equal to --hover-throttle-max")
    df = tables["CTUN"]
    alt_col = get_col(df, ["Alt", "BAlt", "DAlt"])
    thr_col = get_col(df, ["ThO", "ThH"])
    if "TimeS" not in df.columns or not alt_col:
        raise AnalysisError("--hover-candidates requested but CTUN TimeS/Alt data is missing")
    data = df[["TimeS", alt_col] + ([thr_col] if thr_col else [])].dropna(subset=["TimeS", alt_col]).sort_values("TimeS")
    if len(data) < 2:
        raise AnalysisError("--hover-candidates requested but too few CTUN rows are available")
    gps_speed_max_m_s = 1.0
    attitude_max_abs_deg = 10.0
    criteria = {
        "min_duration_s": min_duration_s,
        "alt_span_max_m": alt_span_max_m,
        "throttle_min": throttle_min,
        "throttle_max": throttle_max,
        "gps_speed_max_m_s": gps_speed_max_m_s,
        "attitude_max_abs_deg": attitude_max_abs_deg,
    }
    candidates = []
    records = data.to_dict(orient="records")
    left = 0
    alt_min = deque()
    alt_max = deque()
    throttle_min_q = deque()
    throttle_max_q = deque()
    for right, row in enumerate(records):
        alt = safe_float(row.get(alt_col))
        if alt is None:
            left = right + 1
            alt_min.clear(); alt_max.clear(); throttle_min_q.clear(); throttle_max_q.clear()
            continue
        throttle = safe_float(row.get(thr_col)) if thr_col else None
        if thr_col and (throttle is None or throttle < throttle_min or throttle > throttle_max):
            left = right + 1
            alt_min.clear(); alt_max.clear(); throttle_min_q.clear(); throttle_max_q.clear()
            continue
        while alt_min and alt_min[-1][1] > alt:
            alt_min.pop()
        alt_min.append((right, alt))
        while alt_max and alt_max[-1][1] < alt:
            alt_max.pop()
        alt_max.append((right, alt))
        if thr_col:
            while throttle_min_q and throttle_min_q[-1][1] > throttle:
                throttle_min_q.pop()
            throttle_min_q.append((right, throttle))
            while throttle_max_q and throttle_max_q[-1][1] < throttle:
                throttle_max_q.pop()
            throttle_max_q.append((right, throttle))
        while alt_min and alt_max and (alt_max[0][1] - alt_min[0][1]) > alt_span_max_m:
            left += 1
            while alt_min and alt_min[0][0] < left:
                alt_min.popleft()
            while alt_max and alt_max[0][0] < left:
                alt_max.popleft()
            while throttle_min_q and throttle_min_q[0][0] < left:
                throttle_min_q.popleft()
            while throttle_max_q and throttle_max_q[0][0] < left:
                throttle_max_q.popleft()
        if left > right or not alt_min or not alt_max:
            continue
        start_s = safe_float(records[left].get("TimeS"))
        end_s = safe_float(row.get("TimeS"))
        if start_s is None or end_s is None:
            continue
        duration = end_s - start_s
        if duration < min_duration_s:
            continue
        candidate = {
            "start_s": start_s,
            "end_s": end_s,
            "duration_s": duration,
            "alt_span_m": float(alt_max[0][1] - alt_min[0][1]),
        }
        if thr_col and throttle_min_q and throttle_max_q:
            candidate["throttle_min"] = float(throttle_min_q[0][1])
            candidate["throttle_max"] = float(throttle_max_q[0][1])
        if "GPS" in tables:
            speeds = _interval_numeric_values(tables["GPS"], start_s, end_s, ["Spd", "Speed", "GSpd"])
            if speeds:
                speed_max = max(speeds)
                if speed_max > gps_speed_max_m_s:
                    continue
                candidate["gps_speed_max_m_s"] = speed_max
        if "ATT" in tables:
            rolls = _interval_numeric_values(tables["ATT"], start_s, end_s, ["Roll"])
            pitches = _interval_numeric_values(tables["ATT"], start_s, end_s, ["Pitch"])
            attitude_values = [abs(v) for v in rolls + pitches]
            if attitude_values:
                attitude_max = max(attitude_values)
                if attitude_max > attitude_max_abs_deg:
                    continue
                candidate["attitude_max_abs_deg"] = attitude_max
        candidates.append(candidate)
    if not candidates:
        raise AnalysisError("--hover-candidates requested but no stable-altitude moderate-throttle window was detected")
    candidates = sorted(candidates, key=lambda item: (item["duration_s"], -item["alt_span_m"]), reverse=True)
    best = candidates[0]
    selection = _window(best["start_s"], best["end_s"], "hover_candidates", source="CTUN", intervals=candidates[:10])
    selection["criteria"] = criteria
    return selection


def _longest_true_interval(times, mask):
    intervals = []
    start = None
    last = None
    for t, ok in zip(times, mask):
        if ok and start is None:
            start = t
        if not ok and start is not None:
            intervals.append({"start_s": start, "end_s": last})
            start = None
        last = t
    if start is not None:
        intervals.append({"start_s": start, "end_s": last})
    if not intervals:
        return None, []
    return max(intervals, key=lambda item: item["end_s"] - item["start_s"]), intervals


def _high_throttle_window(tables, percentile=90.0, threshold=None):
    candidates = []
    if "CTUN" in tables:
        df = tables["CTUN"]
        col = get_col(df, ["ThO", "ThH"])
        if col and "TimeS" in df.columns:
            candidates.append(("CTUN", df[["TimeS", col]].dropna(), col))
    if "RCOU" in tables:
        df = tables["RCOU"]
        cols = [c for c in df.columns if str(c).startswith("C") and str(c)[1:].isdigit()]
        if cols and "TimeS" in df.columns:
            frame = df[["TimeS", *cols]].dropna(subset=["TimeS"]).copy()
            frame["ThrottleOut"] = frame[cols].max(axis=1)
            candidates.append(("RCOU", frame[["TimeS", "ThrottleOut"]].dropna(), "ThrottleOut"))
    for source, data, col in candidates:
        if len(data) < 2:
            continue
        series = data[col]
        cutoff = float(threshold) if threshold is not None else float(series.quantile(float(percentile) / 100.0))
        best, intervals = _longest_true_interval(data["TimeS"].astype(float).tolist(), (series >= cutoff).tolist())
        if best:
            return _window(best["start_s"], best["end_s"], "high_throttle_only", source=f"{source}.{col}>={cutoff:.2f}", intervals=intervals[:10])
    raise AnalysisError("--high-throttle-only requested but no throttle signal window was detected")


def selector_requested(**kwargs):
    return any(bool(v) for v in kwargs.values())


def select_analysis_window(
    tables,
    window=None,
    mode=None,
    armed_only=False,
    around_msg=None,
    around_event=None,
    around_error=False,
    takeoff_only=False,
    hover_candidates=False,
    hover_min_duration_s=5.0,
    hover_alt_span_max_m=0.75,
    hover_throttle_min=0.25,
    hover_throttle_max=0.75,
    high_throttle_only=False,
    around_radius_s=10.0,
    high_throttle_percentile=90.0,
    high_throttle_threshold=None,
    log_end_s=None,
    vehicle_scope=None,
):
    requested = [
        bool(window),
        bool(mode),
        bool(around_msg),
        bool(around_event),
        bool(around_error),
        bool(takeoff_only),
        bool(hover_candidates),
        bool(high_throttle_only),
    ]
    if sum(1 for item in requested if item) > 1:
        raise AnalysisError("Only one analysis-window selector can be requested at a time")
    if window:
        parsed = parse_time_window(window)
        table_start, table_end = _table_time_bounds(tables)
        if parsed["start_s"] is None:
            parsed["start_s"] = table_start
        if parsed["end_s"] is None:
            parsed["end_s"] = table_end
        return _window(parsed["start_s"], parsed["end_s"], "window", source=window)
    if mode:
        return _mode_window(tables, mode, log_end_s=log_end_s, vehicle_scope=vehicle_scope)
    if around_msg:
        return _around_msg_window(tables, around_msg, around_radius_s)
    if around_event:
        return _around_event_window(tables, around_event, around_radius_s)
    if around_error:
        return _around_error_window(tables, around_radius_s)
    if takeoff_only:
        return _takeoff_window(tables)
    if hover_candidates:
        return _hover_window(
            tables,
            min_duration_s=hover_min_duration_s,
            alt_span_max_m=hover_alt_span_max_m,
            throttle_min=hover_throttle_min,
            throttle_max=hover_throttle_max,
        )
    if high_throttle_only:
        return _high_throttle_window(tables, percentile=high_throttle_percentile, threshold=high_throttle_threshold)
    start, end = _table_time_bounds(tables)
    return {
        "start_s": start,
        "end_s": end,
        "rule": "whole_log",
        "source": None,
        "intervals": [] if start is None or end is None else [{"start_s": start, "end_s": end}],
        "intervals_found": [] if start is None or end is None else [{"start_s": start, "end_s": end}],
        "intervals_used": [] if start is None or end is None else [{"start_s": start, "end_s": end}],
        "non_matching_gaps_excluded": False,
        "warnings": ["Whole-log analysis window used because no selector was requested."],
        "armed_only": bool(armed_only),
    }
