#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ap_common import read_json, safe_float, write_json
from ap_parameters import select_relevant_parameters


def _coerce_value(value: Any):
    numeric = safe_float(value)
    return value if numeric is None else numeric


def _looks_like_param_name(value: str) -> bool:
    return bool(re.match(r"^[A-Za-z][A-Za-z0-9_]*$", value or ""))


def _strip_inline_comment(line: str) -> str:
    for marker in ["#", ";"]:
        if marker in line:
            line = line.split(marker, 1)[0]
    return line.strip()


def parse_param_file(path: str | Path) -> Dict[str, Any]:
    path = Path(path)
    params: Dict[str, Any] = {}
    defaults: Dict[str, Any] = {}
    warnings = []
    formats = set()
    skipped = 0

    with path.open("r", encoding="utf-8", errors="replace") as f:
        for raw_line in f:
            line = _strip_inline_comment(raw_line.strip())
            if not line:
                continue
            if line.startswith("<") or line.startswith("{"):
                skipped += 1
                warnings.append("Structured XML/JSON parameter exports are not supported yet; skipped structured-looking line.")
                continue
            name = None
            value = None
            default = None
            if "," in line:
                parts = [p.strip() for p in line.split(",") if p.strip()]
                if len(parts) >= 2 and _looks_like_param_name(parts[0]):
                    name, value = parts[0], parts[1]
                    if len(parts) >= 3:
                        default = parts[2]
                    formats.add("comma_separated")
                elif len(parts) >= 4 and _looks_like_param_name(parts[2]):
                    name, value = parts[2], parts[3]
                    formats.add("qgroundcontrol")
            else:
                parts = line.split()
                if len(parts) >= 5 and parts[0].lstrip("-").isdigit() and parts[1].lstrip("-").isdigit() and _looks_like_param_name(parts[2]):
                    name, value = parts[2], parts[3]
                    formats.add("qgroundcontrol")
                elif len(parts) >= 2 and _looks_like_param_name(parts[0]):
                    name, value = parts[0], parts[1]
                    if len(parts) >= 3:
                        default = parts[2]
                    formats.add("name_value")
            if not name:
                skipped += 1
                continue
            params[name] = _coerce_value(value)
            if default is not None:
                defaults[name] = _coerce_value(default)

    if not params:
        warnings.append("No parameters were parsed from the external parameter file.")
    if skipped:
        warnings.append(f"Skipped {skipped} non-parameter lines.")
    if not formats:
        fmt = "unknown"
    elif len(formats) == 1:
        fmt = next(iter(formats))
    elif "qgroundcontrol" in formats:
        fmt = "qgroundcontrol_mixed"
    elif "comma_separated" in formats:
        fmt = "mission_planner_or_csv_mixed"
    else:
        fmt = "mixed"
    return {
        "parameters": params,
        "parameter_defaults": defaults,
        "source_file": str(path),
        "format_detected": fmt,
        "warnings": warnings,
    }


def merge_external_parameters(index: Optional[Dict[str, Any]] = None, external_context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    index = index or {}
    external_context = external_context or {}
    log_params = dict(index.get("parameters", {}) or {})
    log_defaults = dict(index.get("parameter_defaults", {}) or {})
    external_params = dict(external_context.get("parameters", {}) or {})
    external_defaults = dict(external_context.get("parameter_defaults", {}) or {})
    merged = dict(log_params)
    merged_defaults = dict(log_defaults)
    conflicts = []
    supplemented = []

    for name, value in external_params.items():
        if name in log_params:
            if str(log_params[name]) != str(value):
                conflicts.append({"name": name, "log_value": log_params[name], "external_value": value, "source_file": external_context.get("source_file")})
            continue
        merged[name] = value
        supplemented.append(name)
    for name, value in external_defaults.items():
        merged_defaults.setdefault(name, value)

    merged_index = dict(index)
    merged_index["parameters"] = merged
    merged_index["parameter_defaults"] = merged_defaults
    return {
        "index": merged_index,
        "parameters": merged,
        "parameter_defaults": merged_defaults,
        "parameter_conflicts": conflicts,
        "supplemented_parameters": sorted(supplemented),
        "parameter_source_precedence": "Log PARM/index parameters are primary flight evidence. External parameter files supplement missing parameters only. Conflicts preserve the logged value and are reported explicitly.",
        "external_parameter_context": external_context if external_context else None,
    }


def context_from_param_file(params_path: str | Path, symptom: Optional[str] = None, vehicle: str = "ArduCopter") -> Dict[str, Any]:
    external = parse_param_file(params_path)
    result = {
        "external_parameter_context": external,
        "parameter_source_precedence": "External parameter files are configuration context. Log PARM remains primary evidence for the flight when available.",
        "parameter_conflicts": [],
    }
    if symptom:
        index = {"parameters": external["parameters"], "parameter_defaults": external.get("parameter_defaults", {})}
        result["parameter_context"] = select_relevant_parameters(symptom, index=index, vehicle=vehicle)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Parse external ArduPilot parameter exports for configuration context.")
    parser.add_argument("--params", required=True, help="Mission Planner/QGC/MAVProxy parameter export")
    parser.add_argument("--symptom", help="Optional symptom class for relevant parameter context")
    parser.add_argument("--vehicle", default="ArduCopter")
    parser.add_argument("--json", help="Write JSON output")
    args = parser.parse_args()
    result = context_from_param_file(args.params, symptom=args.symptom, vehicle=args.vehicle)
    if args.json:
        write_json(args.json, result)
    else:
        import json
        print(json.dumps(result, indent=2, sort_keys=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
