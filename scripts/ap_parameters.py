from __future__ import annotations

import fnmatch
import re
from typing import Any, Dict, List, Optional, Sequence

from ap_common import params_from_tables, safe_float
from ap_symptom_map import requirement_spec


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


def select_relevant_parameters(
    symptom_class: str,
    index: Optional[Dict[str, Any]] = None,
    tables: Optional[Dict[str, Any]] = None,
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
    }
