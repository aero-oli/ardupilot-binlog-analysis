#!/usr/bin/env bash
set -euo pipefail
python - <<'PY'
from pathlib import Path
for path in Path("scripts").glob("*.py"):
    compile(path.read_text(encoding="utf-8"), str(path), "exec")
PY
python scripts/ap_symptom_classifier.py "the yaw seems to be misbehaving" | grep yaw_misbehaviour >/dev/null
python scripts/ap_fault_tree.py yaw_misbehaviour | grep RATE.YDes >/dev/null
python - <<'PY'
import sys
sys.path.insert(0, "scripts")
import ap_common
from ap_log_compare import metric_differences
from ap_log_validate import module_availability

assert ap_common.parse_time_window("1:2") == {"start_s": 1.0, "end_s": 2.0}
assert ap_common.parse_time_window("around:10:3") == {"start_s": 7.0, "end_s": 13.0}
mapping = ap_common.output_mapping_from_params({"SERVO1_FUNCTION": 33})
assert mapping["C1"]["role"] == "motor1"
assert ap_common.classify_symptom("toilet bowling in loiter after a GPS glitch") == "ekf_gps_issue"
modules = module_availability({"messages": {"ATT": {}, "RATE": {}, "PIDY": {}, "RCOU": {}, "MODE": {}, "MSG": {}, "EV": {}, "ERR": {}}})
assert modules["yaw_diagnosis"]["status"] == "available"
assert "MAG" in modules["yaw_diagnosis"]["missing_optional"]
diffs = metric_differences({"health": {"battery": {"min_voltage": 15.0}}}, {"health": {"battery": {"min_voltage": 14.5}}})
assert diffs and diffs[0]["metric"] == "health.battery.min_voltage"
PY
