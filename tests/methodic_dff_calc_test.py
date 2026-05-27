#!/usr/bin/env python3
from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from ap_methodic_dff_calc import analyze_dff_calc


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


def make_messages(*, poor_rc_isolation=False, saturation=False, missing_rate=False):
    messages = [
        FakeMessage("PARM", TimeS=0.0, Name="ATC_RAT_RLL_D_FF", Value=0.0),
        FakeMessage("PARM", TimeS=0.0, Name="ATC_RAT_PIT_D_FF", Value=0.0),
        FakeMessage("PARM", TimeS=0.0, Name="ATC_RAT_YAW_D_FF", Value=0.0),
        FakeMessage("PARM", TimeS=0.0, Name="INS_HNTCH_ENABLE", Value=1),
        FakeMessage("PARM", TimeS=0.0, Name="INS_GYRO_FILTER", Value=40),
        FakeMessage("MODE", TimeS=0.0, Mode="ALTHOLD"),
        FakeMessage("ARM", TimeS=0.0, ArmState=1),
    ]
    dt = 0.04
    dff = 0.012
    previous = {"roll": 0.0, "pitch": 0.0, "yaw": 0.0}
    for i in range(900):
        t = i * dt
        seg = int(t // 10)
        roll = pitch = yaw = 0.0
        rdes = pdes = ydes = 0.0
        c1 = c2 = c4 = 1500
        if seg == 0:
            roll = 65.0 * math.sin(t * 2.6)
            rdes = roll
            c1 = 1500 + 160 * math.sin(t * 2.6)
            if poor_rc_isolation:
                pitch = 45.0 * math.sin(t * 2.2)
                pdes = pitch
                c2 = 1500 + 150 * math.sin(t * 2.2)
        elif seg == 1:
            pitch = 60.0 * math.sin(t * 2.4)
            pdes = pitch
            c2 = 1500 + 160 * math.sin(t * 2.4)
            if poor_rc_isolation:
                roll = 45.0 * math.sin(t * 2.1)
                rdes = roll
                c1 = 1500 + 150 * math.sin(t * 2.1)
        else:
            yaw = 35.0 * math.sin(t * 2.0)
            ydes = yaw
            c4 = 1500 + 150 * math.sin(t * 2.0)
            if poor_rc_isolation:
                roll = 35.0 * math.sin(t * 1.9)
                rdes = roll
                c1 = 1500 + 130 * math.sin(t * 1.9)
        racc = (roll - previous["roll"]) * math.pi / (180.0 * dt)
        pacc = (pitch - previous["pitch"]) * math.pi / (180.0 * dt)
        yacc = (yaw - previous["yaw"]) * math.pi / (180.0 * dt)
        previous = {"roll": roll, "pitch": pitch, "yaw": yaw}
        rout = max(min(dff * racc, 0.38), -0.38)
        pout = max(min(dff * pacc, 0.38), -0.38)
        yout = max(min(dff * yacc, 0.38), -0.38)
        if not missing_rate:
            messages.append(FakeMessage("RATE", TimeS=t, RDes=rdes, R=roll, PDes=pdes, P=pitch, YDes=ydes, Y=yaw, ROut=rout, POut=pout, YOut=yout))
        high = 1910 if saturation else 1520
        messages.extend([
            FakeMessage("ATT", TimeS=t, DesRoll=rdes / 12.0, Roll=roll / 12.0, DesPitch=pdes / 12.0, Pitch=pitch / 12.0, DesYaw=ydes / 8.0, Yaw=yaw / 8.0),
            FakeMessage("RCIN", TimeS=t, C1=c1, C2=c2, C3=1500, C4=c4),
            FakeMessage("RCOU", TimeS=t, C1=high if saturation else 1500 + 20 * math.sin(t), C2=1500 + 18 * math.sin(t * 0.8), C3=1498 + 16 * math.sin(t * 1.1), C4=1505 + 14 * math.sin(t * 0.9)),
            FakeMessage("VIBE", TimeS=t, VibeX=8.0, VibeY=7.0, VibeZ=6.0, Clip0=0, Clip1=0, Clip2=0),
            FakeMessage("BAT", TimeS=t, Volt=15.6, Curr=18.0),
            FakeMessage("POWR", TimeS=t, Vcc=5.05, Vservo=5.1, Flags=0),
            FakeMessage("PIDR", TimeS=t, P=0.02, I=0.01, D=0.002, FF=0.01, DFF=0.0, Dmod=1.0, Flags=0),
            FakeMessage("PIDP", TimeS=t, P=0.02, I=0.01, D=0.002, FF=0.01, DFF=0.0, Dmod=1.0, Flags=0),
            FakeMessage("PIDY", TimeS=t, P=0.015, I=0.015, D=0.0, FF=0.01, DFF=0.0, Dmod=1.0, Flags=0),
        ])
    return sorted(messages, key=lambda msg: (msg.fields.get("TimeS", 0.0), msg.typ))


def run_case(messages, **kwargs):
    import ap_common
    import ap_methodic_dff_calc

    original_common = ap_common.iter_dataflash_messages
    original_local = ap_methodic_dff_calc.collect_dataflash.__globals__["_message_iter"]

    def fake_iter(_source, max_messages=None):
        yield from messages[:max_messages]

    ap_common.iter_dataflash_messages = fake_iter
    ap_methodic_dff_calc.collect_dataflash.__globals__["_message_iter"] = fake_iter
    try:
        return analyze_dff_calc("synthetic.BIN", **kwargs)
    finally:
        ap_common.iter_dataflash_messages = original_common
        ap_methodic_dff_calc.collect_dataflash.__globals__["_message_iter"] = original_local


def test_good_isolated_manoeuvre_produces_candidate():
    result = run_case(make_messages(), axes=["roll,pitch,yaw"])
    assert_true(result["result"] == "candidate", result)
    assert_true(result["validation_required"] is True, result)
    assert_true("roll" in result["candidate_dff"], result)
    assert_true(0.006 < result["candidate_dff"]["roll"] < 0.02, result)


def test_poor_rc_isolation_is_inconclusive():
    result = run_case(make_messages(poor_rc_isolation=True), axes=["roll,pitch"])
    assert_true(result["result"] == "inconclusive", result)
    assert_true(any("isolation" in reason.lower() for reason in result["reason_not_recommended"]), result)


def test_saturation_is_do_not_use():
    result = run_case(make_messages(saturation=True), axes=["roll"])
    assert_true(result["result"] == "do_not_use", result)
    assert_true(result["safety_gate"] == "do_not_proceed", result)


def test_missing_rate_is_inconclusive():
    result = run_case(make_messages(missing_rate=True), axes=["roll"])
    assert_true(result["result"] == "inconclusive", result)
    assert_true(any("RATE" in item for item in result["missing_evidence"]), result)


def test_methodic_step_dispatches_9_7():
    import ap_methodic_step

    assert_true(ap_methodic_step.STEP_IMPLEMENTATIONS.get("9.7") == "analyze_9_7", ap_methodic_step.STEP_IMPLEMENTATIONS)


def main():
    test_good_isolated_manoeuvre_produces_candidate()
    test_poor_rc_isolation_is_inconclusive()
    test_saturation_is_do_not_use()
    test_missing_rate_is_inconclusive()
    test_methodic_step_dispatches_9_7()
    print("methodic D_FF calculation tests passed")


if __name__ == "__main__":
    main()
