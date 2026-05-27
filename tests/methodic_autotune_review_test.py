#!/usr/bin/env python3
from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from ap_methodic_autotune_review import analyze_autotune_review


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


def make_messages(*, complete=True, saved=True, missing_atun=False, severe_vibe=False, bad_solution=False, partial=False):
    messages = [
        FakeMessage("PARM", TimeS=0.0, Name="AUTOTUNE_MIN_D", Value=0.001),
        FakeMessage("PARM", TimeS=0.0, Name="AUTOTUNE_AXES", Value=7),
        FakeMessage("PARM", TimeS=0.0, Name="ATC_RAT_RLL_D", Value=0.006),
        FakeMessage("PARM", TimeS=0.0, Name="ATC_RAT_PIT_D", Value=0.006),
        FakeMessage("PARM", TimeS=0.0, Name="ATC_RAT_YAW_D", Value=0.003),
        FakeMessage("PARM", TimeS=0.0, Name="ATC_ANG_RLL_P", Value=4.5),
        FakeMessage("PARM", TimeS=0.0, Name="ATC_ANG_PIT_P", Value=4.5),
        FakeMessage("PARM", TimeS=0.0, Name="ATC_ANG_YAW_P", Value=4.5),
        FakeMessage("PARM", TimeS=0.0, Name="INS_HNTCH_ENABLE", Value=1),
        FakeMessage("PARM", TimeS=0.0, Name="INS_GYRO_FILTER", Value=40),
        FakeMessage("MODE", TimeS=0.0, Mode="ALTHOLD"),
        FakeMessage("MODE", TimeS=5.0, Mode="AUTOTUNE"),
        FakeMessage("MODE", TimeS=50.0, Mode="ALTHOLD"),
        FakeMessage("MSG", TimeS=5.0, Message="AutoTune: started"),
        FakeMessage("EV", TimeS=6.0, Id=1),
        FakeMessage("ERR", TimeS=60.0, Subsys=0, ECode=0),
    ]
    if complete:
        messages.append(FakeMessage("MSG", TimeS=48.0, Message="AutoTune: complete"))
    if saved:
        messages.append(FakeMessage("MSG", TimeS=49.0, Message="AutoTune: gains saved"))
    if not saved:
        messages.append(FakeMessage("MSG", TimeS=49.0, Message="AutoTune: discarded"))
    final_d = 0.001 if bad_solution else 0.006
    final_angle_p = 2.5 if bad_solution else 4.8
    messages.extend([
        FakeMessage("PARM", TimeS=50.0, Name="ATC_RAT_RLL_D", Value=final_d),
        FakeMessage("PARM", TimeS=50.0, Name="ATC_RAT_PIT_D", Value=final_d),
        FakeMessage("PARM", TimeS=50.0, Name="ATC_RAT_YAW_D", Value=0.001 if bad_solution else 0.003),
        FakeMessage("PARM", TimeS=50.0, Name="ATC_ANG_RLL_P", Value=final_angle_p),
        FakeMessage("PARM", TimeS=50.0, Name="ATC_ANG_PIT_P", Value=final_angle_p),
    ])
    atun_rows = 8 if partial else 48
    if not missing_atun:
        for i in range(atun_rows):
            t = 6.0 + i * 0.8
            axis = i % 3
            messages.append(FakeMessage("ATUN", TimeS=t, Axis=axis, TuneStep=i % 5, Targ=20, Min=-5, Max=5, RP=0.12, RD=final_d, SP=0.12, SD=final_d, YD=0.003))
            messages.append(FakeMessage("ATDE", TimeS=t, Axis=axis, Value=0.1, Notes=0))
    for i in range(620):
        t = i * 0.1
        rdes = 24.0 * math.sin(t * 0.8)
        pdes = 20.0 * math.sin(t * 0.7)
        ydes = 12.0 * math.sin(t * 0.5)
        rout = 0.04 * math.sin(t * 1.4)
        pout = 0.04 * math.sin(t * 1.3)
        yout = 0.03 * math.sin(t * 1.1)
        if bad_solution:
            rout = 0.32 * math.sin(t * 8.0)
        vibe = 55.0 if severe_vibe else 8.0
        clip = i if severe_vibe else 0
        messages.extend([
            FakeMessage("ATT", TimeS=t, DesRoll=rdes / 10.0, Roll=rdes / 10.2, DesPitch=pdes / 10.0, Pitch=pdes / 10.2, DesYaw=ydes / 6.0, Yaw=ydes / 6.2),
            FakeMessage("RATE", TimeS=t, RDes=rdes, R=rdes * 0.94, PDes=pdes, P=pdes * 0.94, YDes=ydes, Y=ydes * 0.92, ROut=rout, POut=pout, YOut=yout),
            FakeMessage("RCOU", TimeS=t, C1=1500 + 20 * math.sin(t), C2=1508 + 18 * math.sin(t * 0.8), C3=1498 + 16 * math.sin(t * 1.1), C4=1505 + 14 * math.sin(t * 0.9)),
            FakeMessage("VIBE", TimeS=t, VibeX=vibe, VibeY=vibe * 0.8, VibeZ=vibe * 0.7, Clip0=clip, Clip1=0, Clip2=0),
            FakeMessage("BAT", TimeS=t, Volt=15.6, Curr=18.0),
            FakeMessage("POWR", TimeS=t, Vcc=5.05, Vservo=5.1, Flags=0),
            FakeMessage("PIDR", TimeS=t, P=0.02, I=0.01, D=0.002, FF=0.01, Dmod=1.0, Flags=0),
            FakeMessage("PIDP", TimeS=t, P=0.02, I=0.01, D=0.002, FF=0.01, Dmod=1.0, Flags=0),
            FakeMessage("PIDY", TimeS=t, P=0.015, I=0.015, D=0.0, FF=0.01, Dmod=1.0, Flags=0),
        ])
    return sorted(messages, key=lambda msg: (msg.fields.get("TimeS", 0.0), msg.typ))


def run_case(messages):
    import ap_common
    import ap_methodic_autotune_review

    original_common = ap_common.iter_dataflash_messages
    original_local = ap_methodic_autotune_review.collect_dataflash.__globals__["_message_iter"]

    def fake_iter(_source, max_messages=None):
        yield from messages[:max_messages]

    ap_common.iter_dataflash_messages = fake_iter
    ap_methodic_autotune_review.collect_dataflash.__globals__["_message_iter"] = fake_iter
    try:
        return analyze_autotune_review("synthetic.BIN")
    finally:
        ap_common.iter_dataflash_messages = original_common
        ap_methodic_autotune_review.collect_dataflash.__globals__["_message_iter"] = original_local


def test_complete_autotune_saved_passes():
    result = run_case(make_messages())
    assert_true(result["autotune_detected"] is True, result)
    assert_true(result["completion"] == "completed", result)
    assert_true(result["saved"] == "saved", result)
    assert_true(result["result"] == "pass", result)
    assert_true(result["next_methodic_step"] == "9.6", result)


def test_partial_autotune_is_conditional():
    result = run_case(make_messages(complete=False, saved=False, partial=True))
    assert_true(result["completion"] in {"partial", "failed"}, result)
    assert_true(result["result"] in {"conditional_pass", "fail"}, result)
    assert_true(any("partial" in item["finding"].lower() or "discarded" in item["finding"].lower() for item in result["findings"]), result)


def test_missing_atun_inconclusive():
    result = run_case(make_messages(missing_atun=True))
    assert_true(result["result"] == "inconclusive", result)
    assert_true(any("ATUN" in item for item in result["missing_evidence"]), result)


def test_severe_vibration_fails():
    result = run_case(make_messages(severe_vibe=True))
    assert_true(result["result"] == "fail", result)
    assert_true(result["safety_gate"] == "do_not_proceed", result)
    assert_true(any("vibration" in item["finding"].lower() for item in result["findings"]), result)


def test_bad_solution_indicators_detected():
    result = run_case(make_messages(bad_solution=True))
    indicators = [item["indicator"] for item in result["poor_solution_indicators"]]
    assert_true("low_angle_p" in indicators, result)
    assert_true("d_at_minimum" in indicators, result)
    assert_true(result["result"] == "fail", result)


def test_methodic_step_dispatches_9_5():
    import ap_methodic_step

    assert_true(ap_methodic_step.STEP_IMPLEMENTATIONS.get("9.5") == "analyze_9_5", ap_methodic_step.STEP_IMPLEMENTATIONS)


def main():
    test_complete_autotune_saved_passes()
    test_partial_autotune_is_conditional()
    test_missing_atun_inconclusive()
    test_severe_vibration_fails()
    test_bad_solution_indicators_detected()
    test_methodic_step_dispatches_9_5()
    print("methodic AutoTune review tests passed")


if __name__ == "__main__":
    main()
