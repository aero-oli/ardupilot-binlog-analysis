#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ap_common import AnalysisError, classify_symptom, collect_dataflash, missing_messages, write_json
from ap_parameters import select_relevant_parameters
from ap_rcin import rc_channel_mapping, rcin_channel_col
from ap_symptom_map import diagnosis_requirements, requirement_spec


EVIDENCE_GROUPS = {
    "core": {"ATT", "RATE", "GPS", "VIBE", "BAT", "CTUN"},
    "controller": {"RATE", "PIDR", "PIDP", "PIDY", "PIDA", "CTUN", "RCIN"},
    "actuator": {"RCOU", "RCO2", "RCO3", "ESC", "ESCX", "EDT2", "PARM"},
    "estimator": {"GPS", "GPA", "GPS2", "MAG", "XKF1", "XKF2", "XKF3", "XKF4", "NKF1", "NKF2", "NKF3", "NKF4", "BARO", "RNGF"},
    "power": {"BAT", "BCL", "POWR"},
    "vibration": {"VIBE", "IMU", "GYR", "ACC", "ISBH", "ISBD"},
    "timeline": {"MODE", "MSG", "EV", "ERR", "ARM", "RCIN"},
}

PLOT_COMMANDS = {
    "yaw_attitude": ["--series ATT.DesYaw", "--series ATT.Yaw", "--title \"Yaw desired vs achieved\""],
    "yaw_rate": ["--series RATE.YDes", "--series RATE.Y", "--series RATE.YOut", "--secondary RATE.YOut", "--title \"Yaw rate tracking and output\""],
    "rcin_yaw_rate": ["--series RCIN.C4=RC yaw input", "--series RATE.YDes", "--series RATE.Y", "--title \"RCIN yaw command and yaw rate response\""],
    "rcin_roll_attitude": ["--series RCIN.C1=RC roll input", "--series ATT.DesRoll", "--series ATT.Roll", "--title \"RCIN roll command and attitude response\""],
    "rcin_pitch_attitude": ["--series RCIN.C2=RC pitch input", "--series ATT.DesPitch", "--series ATT.Pitch", "--title \"RCIN pitch command and attitude response\""],
    "rcin_throttle_power": ["--series RCIN.C3=RC throttle input", "--series CTUN.ThO", "--series BAT.Curr", "--series BAT.Volt", "--secondary BAT.Curr", "--title \"RCIN throttle, output, and battery\""],
    "yaw_pid": ["--series PIDY.Tar", "--series PIDY.Act", "--series PIDY.Err", "--secondary PIDY.Flags", "--title \"Yaw PID evidence\""],
    "motor_outputs": ["--series RCOU.C1", "--series RCOU.C2", "--series RCOU.C3", "--series RCOU.C4", "--title \"Motor outputs\""],
    "power": ["--series BAT.Volt", "--series BAT.Curr", "--secondary BAT.Curr", "--title \"Battery voltage and current\""],
    "vibration": ["--series VIBE.VibeX", "--series VIBE.VibeY", "--series VIBE.VibeZ", "--title \"Vibration\""],
    "rate_tracking": ["--series RATE.RDes", "--series RATE.R", "--series RATE.PDes", "--series RATE.P", "--series RATE.YDes", "--series RATE.Y", "--title \"Rate tracking\""],
    "mag": ["--series XKF4.SM", "--series XKF4.SH", "--title \"EKF yaw/mag test ratios\""],
    "ekf_mag": ["--series XKF4.SM", "--series XKF4.SH", "--title \"EKF yaw/mag test ratios\""],
    "ekf_gps": ["--series GPS.Status", "--series GPS.NSats", "--series GPS.HDop", "--secondary GPS.HDop", "--title \"GPS quality\""],
    "gps_quality": ["--series GPS.Status", "--series GPS.NSats", "--series GPS.HDop", "--secondary GPS.HDop", "--title \"GPS quality\""],
    "ekf_innovations": ["--series XKF4.SV", "--series XKF4.SP", "--series XKF4.SH", "--series XKF4.SM", "--title \"EKF test ratios\""],
    "attitude_rate": ["--series ATT.DesRoll", "--series ATT.Roll", "--series ATT.DesPitch", "--series ATT.Pitch", "--title \"Attitude tracking\""],
    "pid_terms": ["--series PIDR.Err", "--series PIDP.Err", "--series PIDY.Err", "--title \"PID errors\""],
    "altitude_throttle": ["--series CTUN.DAlt", "--series CTUN.Alt", "--series CTUN.ThO", "--secondary CTUN.ThO", "--title \"Altitude and throttle\""],
    "baro_altitude": ["--series CTUN.DAlt", "--series CTUN.Alt", "--series BARO.Alt", "--series BARO.Press", "--secondary BARO.Press", "--title \"Barometer and altitude estimate\""],
    "esc_telemetry": ["--series ESC.RPM", "--series ESC.Curr", "--series ESC.Err", "--secondary ESC.Err", "--title \"ESC telemetry\""],
    "mode_timeline": ["--series RATE.R", "--events", "--title \"Timeline context\""],
}

SPECIAL_PLOT_GROUPS = {
    "fft": {
        "handled_by": "ap_log_fft.py",
        "available_when_any": {"GYR", "ACC", "IMU", "IMU_FAST", "RAW_IMU", "ISBH", "ISBD"},
        "command": "python scripts/ap_log_fft.py {log} --out out/fft --json out/fft.json",
    },
}

RCIN_PLOT_GROUPS = {
    "rcin_yaw_rate": {"axis": "yaw", "label": "RC yaw input"},
    "rcin_roll_attitude": {"axis": "roll", "label": "RC roll input"},
    "rcin_pitch_attitude": {"axis": "pitch", "label": "RC pitch input"},
    "rcin_throttle_power": {"axis": "throttle", "label": "RC throttle input"},
}

BASE_LOGGING_SETTINGS = ["LOG_BITMASK", "LOG_BACKEND_TYPE", "LOG_FILE_RATEMAX", "LOG_DARM_RATEMAX", "LOG_BLK_RATEMAX"]
HIGH_RATE_LOGGING_SETTINGS = ["INS_RAW_LOG_OPT", "INS_LOG_BAT_MASK", "INS_LOG_BAT_OPT"]
ACTUATOR_OUTPUT_MESSAGES = {"RCOU", "RCO2", "RCO3"}
ESC_TELEMETRY_MESSAGES = {"ESC", "ESCX", "EDT2"}

SYMPTOM_EVIDENCE_PLANS = {
    "yaw_misbehaviour": {
        "default_step": "controlled_flight",
        "safe_to_request_flight": True,
        "bench_checks_first": [
            "Check prop condition/orientation, motor direction, frame twist, loose arms, ESC connections, output mapping, compass mounting, and battery condition before any flight capture.",
        ],
        "logging_settings": BASE_LOGGING_SETTINGS + ["EK3_LOG_LEVEL"],
        "safe_capture": [
            "If bench/setup checks pass, capture 30-60 seconds of stable hover plus small yaw, roll, and pitch inputs in Stabilize or AltHold.",
            "If the symptom is navigation-only, compare AltHold and Loiter only if the vehicle is controllable.",
        ],
        "do_not_attempt": [
            "Do not repeat flight if yaw authority was lost, a motor/ESC/power fault is suspected, or the vehicle spun uncontrollably.",
        ],
        "reset": ["Disable raw/batch IMU logging if it was enabled for supporting vibration evidence; restore ordinary logging rate limits."],
    },
    "attitude_rate_issue": {
        "default_step": "controlled_flight",
        "safe_to_request_flight": True,
        "bench_checks_first": [
            "Check frame, arms, props, motor bearings, ESC/motor sync, CG, payload security, flight-controller mounting, tune plausibility, and output mapping before flight.",
        ],
        "logging_settings": BASE_LOGGING_SETTINGS,
        "safe_capture": [
            "If mechanical/setup checks pass, capture a low-altitude stable hover with small gentle roll and pitch inputs in an open area.",
        ],
        "do_not_attempt": [
            "Do not use aggressive stick steps or fly if oscillation was severe, the aircraft diverged, outputs saturated without recovery, or mechanical faults remain.",
        ],
        "reset": ["Disable high-rate vibration logging if it was enabled for the capture."],
    },
    "motor_esc_issue": {
        "default_step": "bench_check",
        "safe_to_request_flight": False,
        "bench_checks_first": [
            "Inspect props, motor bearings, bells, screws, solder joints, connectors, ESC configuration, motor order, frame damage, and battery/connector health.",
            "Use props-off motor tests where appropriate before considering restrained ground checks.",
        ],
        "logging_settings": BASE_LOGGING_SETTINGS + ["LOG_DISARMED"],
        "safe_capture": [
            "Collect bench or restrained ground-test evidence for motor order, direction, ESC telemetry, abnormal heat/noise, mapped outputs, and power response before flight.",
        ],
        "do_not_attempt": [
            "Do not fly if any motor stopped, desynced, overheated, emitted smoke/smell, has bearing damage, has intermittent wiring, shows ESC errors, or power integrity is in doubt.",
        ],
        "reset": ["Disable LOG_DISARMED if it was only enabled for bench logging."],
    },
    "vibration_issue": {
        "default_step": "controlled_flight",
        "safe_to_request_flight": True,
        "bench_checks_first": [
            "Inspect prop balance/damage, motor bearings, loose screws, flight-controller mounting, wiring contact with the controller, frame resonance, and payload movement before flight.",
        ],
        "logging_settings": BASE_LOGGING_SETTINGS + HIGH_RATE_LOGGING_SETTINGS,
        "safe_capture": [
            "If the vehicle is otherwise controllable, capture a short steady hover or low-risk flight segment with raw/high-rate IMU or batch-sampler evidence.",
            "Keep the raw/filter capture short, expect large logs, and check DSF/DMS dropouts and timestamp continuity afterwards.",
        ],
        "do_not_attempt": [
            "Do not fly if clipping rises rapidly, vibration is severe enough to affect attitude/position hold, hardware is loose/damaged, or the previous event was loss of control.",
        ],
        "reset": ["Clear INS_RAW_LOG_OPT, clear or reduce INS_LOG_BAT_MASK and INS_LOG_BAT_OPT, and restore logging rate limits after the capture."],
    },
    "ekf_gps_issue": {
        "default_step": "controlled_flight",
        "safe_to_request_flight": True,
        "bench_checks_first": [
            "Check GPS placement and antenna view, compass orientation/calibration, magnetic interference, GPS power, EKF source parameters, vibration, and pre-arm messages first.",
        ],
        "logging_settings": BASE_LOGGING_SETTINGS + ["EK3_LOG_LEVEL", "LOG_DISARMED"],
        "safe_capture": [
            "Only if Stabilize/AltHold are already stable, capture an AltHold versus Loiter comparison in open sky/open area and abort at the first navigation drift, yaw-source warning, or EKF/GPS failsafe.",
        ],
        "do_not_attempt": [
            "Do not test Auto until Loiter is behaving safely; do not fly if GPS/compass/EKF warnings persist or manual/altitude modes are not reliable.",
        ],
        "reset": ["Disable LOG_DISARMED if it was only enabled for startup EKF or sensor-init evidence."],
    },
    "compass_yaw_source_issue": {
        "default_step": "controlled_flight",
        "safe_to_request_flight": True,
        "bench_checks_first": [
            "Check compass mounting/orientation, nearby current-carrying wiring, GPS/yaw-source placement, moving-baseline GPS health if used, EKF source parameters, vibration, and pre-arm messages before flight.",
        ],
        "logging_settings": BASE_LOGGING_SETTINGS + ["EK3_LOG_LEVEL", "LOG_DISARMED"],
        "safe_capture": [
            "Only if manual and AltHold control are stable, capture a short open-area hover with small yaw inputs and compare AltHold against Loiter only when navigation behaviour is already safe.",
        ],
        "do_not_attempt": [
            "Do not fly if compass/EKF/yaw-source warnings persist, heading jumps appear on the ground, manual control is unstable, or GPS yaw/moving-baseline health is unresolved.",
        ],
        "reset": ["Disable LOG_DISARMED if it was only enabled for startup yaw-source evidence; restore any temporary high-rate logging."],
    },
    "battery_power_issue": {
        "default_step": "bench_check",
        "safe_to_request_flight": False,
        "bench_checks_first": [
            "Check battery health/internal resistance, connector fit, solder joints, power module, BEC/regulator, voltage/current calibration, ESC power leads, and brownout/reset evidence.",
        ],
        "logging_settings": BASE_LOGGING_SETTINGS + ["LOG_DISARMED"],
        "safe_capture": [
            "Collect bench load or restrained ground evidence first; only consider a short low-altitude hover after power integrity is confirmed.",
        ],
        "do_not_attempt": [
            "Do not fly if brownout, severe voltage sag, connector heating, intermittent power, board Vcc faults, battery damage, or unexplained log termination is suspected.",
        ],
        "reset": ["Disable LOG_DISARMED after bench capture if it was only enabled for this investigation."],
    },
    "rc_failsafe_prearm_issue": {
        "default_step": "ground_test",
        "safe_to_request_flight": False,
        "bench_checks_first": [
            "Check receiver binding/link quality, transmitter setup, RC channel mapping, throttle channel behaviour, safety-switch state, battery voltage, board power, GPS/EKF/compass pre-arm messages, wiring, and parameter dump before any flight.",
        ],
        "logging_settings": BASE_LOGGING_SETTINGS + ["LOG_DISARMED"],
        "safe_capture": [
            "Collect a ground-only boot/pre-arm/arming attempt with LOG_DISARMED if the issue occurs before arming; record the exact GCS pre-arm or failsafe message.",
            "Capture RCIN while moving roll, pitch, throttle, and yaw sticks with props removed or vehicle otherwise made safe for bench checks.",
        ],
        "do_not_attempt": [
            "Do not bypass arming checks, failsafes, receiver checks, GPS/EKF/compass checks, battery checks, or the safety switch to make the vehicle fly for logging.",
            "Do not fly until lost RC input, failsafe, power, or pre-arm messages are understood and corrected.",
        ],
        "reset": ["Disable LOG_DISARMED after the boot/pre-arm/failsafe evidence capture if it is not normally needed."],
    },
    "altitude_throttle_issue": {
        "default_step": "controlled_flight",
        "safe_to_request_flight": True,
        "bench_checks_first": [
            "Check prop/motor thrust, battery condition, barometer foam/airflow, rangefinder mounting/health, vibration, payload/CG, throttle calibration, and altitude-controller parameter sanity.",
        ],
        "logging_settings": BASE_LOGGING_SETTINGS + ["EK3_LOG_LEVEL"],
        "safe_capture": [
            "If thrust authority and sensor health are confirmed, capture stable hover plus small gentle altitude changes in AltHold.",
        ],
        "do_not_attempt": [
            "Do not fly if thrust loss, power sag, severe vibration, barometer/rangefinder fault, or uncontrolled climb/descent is suspected.",
        ],
        "reset": ["Restore any high-rate logging used for vibration or height evidence."],
    },
    "baro_rangefinder_altitude_issue": {
        "default_step": "controlled_flight",
        "safe_to_request_flight": True,
        "bench_checks_first": [
            "Check barometer foam and airflow exposure, rangefinder mounting/orientation/cleanliness, wiring, terrain/range limits, vibration, prop wash, power, and altitude-controller parameter sanity before flight.",
        ],
        "logging_settings": BASE_LOGGING_SETTINGS + ["EK3_LOG_LEVEL"],
        "safe_capture": [
            "If thrust authority and height sensors are healthy, capture a stable hover plus small gentle AltHold altitude changes; include rangefinder/terrain behaviour only in an appropriate environment.",
        ],
        "do_not_attempt": [
            "Do not fly if uncontrolled climb/descent, thrust loss, power sag, severe vibration, barometer fault, or rangefinder fault is suspected.",
        ],
        "reset": ["Restore high-volume logging or temporary height-sensor logging changes after the diagnostic capture."],
    },
    "crash_or_loss_of_control": {
        "default_step": "do_not_fly_until_checked",
        "safe_to_request_flight": False,
        "bench_checks_first": [
            "Perform full airframe, prop, motor, ESC, wiring, connector, battery, power-module, flight-controller mount, sensor-orientation, compass/GPS, failsafe, arming-check, and parameter review before any flight.",
        ],
        "logging_settings": BASE_LOGGING_SETTINGS + ["LOG_DISARMED", "EK3_LOG_LEVEL"],
        "safe_capture": [
            "Start with parameter dump, hardware inspection, repair evidence, boot/pre-arm logging, and ground checks. Treat controlled flight as a later post-repair validation step, not the first evidence activity.",
        ],
        "do_not_attempt": [
            "Do not repeat flight until the cause of loss of control is understood or mitigated and structural, power, propulsion, failsafe, and arming checks are complete.",
        ],
        "reset": ["Disable LOG_DISARMED if it was only enabled for repair validation or startup capture."],
    },
    "general_investigation": {
        "default_step": "parameter_review",
        "safe_to_request_flight": False,
        "bench_checks_first": [
            "Ask for the symptom, phase of flight, mode, timestamp, parameter dump, and general preflight/mechanical inspection before proposing a flight capture.",
        ],
        "logging_settings": BASE_LOGGING_SETTINGS + ["LOG_DISARMED"],
        "safe_capture": [
            "If no safety-critical issue is suspected after triage, a normal low-risk hover or short conservative flight can collect broad baseline data.",
        ],
        "do_not_attempt": [
            "Do not fly if a safety-critical symptom is present but unclassified, the log ended unexpectedly, failsafe/arming warnings are unresolved, or hardware condition is unknown after a hard landing.",
        ],
        "reset": ["Return any temporary logging changes to the normal profile."],
    },
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


def validate_recommended_plot_groups(symptom_specs=None):
    specs = symptom_specs or diagnosis_requirements()
    unknown = []
    for class_name, spec in specs.items():
        for group in spec.get("recommended_plot_groups", []):
            if group not in PLOT_COMMANDS and group not in SPECIAL_PLOT_GROUPS:
                unknown.append(f"{class_name}: {group}")
    if unknown:
        raise AnalysisError(
            "unknown recommended_plot_groups entries; add PLOT_COMMANDS entries or SPECIAL_PLOT_GROUPS registrations: "
            + ", ".join(unknown)
        )


class _InventoryTable:
    def __init__(self, fields):
        self.columns = list(fields)


def _message_fields(index, message):
    return set((index.get("messages", {}).get(message, {}) or {}).get("fields", []) or [])


def _rcin_plot_parts(group, index):
    config = RCIN_PLOT_GROUPS.get(group)
    if not config or "RCIN" not in index.get("messages", {}):
        return None
    fields = _message_fields(index, "RCIN")
    if not fields:
        return None
    mapping = rc_channel_mapping({}, index)
    axis_info = mapping["axes"][config["axis"]]
    field = rcin_channel_col(_InventoryTable(fields), axis_info["channel"])
    if not field:
        return None
    return [f"--series RCIN.{field}={config['label']}"] + PLOT_COMMANDS[group][1:]


def _plot_command_parts(group, index):
    if group in RCIN_PLOT_GROUPS:
        return _rcin_plot_parts(group, index)
    return PLOT_COMMANDS.get(group)


def _available_plot_groups(spec, present, index):
    groups = []
    for group in spec.get("recommended_plot_groups", []):
        command_parts = _plot_command_parts(group, index)
        special = SPECIAL_PLOT_GROUPS.get(group)
        if not command_parts and not special:
            continue
        if command_parts:
            required_messages = {part.split()[1].split(".")[0] for part in command_parts if part.startswith("--series ") or part.startswith("--secondary ")}
            if required_messages and not required_messages.issubset(present):
                continue
        elif special.get("available_when_any") and not (special["available_when_any"] & present):
            continue
        groups.append(group)
    return groups


def _custom_plot_command(log_path, group, index):
    parts = _plot_command_parts(group, index)
    if not parts:
        return None
    out_name = group.replace("/", "_") + ".html"
    return "python scripts/ap_log_custom_plot.py --tables out/tables {parts} --events --out out/plots/{out}".format(
        parts=" ".join(parts),
        out=out_name,
    )


def _special_plot_command(log_path, group):
    spec = SPECIAL_PLOT_GROUPS.get(group)
    if not spec:
        return None
    return spec["command"].format(log=log_path)


def _mode_compare_requested(symptom_text):
    text = str(symptom_text or "").lower()
    return any(token in text for token in ["mission", "auto", "waypoint", "manual", "poshold", "loiter", "during missions"])


def _recommended_commands(log_path, symptom_text, spec, present, missing, index):
    commands = [
        f"python scripts/ap_log_diagnose.py {log_path} --symptom \"{symptom_text}\" --out out/diagnosis.json --plots out/plots/diagnosis"
    ]
    if _mode_compare_requested(symptom_text):
        commands.append(
            f"python scripts/ap_log_mode_compare.py {log_path} --symptom {spec.get('name', classify_symptom(symptom_text))} "
            "--compare-modes AUTO,POSHOLD,LOITER,ALTHOLD,STABILIZE --active-flight-only "
            "--json out/mode_compare.json --plots out/plots/mode_compare"
        )
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
    for group in _available_plot_groups(spec, present, index)[:5]:
        command = _custom_plot_command(log_path, group, index)
        if command is None:
            command = _special_plot_command(log_path, group)
        if command:
            commands.append(command)
    return commands


def _rcin_mapping_limitation(spec, present, index):
    if "RCIN" not in present or not any(group in RCIN_PLOT_GROUPS for group in spec.get("recommended_plot_groups", [])):
        return None
    return rc_channel_mapping({}, index).get("limitation")


def _confidence_limits(missing, rcin_mapping_limitation=None):
    limits = []
    if missing["required"]:
        limits.append("Cannot answer core diagnosis until required evidence is available: " + ", ".join(missing["required"]))
    if missing["strongly_recommended"]:
        limits.append("Do not claim high confidence while strongly recommended evidence is missing: " + ", ".join(missing["strongly_recommended"]))
    if missing["optional_context"]:
        timeline_missing = [name for name in ["MODE", "MSG", "EV", "ERR"] if name in missing["optional_context"]]
        if timeline_missing:
            limits.append("Timeline confidence is reduced because optional timeline context is missing: " + ", ".join(timeline_missing))
    if rcin_mapping_limitation:
        limits.append(rcin_mapping_limitation)
    return limits


def _dedupe(items):
    out = []
    for item in items:
        if item and item not in out:
            out.append(item)
    return out


def _messages_to_capture(spec, missing):
    priority = missing["required"] + missing["strongly_recommended"] + missing["optional_context"]
    full_plan = spec["required_messages"] + spec["strongly_recommended_messages"] + spec["optional_context_messages"]
    return _dedupe(priority + full_plan)


def _is_boot_or_prearm_request(symptom_text):
    text = str(symptom_text or "").lower()
    return any(token in text for token in ["pre-arm", "prearm", "boot", "startup", "start-up", "arming", "arm failure", "won't arm", "wont arm"])


def _next_evidence_reason(symptom_class, step_type, missing):
    missing_required = ", ".join(missing["required"]) or "none"
    missing_strong = ", ".join(missing["strongly_recommended"]) or "none"
    if step_type == "existing_log_analysis":
        return f"{symptom_class} has the required and strongly recommended message tiers available; continue existing-log analysis before requesting new activity."
    if step_type == "do_not_fly_until_checked":
        return f"{symptom_class} is safety-critical; missing required evidence: {missing_required}; missing strongly recommended evidence: {missing_strong}. Do not request repeat flight until checks are complete."
    return f"{symptom_class} is missing required evidence: {missing_required}; missing strongly recommended evidence: {missing_strong}. Choose the lowest-risk activity that can capture those messages."


def _step_type(symptom_class, missing, present, symptom_text):
    if _is_boot_or_prearm_request(symptom_text):
        return "ground_test", False
    if symptom_class == "crash_or_loss_of_control":
        return "do_not_fly_until_checked", False
    if symptom_class in {"motor_esc_issue", "battery_power_issue"}:
        if missing["required"] or missing["strongly_recommended"] or not (present & ACTUATOR_OUTPUT_MESSAGES):
            return "bench_check", False
        return "ground_test", False
    if symptom_class == "vibration_issue" and not (present & {"GYR", "ACC", "IMU", "ISBH", "ISBD"}):
        return "controlled_flight", True
    if not missing["required"] and not missing["strongly_recommended"]:
        return "existing_log_analysis", False
    plan = SYMPTOM_EVIDENCE_PLANS.get(symptom_class, SYMPTOM_EVIDENCE_PLANS["general_investigation"])
    return plan["default_step"], bool(plan["safe_to_request_flight"])


def _next_evidence_gathering(symptom_class, symptom_text, spec, present, missing, confidence_limits):
    plan = SYMPTOM_EVIDENCE_PLANS.get(symptom_class, SYMPTOM_EVIDENCE_PLANS["general_investigation"])
    step_type, safe_to_request_flight = _step_type(symptom_class, missing, present, symptom_text)
    logging_settings = list(plan["logging_settings"])
    suggested_safe_capture = list(plan["safe_capture"])
    do_not_attempt = list(plan["do_not_attempt"])
    reset_after_test = list(plan["reset"])
    confidence = list(confidence_limits)

    if _is_boot_or_prearm_request(symptom_text):
        logging_settings.append("LOG_DISARMED")
        suggested_safe_capture.insert(0, "Use LOG_DISARMED for a boot/pre-arm/arming ground capture; do not request a flight just to collect arming evidence.")
        do_not_attempt.append("Do not bypass arming checks or failsafes to make the vehicle fly for logging.")
        reset_after_test.append("Disable LOG_DISARMED again after boot/pre-arm evidence is collected if it is not normally needed.")
        confidence.append("Boot/pre-arm/arming evidence requires disarmed logging and timeline messages; flight evidence is not required for this step.")

    if symptom_class == "vibration_issue" and not (present & {"GYR", "ACC", "IMU", "ISBH", "ISBD"}):
        logging_settings.extend(HIGH_RATE_LOGGING_SETTINGS)
        suggested_safe_capture.append("For filtering/FFT questions, collect a short raw/high-rate IMU or batch-sampler capture only if the vehicle is otherwise controllable.")
        suggested_safe_capture.append("Warn that raw/high-rate IMU and batch sampling can create large logs or dropouts; check DSF/DMS after capture.")
        reset_after_test.append("Reset high-volume raw/batch IMU logging after the short capture.")
        confidence.append("FFT/filter confidence is limited until raw IMU, high-rate IMU, or batch-sampler evidence is available.")

    if not (present & ESC_TELEMETRY_MESSAGES):
        if symptom_class in {"motor_esc_issue", "yaw_misbehaviour", "attitude_rate_issue", "battery_power_issue", "crash_or_loss_of_control"}:
            suggested_safe_capture.append("Enable ESC telemetry if hardware and firmware support it.")
            suggested_safe_capture.append("If ESC telemetry is not available, use RCOU/RCO2/RCO3 plus BAT/POWR current and voltage as proxy evidence.")
            confidence.append("ESC-level confirmation is limited without ESC/ESCX/EDT2 telemetry.")

    if missing["required"] or missing["strongly_recommended"]:
        confidence.append("New evidence should prioritize missing required and strongly recommended message tiers before optional context.")

    return {
        "safe_to_request_flight": safe_to_request_flight,
        "recommended_next_step_type": step_type,
        "reason": _next_evidence_reason(symptom_class, step_type, missing),
        "bench_checks_first": list(plan["bench_checks_first"]),
        "logging_settings_to_review": _dedupe(logging_settings),
        "messages_to_capture": _messages_to_capture(spec, missing),
        "suggested_safe_capture": _dedupe(suggested_safe_capture),
        "do_not_attempt": _dedupe(do_not_attempt),
        "reset_after_test": _dedupe(reset_after_test),
        "confidence_limits": _dedupe(confidence),
    }


def _yaw_questions_first(symptom_class, questions, symptom_text=""):
    if symptom_class != "yaw_misbehaviour":
        return questions
    required = [
        "Was yaw commanded or uncommanded?",
        "Did RATE.Y follow RATE.YDes?",
        "Was RATE.YOut high during the error?",
        "Were motor outputs saturated?",
        "Was there EKF or magnetic evidence at the same time?",
    ]
    text = str(symptom_text or "").lower()
    if "mission" in text or "auto" in text:
        required.extend([
            "Is the yaw issue mostly in AUTO/mission?",
            "Is RATE.YDes unusually high or continuous in AUTO?",
            "Does WP_YAW_BEHAVIOR explain mission yaw demands?",
        ])
    merged = required + [q for q in questions if q not in required]
    return merged


def build_manifest_from_index(index, symptom_text, log_path):
    validate_recommended_plot_groups()
    symptom_class = classify_symptom(symptom_text)
    spec = requirement_spec(symptom_class)
    present = _present_messages(index)
    missing = _missing_evidence(index, spec)
    plot_groups = _available_plot_groups(spec, present, index)
    rcin_mapping_limitation = _rcin_mapping_limitation(spec, present, index)
    confidence_limits = _confidence_limits(missing, rcin_mapping_limitation=rcin_mapping_limitation)
    warnings = []
    stats = index.get("parser_stats", {})
    if stats.get("max_messages_reached"):
        warnings.append("Manifest stopped at --max-messages; evidence inventory may be partial.")
    if stats.get("armed_only") and not stats.get("armed_filter_supported"):
        warnings.append("--armed-only was requested, but ARM state could not be confirmed from ARM messages.")
    logging_health = index.get("logging_health", {})
    if logging_health.get("confirmed_dropouts"):
        warnings.append("Confirmed logging dropout/drop count evidence was found; inspect index.logging_health.confirmed_dropouts.")
    if logging_health.get("possible_dropouts"):
        warnings.append("Possible logging dropout context was found; inspect index.logging_health.possible_dropouts.")
    if logging_health.get("limits_diagnosis"):
        warnings.append("Logging health limits diagnosis confidence: " + logging_health.get("confidence_impact", "inspect logging_health"))
    return {
        "symptom_text": symptom_text,
        "symptom_class": symptom_class,
        "warnings": warnings,
        "logging_health": logging_health,
        "parameter_context": select_relevant_parameters(symptom_class, index=index),
        "available_evidence": _available_evidence(index),
        "missing_evidence": missing,
        "next_evidence_gathering": _next_evidence_gathering(symptom_class, symptom_text, spec, present, missing, confidence_limits),
        "recommended_next_commands": _recommended_commands(log_path, symptom_text, spec, present, missing, index),
        "recommended_plots": plot_groups,
        "questions_to_answer": _yaw_questions_first(symptom_class, spec.get("diagnostic_questions", []), symptom_text),
        "confidence_limits": confidence_limits,
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
