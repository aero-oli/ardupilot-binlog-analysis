#!/usr/bin/env python3
from __future__ import annotations
import argparse
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ap_common import AnalysisError, collect_dataflash, missing_messages, vehicle_scope, write_json
from ap_diag_requirements import DIAGNOSIS_REQUIREMENTS, missing_by_tier

MODULES = {
    "core": {"required": ["ATT", "RATE", "RCOU", "MODE", "MSG", "EV", "ERR"], "optional": ["PARM", "ARM"]},
    "tuning": {"required": ["ATT", "RATE"], "optional": ["PIDR", "PIDP", "PIDY", "RCOU", "VIBE"]},
    "yaw_diagnosis": DIAGNOSIS_REQUIREMENTS["yaw_misbehaviour"],
    "vibration": DIAGNOSIS_REQUIREMENTS["vibration_issue"],
    "ekf_gps": DIAGNOSIS_REQUIREMENTS["ekf_gps_issue"],
    "power": DIAGNOSIS_REQUIREMENTS["battery_power_issue"],
    "motor_esc": DIAGNOSIS_REQUIREMENTS["motor_esc_issue"],
    "system_id": {"required": [], "optional": ["SID", "SIDD", "SIDS"]},
}

MODULE_SYMPTOM_CLASS = {
    "yaw_diagnosis": "yaw_misbehaviour",
    "vibration": "vibration_issue",
    "ekf_gps": "ekf_gps_issue",
    "power": "battery_power_issue",
    "motor_esc": "motor_esc_issue",
}


def module_availability(index):
    modules = {}
    for name, spec in MODULES.items():
        required = spec.get("required_messages", spec.get("required", []))
        strongly = spec.get("strongly_recommended_messages", spec.get("strongly_recommended", []))
        optional = spec.get("optional_context_messages", spec.get("optional_context", spec.get("optional", [])))
        if "strongly_recommended_messages" in spec or "optional_context_messages" in spec:
            missing_required, missing_strongly, missing_optional = missing_by_tier(index, MODULE_SYMPTOM_CLASS[name], missing_messages)
        else:
            missing_required = missing_messages(index, required)
            missing_strongly = missing_messages(index, strongly)
            missing_optional = missing_messages(index, optional)
        supporting = strongly + optional
        has_supporting_data = len(missing_messages(index, supporting)) < len(supporting) if supporting else False
        if not missing_required and (required or has_supporting_data):
            status = "available"
        elif len(missing_required) < len(required):
            status = "partial"
        else:
            status = "not_possible"
        modules[name] = {
            "required": required,
            "strongly_recommended": strongly,
            "optional_context": optional,
            "missing_required": missing_required,
            "missing_strongly_recommended": missing_strongly,
            "missing_optional_context": missing_optional,
            "status": status,
        }
    return modules

def main() -> int:
    p = argparse.ArgumentParser(description="Validate whether a log contains enough data for ArduPilot analysis modules.")
    p.add_argument("log")
    p.add_argument("--json", default="validate.json")
    p.add_argument("--summary", default=None)
    p.add_argument("--max-messages", type=int, default=None, help="Optional parse limit for quick validation")
    args = p.parse_args()
    path = Path(args.log)
    warnings = []
    if path.suffix.lower() == ".tlog":
        warnings.append("Input looks like a telemetry .tlog. This skill is optimized for onboard DataFlash .bin/.log files.")
    if path.suffix.lower() not in {".bin", ".log", ".tlog"}:
        warnings.append(f"Unexpected extension '{path.suffix}'. Attempting DataFlash parse anyway.")
    try:
        _rows, index, _stats = collect_dataflash(path, include=[], max_messages=args.max_messages)
        modules = module_availability(index)
        scope = vehicle_scope(index)
        if not index["messages"]:
            warnings.append("No DataFlash messages parsed.")
        if index.get("parser_stats", {}).get("max_messages_reached"):
            warnings.append("Validation stopped at --max-messages; message counts and module availability may be partial.")
        if index.get("logging_dropouts"):
            warnings.append("Possible logging dropout/drop count evidence was found; inspect index.logging_dropouts.")
        if index.get("duration_s") is None:
            warnings.append("No usable time base found; segmenting and time plots may be limited.")
        if modules["tuning"]["status"] != "available":
            warnings.append("Tuning analysis is incomplete without required ATT and RATE messages.")
        if modules["yaw_diagnosis"]["status"] != "available":
            warnings.append("Yaw diagnosis may be partial; missing required messages are listed in yaw_diagnosis.missing_required.")
        elif modules["yaw_diagnosis"]["missing_strongly_recommended"]:
            warnings.append("Yaw diagnosis is available from ATT/RATE but confidence is reduced without yaw_diagnosis.missing_strongly_recommended messages.")
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
                strongly = ", ".join(m["missing_strongly_recommended"]) or "-"
                optional = ", ".join(m["missing_optional_context"]) or "-"
                lines.append(f"| {name} | {m['status']} | required: {missing}; strongly recommended: {strongly}; optional context: {optional} |")
            Path(args.summary).parent.mkdir(parents=True, exist_ok=True)
            Path(args.summary).write_text("\n".join(lines)+"\n", encoding="utf-8")
        print(f"Validated {path}: {len(warnings)} warnings")
        return 0
    except AnalysisError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
