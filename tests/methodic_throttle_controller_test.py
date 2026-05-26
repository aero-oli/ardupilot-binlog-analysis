#!/usr/bin/env python3
from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from ap_methodic_throttle_controller import analyze_throttle_controller


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


def make_messages(*, missing_ctun=False, poor_altitude=False, sag="none", severe_vibe=False):
    messages = [
        FakeMessage("PARM", TimeS=0.0, Name="MOT_THST_HOVER", Value=0.48),
        FakeMessage("PARM", TimeS=0.0, Name="MOT_HOVER_LEARN", Value=2),
        FakeMessage("PARM", TimeS=0.0, Name="MOT_THST_EXPO", Value=0.65),
        FakeMessage("PARM", TimeS=0.0, Name="MOT_SPIN_MIN", Value=0.12),
        FakeMessage("PARM", TimeS=0.0, Name="PILOT_THR_BHV", Value=0),
        FakeMessage("PARM", TimeS=0.0, Name="PSC_ACCZ_P", Value=0.5),
        FakeMessage("PARM", TimeS=0.0, Name="PSC_ACCZ_I", Value=1.0),
        FakeMessage("PARM", TimeS=0.0, Name="PSC_ACCZ_D", Value=0.0),
        FakeMessage("PARM", TimeS=0.0, Name="PSC_VELZ_P", Value=5.0),
        FakeMessage("PARM", TimeS=0.0, Name="PSC_VELZ_I", Value=1.0),
        FakeMessage("PARM", TimeS=0.0, Name="PSC_POSZ_P", Value=1.0),
        FakeMessage("PARM", TimeS=0.0, Name="RCMAP_ROLL", Value=1),
        FakeMessage("PARM", TimeS=0.0, Name="RCMAP_PITCH", Value=2),
        FakeMessage("PARM", TimeS=0.0, Name="RCMAP_THROTTLE", Value=3),
        FakeMessage("PARM", TimeS=0.0, Name="RCMAP_YAW", Value=4),
        FakeMessage("PARM", TimeS=0.0, Name="RC1_TRIM", Value=1500),
        FakeMessage("PARM", TimeS=0.0, Name="RC2_TRIM", Value=1500),
        FakeMessage("PARM", TimeS=0.0, Name="RC3_TRIM", Value=1500),
        FakeMessage("PARM", TimeS=0.0, Name="RC4_TRIM", Value=1500),
        FakeMessage("MODE", TimeS=0.0, Mode="STABILIZE"),
        FakeMessage("MODE", TimeS=8.0, Mode="ALTHOLD"),
        FakeMessage("MODE", TimeS=40.0, Mode="LAND"),
        FakeMessage("ARM", TimeS=2.0, ArmState=1),
        FakeMessage("ARM", TimeS=45.0, ArmState=0),
    ]
    for i in range(260):
        t = i * 0.2
        if t < 7:
            alt = 0.0
            dalt = 0.0
            tho = 0.12
            pwm = 1120
        elif t < 11:
            alt = (t - 7.0) * 0.35
            dalt = alt
            tho = 0.55
            pwm = 1460
        elif t < 36:
            dalt = 1.5
            alt = 1.5 + (1.35 * math.sin(t * 0.7) if poor_altitude else 0.04 * math.sin(t))
            tho = 0.50 + (0.04 * math.sin(t * 0.5))
            pwm = 1500
        elif t < 41:
            alt = max(0.0, 1.5 - (t - 36.0) * 0.3)
            dalt = alt
            tho = 0.35
            pwm = 1320
        else:
            alt = 0.0
            dalt = 0.0
            tho = 0.10
            pwm = 1080
        if sag == "conditional":
            volt = 14.0 if 11 <= t <= 36 else 15.8
        elif sag == "severe":
            volt = 10.8 if 11 <= t <= 36 else 15.8
        else:
            volt = 15.2
        vibe = 75.0 if severe_vibe and 14 <= t <= 24 else 8.0
        clip = 3 if severe_vibe and t >= 18 else 0
        row = [
            FakeMessage("ATT", TimeS=t, Roll=1.0, Pitch=1.0, Yaw=45.0),
            FakeMessage("RATE", TimeS=t, ROut=0.02, POut=0.02, YOut=0.01),
            FakeMessage("RCIN", TimeS=t, C1=1500, C2=1500, C3=1500, C4=1500),
            FakeMessage("RCOU", TimeS=t, C1=pwm, C2=pwm + 8, C3=pwm - 6, C4=pwm + 4),
            FakeMessage("BAT", TimeS=t, Volt=volt, Curr=14.0),
            FakeMessage("POWR", TimeS=t, Vcc=5.05 if sag != "severe" else 4.9),
            FakeMessage("BARO", TimeS=t, Alt=alt),
            FakeMessage("VIBE", TimeS=t, VibeX=vibe, VibeY=vibe * 0.7, VibeZ=vibe * 0.5, Clip0=clip),
        ]
        if not missing_ctun:
            row.append(FakeMessage("CTUN", TimeS=t, Alt=alt, DAlt=dalt, ThO=tho, ThH=0.48))
        messages.extend(row)
    return sorted(messages, key=lambda msg: (msg.fields.get("TimeS", 0.0), msg.typ))


def run_case(messages):
    import ap_common
    import ap_methodic_throttle_controller

    original_common = ap_common.iter_dataflash_messages
    original_local = ap_methodic_throttle_controller.collect_dataflash.__globals__["_message_iter"]

    def fake_iter(_source, max_messages=None):
        yield from messages[:max_messages]

    ap_common.iter_dataflash_messages = fake_iter
    ap_methodic_throttle_controller.collect_dataflash.__globals__["_message_iter"] = fake_iter
    try:
        return analyze_throttle_controller("synthetic.BIN")
    finally:
        ap_common.iter_dataflash_messages = original_common
        ap_methodic_throttle_controller.collect_dataflash.__globals__["_message_iter"] = original_local


def test_stable_hover_pass():
    result = run_case(make_messages())
    assert_true(result["result"] == "pass", result)
    assert_true(result["hover_throttle_assessment"]["assessment"] == "plausible", result)


def test_missing_ctun_inconclusive():
    result = run_case(make_messages(missing_ctun=True))
    assert_true(result["result"] == "inconclusive", result)
    assert_true(any("CTUN" in item for item in result["missing_evidence"]), result)


def test_poor_altitude_tracking_fail():
    result = run_case(make_messages(poor_altitude=True))
    assert_true(result["result"] == "fail", result)
    assert_true(result["altitude_control_assessment"]["assessment"] == "poor", result)


def test_voltage_sag_conditional_or_fail():
    conditional = run_case(make_messages(sag="conditional"))
    severe = run_case(make_messages(sag="severe"))
    assert_true(conditional["result"] in {"conditional_pass", "fail"}, conditional)
    assert_true(any("Battery voltage sag" in item["finding"] for item in conditional["findings"]), conditional)
    assert_true(severe["result"] == "fail", severe)


def test_vibration_blocker_fail():
    result = run_case(make_messages(severe_vibe=True))
    assert_true(result["result"] == "fail", result)
    assert_true(result["safety_gate"] == "bench_check_required", result)


def main():
    test_stable_hover_pass()
    test_missing_ctun_inconclusive()
    test_poor_altitude_tracking_fail()
    test_voltage_sag_conditional_or_fail()
    test_vibration_blocker_fail()
    print("methodic throttle-controller tests passed")


if __name__ == "__main__":
    main()
