#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_METADATA = ROOT / "references" / "parameter-metadata" / "ArduCopter-latest.min.json"
DEFAULT_URLS = {
    "ArduCopter": "https://autotest.ardupilot.org/Parameters/ArduCopter/apm.pdef.json",
}
DOC_URLS = {
    "ArduCopter": "https://ardupilot.org/copter/docs/parameters.html",
}
CAVEAT = (
    "Parameter metadata may not exactly match the firmware that produced the log. "
    "Latest-source metadata may include unreleased, renamed, or removed parameters. "
    "Use as explanatory context, not proof of firmware-specific behaviour or automatic parameter-change advice."
)

EXACT_NAMES = {
    "LOG_BITMASK",
    "LOG_BACKEND_TYPE",
    "LOG_DISARMED",
    "LOG_FILE_RATEMAX",
    "LOG_DARM_RATEMAX",
    "LOG_BLK_RATEMAX",
    "INS_RAW_LOG_OPT",
    "INS_LOG_BAT_MASK",
    "INS_LOG_BAT_OPT",
    "EK3_LOG_LEVEL",
    "WP_YAW_BEHAVIOR",
    "WPNAV_SPEED",
    "WPNAV_ACCEL",
    "WPNAV_ACCEL_C",
    "ATC_RAT_YAW_P",
    "ATC_RAT_YAW_I",
    "ATC_RAT_YAW_D",
    "ATC_RAT_YAW_FF",
    "ATC_ANG_YAW_P",
    "ATC_ACCEL_Y_MAX",
    "ATC_RATE_Y_MAX",
    "MOT_YAW_HEADROOM",
    "SERVO1_FUNCTION",
    "ARMING_CHECK",
    "RC_OPTIONS",
}
PREFIX_PATTERNS = ["RCMAP_", "FS_", "BATT_", "COMPASS_"]
FALLBACK_ENTRIES = {
    "ARMING_CHECK": {
        "DisplayName": "Arming check bitmask",
        "Description": "Controls which pre-arm safety checks are required before arming on firmware that uses ARMING_CHECK. Newer metadata may expose related arming check parameters instead.",
        "Values": {"0": "Disabled", "1": "All checks"},
        "Bitmask": {
            "1": "Barometer",
            "2": "Compass",
            "3": "GPS",
            "4": "INS",
            "5": "Parameters",
            "6": "RC Channels",
            "7": "Board voltage",
            "8": "Battery Level",
            "10": "Logging Available",
            "11": "Hardware safety switch",
            "12": "GPS Configuration",
            "13": "System",
            "14": "Mission",
            "15": "Rangefinder",
            "16": "Camera",
            "17": "AuxAuth",
        },
        "User": "Standard",
    },
}


def _anchor(name):
    return name.lower().replace("_", "-")


def _compact_range(value):
    if not isinstance(value, dict):
        return value
    low = value.get("low")
    high = value.get("high")
    def coerce(item):
        try:
            return float(item)
        except Exception:
            return item
    if low is not None and high is not None:
        return [coerce(low), coerce(high)]
    return value


def _fetch_json(url):
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.load(response)


def _flatten(raw):
    flat = {}
    for group, params in raw.items():
        if group == "json" or not isinstance(params, dict):
            continue
        for name, meta in params.items():
            if isinstance(meta, dict):
                flat[name] = meta
    return flat


def _compact_entry(name, meta, vehicle, metadata_version, docs_url, source_url):
    return {
        "name": name,
        "display_name": meta.get("DisplayName"),
        "description": meta.get("Description"),
        "units": meta.get("Units"),
        "range": _compact_range(meta.get("Range")),
        "values": meta.get("Values"),
        "bitmask": meta.get("Bitmask"),
        "user_level": meta.get("User"),
        "reboot_required": meta.get("RebootRequired"),
        "source_vehicle": vehicle,
        "metadata_version": metadata_version,
        "source_url": f"{docs_url}#{_anchor(name)}" if docs_url else source_url,
    }


def compact_from_raw(raw, vehicle="ArduCopter", source_url=None, docs_url=None):
    flat = _flatten(raw)
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    metadata_version = f"{fetched_at}-autotest"
    selected = set(EXACT_NAMES)
    for name in flat:
        if any(name.startswith(prefix) for prefix in PREFIX_PATTERNS):
            selected.add(name)

    entries = []
    for name in sorted(selected):
        meta = flat.get(name)
        if meta:
            entries.append(_compact_entry(name, meta, vehicle, metadata_version, docs_url, source_url))
        elif name in FALLBACK_ENTRIES:
            entry = _compact_entry(name, FALLBACK_ENTRIES[name], vehicle, metadata_version, docs_url, source_url)
            entry["source_note"] = "Fallback compact metadata because this exact parameter was not present in the fetched latest-source metadata."
            entries.append(entry)

    if "SERVO1_FUNCTION" in flat and not any(entry["name"] == "SERVO*_FUNCTION" for entry in entries):
        entry = _compact_entry("SERVO*_FUNCTION", flat["SERVO1_FUNCTION"], vehicle, metadata_version, docs_url, source_url)
        entry["display_name"] = "Servo output function"
        entry["description"] = "Wildcard metadata for SERVOx_FUNCTION output assignments."
        entry["source_url"] = f"{docs_url}#servo-functions" if docs_url else source_url
        entries.append(entry)
    for prefix in PREFIX_PATTERNS:
        wildcard_name = prefix + "*"
        if not any(entry["name"] == wildcard_name for entry in entries):
            first = next((flat[name] for name in sorted(flat) if name.startswith(prefix)), None)
            if first:
                entry = _compact_entry(wildcard_name, first, vehicle, metadata_version, docs_url, source_url)
                entry["display_name"] = f"{prefix} parameter family"
                entry["description"] = f"Wildcard metadata for {prefix} parameter family; exact meaning depends on the specific parameter."
                entry["source_url"] = f"{docs_url}#{_anchor(prefix)}" if docs_url else source_url
                entries.append(entry)

    return {
        "metadata_version": metadata_version,
        "source_vehicle": vehicle,
        "source_url": source_url,
        "source_note": "Fetched from ArduPilot machine-readable apm.pdef.json and compacted to the skill's investigation-focused parameter subset.",
        "caveat": CAVEAT,
        "parameters": sorted(entries, key=lambda item: item["name"]),
    }


def validate(data):
    required_top = {"metadata_version", "source_vehicle", "caveat", "parameters"}
    missing_top = sorted(required_top - set(data))
    if missing_top:
        raise SystemExit(f"metadata missing top-level keys: {', '.join(missing_top)}")
    required_param = {"name", "description", "source_vehicle", "metadata_version"}
    for entry in data.get("parameters", []):
        missing = sorted(required_param - set(entry))
        if missing:
            raise SystemExit(f"{entry.get('name', '<unnamed>')} missing keys: {', '.join(missing)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch, compact, validate, or copy parameter metadata snapshots.")
    parser.add_argument("--vehicle", default="ArduCopter", help="Vehicle metadata set to fetch")
    parser.add_argument("--url", help="Machine-readable apm.pdef.json URL")
    parser.add_argument("--input", help="Existing compact metadata JSON to validate/copy")
    parser.add_argument("--output", default=str(DEFAULT_METADATA), help="Output compact metadata JSON path")
    parser.add_argument("--check", action="store_true", help="Only validate the input/output compact metadata file")
    parser.add_argument("--fetch", action="store_true", help="Fetch machine-readable parameter metadata from the web and compact it")
    args = parser.parse_args()

    if args.fetch:
        url = args.url or DEFAULT_URLS.get(args.vehicle)
        if not url:
            raise SystemExit(f"no default URL for vehicle {args.vehicle}; provide --url")
        raw = _fetch_json(url)
        data = compact_from_raw(raw, vehicle=args.vehicle, source_url=url, docs_url=DOC_URLS.get(args.vehicle))
        validate(data)
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(data, separators=(",", ":"), ensure_ascii=True), encoding="utf-8")
        print(f"fetched and wrote compact metadata: {out}")
        return 0

    source = Path(args.input) if args.input else Path(args.output)
    data = json.loads(source.read_text(encoding="utf-8"))
    validate(data)
    if not args.check and args.input:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(data, separators=(",", ":"), ensure_ascii=True), encoding="utf-8")
        print(f"wrote compact metadata: {out}")
    else:
        print(f"metadata ok: {source}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
