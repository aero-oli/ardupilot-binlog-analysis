#!/usr/bin/env bash
set -euo pipefail
python - <<'PY'
from pathlib import Path
for path in Path("scripts").glob("*.py"):
    compile(path.read_text(encoding="utf-8"), str(path), "exec")
PY
python - <<'PY'
from pathlib import Path

skill = Path("SKILL.md").read_text(encoding="utf-8")
openai_yaml = Path("agents/openai.yaml").read_text(encoding="utf-8")
how_to = Path("references/how-to-investigate.md").read_text(encoding="utf-8")
logging_reference_path = Path("references/logging-configuration-for-investigation.md")
evidence_reference_path = Path("references/evidence-gathering-flights.md")
rc_reference_path = Path("references/rc-failsafe-prearm-diagnosis.md")
compass_reference_path = Path("references/compass-yaw-source-diagnosis.md")
baro_reference_path = Path("references/baro-rangefinder-altitude-diagnosis.md")

frontmatter = skill.split("---", 2)[1]
assert "name: ardupilot-binlog-analysis" in frontmatter
description_line = next(line for line in frontmatter.splitlines() if line.startswith("description: "))
description = description_line.removeprefix("description: ")
assert len(description) <= 300, len(description)
for required in [
    "ArduPilot DataFlash",
    ".bin/.log",
    "Copter log diagnosis",
    "tuning review",
    "symptom-led fault analysis",
    "plots",
    "vibration/FFT",
    "EKF/GPS",
    "power",
    "motor/ESC",
    "AutoTune",
    "System ID",
    "before/after comparison",
    "Not for PX4 .ulg logs unless converted",
]:
    assert required in description, required
for required in [
    "short_description:",
    "DataFlash .bin/.log",
    "Copter diagnosis",
    "vibration/FFT",
    "EKF/GPS",
    "motor/ESC",
    "AutoTune",
    "System ID",
    "comparisons",
    "safety-first, evidence-backed reasoning",
    "missing data",
    "confidence limits",
]:
    assert required in openai_yaml, required

assert logging_reference_path.exists()
reference = logging_reference_path.read_text(encoding="utf-8")
assert "references/logging-configuration-for-investigation.md" in skill
for required in [
    "## When evidence is missing",
    "missing_required",
    "missing_strongly_recommended",
    "missing_optional",
    "what_cannot_be_concluded",
    "next_evidence_gathering",
    "secondary_symptom_classes",
    "recommended_secondary_commands",
    "flight_status",
    "recommended_next_steps",
    "manual_control_confidence",
    "manual_control_limitations",
    "do not describe POSHOLD as pure manual control",
    "references/err-subsys-ecode.md",
    "python scripts/ap_err_decode.py",
    "python scripts/ap_case_investigate.py",
    "python scripts/ap_evidence_digest.py",
    "python scripts/ap_log_diagnose_modes.py",
    "python scripts/ap_next_steps.py",
    "python scripts/ap_log_investigation_manifest.py LOG.BIN --symptom \"USER SYMPTOM\" --out out/investigation.json",
    "Do not automatically recommend another flight",
    "bench inspection and ground checks first",
    "no repeat flight until hardware and setup checks are complete",
    "`LOG_DISARMED`/boot logging",
    "normally disabled again after the diagnostic test",
]:
    assert required in skill, required
for required in [
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
    "Do not disable arming checks",
    "High-volume settings should normally be returned to normal",
]:
    assert required in reference, required

assert evidence_reference_path.exists()
evidence_reference = evidence_reference_path.read_text(encoding="utf-8")
assert "references/evidence-gathering-flights.md" in skill
assert "evidence-gathering-flights.md" in how_to
for how_to_section in [
    "## Worked Yaw Example",
    "## Worked Loiter / GPS-EKF Example",
    "## Worked Vibration / Filter / FFT Example",
    "## Worked Motor / ESC / Thrust-Loss Example",
    "## Worked Altitude / Throttle Example",
]:
    assert how_to_section in how_to, how_to_section
for how_to_required in [
    "python scripts/ap_log_validate.py",
    "python scripts/ap_log_index.py",
    "python scripts/ap_log_investigation_manifest.py",
    "python scripts/ap_log_diagnose.py",
    "python scripts/ap_log_extract.py",
    "python scripts/ap_log_custom_plot.py",
    "python scripts/ap_log_fft.py",
    "scripts provide evidence and hypotheses",
]:
    assert how_to_required in how_to, how_to_required
for symptom_class in [
    "yaw_misbehaviour",
    "attitude_rate_issue",
    "motor_esc_issue",
    "vibration_issue",
    "ekf_gps_issue",
    "compass_yaw_source_issue",
    "battery_power_issue",
    "altitude_throttle_issue",
    "baro_rangefinder_altitude_issue",
    "rc_failsafe_prearm_issue",
    "crash_or_loss_of_control",
    "general_investigation",
]:
    assert f"### {symptom_class}" in evidence_reference, symptom_class
for required in [
    "Minimum required evidence",
    "Strongly recommended evidence",
    "Useful optional context",
    "Logging parameters to review",
    "Suggested plots/signals",
    "Bench checks before flight",
    "Do not fly if",
    "Cleanup",
    "do not recommend repeat flight",
    "LOG_DISARMED",
    "INS_RAW_LOG_OPT",
]:
    assert required in evidence_reference, required

assert rc_reference_path.exists()
rc_reference = rc_reference_path.read_text(encoding="utf-8")
assert "references/rc-failsafe-prearm-diagnosis.md" in skill
for required in [
    "MSG",
    "ERR",
    "RCIN",
    "RCMAP_ROLL",
    "BATT_ARM_VOLT",
    "GPS",
    "XKF4",
    "LOG_DISARMED",
    "Do not bypass arming checks",
]:
    assert required in rc_reference, required

assert compass_reference_path.exists()
compass_reference = compass_reference_path.read_text(encoding="utf-8")
assert "references/compass-yaw-source-diagnosis.md" in skill
for required in [
    "MAG",
    "XKF3",
    "XKF4",
    "EK3_SRC1_YAW",
    "GPS yaw",
    "Do not recommend disabling compass",
]:
    assert required in compass_reference, required

assert baro_reference_path.exists()
baro_reference = baro_reference_path.read_text(encoding="utf-8")
assert "references/baro-rangefinder-altitude-diagnosis.md" in skill
for required in [
    "CTUN",
    "BARO",
    "RNGF",
    "RNGFND1_TYPE",
    "EK3_SRC1_POSZ",
    "Do not suggest aggressive altitude testing",
]:
    assert required in baro_reference, required
PY
python scripts/ap_symptom_classifier.py "the yaw seems to be misbehaving" | grep yaw_misbehaviour >/dev/null
python scripts/ap_symptom_classifier.py "compass interference" | grep compass_yaw_source_issue >/dev/null
python scripts/ap_symptom_classifier.py "rangefinder altitude jumps" | grep baro_rangefinder_altitude_issue >/dev/null
python scripts/ap_fault_tree.py yaw_misbehaviour | grep RATE.YDes >/dev/null
python tests/reference_consistency_test.py
python tests/methodic_helpers_test.py
python tests/methodic_first_flight_test.py
python tests/methodic_711_test.py
python tests/methodic_notch_review_test.py
python tests/methodic_throttle_controller_test.py
python tests/methodic_pid_notch_review_test.py
python tests/methodic_ekf_altitude_source_test.py
python - <<'PY'
import sys
sys.path.insert(0, "scripts")
import ap_common
from ap_log_compare import metric_differences
from ap_log_validate import module_availability

assert ap_common.parse_time_window("1:2") == {"start_s": 1.0, "end_s": 2.0}
assert ap_common.parse_time_window("around:10:3") == {"start_s": 7.0, "end_s": 13.0}
mapping = ap_common.output_mapping_from_params({"SERVO1_FUNCTION": 33, "SERVO9_FUNCTION": 82, "SERVO2_FUNCTION": 41})
assert mapping["C1"]["role"] == "motor1"
assert mapping["C1"]["category"] == "motor"
assert mapping["C9"]["role"] == "motor9"
assert mapping["C2"]["category"] == "tilt"
assert ap_common.classify_symptom("toilet bowling in loiter after a GPS glitch") == "ekf_gps_issue"
modules = module_availability({"messages": {"ATT": {}, "RATE": {}, "PIDY": {}, "RCOU": {}, "MODE": {}, "MSG": {}, "EV": {}, "ERR": {}}})
assert modules["yaw_diagnosis"]["status"] == "available"
assert modules["yaw_diagnosis"]["missing_strongly_recommended"] == []
assert "MAG" in modules["yaw_diagnosis"]["missing_optional_context"]
diffs = metric_differences({"health": {"battery": {"min_voltage": 15.0}}}, {"health": {"battery": {"min_voltage": 14.5}}})
assert diffs and diffs[0]["metric"] == "health.battery.min_voltage"
PY
