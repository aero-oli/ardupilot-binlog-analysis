from __future__ import annotations

from typing import Any, Dict, Optional


UNKNOWN = "unknown"


def value_with_unit(name: str, value: Any, unit: Optional[str] = None) -> Dict[str, Any]:
    return {"name": name, "value": value, "unit": unit or unit_for_name(name)}


def unit_for_name(name: str, *, message: Optional[str] = None, field: Optional[str] = None) -> str:
    text = f"{message or ''}.{field or name}".lower()
    key = str(field or name).lower()
    if key in {"times", "time_s", "timeus", "timems", "time", "ts"} or text.endswith("_s") or "duration_s" in text or "gap_s" in text:
        return "s"
    if "frequency_hz" in text or "sample_rate_hz" in text or key in {"freq", "frequency"}:
        return "Hz"
    if "voltage" in text or "volt" in text or "vcc" in text:
        return "V"
    if "current" in text or key in {"curr", "i"}:
        return "A"
    if "capacity" in text or "mah" in text or "currtot" in text:
        return "mAh"
    if "altitude" in text or key in {"alt", "balt", "dalt", "relalt"}:
        return "m"
    if "rate_error" in text or key in {"r", "p", "y", "rdes", "pdes", "ydes", "tar", "act", "err"} and (message or "").upper().startswith(("RATE", "PID")):
        return "deg/s"
    if "att_" in text or "yaw" in text or key in {"roll", "pitch", "yaw", "desroll", "despitch", "desyaw"}:
        return "deg"
    if "clip" in text:
        return "count"
    if "vibe" in text or key in {"accx", "accy", "accz", "ax", "ay", "az"}:
        return "m/s/s"
    if "rpm" in text:
        return "rpm"
    if "temp" in text:
        return "degC"
    if "pct" in text or key in {"inpct", "outpct"}:
        return "%"
    if "output_abs" in text or key in {"rout", "pout", "yout", "tho", "thh", "thi", "dmod"}:
        return "normalized"
    if key.startswith("c") and key[1:].isdigit():
        return "PWM us"
    if "hdop" in text or "hacc" in text or "test_ratio" in text or key in {"sv", "sp", "sh", "sm", "svt"}:
        return "ratio"
    if "nsats" in text or "samples" in text or "count" in text or "rows" in text:
        return "count"
    if "status" in text or "flags" in text:
        return "bitmask/enum"
    if key in {"amplitude", "pwr", "stress", "maxstress"}:
        return UNKNOWN
    return UNKNOWN


def units_for_keys(keys, *, message: Optional[str] = None) -> Dict[str, str]:
    return {str(key): unit_for_name(str(key), message=message, field=str(key)) for key in keys}


def add_units(summary: Dict[str, Any], *, message: Optional[str] = None) -> Dict[str, Any]:
    numeric_keys = [
        key for key, value in summary.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    if numeric_keys:
        summary["units"] = units_for_keys(numeric_keys, message=message)
    return summary
