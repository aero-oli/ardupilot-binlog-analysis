from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ap_common import params_from_tables, safe_float
from ap_symptom_map import requirement_spec

METADATA_CAVEAT = "Parameter metadata may not exactly match the firmware that produced the log. Latest-source metadata may include unreleased, renamed, or removed parameters. Use metadata as explanatory context, not proof of firmware-specific behaviour or automatic parameter-change advice."
BITMASK_CAVEAT = "Bit definitions may vary by vehicle and firmware; decode using the metadata source as explanatory context only."
METADATA_DIR = Path(__file__).resolve().parents[1] / "references" / "parameter-metadata"
LOG_BITMASK_MESSAGE_FAMILIES = {
    "PIDY": ["PID"],
    "PIDR": ["PID"],
    "PIDP": ["PID"],
    "PIDA": ["PID"],
    "RCIN": ["RC input"],
    "RCOU": ["RC output"],
    "RCO2": ["RC output"],
    "RCO3": ["RC output"],
    "GPS": ["GPS"],
    "GPS2": ["GPS"],
    "GPA": ["GPS"],
    "MAG": ["Compass"],
    "CTUN": ["Control Tuning"],
    "NTUN": ["Navigation Tuning"],
    "BAT": ["Battery Monitor"],
    "POWR": ["Battery Monitor"],
    "IMU": ["IMU", "Fast IMU", "Raw IMU"],
    "GYR": ["IMU", "Fast IMU", "Raw IMU"],
    "ACC": ["IMU", "Fast IMU", "Raw IMU"],
}


def _combined_parameter_values(index: Optional[Dict[str, Any]] = None, tables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    params: Dict[str, Any] = {}
    if index:
        params.update(index.get("parameters", {}) or {})
    if tables:
        params.update(params_from_tables(tables))
    return params


def _parameter_defaults(index: Optional[Dict[str, Any]] = None, tables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    defaults: Dict[str, Any] = {}
    if index:
        defaults.update(index.get("parameter_defaults", {}) or {})
    parm = (tables or {}).get("PARM")
    if parm is not None and hasattr(parm, "to_dict"):
        for row in parm.to_dict(orient="records"):
            name = row.get("Name")
            if not name or "Default" not in row:
                continue
            defaults[str(name)] = row.get("Default")
    return defaults


def _is_pattern(name: str) -> bool:
    return any(token in name for token in ["*", "?", "["])


def _pattern_matches(pattern: str, names: Sequence[str]) -> List[str]:
    regex_pattern = pattern
    if "x" in pattern and not _is_pattern(pattern):
        regex_pattern = pattern.replace("x", "*")
    return sorted(name for name in names if fnmatch.fnmatchcase(name, regex_pattern))


def _parameter_sort_key(name: str):
    parts = re.split(r"(\d+)", name)
    return [int(part) if part.isdigit() else part for part in parts]


def _numeric_flags(value: Any, default: Any) -> Dict[str, Any]:
    value_f = safe_float(value)
    default_f = safe_float(default)
    is_zero = value_f is not None and abs(value_f) <= 1e-12
    is_default = None
    if default is not None:
        if value_f is not None and default_f is not None:
            is_default = abs(value_f - default_f) <= 1e-12
        else:
            is_default = str(value) == str(default)
    return {"is_zero": bool(is_zero), "is_default": is_default}


def load_parameter_metadata(vehicle: str = "ArduCopter", metadata_path: Optional[str | Path] = None) -> Dict[str, Any]:
    path = Path(metadata_path) if metadata_path else METADATA_DIR / f"{vehicle}-latest.min.json"
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {"metadata_version": None, "source_vehicle": vehicle, "caveat": METADATA_CAVEAT, "parameters": [], "metadata_missing": True}
    data.setdefault("caveat", METADATA_CAVEAT)
    data["_path"] = str(path)
    return data


def _metadata_entries(metadata: Dict[str, Any]) -> List[Dict[str, Any]]:
    return list(metadata.get("parameters", []) or [])


def find_parameter_metadata(name: str, metadata: Optional[Dict[str, Any]] = None, vehicle: str = "ArduCopter") -> Optional[Dict[str, Any]]:
    metadata = metadata or load_parameter_metadata(vehicle)
    name = str(name)
    entries = _metadata_entries(metadata)
    for entry in entries:
        if entry.get("name") == name:
            return dict(entry)
    wildcard_matches = []
    for entry in entries:
        pattern = str(entry.get("name", ""))
        if "*" in pattern and fnmatch.fnmatchcase(name, pattern):
            wildcard_matches.append(entry)
    if wildcard_matches:
        return dict(sorted(wildcard_matches, key=lambda item: len(str(item.get("name", ""))), reverse=True)[0])
    if re.match(r"SERVO\d+_FUNCTION$", name):
        for entry in entries:
            if entry.get("name") == "SERVO*_FUNCTION":
                return dict(entry)
    return None


def _decode_enum(value: Any, metadata_entry: Optional[Dict[str, Any]]) -> Optional[str]:
    if not metadata_entry:
        return None
    values = metadata_entry.get("values") or {}
    if value is None or not values:
        return None
    value_f = safe_float(value)
    keys = [str(value)]
    if value_f is not None:
        keys.extend([str(int(value_f)), str(float(value_f))])
    for key in keys:
        if key in values:
            return values[key]
    return None


def decode_bitmask(value: Any, metadata_bitmask: Optional[Dict[str, Any]]) -> List[str]:
    value_f = safe_float(value)
    if value_f is None or not metadata_bitmask:
        return []
    value_i = int(value_f)
    enabled = []
    for bit, label in sorted(metadata_bitmask.items(), key=lambda item: int(item[0]) if str(item[0]).isdigit() else str(item[0])):
        try:
            bit_i = int(bit)
        except Exception:
            continue
        if value_i & (1 << bit_i):
            enabled.append(str(label))
    return enabled


def _decode_bitmask(value: Any, metadata_entry: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not metadata_entry:
        return []
    bitmask = metadata_entry.get("bitmask") or {}
    value_f = safe_float(value)
    if value_f is None or not bitmask:
        return []
    value_i = int(value_f)
    decoded = []
    for bit, label in sorted(bitmask.items(), key=lambda item: int(item[0]) if str(item[0]).isdigit() else str(item[0])):
        try:
            bit_i = int(bit)
        except Exception:
            continue
        decoded.append({"bit": bit_i, "label": label, "set": bool(value_i & (1 << bit_i))})
    return decoded


def log_bitmask_missing_guidance(value: Any, metadata_entry: Optional[Dict[str, Any]], missing_messages: Sequence[str]) -> List[Dict[str, Any]]:
    if not metadata_entry:
        return []
    bitmask = metadata_entry.get("bitmask") or {}
    if not bitmask:
        return []
    enabled = set(decode_bitmask(value, bitmask))
    out = []
    for message in missing_messages:
        families = LOG_BITMASK_MESSAGE_FAMILIES.get(str(message).upper(), [])
        absent = [family for family in families if family not in enabled]
        if absent:
            out.append({
                "message": str(message).upper(),
                "likely_logging_families": families,
                "absent_logging_families": absent,
                "explanation": f"{message} may be missing because LOG_BITMASK does not appear to enable: {', '.join(absent)}.",
                "caveat": BITMASK_CAVEAT,
            })
    return out


def enrich_parameter_entry(entry: Dict[str, Any], metadata: Optional[Dict[str, Any]] = None, vehicle: str = "ArduCopter") -> Dict[str, Any]:
    metadata = metadata or load_parameter_metadata(vehicle)
    meta = find_parameter_metadata(entry.get("name"), metadata=metadata, vehicle=vehicle)
    out = dict(entry)
    out["metadata_caveat"] = metadata.get("caveat") or METADATA_CAVEAT
    if not meta:
        out["metadata_missing"] = True
        return out
    out["metadata_missing"] = False
    for key in ["display_name", "description", "units", "range", "values", "bitmask", "user_level", "reboot_required", "source_vehicle", "metadata_version", "source_url", "source_note"]:
        if key in meta:
            out[key] = meta.get(key)
    out["enum_value"] = _decode_enum(out.get("value"), meta)
    bitmask_decode = _decode_bitmask(out.get("value"), meta)
    if bitmask_decode:
        out["bitmask_decode"] = bitmask_decode
        out["decoded_bits"] = decode_bitmask(out.get("value"), meta.get("bitmask"))
        out["bitmask_caveat"] = BITMASK_CAVEAT
    return out


def select_relevant_parameters(
    symptom_class: str,
    index: Optional[Dict[str, Any]] = None,
    tables: Optional[Dict[str, Any]] = None,
    enrich_metadata: bool = True,
    vehicle: str = "ArduCopter",
) -> Dict[str, Any]:
    """Return concise symptom-relevant parameter context.

    Exact names from the YAML map are reported as missing when absent. Wildcard
    entries, such as SERVO*_FUNCTION, expand only to parameters present in the
    log so output remains compact.
    """
    spec = requirement_spec(symptom_class)
    selectors = list(spec.get("relevant_parameters", []))
    params = _combined_parameter_values(index=index, tables=tables)
    defaults = _parameter_defaults(index=index, tables=tables)
    names = sorted(params.keys(), key=_parameter_sort_key)
    selected_names: List[str] = []
    missing: List[str] = []
    unmatched_patterns: List[str] = []

    for selector in selectors:
        selector = str(selector).strip()
        if not selector:
            continue
        if _is_pattern(selector) or ("x" in selector and selector.upper().startswith("SERVO")):
            matches = _pattern_matches(selector, names)
            if not matches:
                unmatched_patterns.append(selector)
            for name in matches:
                if name not in selected_names:
                    selected_names.append(name)
            continue
        if selector in params:
            if selector not in selected_names:
                selected_names.append(selector)
        else:
            missing.append(selector)

    metadata = load_parameter_metadata(vehicle) if enrich_metadata else None
    selected = []
    default_or_zero = []
    for name in sorted(selected_names, key=_parameter_sort_key):
        value = params.get(name)
        default = defaults.get(name)
        flags = _numeric_flags(value, default)
        entry = {
            "name": name,
            "value": value,
            "default": default,
            "is_zero": flags["is_zero"],
            "is_default": flags["is_default"],
        }
        if metadata is not None:
            entry = enrich_parameter_entry(entry, metadata=metadata, vehicle=vehicle)
        selected.append(entry)
        reasons = []
        if flags["is_zero"]:
            reasons.append("zero")
        if flags["is_default"] is True:
            reasons.append("matches_default")
        if reasons:
            default_or_zero.append({"name": name, "value": value, "default": default, "reasons": reasons})

    limitation = None
    if not params:
        limitation = "No PARM/index parameter values were available; parameter context cannot be shown."
    elif unmatched_patterns:
        limitation = "Some YAML parameter selector patterns matched no logged parameters: " + ", ".join(unmatched_patterns)

    return {
        "symptom_class": symptom_class,
        "selectors": selectors,
        "selected": selected,
        "missing": missing,
        "default_or_zero": default_or_zero,
        "unmatched_patterns": unmatched_patterns,
        "parameter_count_available": len(params),
        "limitation": limitation,
        "note": "Parameter values are context for investigation only; this tool does not recommend parameter changes automatically.",
        "metadata_caveat": (metadata or {}).get("caveat") if metadata is not None else METADATA_CAVEAT,
    }
