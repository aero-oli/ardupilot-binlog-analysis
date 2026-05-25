#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ap_common import read_json, safe_float, write_json
from ap_parameters import (
    METADATA_CAVEAT,
    _numeric_flags,
    enrich_parameter_entry,
    load_parameter_metadata,
    select_relevant_parameters,
)


def _parse_names(value):
    if not value:
        return []
    if isinstance(value, str):
        parts = []
        for chunk in value.split(","):
            chunk = chunk.strip()
            if chunk:
                parts.append(chunk)
        return parts
    return list(value)


def _load_param_file(path):
    params = {}
    defaults = {}
    if not path:
        return params, defaults
    with Path(path).open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or line.startswith(";"):
                continue
            if "," in line:
                parts = [p.strip() for p in line.split(",")]
            else:
                parts = line.split()
            if len(parts) < 2:
                continue
            name = parts[0]
            value = safe_float(parts[1])
            params[name] = parts[1] if value is None else value
            if len(parts) >= 3:
                default = safe_float(parts[2])
                defaults[name] = parts[2] if default is None else default
    return params, defaults


def _index_params(index_path):
    if not index_path:
        return {}, {}
    index = read_json(index_path)
    return dict(index.get("parameters", {}) or {}), dict(index.get("parameter_defaults", {}) or {})


def lookup_parameters(index_path=None, params_path=None, names=None, symptom=None, vehicle="ArduCopter"):
    index_params, index_defaults = _index_params(index_path)
    file_params, file_defaults = _load_param_file(params_path)
    params = {**index_params, **file_params}
    defaults = {**index_defaults, **file_defaults}
    metadata = load_parameter_metadata(vehicle)
    requested_names = _parse_names(names)
    symptom_context = None

    if symptom:
        index_for_context = {"parameters": params, "parameter_defaults": defaults}
        symptom_context = select_relevant_parameters(symptom, index=index_for_context, enrich_metadata=True, vehicle=vehicle)
        for entry in symptom_context.get("selected", []):
            if entry["name"] not in requested_names:
                requested_names.append(entry["name"])

    results = []
    for name in requested_names:
        value = params.get(name)
        default = defaults.get(name)
        flags = _numeric_flags(value, default)
        entry = {
            "name": name,
            "logged_value": value,
            "value": value,
            "default": default,
            "is_zero": flags["is_zero"],
            "is_default": flags["is_default"],
            "logged": name in params,
        }
        enriched = enrich_parameter_entry(entry, metadata=metadata, vehicle=vehicle)
        enriched["logged_value"] = enriched.get("value")
        results.append(enriched)

    output = {
        "vehicle": vehicle,
        "metadata_source": metadata.get("source_url") or metadata.get("source_note"),
        "metadata_version": metadata.get("metadata_version"),
        "metadata_caveat": metadata.get("caveat") or METADATA_CAVEAT,
        "parameters": results,
        "symptom_context": symptom_context,
        "note": "Parameter metadata is explanatory context only. Do not recommend parameter changes automatically without matching log evidence and safe bench/ground verification.",
    }
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Lookup compact ArduPilot parameter metadata and logged values.")
    parser.add_argument("--index", help="Index JSON from ap_log_index.py or manifest/diagnosis workflow")
    parser.add_argument("--params", help="Optional external parameter file for future/offline support")
    parser.add_argument("--names", help="Comma-separated parameter names")
    parser.add_argument("--symptom", help="Symptom class, for example yaw_misbehaviour")
    parser.add_argument("--vehicle", default="ArduCopter", help="Vehicle metadata set to use")
    parser.add_argument("--json", help="Write JSON output to this path")
    parser.add_argument("--refresh-metadata", action="store_true", help="Fetch latest machine-readable ArduPilot metadata into the local compact cache before lookup")
    args = parser.parse_args()
    if args.refresh_metadata:
        updater = Path(__file__).resolve().parent / "update_parameter_metadata.py"
        subprocess.run([sys.executable, str(updater), "--fetch", "--vehicle", args.vehicle], check=True)
    result = lookup_parameters(index_path=args.index, params_path=args.params, names=args.names, symptom=args.symptom, vehicle=args.vehicle)
    if args.json:
        write_json(args.json, result)
    else:
        print(json.dumps(result, indent=2, sort_keys=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
