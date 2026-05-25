#!/usr/bin/env python3
"""ArduPilot flight-mode helpers.

DataFlash MODE rows are not consistent across logs: some contain human-readable
names and others contain numeric mode ids. The mapping below is Copter-specific.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional, Sequence, Set


COPTER_MODE_BY_ID = {
    0: "STABILIZE",
    1: "ACRO",
    2: "ALTHOLD",
    3: "AUTO",
    4: "GUIDED",
    5: "LOITER",
    6: "RTL",
    7: "CIRCLE",
    9: "LAND",
    11: "DRIFT",
    13: "SPORT",
    14: "FLIP",
    15: "AUTOTUNE",
    16: "POSHOLD",
    17: "BRAKE",
    18: "THROW",
    19: "AVOID_ADSB",
    20: "GUIDED_NOGPS",
    21: "SMART_RTL",
    22: "FLOWHOLD",
    23: "FOLLOW",
    24: "ZIGZAG",
    25: "SYSTEMID",
    26: "AUTOROTATE",
    27: "AUTO_RTL",
}

_MODE_ID_BY_NAME = {name: mode_id for mode_id, name in COPTER_MODE_BY_ID.items()}
_ALIASES = {
    "ALT_HOLD": "ALTHOLD",
    "ALTHOLD": "ALTHOLD",
    "POS_HOLD": "POSHOLD",
    "POSHOLD": "POSHOLD",
    "SMARTRTL": "SMART_RTL",
    "SMART_RTL": "SMART_RTL",
    "GUIDED_NO_GPS": "GUIDED_NOGPS",
    "GUIDED_NOGPS": "GUIDED_NOGPS",
}


def _clean(value: Any) -> str:
    text = str(value).strip().upper()
    if text.endswith(".0"):
        text = text[:-2]
    return text


def _int_value(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        number = float(str(value).strip())
    except Exception:
        return None
    if not number.is_integer():
        return None
    return int(number)


def _canonical_name(value: Any) -> Optional[str]:
    text = _clean(value)
    if not text:
        return None
    if text in _ALIASES:
        return _ALIASES[text]
    compact = text.replace("_", "")
    for alias, canonical in _ALIASES.items():
        if alias.replace("_", "") == compact:
            return canonical
    if text in _MODE_ID_BY_NAME:
        return text
    return text


def decode_copter_mode(value: Any) -> str | None:
    """Decode a Copter numeric mode id or normalize a named mode."""
    mode_id = _int_value(value)
    if mode_id is not None:
        return COPTER_MODE_BY_ID.get(mode_id)
    name = _canonical_name(value)
    if name and name in _MODE_ID_BY_NAME:
        return name
    return None


def normalise_mode_query(value: Any) -> Set[str]:
    """Return exact accepted aliases for a user/log mode value.

    The returned set always contains upper-case strings. For known Copter modes
    it includes both the numeric id and accepted spellings.
    """
    aliases: Set[str] = set()
    text = _clean(value)
    if text:
        aliases.add(text)
        aliases.add(text.replace("_", ""))
    mode_id = _int_value(value)
    decoded = decode_copter_mode(value)
    if decoded:
        aliases.add(decoded)
        aliases.add(decoded.replace("_", ""))
        mapped_id = _MODE_ID_BY_NAME.get(decoded)
        if mapped_id is not None:
            aliases.add(str(mapped_id))
    elif mode_id is not None:
        aliases.add(str(mode_id))
    canonical = _canonical_name(value)
    if canonical:
        aliases.add(canonical)
        aliases.add(canonical.replace("_", ""))
        mapped_id = _MODE_ID_BY_NAME.get(canonical)
        if mapped_id is not None:
            aliases.add(str(mapped_id))
    for alias, canonical_name in _ALIASES.items():
        if canonical and canonical_name == canonical:
            aliases.add(alias)
            aliases.add(alias.replace("_", ""))
    return {a for a in aliases if a}


def mode_matches(log_value: Any, query: Any, vehicle_scope: Any = None) -> bool:
    """Return whether a raw log mode value matches a query by id or name.

    Numeric decoding is Copter-specific. When the caller has not confirmed a
    Copter log this still performs the Copter lookup as a heuristic; summaries
    should label that caveat for the agent.
    """
    return bool(normalise_mode_query(log_value) & normalise_mode_query(query))


def mode_label(raw_value: Any) -> str:
    decoded = decode_copter_mode(raw_value)
    if decoded:
        return decoded
    raw = "" if raw_value is None else _clean(raw_value)
    return f"UNKNOWN_COPTER_MODE_{raw}" if raw else "UNKNOWN"


def mode_decoding_note(vehicle_scope: Any = None) -> str:
    primary = None
    if isinstance(vehicle_scope, dict):
        primary = vehicle_scope.get("primary_vehicle") or vehicle_scope.get("vehicle")
    elif vehicle_scope is not None:
        primary = str(vehicle_scope)
    text = str(primary or "").lower()
    if "copter" in text:
        return "Mode names are decoded with the ArduCopter mode table."
    if text:
        return "Mode names use the ArduCopter mode table as a labelled heuristic because this log is not confirmed Copter."
    return "Mode names use the ArduCopter mode table as a labelled heuristic because vehicle type is unknown."


def first_present(row: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in row and row.get(key) is not None:
            return row.get(key)
    return None


def mode_timeline_from_rows(rows: Sequence[dict[str, Any]], log_end_s: Any = None) -> list[dict[str, Any]]:
    timed = []
    for row in rows:
        try:
            start = float(row.get("time_s") if row.get("time_s") is not None else row.get("TimeS"))
        except Exception:
            continue
        raw_mode = first_present(row, ["raw_mode", "Mode", "ModeNum", "Name", "mode"])
        timed.append({"start_s": start, "raw_mode": raw_mode, "decoded_mode": mode_label(raw_mode)})
    timed.sort(key=lambda item: item["start_s"])
    try:
        final_end = float(log_end_s) if log_end_s is not None else None
    except Exception:
        final_end = None
    out = []
    for i, item in enumerate(timed):
        end = timed[i + 1]["start_s"] if i + 1 < len(timed) else final_end
        duration = None if end is None else max(0.0, end - item["start_s"])
        out.append({**item, "end_s": end, "duration_s": duration, "mode": item["decoded_mode"]})
    return out
