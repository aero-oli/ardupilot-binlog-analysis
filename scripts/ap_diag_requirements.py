from __future__ import annotations

ESC_TELEMETRY_GROUP = {"ESC", "ESCX", "EDT2"}


DIAGNOSIS_REQUIREMENTS = {
    "yaw_misbehaviour": {
        "required": ["ATT", "RATE"],
        "strongly_recommended": ["PIDY", "RCOU", "MODE"],
        "optional_context": ["MSG", "EV", "ERR", "RCIN", "MAG", "XKF3", "XKF4", "VIBE", "BAT", "POWR", "ESC", "ESCX", "EDT2"],
    },
    "attitude_rate_issue": {
        "required": ["ATT", "RATE"],
        "strongly_recommended": ["PIDR", "PIDP", "RCOU", "MODE"],
        "optional_context": ["PIDY", "MSG", "EV", "ERR", "VIBE", "BAT", "POWR", "ESC", "ESCX", "EDT2"],
    },
    "ekf_gps_issue": {
        "required": ["GPS"],
        "strongly_recommended": ["XKF1", "XKF3", "XKF4", "MODE"],
        "optional_context": ["MSG", "EV", "ERR", "GPA", "GPS2", "MAG", "VIBE", "BAT", "POWR"],
    },
    "vibration_issue": {
        "required": ["VIBE"],
        "strongly_recommended": ["RATE"],
        "optional_context": ["PIDR", "PIDP", "PIDY", "IMU", "GYR", "ACC", "ISBH", "ISBD", "BAT", "POWR"],
    },
    "battery_power_issue": {
        "required": ["BAT"],
        "strongly_recommended": ["POWR", "RCOU"],
        "optional_context": ["CTUN", "VIBE", "ESC", "ESCX", "EDT2"],
    },
    "motor_esc_issue": {
        "required": [],
        "strongly_recommended": ["RCOU", "RATE"],
        "optional_context": ["ESC", "ESCX", "EDT2", "PIDR", "PIDP", "PIDY", "BAT", "VIBE", "POWR"],
    },
    "crash_or_loss_of_control": {
        "required": [],
        "strongly_recommended": ["ATT", "RATE", "RCOU", "MODE"],
        "optional_context": ["EV", "ERR", "MSG", "BAT", "GPS", "XKF4", "VIBE", "PIDR", "PIDP", "PIDY", "ESC", "ESCX", "EDT2", "POWR", "MAG"],
    },
    "altitude_throttle_issue": {
        "required": [],
        "strongly_recommended": ["CTUN"],
        "optional_context": ["ATT", "RATE", "BAT", "POWR", "VIBE", "BARO", "RNGF", "GPS", "XKF4", "ESC", "ESCX", "EDT2"],
    },
    "general_diagnosis": {
        "required": [],
        "strongly_recommended": ["ATT", "RATE"],
        "optional_context": ["RCOU", "MODE", "MSG", "EV", "ERR", "PIDR", "PIDP", "PIDY", "VIBE", "BAT", "POWR", "GPS", "XKF4", "ESC", "ESCX", "EDT2"],
    },
}


def requirement_spec(symptom_class):
    return DIAGNOSIS_REQUIREMENTS.get(symptom_class, DIAGNOSIS_REQUIREMENTS["general_diagnosis"])


def missing_by_tier(index, symptom_class, missing_messages):
    spec = requirement_spec(symptom_class)
    missing_required = missing_messages(index, spec["required"])
    missing_strongly = missing_messages(index, spec["strongly_recommended"])
    missing_optional = missing_messages(index, spec["optional_context"])
    if any(msg in index.get("messages", {}) for msg in ESC_TELEMETRY_GROUP):
        missing_strongly = [msg for msg in missing_strongly if msg not in ESC_TELEMETRY_GROUP]
        missing_optional = [msg for msg in missing_optional if msg not in ESC_TELEMETRY_GROUP]
    return missing_required, missing_strongly, missing_optional
