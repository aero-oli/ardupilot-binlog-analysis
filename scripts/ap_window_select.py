from __future__ import annotations

from ap_common import AnalysisError, get_col, parse_time_window, safe_float


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


def _mode_window(tables, mode, log_end_s=None):
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
    wanted = str(mode).lower()
    for i, row in enumerate(rows):
        current = str(row.get(mode_col, "")).lower()
        if wanted not in current:
            continue
        start = safe_float(row.get("TimeS"))
        end = safe_float(rows[i + 1].get("TimeS")) if i + 1 < len(rows) else log_end_s
        if end is None:
            _start, end = _table_time_bounds(tables)
        if start is not None and end is not None and end >= start:
            intervals.append({"start_s": start, "end_s": end})
    if not intervals:
        raise AnalysisError(f"--mode {mode!r} did not match any MODE intervals")
    selection = _window(intervals[0]["start_s"], intervals[-1]["end_s"], "mode", source=str(mode), intervals=intervals)
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


def _hover_window(tables):
    if "CTUN" not in tables:
        raise AnalysisError("--hover-candidates requested but CTUN data is missing")
    df = tables["CTUN"]
    alt_col = get_col(df, ["Alt", "BAlt", "DAlt"])
    thr_col = get_col(df, ["ThO", "ThH"])
    if "TimeS" not in df.columns or not alt_col:
        raise AnalysisError("--hover-candidates requested but CTUN TimeS/Alt data is missing")
    data = df[["TimeS", alt_col] + ([thr_col] if thr_col else [])].dropna(subset=["TimeS", alt_col]).sort_values("TimeS")
    if len(data) < 3:
        raise AnalysisError("--hover-candidates requested but too few CTUN rows are available")
    candidates = []
    for i in range(len(data) - 2):
        chunk = data.iloc[i:i + 3]
        alt_span = float(chunk[alt_col].max() - chunk[alt_col].min())
        duration = float(chunk["TimeS"].iloc[-1] - chunk["TimeS"].iloc[0])
        if duration <= 0 or alt_span > 0.75:
            continue
        if thr_col:
            throttle = chunk[thr_col].mean()
            if throttle < 0.25 or throttle > 0.75:
                continue
        candidates.append({"start_s": float(chunk["TimeS"].iloc[0]), "end_s": float(chunk["TimeS"].iloc[-1])})
    if not candidates:
        raise AnalysisError("--hover-candidates requested but no stable-altitude moderate-throttle window was detected")
    best = max(candidates, key=lambda item: item["end_s"] - item["start_s"])
    return _window(best["start_s"], best["end_s"], "hover_candidates", source="CTUN", intervals=candidates[:10])


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
    high_throttle_only=False,
    around_radius_s=10.0,
    high_throttle_percentile=90.0,
    high_throttle_threshold=None,
    log_end_s=None,
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
        return _mode_window(tables, mode, log_end_s=log_end_s)
    if around_msg:
        return _around_msg_window(tables, around_msg, around_radius_s)
    if around_event:
        return _around_event_window(tables, around_event, around_radius_s)
    if around_error:
        return _around_error_window(tables, around_radius_s)
    if takeoff_only:
        return _takeoff_window(tables)
    if hover_candidates:
        return _hover_window(tables)
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
