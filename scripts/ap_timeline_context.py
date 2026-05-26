#!/usr/bin/env python3
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ap_common import get_col, safe_float
from ap_modes import mode_label


TIMELINE_HINTS = {
    "before_window": "Before-window timeline entries are setup/precondition evidence. They can explain context, but need linkage before being treated as the symptom cause.",
    "inside_window": "Inside-window timeline entries are candidate causal/supporting evidence because they overlap the selected symptom window.",
    "after_window": "After-window timeline entries are safety context for the next flight, not direct cause unless linked by timing or continuing symptoms.",
}


def _row_time(row: Dict[str, Any]) -> Optional[float]:
    return safe_float(row.get("TimeS", row.get("time_s")))


def _window_intervals(analysis_window: Optional[Dict[str, Any]]) -> List[Dict[str, float]]:
    window = analysis_window or {}
    intervals = []
    for interval in window.get("intervals_used") or []:
        start = safe_float(interval.get("start_s"))
        end = safe_float(interval.get("end_s"))
        if start is not None and end is not None:
            intervals.append({"start_s": start, "end_s": end})
    if intervals:
        return intervals
    start = safe_float(window.get("start_s"))
    end = safe_float(window.get("end_s"))
    if start is not None and end is not None:
        return [{"start_s": start, "end_s": end}]
    return []


def _classify_time(time_s: float, intervals: List[Dict[str, float]]) -> str:
    if not intervals:
        return "inside_window"
    if any(interval["start_s"] <= time_s <= interval["end_s"] for interval in intervals):
        return "inside_window"
    first_start = min(interval["start_s"] for interval in intervals)
    last_end = max(interval["end_s"] for interval in intervals)
    if time_s < first_start:
        return "before_window"
    if time_s > last_end:
        return "after_window"
    return "before_window"


def _append_event(events: List[Dict[str, Any]], source: str, time_s: Optional[float], label: str, row: Dict[str, Any]) -> None:
    if time_s is None:
        return
    events.append({
        "time_s": time_s,
        "source": source,
        "label": label,
        "row": {k: v for k, v in row.items() if not str(k).startswith("_")},
    })


def _timeline_events_from_tables(tables: Dict[str, Any]) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    mode = tables.get("MODE")
    if mode is not None and hasattr(mode, "to_dict"):
        col = get_col(mode, ["Mode", "Name", "ModeNum"])
        if col:
            for row in mode.to_dict(orient="records"):
                raw_mode = row.get(col)
                _append_event(events, "MODE", _row_time(row), f"MODE {mode_label(raw_mode)} ({raw_mode})", row)
    err = tables.get("ERR")
    if err is not None and hasattr(err, "to_dict"):
        for row in err.to_dict(orient="records"):
            _append_event(events, "ERR", _row_time(row), f"ERR {row.get('Subsys')}:{row.get('ECode')}", row)
    ev = tables.get("EV")
    if ev is not None and hasattr(ev, "to_dict"):
        for row in ev.to_dict(orient="records"):
            event_id = row.get("Id", row.get("ID"))
            _append_event(events, "EV", _row_time(row), f"EV {event_id}", row)
    msg = tables.get("MSG")
    if msg is not None and hasattr(msg, "to_dict"):
        text_col = get_col(msg, ["Message", "Msg", "Text"])
        if text_col:
            for row in msg.to_dict(orient="records"):
                text = str(row.get(text_col, ""))[:120]
                _append_event(events, "MSG", _row_time(row), f"MSG {text}", row)
    arm = tables.get("ARM")
    if arm is not None and hasattr(arm, "to_dict"):
        for row in arm.to_dict(orient="records"):
            state = row.get("Armed", row.get("ArmState", row.get("ARM", row.get("State"))))
            reason = row.get("Reason", row.get("Method", ""))
            suffix = f" {reason}" if reason not in {None, ""} else ""
            _append_event(events, "ARM", _row_time(row), f"ARM {state}{suffix}", row)
    return sorted(events, key=lambda item: item["time_s"])


def build_events_relative_to_window(tables: Dict[str, Any], analysis_window: Optional[Dict[str, Any]] = None, *, limit_per_bucket: int = 80) -> Dict[str, Any]:
    intervals = _window_intervals(analysis_window)
    buckets = {"before_window": [], "inside_window": [], "after_window": []}
    for event in _timeline_events_from_tables(tables):
        bucket = _classify_time(event["time_s"], intervals)
        if len(buckets[bucket]) < limit_per_bucket:
            buckets[bucket].append({**event, "relative_position": bucket})
    return {
        "window_intervals": intervals,
        "before_window": buckets["before_window"],
        "inside_window": buckets["inside_window"],
        "after_window": buckets["after_window"],
        "interpretation_hints": {
            key: ([TIMELINE_HINTS[key]] if buckets[key] else [])
            for key in ["before_window", "inside_window", "after_window"]
        },
        "note": "Timeline classification is a context aid. In-window entries carry more causal weight; before/after entries need timing linkage before being treated as causal.",
    }
