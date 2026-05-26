#!/usr/bin/env python3
from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import pandas as pd

from ap_methodic_first_flight import analyze_first_flight


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


def make_messages(*, no_althold=False, severe_vibe=False, failsafe=False, rc_contaminated=False):
    messages = [
        FakeMessage("PARM", TimeS=0.0, Name="MOT_THST_HOVER", Value=0.48),
        FakeMessage("PARM", TimeS=0.0, Name="RCMAP_ROLL", Value=1),
        FakeMessage("PARM", TimeS=0.0, Name="RCMAP_PITCH", Value=2),
        FakeMessage("PARM", TimeS=0.0, Name="RCMAP_THROTTLE", Value=3),
        FakeMessage("PARM", TimeS=0.0, Name="RCMAP_YAW", Value=4),
        FakeMessage("PARM", TimeS=0.0, Name="RC1_TRIM", Value=1500),
        FakeMessage("PARM", TimeS=0.0, Name="RC2_TRIM", Value=1500),
        FakeMessage("PARM", TimeS=0.0, Name="RC3_TRIM", Value=1500),
        FakeMessage("PARM", TimeS=0.0, Name="RC4_TRIM", Value=1500),
        FakeMessage("ARM", TimeS=2.0, ArmState=1),
        FakeMessage("MODE", TimeS=0.0, Mode="STABILIZE"),
        FakeMessage("MODE", TimeS=8.0, Mode="STABILIZE" if no_althold else "ALTHOLD"),
        FakeMessage("MODE", TimeS=42.0, Mode="LAND"),
        FakeMessage("ARM", TimeS=46.0, ArmState=0),
    ]
    if failsafe:
        messages.append(FakeMessage("MSG", TimeS=24.0, Message="Radio failsafe triggered"))
    for i in range(50):
        t = float(i)
        if t < 6:
            alt = 0.0
            tho = 0.12
        elif t < 12:
            alt = (t - 6) * 0.3
            tho = 0.55
        elif t < 36:
            alt = 1.8 + 0.04 * math.sin(t)
            tho = 0.48 + 0.01 * math.sin(t / 2)
        elif t < 42:
            alt = max(0.0, 1.8 - (t - 36) * 0.3)
            tho = 0.35
        else:
            alt = 0.0
            tho = 0.1
        out = 1120 if t < 6 else (1450 if t < 42 else 1100)
        vibe = 75.0 if severe_vibe and 15 <= t <= 25 else 8.0
        clip = 10 if severe_vibe and t >= 20 else 0
        yaw = 1650 if rc_contaminated and 12 <= t <= 34 else 1500
        messages.extend([
            FakeMessage("CTUN", TimeS=t, Alt=alt, ThO=tho, ThH=0.48),
            FakeMessage("ATT", TimeS=t, Roll=1.0, Pitch=1.0),
            FakeMessage("RCIN", TimeS=t, C1=1500, C2=1500, C3=1500, C4=yaw),
            FakeMessage("RCOU", TimeS=t, C1=out, C2=out, C3=out, C4=out),
            FakeMessage("VIBE", TimeS=t, VibeX=vibe, VibeY=vibe * 0.7, VibeZ=vibe * 0.5, Clip0=clip),
            FakeMessage("BAT", TimeS=t, Volt=15.2, Curr=12.0),
            FakeMessage("POWR", TimeS=t, Vcc=5.05),
        ])
    return sorted(messages, key=lambda msg: (msg.fields.get("TimeS", 0.0), msg.typ))


def run_case(monkey_messages):
    import ap_common
    import ap_methodic_first_flight

    original_common = ap_common.iter_dataflash_messages
    original_local = ap_methodic_first_flight.collect_dataflash.__globals__["_message_iter"]

    def fake_iter(_source, max_messages=None):
        yield from monkey_messages[:max_messages]

    ap_common.iter_dataflash_messages = fake_iter
    ap_methodic_first_flight.collect_dataflash.__globals__["_message_iter"] = fake_iter
    try:
        return analyze_first_flight("synthetic.BIN")
    finally:
        ap_common.iter_dataflash_messages = original_common
        ap_methodic_first_flight.collect_dataflash.__globals__["_message_iter"] = original_local


def test_good_althold_hover():
    result = run_case(make_messages())
    assert_true(result["detected"]["vehicle_armed"], result)
    assert_true(result["detected"]["takeoff_occurred"], result)
    assert_true(result["detected"]["althold_segment_exists"], result)
    assert_true(result["detected"]["hover_like_segment_exists"], result)
    assert_true(result["next_step"] == "7.1.1", result)
    assert_true(result["result"] in {"pass", "conditional_pass"}, result)


def test_no_althold_hover():
    result = run_case(make_messages(no_althold=True))
    assert_true(not result["detected"]["althold_segment_exists"], result)
    assert_true(result["result"] == "inconclusive", result)
    assert_true(result["next_step"] == "repeat_7.1", result)


def test_severe_vibration_clipping():
    result = run_case(make_messages(severe_vibe=True))
    assert_true(result["result"] == "fail", result)
    assert_true(any("vibration" in item["finding"].lower() for item in result["safety_findings"]), result)


def test_failsafe_event():
    result = run_case(make_messages(failsafe=True))
    assert_true(result["result"] == "fail", result)
    assert_true(any("failsafe" in item["finding"].lower() for item in result["safety_findings"]), result)


def test_rc_contaminated_hover():
    result = run_case(make_messages(rc_contaminated=True))
    assert_true(result["result"] == "conditional_pass", result)
    assert_true(any("rc input" in item["finding"].lower() for item in result["safety_findings"]), result)


def main():
    test_good_althold_hover()
    test_no_althold_hover()
    test_severe_vibration_clipping()
    test_failsafe_event()
    test_rc_contaminated_hover()
    print("methodic first-flight tests passed")


if __name__ == "__main__":
    main()
