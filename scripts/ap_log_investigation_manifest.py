#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ap_common import AnalysisError, classify_symptom, collect_dataflash, missing_messages, write_json
from ap_symptom_map import requirement_spec


EVIDENCE_GROUPS = {
    "core": {"ATT", "RATE", "GPS", "VIBE", "BAT", "CTUN"},
    "controller": {"RATE", "PIDR", "PIDP", "PIDY", "PIDA", "CTUN"},
    "actuator": {"RCOU", "RCO2", "RCO3", "ESC", "ESCX", "EDT2", "PARM"},
    "estimator": {"GPS", "GPA", "GPS2", "MAG", "XKF1", "XKF2", "XKF3", "XKF4", "NKF1", "NKF2", "NKF3", "NKF4", "BARO", "RNGF"},
    "power": {"BAT", "BCL", "POWR"},
    "vibration": {"VIBE", "IMU", "GYR", "ACC", "ISBH", "ISBD"},
    "timeline": {"MODE", "MSG", "EV", "ERR", "ARM", "RCIN"},
}

PLOT_COMMANDS = {
    "yaw_attitude": ["--series ATT.DesYaw", "--series ATT.Yaw", "--title \"Yaw desired vs achieved\""],
    "yaw_rate": ["--series RATE.YDes", "--series RATE.Y", "--series RATE.YOut", "--secondary RATE.YOut", "--title \"Yaw rate tracking and output\""],
    "yaw_pid": ["--series PIDY.Tar", "--series PIDY.Act", "--series PIDY.Err", "--secondary PIDY.Flags", "--title \"Yaw PID evidence\""],
    "motor_outputs": ["--series RCOU.C1", "--series RCOU.C2", "--series RCOU.C3", "--series RCOU.C4", "--title \"Motor outputs\""],
    "power": ["--series BAT.Volt", "--series BAT.Curr", "--secondary BAT.Curr", "--title \"Battery voltage and current\""],
    "vibration": ["--series VIBE.VibeX", "--series VIBE.VibeY", "--series VIBE.VibeZ", "--title \"Vibration\""],
    "ekf_mag": ["--series XKF4.SM", "--series XKF4.SH", "--title \"EKF yaw/mag test ratios\""],
    "ekf_gps": ["--series GPS.Status", "--series GPS.NSats", "--series GPS.HDop", "--secondary GPS.HDop", "--title \"GPS quality\""],
    "gps_quality": ["--series GPS.Status", "--series GPS.NSats", "--series GPS.HDop", "--secondary GPS.HDop", "--title \"GPS quality\""],
    "ekf_innovations": ["--series XKF4.SV", "--series XKF4.SP", "--series XKF4.SH", "--series XKF4.SM", "--title \"EKF test ratios\""],
    "attitude_rate": ["--series ATT.DesRoll", "--series ATT.Roll", "--series ATT.DesPitch", "--series ATT.Pitch", "--title \"Attitude tracking\""],
    "pid_terms": ["--series PIDR.Err", "--series PIDP.Err", "--series PIDY.Err", "--title \"PID errors\""],
    "altitude_throttle": ["--series CTUN.DAlt", "--series CTUN.Alt", "--series CTUN.ThO", "--secondary CTUN.ThO", "--title \"Altitude and throttle\""],
    "esc_telemetry": ["--series ESC.RPM", "--series ESC.Curr", "--series ESC.Err", "--secondary ESC.Err", "--title \"ESC telemetry\""],
    "mode_timeline": ["--series RATE.R", "--events", "--title \"Timeline context\""],
}


def _present_messages(index):
    return set(index.get("messages", {}).keys())


def _available_evidence(index):
    present = _present_messages(index)
    return {
        group: sorted(name for name in names if name in present)
        for group, names in EVIDENCE_GROUPS.items()
    }


def _missing_evidence(index, spec):
    return {
        "required": missing_messages(index, spec["required_messages"]),
        "strongly_recommended": missing_messages(index, spec["strongly_recommended_messages"]),
        "optional_context": missing_messages(index, spec["optional_context_messages"]),
    }


def _available_plot_groups(spec, present):
    groups = []
    for group in spec.get("recommended_plot_groups", []):
        command_parts = PLOT_COMMANDS.get(group)
        if not command_parts:
            continue
        required_messages = {part.split()[1].split(".")[0] for part in command_parts if part.startswith("--series ") or part.startswith("--secondary ")}
        if required_messages and not required_messages.intersection(present):
            continue
        groups.append(group)
    return groups


def _custom_plot_command(log_path, group):
    parts = PLOT_COMMANDS.get(group)
    if not parts:
        return None
    out_name = group.replace("/", "_") + ".html"
    return "python scripts/ap_log_custom_plot.py --tables out/tables {parts} --events --out out/plots/{out}".format(
        parts=" ".join(parts),
        out=out_name,
    )


def _recommended_commands(log_path, symptom_text, spec, present, missing):
    commands = [
        f"python scripts/ap_log_diagnose.py {log_path} --symptom \"{symptom_text}\" --out out/diagnosis.json --plots out/plots/diagnosis"
    ]
    message_plan = []
    for message in spec["required_messages"] + spec["strongly_recommended_messages"] + spec["optional_context_messages"]:
        if message not in message_plan:
            message_plan.append(message)
    if message_plan:
        commands.append(
            "python scripts/ap_log_extract.py {log} --messages {messages} --out out/tables --format csv".format(
                log=log_path,
                messages=",".join(message_plan),
            )
        )
    elif spec.get("recommended_plot_groups"):
        commands.append(f"python scripts/ap_log_extract.py {log_path} --out out/tables --format csv")
    for group in _available_plot_groups(spec, present)[:5]:
        command = _custom_plot_command(log_path, group)
        if command:
            commands.append(command)
    return commands


def _confidence_limits(missing):
    limits = []
    if missing["required"]:
        limits.append("Cannot answer core diagnosis until required evidence is available: " + ", ".join(missing["required"]))
    if missing["strongly_recommended"]:
        limits.append("Do not claim high confidence while strongly recommended evidence is missing: " + ", ".join(missing["strongly_recommended"]))
    if missing["optional_context"]:
        timeline_missing = [name for name in ["MODE", "MSG", "EV", "ERR"] if name in missing["optional_context"]]
        if timeline_missing:
            limits.append("Timeline confidence is reduced because optional timeline context is missing: " + ", ".join(timeline_missing))
    return limits


def _yaw_questions_first(symptom_class, questions):
    if symptom_class != "yaw_misbehaviour":
        return questions
    required = [
        "Was yaw commanded or uncommanded?",
        "Did RATE.Y follow RATE.YDes?",
        "Was RATE.YOut high during the error?",
        "Were motor outputs saturated?",
        "Was there EKF or magnetic evidence at the same time?",
    ]
    merged = required + [q for q in questions if q not in required]
    return merged


def build_manifest_from_index(index, symptom_text, log_path):
    symptom_class = classify_symptom(symptom_text)
    spec = requirement_spec(symptom_class)
    present = _present_messages(index)
    missing = _missing_evidence(index, spec)
    plot_groups = _available_plot_groups(spec, present)
    warnings = []
    stats = index.get("parser_stats", {})
    if stats.get("max_messages_reached"):
        warnings.append("Manifest stopped at --max-messages; evidence inventory may be partial.")
    if stats.get("armed_only") and not stats.get("armed_filter_supported"):
        warnings.append("--armed-only was requested, but ARM state could not be confirmed from ARM messages.")
    if index.get("logging_dropouts"):
        warnings.append("Possible logging dropout/drop count evidence was found; inspect index.logging_dropouts.")
    return {
        "symptom_text": symptom_text,
        "symptom_class": symptom_class,
        "warnings": warnings,
        "available_evidence": _available_evidence(index),
        "missing_evidence": missing,
        "recommended_next_commands": _recommended_commands(log_path, symptom_text, spec, present, missing),
        "recommended_plots": plot_groups,
        "questions_to_answer": _yaw_questions_first(symptom_class, spec.get("diagnostic_questions", [])),
        "confidence_limits": _confidence_limits(missing),
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Create a pre-diagnosis ArduPilot investigation manifest.")
    p.add_argument("log", help="ArduPilot DataFlash .bin/.log file")
    p.add_argument("--symptom", required=True, help="User-reported symptom text")
    p.add_argument("--out", default="investigation.json", help="Output JSON path")
    p.add_argument("--max-messages", type=int, default=None, help="Optional parse limit for quick inspection")
    p.add_argument("--start-time", type=float, default=None, help="Optional start TimeS")
    p.add_argument("--end-time", type=float, default=None, help="Optional end TimeS")
    p.add_argument("--armed-only", action="store_true", help="Index rows only while ARM messages indicate armed state when available")
    args = p.parse_args()
    try:
        if args.start_time is not None and args.end_time is not None and args.end_time < args.start_time:
            raise AnalysisError("--end-time must be greater than or equal to --start-time")
        _rows, index, _stats = collect_dataflash(
            args.log,
            include=[],
            max_messages=args.max_messages,
            start_s=args.start_time,
            end_s=args.end_time,
            armed_only=args.armed_only,
        )
        manifest = build_manifest_from_index(index, args.symptom, args.log)
        write_json(args.out, manifest)
        print(f"Investigation manifest class={manifest['symptom_class']}; commands={len(manifest['recommended_next_commands'])}")
        return 0
    except AnalysisError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
