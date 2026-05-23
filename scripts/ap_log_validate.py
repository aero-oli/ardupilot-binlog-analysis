#!/usr/bin/env python3
from __future__ import annotations
import argparse
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ap_common import AnalysisError, build_index, missing_messages, parse_dataflash, vehicle_scope, write_json

MODULES = {
    "core": {"required": ["ATT", "RATE", "RCOU", "MODE", "MSG", "EV", "ERR"], "optional": ["PARM", "ARM"]},
    "tuning": {"required": ["ATT", "RATE"], "optional": ["PIDR", "PIDP", "PIDY", "RCOU", "VIBE"]},
    "yaw_diagnosis": {"required": ["ATT", "RATE", "PIDY", "RCOU", "MODE", "MSG", "EV", "ERR"], "optional": ["MAG", "XKF3", "XKF4", "VIBE", "BAT", "ESC", "ESCX", "EDT2", "RCIN", "POWR"]},
    "vibration": {"required": ["VIBE"], "optional": ["IMU", "GYR", "ACC", "ISBH", "ISBD", "RATE", "PIDR", "PIDP", "PIDY"]},
    "ekf_gps": {"required": ["GPS"], "optional": ["GPA", "GPS2", "XKF1", "XKF3", "XKF4", "NKF1", "NKF3", "NKF4", "MAG", "VIBE", "BAT"]},
    "power": {"required": ["BAT"], "optional": ["POWR", "RCOU", "ESC", "ESCX", "EDT2"]},
    "motor_esc": {"required": ["RCOU"], "optional": ["ESC", "ESCX", "EDT2", "RATE", "PIDR", "PIDP", "PIDY", "BAT", "VIBE"]},
    "system_id": {"required": [], "optional": ["SID", "SIDD", "SIDS"]},
}


def module_availability(index):
    modules = {}
    for name, spec in MODULES.items():
        required = spec["required"]
        optional = spec["optional"]
        missing_required = missing_messages(index, required)
        missing_optional = missing_messages(index, optional)
        if not missing_required and (required or len(missing_optional) < len(optional)):
            status = "available"
        elif len(missing_required) < len(required):
            status = "partial"
        else:
            status = "not_possible"
        modules[name] = {
            "required": required,
            "optional": optional,
            "missing_required": missing_required,
            "missing_optional": missing_optional,
            "status": status,
        }
    return modules

def main() -> int:
    p = argparse.ArgumentParser(description="Validate whether a log contains enough data for ArduPilot analysis modules.")
    p.add_argument("log")
    p.add_argument("--json", default="validate.json")
    p.add_argument("--summary", default=None)
    args = p.parse_args()
    path = Path(args.log)
    warnings = []
    if path.suffix.lower() == ".tlog":
        warnings.append("Input looks like a telemetry .tlog. This skill is optimized for onboard DataFlash .bin/.log files.")
    if path.suffix.lower() not in {".bin", ".log", ".tlog"}:
        warnings.append(f"Unexpected extension '{path.suffix}'. Attempting DataFlash parse anyway.")
    try:
        rows = parse_dataflash(path)
        index = build_index(path, rows)
        modules = module_availability(index)
        scope = vehicle_scope(index)
        if not index["messages"]:
            warnings.append("No DataFlash messages parsed.")
        if index.get("duration_s") is None:
            warnings.append("No usable time base found; segmenting and time plots may be limited.")
        if modules["tuning"]["status"] != "available":
            warnings.append("Tuning analysis is incomplete without required ATT and RATE messages.")
        if modules["yaw_diagnosis"]["status"] != "available":
            warnings.append("Yaw diagnosis may be partial; missing required messages are listed in yaw_diagnosis.missing_required.")
        warnings.extend(scope.get("notes", []))
        result = {"file": str(path), "warnings": warnings, "vehicle_scope": scope, "modules": modules, "index": index}
        write_json(args.json, result)
        if args.summary:
            lines = [f"# Validation: {path.name}\n"]
            if warnings:
                lines.append("## Warnings")
                lines.extend(f"- {w}" for w in warnings)
                lines.append("")
            lines.append("## Module availability")
            lines.append(f"\n## Vehicle scope\n- Primary vehicle: {scope['primary_vehicle']}\n- Copter heuristic confidence: {scope['copter_heuristics_confidence']}\n")
            lines.append("| Module | Status | Missing |")
            lines.append("|---|---|---|")
            for name, m in modules.items():
                missing = ", ".join(m["missing_required"]) or "-"
                optional = ", ".join(m["missing_optional"]) or "-"
                lines.append(f"| {name} | {m['status']} | required: {missing}; optional: {optional} |")
            Path(args.summary).parent.mkdir(parents=True, exist_ok=True)
            Path(args.summary).write_text("\n".join(lines)+"\n", encoding="utf-8")
        print(f"Validated {path}: {len(warnings)} warnings")
        return 0
    except AnalysisError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
