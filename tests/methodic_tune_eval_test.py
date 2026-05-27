#!/usr/bin/env python3
from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from ap_methodic_tune_eval import analyze_tune_eval, compare_tune_logs


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


def make_messages(*, excitation=True, high_output=False, severe_vibe=False, axes=("roll", "pitch", "yaw"), duration=36.0):
    messages = [
        FakeMessage("PARM", TimeS=0.0, Name="ATC_RAT_RLL_P", Value=0.12),
        FakeMessage("PARM", TimeS=0.0, Name="ATC_RAT_PIT_P", Value=0.12),
        FakeMessage("PARM", TimeS=0.0, Name="ATC_RAT_YAW_P", Value=0.18),
        FakeMessage("PARM", TimeS=0.0, Name="ATC_RAT_RLL_FF", Value=0.10),
        FakeMessage("PARM", TimeS=0.0, Name="ATC_RAT_PIT_FF", Value=0.10),
        FakeMessage("PARM", TimeS=0.0, Name="ATC_ANG_RLL_P", Value=4.5),
        FakeMessage("PARM", TimeS=0.0, Name="INS_GYRO_FILTER", Value=40),
        FakeMessage("MODE", TimeS=0.0, Mode="ALTHOLD"),
        FakeMessage("ARM", TimeS=0.0, ArmState=1),
    ]
    samples = int(duration / 0.1)
    for i in range(samples):
        t = i * 0.1
        segment = int(t // 10)
        rdes = pdes = ydes = 0.0
        c1 = c2 = c4 = 1500
        if excitation:
            if segment == 0 and "roll" in axes:
                rdes = 30.0 * math.sin(t * 1.2)
                c1 = 1500 + 120 * math.sin(t * 1.2)
            elif segment == 1 and "pitch" in axes:
                pdes = 28.0 * math.sin(t * 1.15)
                c2 = 1500 + 120 * math.sin(t * 1.15)
            elif segment == 2 and "yaw" in axes:
                ydes = 18.0 * math.sin(t * 0.9)
                c4 = 1500 + 120 * math.sin(t * 0.9)
        rout = 0.035 * math.sin(t * 1.7)
        pout = 0.035 * math.sin(t * 1.5)
        yout = 0.03 * math.sin(t * 1.1)
        if high_output:
            rout = 0.28 * math.sin(t * 9.0)
            pout = 0.24 * math.sin(t * 8.0)
        vibe = 55.0 if severe_vibe else 8.0
        clip = i if severe_vibe else 0
        messages.extend([
            FakeMessage("ATT", TimeS=t, DesRoll=rdes / 10.0, Roll=rdes / 10.4, DesPitch=pdes / 10.0, Pitch=pdes / 10.4, DesYaw=ydes / 6.0, Yaw=ydes / 6.3),
            FakeMessage("RATE", TimeS=t, RDes=rdes, R=rdes * 0.92, PDes=pdes, P=pdes * 0.92, YDes=ydes, Y=ydes * 0.9, ROut=rout, POut=pout, YOut=yout),
            FakeMessage("RCIN", TimeS=t, C1=c1, C2=c2, C3=1500, C4=c4),
            FakeMessage("RCOU", TimeS=t, C1=1500 + 20 * math.sin(t), C2=1510 + 18 * math.sin(t * 0.8), C3=1495 + 16 * math.sin(t * 1.1), C4=1505 + 14 * math.sin(t * 0.9)),
            FakeMessage("VIBE", TimeS=t, VibeX=vibe, VibeY=vibe * 0.8, VibeZ=vibe * 0.7, Clip0=clip, Clip1=0, Clip2=0),
            FakeMessage("BAT", TimeS=t, Volt=15.8 - 0.05 * math.sin(t), Curr=18.0),
            FakeMessage("POWR", TimeS=t, Vcc=5.05, Vservo=5.1, Flags=0),
            FakeMessage("PIDR", TimeS=t, P=0.02, I=0.01, D=0.002, FF=0.01, Dmod=1.0, Flags=0),
            FakeMessage("PIDP", TimeS=t, P=0.02, I=0.01, D=0.002, FF=0.01, Dmod=1.0, Flags=0),
            FakeMessage("PIDY", TimeS=t, P=0.015, I=0.015, D=0.0, FF=0.01, Dmod=1.0, Flags=0),
        ])
    return sorted(messages, key=lambda msg: (msg.fields.get("TimeS", 0.0), msg.typ))


def run_case(messages, **kwargs):
    import ap_common
    import ap_methodic_tune_eval

    original_common = ap_common.iter_dataflash_messages
    original_local = ap_methodic_tune_eval.collect_dataflash.__globals__["_message_iter"]

    def fake_iter(_source, max_messages=None):
        yield from messages[:max_messages]

    ap_common.iter_dataflash_messages = fake_iter
    ap_methodic_tune_eval.collect_dataflash.__globals__["_message_iter"] = fake_iter
    try:
        return analyze_tune_eval("synthetic.BIN", **kwargs)
    finally:
        ap_common.iter_dataflash_messages = original_common
        ap_methodic_tune_eval.collect_dataflash.__globals__["_message_iter"] = original_local


def run_compare(before_messages, after_messages):
    import ap_common
    import ap_methodic_tune_eval

    original_common = ap_common.iter_dataflash_messages
    original_local = ap_methodic_tune_eval.collect_dataflash.__globals__["_message_iter"]

    def fake_iter(source, max_messages=None):
        messages = before_messages if "before" in str(source).lower() else after_messages
        yield from messages[:max_messages]

    ap_common.iter_dataflash_messages = fake_iter
    ap_methodic_tune_eval.collect_dataflash.__globals__["_message_iter"] = fake_iter
    try:
        return compare_tune_logs("before.BIN", "after.BIN")
    finally:
        ap_common.iter_dataflash_messages = original_common
        ap_methodic_tune_eval.collect_dataflash.__globals__["_message_iter"] = original_local


def test_good_tracking_passes():
    result = run_case(make_messages(), step="9.6")
    assert_true(result["result"] == "pass", result)
    assert_true(result["next_methodic_step"] == "9.7", result)
    assert_true(result["axis_results"]["roll"]["controller_output"]["p95_abs"] < 0.1, result)


def test_no_rc_excitation_inconclusive():
    result = run_case(make_messages(excitation=False), step="9.3")
    assert_true(result["result"] == "inconclusive", result)
    assert_true(any("No meaningful" in item["finding"] for item in result["findings"]), result)


def test_high_rate_output_reduces_gains_path():
    result = run_case(make_messages(high_output=True), step="9.4")
    assert_true(result["result"] == "reduce_gains", result)
    assert_true(result["safety_gate"] == "do_not_proceed", result)


def test_vibration_blocker_improves_filters_path():
    result = run_case(make_messages(severe_vibe=True), step="9.6")
    assert_true(result["result"] == "improve_filters", result)
    assert_true(result["safety_gate"] == "bench_check_required", result)


def test_before_after_non_comparable_warning():
    result = run_compare(make_messages(axes=("roll", "pitch", "yaw")), make_messages(axes=("roll",)))
    assert_true(result["result"] == "repeat_evaluation", result)
    assert_true(result["comparison"]["comparable_window_confidence"]["confidence"] == "low", result)
    assert_true(any("different axes" in reason for reason in result["comparison"]["comparable_window_confidence"]["reasons"]), result)


def test_methodic_step_dispatches_tune_eval_steps():
    import ap_methodic_step

    assert_true(ap_methodic_step.STEP_IMPLEMENTATIONS.get("9.3") == "analyze_9_3", ap_methodic_step.STEP_IMPLEMENTATIONS)
    assert_true(ap_methodic_step.STEP_IMPLEMENTATIONS.get("9.4") == "analyze_9_4", ap_methodic_step.STEP_IMPLEMENTATIONS)
    assert_true(ap_methodic_step.STEP_IMPLEMENTATIONS.get("9.6") == "analyze_9_6", ap_methodic_step.STEP_IMPLEMENTATIONS)


def main():
    test_good_tracking_passes()
    test_no_rc_excitation_inconclusive()
    test_high_rate_output_reduces_gains_path()
    test_vibration_blocker_improves_filters_path()
    test_before_after_non_comparable_warning()
    test_methodic_step_dispatches_tune_eval_steps()
    print("methodic tune evaluation tests passed")


if __name__ == "__main__":
    main()
