#!/usr/bin/env python3
from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import pandas as pd

from ap_methodic_oscillation import classify_oscillation
from ap_methodic_rc import analyze_rc_input_contamination
from ap_methodic_windows import select_methodic_window


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


def synthetic_hover_tables(*, include_spool=True, rc_yaw_step=False):
    times = [float(i) for i in range(50)]
    alt = []
    tho = []
    for t in times:
        if t < 6:
            alt.append(0.0)
            tho.append(0.12 if include_spool else 0.0)
        elif t < 12:
            alt.append((t - 6) * 0.3)
            tho.append(0.55)
        elif t < 36:
            alt.append(1.8 + 0.04 * math.sin(t))
            tho.append(0.48 + 0.01 * math.sin(t / 2))
        elif t < 42:
            alt.append(max(0.0, 1.8 - (t - 36) * 0.3))
            tho.append(0.35)
        else:
            alt.append(0.0)
            tho.append(0.1)
    rcout = []
    for t in times:
        value = 1120 if t < 6 else (1450 if t < 42 else 1100)
        rcout.append(value)
    yaw = [1500 + (120 if rc_yaw_step and 20 <= t <= 25 else 0) for t in times]
    return {
        "MODE": pd.DataFrame({"TimeS": [0.0, 8.0, 42.0], "Mode": ["STABILIZE", "ALTHOLD", "LAND"]}),
        "CTUN": pd.DataFrame({"TimeS": times, "Alt": alt, "ThO": tho, "ThH": tho}),
        "ATT": pd.DataFrame({"TimeS": times, "Roll": [1.0] * len(times), "Pitch": [1.5] * len(times)}),
        "RCOU": pd.DataFrame({"TimeS": times, "C1": rcout, "C2": rcout, "C3": rcout, "C4": rcout}),
        "RCIN": pd.DataFrame({
            "TimeS": times,
            "C1": [1500] * len(times),
            "C2": [1500] * len(times),
            "C3": [1500] * len(times),
            "C4": yaw,
        }),
        "PARM": pd.DataFrame({
            "TimeS": [0.0] * 8,
            "Name": ["RCMAP_ROLL", "RCMAP_PITCH", "RCMAP_THROTTLE", "RCMAP_YAW", "RC1_TRIM", "RC2_TRIM", "RC3_TRIM", "RC4_TRIM"],
            "Value": [1, 2, 3, 4, 1500, 1500, 1500, 1500],
        }),
    }


def test_clean_hover_window_detected_from_synthetic_ctun_mode():
    result = select_methodic_window(synthetic_hover_tables(), "first_althold_hover", min_duration_s=5.0)
    selected = result["selected_window"]
    assert_true(selected is not None, "expected first AltHold hover window")
    assert_true(12.0 <= selected["start_s"] <= 14.0, selected)
    assert_true(selected["end_s"] >= 30.0, selected)
    assert_true(result["confidence"] in {"medium", "high"}, result)


def test_ground_spool_excluded():
    result = select_methodic_window(synthetic_hover_tables(include_spool=True), "methodic_hover", min_duration_s=5.0)
    selected = result["selected_window"]
    assert_true(selected is not None, "expected selected hover")
    assert_true(selected["start_s"] > 6.0, f"ground spool was included in selected window: {selected}")
    assert_true(result["spool_rows_excluded"], "expected spool/takeoff/landing rows to be excluded")


def test_rc_centered_subset_created():
    result = analyze_rc_input_contamination(synthetic_hover_tables(rc_yaw_step=True), centered_deadband_us=30.0, yaw_only_centered=True)
    assert_true(result["available"], "RCIN should be available")
    assert_true(result["axis_activity"]["yaw"]["active_percent_by_deadband_us"]["30"] > 0.0, "yaw activity should be detected")
    assert_true(result["rc_centered_windows"], "expected at least one centered RC window")
    assert_true(any(window["end_s"] < 20.0 or window["start_s"] > 25.0 for window in result["rc_centered_windows"]), result["rc_centered_windows"])


def test_steady_biased_signal_classified_as_steady_bias():
    times = [i * 0.1 for i in range(200)]
    values = [0.18 + 0.005 * math.sin(i * 0.2) for i in range(200)]
    result = classify_oscillation(values, times, threshold=0.15)
    assert_true(result["classification"] == "steady_bias", result)


def test_sinusoidal_signal_classified_as_oscillatory():
    times = [i * 0.05 for i in range(300)]
    values = [0.20 * math.sin(2 * math.pi * 1.5 * t) for t in times]
    result = classify_oscillation(values, times, threshold=0.15)
    assert_true(result["classification"] == "oscillatory", result)


def test_noisy_short_signal_classified_inconclusive():
    times = [0.0, 0.05, 0.10, 0.15]
    values = [0.0, 0.2, -0.1, 0.15]
    result = classify_oscillation(values, times, threshold=0.15)
    assert_true(result["classification"] == "inconclusive", result)


def main():
    test_clean_hover_window_detected_from_synthetic_ctun_mode()
    test_ground_spool_excluded()
    test_rc_centered_subset_created()
    test_steady_biased_signal_classified_as_steady_bias()
    test_sinusoidal_signal_classified_as_oscillatory()
    test_noisy_short_signal_classified_inconclusive()
    print("methodic helper tests passed")


if __name__ == "__main__":
    main()
