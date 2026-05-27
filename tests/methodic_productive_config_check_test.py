#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from ap_methodic_productive_config_check import analyze_productive_config_check


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


def base_params(**overrides):
    params = {
        "ARMING_CHECK": 1,
        "BATT_MONITOR": 4,
        "FS_BATT_ENABLE": 1,
        "FS_THR_ENABLE": 1,
        "FS_EKF_ACTION": 1,
        "FS_GCS_ENABLE": 1,
        "FENCE_ENABLE": 1,
        "FENCE_ACTION": 1,
        "LOG_BITMASK": 65535,
        "LOG_DISARMED": 0,
        "INS_RAW_LOG_OPT": 0,
        "INS_LOG_BAT_MASK": 0,
        "INS_LOG_BAT_OPT": 0,
        "INS_HNTCH_ENABLE": 1,
        "INS_HNTCH_MODE": 3,
        "INS_HNTCH_FREQ": 80,
        "INS_HNTCH_BW": 40,
        "INS_GYRO_FILTER": 80,
        "GUID_OPTIONS": 0,
        "EK3_SRC1_YAW": 1,
        "GPS_TYPE": 1,
        "GPS_TYPE2": 0,
        "RCMAP_ROLL": 1,
        "RCMAP_PITCH": 2,
        "RCMAP_THROTTLE": 3,
        "RCMAP_YAW": 4,
        "FLTMODE1": 2,
        "FLTMODE2": 5,
    }
    params.update(overrides)
    return params


def base_progress():
    steps = {
        "7.1": "pass",
        "7.1.1": "pass",
        "8.1": "pass",
        "8.2": "pass",
        "8.4": "pass",
        "8.5": "conditional_pass",
        "9.1": "pass",
        "9.3": "pass",
        "9.4": "pass",
        "9.6": "pass",
        "12.1": "pass",
        "12.2": "not_applicable",
        "12.3": "not_applicable",
    }
    return {"steps": {step: {"methodic_step": step, "result": result} for step, result in steps.items()}}


def write_inputs(tmp: Path, *, params=None, progress=None):
    params = params or base_params()
    progress = progress if progress is not None else base_progress()
    index_path = tmp / "index.json"
    params_path = tmp / "vehicle.param"
    progress_path = tmp / "methodic_progress.json"
    index_path.write_text(json.dumps({"messages": {"PARM": {}, "MODE": {}, "BAT": {}, "GPS": {}, "XKF4": {}}, "parameters": params}), encoding="utf-8")
    params_path.write_text("\n".join(f"{name} {value}" for name, value in sorted(params.items())) + "\n", encoding="utf-8")
    if progress is not None:
        progress_path.write_text(json.dumps(progress), encoding="utf-8")
    return index_path, params_path, progress_path


def run_case(*, params=None, progress=None, include_progress=True):
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        index_path, params_path, progress_path = write_inputs(tmp, params=params, progress=progress if include_progress else None)
        return analyze_productive_config_check(
            index_path=index_path,
            params_path=params_path,
            methodic_progress_path=progress_path if include_progress else None,
        )


def test_raw_logging_left_on_warns_and_cleanup():
    result = run_case(params=base_params(INS_RAW_LOG_OPT=1, LOG_DISARMED=1))
    assert_true(result["productive_config_status"] == "ready_for_operational_checks", result)
    assert_true(any("INS_RAW_LOG_OPT" in item for item in result["warnings"]), result)
    assert_true(any("LOG_DISARMED" in item for item in result["cleanup_actions"]), result)


def test_arming_check_disabled_not_ready():
    result = run_case(params=base_params(ARMING_CHECK=0))
    assert_true(result["productive_config_status"] == "not_ready", result)
    assert_true(result["safety_gate"] == "do_not_proceed", result)
    assert_true(any("ARMING_CHECK" in item for item in result["blocking_items"]), result)


def test_missing_progress_is_inconclusive():
    result = run_case(include_progress=False)
    assert_true(result["productive_config_status"] == "inconclusive", result)
    assert_true(any("--methodic-progress" in item for item in result["missing_evidence"]), result)


def test_clean_config_ready_for_operational_checks():
    result = run_case()
    assert_true(result["productive_config_status"] == "ready_for_operational_checks", result)
    assert_true(result["blocking_items"] == [], result)
    assert_true("safe to fly" not in json.dumps(result).lower(), result)


def test_methodic_step_dispatches_13():
    import ap_methodic_step

    assert_true(ap_methodic_step.STEP_IMPLEMENTATIONS.get("13") == "analyze_13", ap_methodic_step.STEP_IMPLEMENTATIONS)


def main():
    test_raw_logging_left_on_warns_and_cleanup()
    test_arming_check_disabled_not_ready()
    test_missing_progress_is_inconclusive()
    test_clean_config_ready_for_operational_checks()
    test_methodic_step_dispatches_13()
    print("methodic productive configuration tests passed")


if __name__ == "__main__":
    main()
