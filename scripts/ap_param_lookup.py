#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ap_common import read_json, write_json
from ap_param_context import merge_external_parameters, parse_param_file
from ap_parameters import (
    BITMASK_CAVEAT,
    METADATA_CAVEAT,
    _numeric_flags,
    enrich_parameter_entry,
    find_parameter_metadata,
    log_bitmask_missing_guidance,
    load_parameter_metadata,
    select_relevant_parameters,
)
from ap_symptom_map import requirement_spec


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


def _index_params(index_path):
    if not index_path:
        return {}, {}, {}
    index = read_json(index_path)
    return dict(index.get("parameters", {}) or {}), dict(index.get("parameter_defaults", {}) or {}), index


def lookup_parameters(index_path=None, params_path=None, names=None, symptom=None, vehicle="ArduCopter"):
    index_params, index_defaults, source_index = _index_params(index_path)
    source_index = {**source_index, "parameters": index_params, "parameter_defaults": index_defaults}
    external_context = parse_param_file(params_path) if params_path else None
    merged = merge_external_parameters(source_index, external_context)
    params = merged["parameters"]
    defaults = merged["parameter_defaults"]
    metadata = load_parameter_metadata(vehicle)
    requested_names = _parse_names(names)
    symptom_context = None
    missing_messages_for_symptom = []

    if symptom:
        index_for_context = {"parameters": params, "parameter_defaults": defaults}
        symptom_context = select_relevant_parameters(symptom, index=index_for_context, enrich_metadata=True, vehicle=vehicle)
        spec = requirement_spec(symptom)
        present_messages = set((source_index.get("messages") or {}).keys())
        message_selectors = spec.get("required_messages", []) + spec.get("strongly_recommended_messages", []) + spec.get("optional_context_messages", [])
        missing_messages_for_symptom = [msg for msg in message_selectors if msg not in present_messages]
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
        if name == "LOG_BITMASK":
            meta = find_parameter_metadata(name, metadata=metadata, vehicle=vehicle)
            enriched["possibly_missing_for_symptom"] = log_bitmask_missing_guidance(enriched.get("value"), meta, missing_messages_for_symptom)
        results.append(enriched)

    output = {
        "vehicle": vehicle,
        "metadata_source": metadata.get("source_url") or metadata.get("source_note"),
        "metadata_version": metadata.get("metadata_version"),
        "metadata_caveat": metadata.get("caveat") or METADATA_CAVEAT,
        "bitmask_caveat": BITMASK_CAVEAT,
        "parameters": results,
        "symptom_context": symptom_context,
        "external_parameter_context": merged["external_parameter_context"],
        "parameter_conflicts": merged["parameter_conflicts"],
        "parameter_source_precedence": merged["parameter_source_precedence"],
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
