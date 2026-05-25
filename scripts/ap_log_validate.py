#!/usr/bin/env python3
from __future__ import annotations
import argparse
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ap_common import AnalysisError, collect_dataflash, missing_messages, vehicle_scope, write_json
from ap_diag_requirements import DIAGNOSIS_REQUIREMENTS, missing_by_tier
from ap_modes import mode_decoding_note, mode_timeline_from_rows

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


def log_quality_status(index, file_path=None, warnings=None):
    messages = index.get("messages", {}) or {}
    stats = index.get("parser_stats", {}) or {}
    logging_health = index.get("logging_health", {}) or {}
    issues = []
    confidence_limits = []
    guidance = []

    suffix = Path(file_path or index.get("file", "")).suffix.lower()
    if suffix == ".tlog":
        issues.append({"code": "telemetry_tlog_supplied", "severity": "warning", "detail": "Input appears to be a telemetry .tlog rather than onboard DataFlash."})
        confidence_limits.append("Telemetry logs may not contain onboard DataFlash messages needed for this skill.")
    if stats.get("parser_errors"):
        issues.append({"code": "parser_errors", "severity": "error", "detail": stats.get("parser_errors")})
        confidence_limits.append("Parser errors mean missing messages cannot be treated as absent faults.")
    if stats.get("bad_byte_warnings") or stats.get("bad_bytes"):
        issues.append({"code": "bad_byte_skips", "severity": "warning", "detail": stats.get("bad_byte_warnings") or stats.get("bad_bytes")})
        confidence_limits.append("Bad-byte skips can hide short events and reduce timing confidence.")
    if not messages:
        issues.append({"code": "no_dataflash_messages", "severity": "error", "detail": "No DataFlash messages were parsed."})
    if "FMT" not in messages:
        issues.append({"code": "no_fmt", "severity": "warning", "detail": "No FMT message was indexed; schema confidence may be limited for damaged or text-converted logs."})
    if index.get("start_time_s") is None or index.get("end_time_s") is None or index.get("duration_s") is None:
        issues.append({"code": "no_usable_timebase", "severity": "warning", "detail": "No usable timebase was found."})
        confidence_limits.append("Time-window selection, event ordering, and correlation plots may be limited.")
    if "PARM" not in messages and not index.get("parameters"):
        issues.append({"code": "no_parm", "severity": "info", "detail": "No PARM message or indexed parameter values were available."})
        confidence_limits.append("Parameter context and output/RC mapping may need an external .param file; external parameters are configuration context, not proof of in-flight values.")
    if logging_health.get("confirmed_dropouts"):
        issues.append({"code": "logging_dropouts", "severity": "warning", "detail": "Confirmed logging dropout/drop-count evidence is present."})
        confidence_limits.append(logging_health.get("confidence_impact") or "Dropouts reduce confidence for missing/timing-sensitive evidence.")
    if logging_health.get("possible_dropouts"):
        issues.append({"code": "possible_logging_dropouts", "severity": "info", "detail": "Possible logging dropout context was found in unrecognized drop-like fields."})
    if logging_health.get("missing_core_messages_after_arm"):
        issues.append({"code": "missing_core_messages_after_arm", "severity": "warning", "detail": logging_health.get("missing_core_messages_after_arm")})
        confidence_limits.append("Core messages are missing after arming; do not treat absence of a message as absence of a fault.")
    if logging_health.get("timestamp_resets"):
        issues.append({"code": "timestamp_resets", "severity": "warning", "detail": "Timestamp resets were detected."})
        confidence_limits.append("Timestamp resets reduce confidence in time-window and correlation conclusions.")
    if logging_health.get("max_time_gap_s", 0) and logging_health.get("max_time_gap_s", 0) >= 10:
        issues.append({"code": "large_timestamp_gaps", "severity": "warning", "detail": f"Maximum indexed timestamp gap is {logging_health.get('max_time_gap_s')} s."})
    raw_count = sum((messages.get(name, {}) or {}).get("count", 0) for name in ["ISBD", "ISBH", "GYR", "ACC", "IMU", "IMU_FAST", "RAW_IMU"])
    if raw_count > 500000:
        issues.append({"code": "very_large_raw_imu_log", "severity": "info", "detail": f"High-rate/raw IMU message count is {raw_count}."})
        guidance.append("Very large raw IMU logs can be partial or sparse; check DSF/DMS/logging health before relying on FFT/filter evidence.")
    if stats.get("max_messages_reached"):
        issues.append({"code": "validation_parse_limited", "severity": "info", "detail": "Validation stopped at --max-messages."})
        confidence_limits.append("Validation/index inventory may be partial because parsing stopped early.")

    if warnings:
        for warning in warnings:
            if "No usable time base" in warning and not any(i["code"] == "no_usable_timebase" for i in issues):
                issues.append({"code": "no_usable_timebase", "severity": "warning", "detail": warning})

    severities = {issue["severity"] for issue in issues}
    if "error" in severities:
        status = "unusable_or_parse_failed"
    elif "warning" in severities:
        status = "limited"
    else:
        status = "usable"
    return {
        "status": status,
        "issues": issues,
        "confidence_limits": list(dict.fromkeys(confidence_limits)),
        "guidance": list(dict.fromkeys(guidance)),
        "reference": "references/corrupt-or-incomplete-log.md",
        "agent_behaviour": [
            "State log quality limitations clearly.",
            "Do not treat absence of a message as absence of a fault.",
            "Prefer evidence that exists before making claims.",
            "Request another log or targeted capture only when safe.",
        ],
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
        logging_health = index.get("logging_health", {})
        if logging_health.get("confirmed_dropouts"):
            warnings.append("Confirmed logging dropout/drop-count evidence was found; inspect index.logging_health.confirmed_dropouts.")
        if logging_health.get("possible_dropouts"):
            warnings.append("Possible logging dropout context was found; inspect index.logging_health.possible_dropouts.")
        if logging_health.get("limits_diagnosis"):
            warnings.append("Logging health limits diagnosis confidence: " + logging_health.get("confidence_impact", "inspect logging_health"))
        if index.get("duration_s") is None:
            warnings.append("No usable time base found; segmenting and time plots may be limited.")
        if modules["tuning"]["status"] != "available":
            warnings.append("Tuning analysis is incomplete without required ATT and RATE messages.")
        if modules["yaw_diagnosis"]["status"] != "available":
            warnings.append("Yaw diagnosis may be partial; missing required messages are listed in yaw_diagnosis.missing_required.")
        elif modules["yaw_diagnosis"]["missing_strongly_recommended"]:
            warnings.append("Yaw diagnosis is available from ATT/RATE but confidence is reduced without yaw_diagnosis.missing_strongly_recommended messages.")
        warnings.extend(scope.get("notes", []))
        mode_timeline = mode_timeline_from_rows(index.get("modes", []), log_end_s=index.get("end_time_s"))
        quality = log_quality_status(index, file_path=path, warnings=warnings)
        result = {
            "file": str(path),
            "warnings": warnings,
            "log_quality_status": quality,
            "vehicle_scope": scope,
            "mode_decoding": mode_decoding_note(scope),
            "mode_timeline": mode_timeline,
            "modules": modules,
            "logging_health": logging_health,
            "index": index,
        }
        write_json(args.json, result)
        if args.summary:
            lines = [f"# Validation: {path.name}\n"]
            if warnings:
                lines.append("## Warnings")
                lines.extend(f"- {w}" for w in warnings)
                lines.append("")
            lines.append("## Log Quality")
            lines.append(f"- Status: {quality.get('status')}")
            lines.append(f"- Reference: {quality.get('reference')}")
            for issue in quality.get("issues", []):
                lines.append(f"- {issue.get('severity')}: {issue.get('code')} - {issue.get('detail')}")
            lines.append("")
            lines.append("## Logging Health")
            lines.append(f"- Confirmed dropouts detected: {logging_health.get('dropouts_detected', False)}")
            lines.append(f"- Possible dropout context: {logging_health.get('possible_dropout_count', 0)}")
            lines.append(f"- Max time gap: {logging_health.get('max_time_gap_s', 0)} s")
            lines.append(f"- Confidence impact: {logging_health.get('confidence_impact', 'unknown')}\n")
            lines.append(f"\n## Vehicle scope\n- Primary vehicle: {scope['primary_vehicle']}\n- Copter heuristic confidence: {scope['copter_heuristics_confidence']}\n")
            if mode_timeline:
                lines.append("## Mode timeline")
                lines.append(f"- {mode_decoding_note(scope)}")
                lines.append("")
                lines.append("| Raw mode | Decoded mode | Start s | End s | Duration s |")
                lines.append("|---|---|---:|---:|---:|")
                for mode in mode_timeline[:100]:
                    end = "" if mode.get("end_s") is None else f"{mode['end_s']:.3f}"
                    dur = "" if mode.get("duration_s") is None else f"{mode['duration_s']:.3f}"
                    lines.append(f"| {mode.get('raw_mode')} | {mode.get('decoded_mode')} | {mode.get('start_s'):.3f} | {end} | {dur} |")
                lines.append("")
            lines.append("## Module availability")
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
