#!/usr/bin/env python3
from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from ap_methodic_wind_drag_review import analyze_wind_drag_review


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


class FakeMessage:
    def __init__(self, typ, **fields):
        self.typ = typ
        self.fields = fields

    def get_type(self):
        return self.typ

    def to_dict(self):
        out = dict(self.fields)
        if "TimeS" in out and "TimeUS" not in out:
            out["TimeUS"] = int(float(out["TimeS"]) * 1_000_000)
        return out


def make_messages(*, poor_data=False, variable_wind=False):
    messages = [
        FakeMessage("PARM", TimeS=0.0, Name="EK3_DRAG_BCOEF_X", Value=0.0),
        FakeMessage("PARM", TimeS=0.0, Name="EK3_DRAG_BCOEF_Y", Value=0.0),
        FakeMessage("PARM", TimeS=0.0, Name="EK3_DRAG_MCOEF", Value=0.0),
        FakeMessage("MODE", TimeS=0.0, Mode="ALTHOLD"),
        FakeMessage("ARM", TimeS=0.0, ArmState=1),
    ]
    for i in range(300):
        t = i * 0.2
        if poor_data:
            spd = 1.0 + 0.1 * math.sin(t * 0.2)
            accx = 0.1 * math.sin(t * 0.2)
            accy = 0.1 * math.cos(t * 0.2)
        else:
            spd = 2.0 + 4.0 * abs(math.sin(t * 0.12))
            accx = 1.8 * math.sin(t * 0.6)
            accy = 1.2 * math.cos(t * 0.5)
        wind_scale = 1.0 + (2.8 * abs(math.sin(t * 0.3)) if variable_wind else 0.2 * math.sin(t * 0.1))
        messages.extend([
            FakeMessage("GPS", TimeS=t, Spd=spd, NSats=18, HDop=0.7),
            FakeMessage("GPA", TimeS=t, VDop=1.0, HAcc=0.5, VAcc=0.8),
            FakeMessage("IMU", TimeS=t, AccX=accx, AccY=accy, AccZ=-9.8),
            FakeMessage("ACC", TimeS=t, AccX=accx, AccY=accy, AccZ=-9.8),
            FakeMessage("ATT", TimeS=t, Roll=4.0 * math.sin(t * 0.2), Pitch=3.0 * math.cos(t * 0.2), Yaw=20.0),
            FakeMessage("RATE", TimeS=t, R=10.0 * math.sin(t), P=8.0 * math.cos(t), Y=2.0),
            FakeMessage("RCIN", TimeS=t, C1=1500 + 60 * math.sin(t), C2=1500 + 40 * math.cos(t), C3=1500, C4=1500),
            FakeMessage("BAT", TimeS=t, Volt=22.2, Curr=18.0),
            FakeMessage("POWR", TimeS=t, Vcc=5.05, Vservo=5.1, Flags=0),
            FakeMessage("XKF2", TimeS=t, VWN=wind_scale, VWE=0.2),
            FakeMessage("XKF4", TimeS=t, SV=0.2, SP=0.2, SH=0.2, SM=0.2),
        ])
    return sorted(messages, key=lambda msg: (msg.fields.get("TimeS", 0.0), msg.typ))


def run_case(messages, **kwargs):
    import ap_common
    import ap_methodic_wind_drag_review

    original_common = ap_common.iter_dataflash_messages
    original_local = ap_methodic_wind_drag_review.collect_dataflash.__globals__["_message_iter"]

    def fake_iter(_source, max_messages=None):
        yield from messages[:max_messages]

    ap_common.iter_dataflash_messages = fake_iter
    ap_methodic_wind_drag_review.collect_dataflash.__globals__["_message_iter"] = fake_iter
    try:
        return analyze_wind_drag_review("synthetic.BIN", **kwargs)
    finally:
        ap_common.iter_dataflash_messages = original_common
        ap_methodic_wind_drag_review.collect_dataflash.__globals__["_message_iter"] = original_local


def test_missing_mass_area_is_inconclusive():
    result = run_case(make_messages())
    assert_true(result["result"] == "inconclusive", result)
    assert_true("mass_kg" in result["missing_inputs"], result)
    assert_true(result["drag_coefficients"]["available"] is False, result)


def test_usable_synthetic_segment_produces_candidate():
    result = run_case(make_messages(), mass_kg=12.5, frontal_area_m2=0.35, side_area_m2=0.32)
    assert_true(result["result"] == "candidate", result)
    assert_true(result["drag_coefficients"]["available"] is True, result)
    assert_true(abs(result["drag_coefficients"]["EK3_DRAG_BCOEF_X"] - (12.5 / 0.35)) < 1e-6, result)
    assert_true(result["safety_gate"] == "proceed_with_caution", result)


def test_poor_data_repeats_or_inconclusive():
    result = run_case(make_messages(poor_data=True), mass_kg=12.5, frontal_area_m2=0.35, side_area_m2=0.32)
    assert_true(result["result"] in {"repeat_flight", "inconclusive"}, result)
    assert_true(result["drag_coefficients"]["available"] is False, result)


def test_variable_wind_repeats():
    result = run_case(make_messages(variable_wind=True), mass_kg=12.5, frontal_area_m2=0.35, side_area_m2=0.32)
    assert_true(result["result"] == "repeat_flight", result)
    assert_true(any("wind" in item.lower() for item in result["recommended_next_steps"] + result["confidence_limits"]), result)


def test_methodic_step_dispatches_10_1():
    import ap_methodic_step

    assert_true(ap_methodic_step.STEP_IMPLEMENTATIONS.get("10.1") == "analyze_10_1", ap_methodic_step.STEP_IMPLEMENTATIONS)


def main():
    test_missing_mass_area_is_inconclusive()
    test_usable_synthetic_segment_produces_candidate()
    test_poor_data_repeats_or_inconclusive()
    test_variable_wind_repeats()
    test_methodic_step_dispatches_10_1()
    print("methodic wind/drag review tests passed")


if __name__ == "__main__":
    main()
