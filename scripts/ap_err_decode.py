#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from ap_common import read_json, safe_float, write_json


FIRMWARE_CAVEAT = (
    "ERR Subsys/ECode meanings can vary by ArduPilot vehicle, firmware version, and source branch; "
    "use this decoded entry as timeline context, not proof without timing correlation."
)
UNKNOWN_CAVEAT = (
    "This ERR Subsys/ECode pair is not in the local conservative mapping; do not infer a specific cause "
    "without firmware-specific source/docs and timing correlation."
)

ERR_SUBSYSTEMS: Dict[int, str] = {
    2: "Radio",
    3: "Compass",
    5: "Radio failsafe",
    6: "Battery failsafe",
    8: "GCS failsafe",
    9: "Fence failsafe",
    10: "Flight mode change failure",
    11: "GPS",
    12: "Crash check",
    13: "Flip mode",
    15: "Parachute",
    16: "EKF check",
    17: "EKF failsafe",
    18: "Barometer",
    19: "CPU load watchdog",
    20: "ADSB failsafe",
    21: "Terrain data",
    22: "Navigation",
    23: "Terrain failsafe",
    24: "EKF primary changed",
    25: "Thrust loss check",
    29: "Vibration failsafe",
}

ERR_CODES: Dict[Tuple[int, int], str] = {
    (2, 0): "Radio errors resolved",
    (2, 2): "Radio late frame: no receiver updates for two seconds",
    (3, 0): "Compass errors resolved",
    (3, 1): "Compass failed to initialise",
    (3, 4): "Compass unhealthy: failed to read from sensor",
    (5, 0): "Radio failsafe resolved",
    (5, 1): "Radio failsafe triggered",
    (6, 0): "Battery failsafe resolved",
    (6, 1): "Battery failsafe triggered",
    (8, 0): "GCS failsafe resolved",
    (8, 1): "GCS failsafe triggered",
    (9, 0): "Fence failsafe resolved",
    (9, 1): "Altitude fence breach, failsafe triggered",
    (9, 2): "Circular fence breach, failsafe triggered",
    (9, 3): "Altitude and circular fence breached, failsafe triggered",
    (9, 4): "Polygon fence breached, failsafe triggered",
    (10, 0): "Flight mode change failure resolved",
    (10, 2): "Flight mode change failed, commonly due to bad position estimate",
    (11, 0): "GPS glitch cleared",
    (11, 2): "GPS glitch occurred",
    (12, 1): "Crash check: crash into ground detected",
    (12, 2): "Crash check: loss of control detected",
    (13, 2): "Flip abandoned",
    (15, 2): "Parachute not deployed: vehicle too low",
    (15, 3): "Parachute not deployed: vehicle landed",
    (16, 0): "EKF variance cleared",
    (16, 2): "EKF bad variance",
    (17, 0): "EKF failsafe resolved",
    (17, 1): "EKF failsafe triggered",
    (18, 0): "Barometer errors resolved",
    (18, 4): "Barometer unhealthy: failed to read from sensor",
    (19, 0): "CPU load watchdog failsafe resolved",
    (19, 1): "CPU load watchdog failsafe triggered",
    (20, 0): "ADSB failsafe resolved",
    (20, 1): "ADSB failsafe: report only",
    (20, 2): "ADSB failsafe: vertical avoidance",
    (20, 3): "ADSB failsafe: horizontal avoidance",
    (20, 4): "ADSB failsafe: perpendicular avoidance",
    (20, 5): "ADSB failsafe: RTL invoked",
    (21, 2): "Terrain data missing",
    (22, 2): "Navigation failed to set destination",
    (22, 3): "Navigation RTL restarted",
    (22, 4): "Navigation circle initialisation failed",
    (22, 5): "Navigation destination outside fence",
    (23, 0): "Terrain failsafe resolved",
    (23, 1): "Terrain failsafe triggered",
    (24, 0): "First EKF lane became primary",
    (24, 1): "Second EKF lane became primary",
    (25, 0): "Thrust restored",
    (25, 1): "Thrust loss detected",
    (29, 0): "Excessive vibration compensation deactivated",
    (29, 1): "Excessive vibration compensation activated",
}


def _int_or_none(value: Any):
    numeric = safe_float(value)
    if numeric is None or not float(numeric).is_integer():
        return None
    return int(numeric)


def decode_err_entries(entries: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    decoded = []
    for entry in entries or []:
        subsys = _int_or_none(entry.get("subsys", entry.get("Subsys")))
        ecode = _int_or_none(entry.get("ecode", entry.get("ECode")))
        time_s = safe_float(entry.get("time_s", entry.get("TimeS")))
        if subsys is None or ecode is None:
            meaning = "Unknown ERR Subsys/ECode"
            confidence = "unknown"
            caveat = UNKNOWN_CAVEAT
        elif (subsys, ecode) in ERR_CODES:
            meaning = ERR_CODES[(subsys, ecode)]
            confidence = "known_mapping"
            caveat = FIRMWARE_CAVEAT
        elif subsys in ERR_SUBSYSTEMS:
            meaning = f"{ERR_SUBSYSTEMS[subsys]} subsystem reported unrecognised ECode {ecode}"
            confidence = "best_effort"
            caveat = UNKNOWN_CAVEAT
        else:
            meaning = "Unknown ERR Subsys/ECode"
            confidence = "unknown"
            caveat = UNKNOWN_CAVEAT
        decoded.append({
            "time_s": time_s,
            "subsys": subsys,
            "ecode": ecode,
            "meaning": meaning,
            "confidence": confidence,
            "caveat": caveat,
            "raw": entry.get("raw", entry),
        })
    return decoded


def _entries_from_index(path: Path) -> List[Dict[str, Any]]:
    index = read_json(path)
    return list(index.get("errors") or [])


def _entries_from_tables(path: Path) -> List[Dict[str, Any]]:
    err_path = path / "ERR.csv" if path.is_dir() else path
    if not err_path.exists():
        return []
    rows = []
    with err_path.open("r", encoding="utf-8", errors="replace", newline="") as f:
        for row in csv.DictReader(f):
            rows.append({
                "time_s": row.get("TimeS"),
                "subsys": row.get("Subsys"),
                "ecode": row.get("ECode"),
                "raw": row,
            })
    return rows


def build_err_decode(*, index=None, tables=None):
    if index:
        entries = _entries_from_index(Path(index))
        source = {"type": "index", "path": str(index)}
    elif tables:
        entries = _entries_from_tables(Path(tables))
        source = {"type": "tables", "path": str(tables)}
    else:
        entries = []
        source = {"type": "none"}
    decoded = decode_err_entries(entries)
    return {
        "source": source,
        "decoded_errors": decoded,
        "count": len(decoded),
        "mapping_scope": "Conservative local mapping for common ArduPilot ERR Subsys/ECode pairs.",
        "caveat": FIRMWARE_CAVEAT,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Decode common ArduPilot ERR Subsys/ECode rows with firmware caveats.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--index", help="Index JSON from ap_log_index.py or diagnosis workflow")
    group.add_argument("--tables", help="Directory containing ERR.csv, or a direct ERR.csv path")
    parser.add_argument("--json", help="Write JSON output")
    args = parser.parse_args()
    result = build_err_decode(index=args.index, tables=args.tables)
    if args.json:
        write_json(args.json, result)
    else:
        print(json.dumps(result, indent=2, sort_keys=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
