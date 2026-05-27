#!/usr/bin/env python3
from __future__ import annotations

import math
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from ap_methodic_quicktune_review import analyze_quicktune_review


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


def make_messages(*, missing_pid=False, post_oscillation=False, no_msgs=False):
    messages = [
        FakeMessage("PARM", TimeS=0.0, Name="ATC_RAT_RLL_P", Value=0.10),
        FakeMessage("PARM", TimeS=0.0, Name="ATC_RAT_PIT_P", Value=0.10),
        FakeMessage("PARM", TimeS=0.0, Name="ATC_RAT_YAW_P", Value=0.18),
        FakeMessage("PARM", TimeS=0.0, Name="ATC_ANG_RLL_P", Value=4.5),
        FakeMessage("PARM", TimeS=0.0, Name="ATC_ANG_PIT_P", Value=4.5),
        FakeMessage("PARM", TimeS=0.0, Name="ATC_ANG_YAW_P", Value=4.5),
        FakeMessage("PARM", TimeS=0.0, Name="ATC_ACCEL_R_MAX", Value=72000),
        FakeMessage("PARM", TimeS=0.0, Name="ATC_ACCEL_P_MAX", Value=72000),
        FakeMessage("PARM", TimeS=0.0, Name="ATC_ACCEL_Y_MAX", Value=27000),
        FakeMessage("PARM", TimeS=0.0, Name="INS_GYRO_FILTER", Value=40),
        FakeMessage("MODE", TimeS=0.0, Mode="ALTHOLD"),
        FakeMessage("ARM", TimeS=0.0, ArmState=1),
    ]
    if not no_msgs:
        messages.extend([
            FakeMessage("MSG", TimeS=4.0, Message="QuikTune started"),
            FakeMessage("MSG", TimeS=20.0, Message="QuikTune complete gains saved"),
        ])
    messages.extend([
        FakeMessage("PARM", TimeS=20.0, Name="ATC_RAT_RLL_P", Value=0.12),
        FakeMessage("PARM", TimeS=20.0, Name="ATC_RAT_PIT_P", Value=0.12),
    ])

    for i in range(240):
        t = i * 0.2
        post = t >= 20.0
        rdes = 20.0 * math.sin(t * 0.55)
        pdes = 18.0 * math.sin(t * 0.45)
        ydes = 10.0 * math.sin(t * 0.3)
        err_scale = 0.12 if post else 0.45
        rout = 0.03 * math.sin(t * 1.3)
        pout = 0.03 * math.sin(t * 1.1)
        yout = 0.02 * math.sin(t * 0.9)
        if post_oscillation and post:
            rout = 0.28 * math.sin(t * 9.0)
            pout = 0.24 * math.sin(t * 8.0)
        messages.extend([
            FakeMessage("ATT", TimeS=t, Roll=2.0 * math.sin(t * 0.2), Pitch=1.5 * math.sin(t * 0.25), DesRoll=0.0, DesPitch=0.0),
            FakeMessage("RATE", TimeS=t, RDes=rdes, R=rdes * (1.0 - err_scale), PDes=pdes, P=pdes * (1.0 - err_scale), YDes=ydes, Y=ydes * (1.0 - err_scale), ROut=rout, POut=pout, YOut=yout),
            FakeMessage("RCOU", TimeS=t, C1=1500 + 30 * math.sin(t), C2=1505 + 25 * math.sin(t * 0.8), C3=1495 + 20 * math.sin(t * 1.1), C4=1502 + 22 * math.sin(t * 0.9)),
            FakeMessage("VIBE", TimeS=t, VibeX=8.0, VibeY=7.0, VibeZ=6.0, Clip0=0, Clip1=0, Clip2=0),
            FakeMessage("BAT", TimeS=t, Volt=15.2, Curr=14.0),
            FakeMessage("RCIN", TimeS=t, C1=1500, C2=1500, C3=1500, C4=1500),
        ])
        if not missing_pid:
            messages.extend([
                FakeMessage("PIDR", TimeS=t, P=0.02, I=0.01, D=0.002, FF=0.0, Dmod=1.0, Flags=0),
                FakeMessage("PIDP", TimeS=t, P=0.02, I=0.01, D=0.002, FF=0.0, Dmod=1.0, Flags=0),
                FakeMessage("PIDY", TimeS=t, P=0.015, I=0.02, D=0.0, FF=0.0, Dmod=1.0, Flags=0),
            ])
    return sorted(messages, key=lambda msg: (msg.fields.get("TimeS", 0.0), msg.typ))


def run_case(messages, **kwargs):
    import ap_common
    import ap_methodic_quicktune_review

    original_common = ap_common.iter_dataflash_messages
    original_local = ap_methodic_quicktune_review.collect_dataflash.__globals__["_message_iter"]

    def fake_iter(_source, max_messages=None):
        yield from messages[:max_messages]

    ap_common.iter_dataflash_messages = fake_iter
    ap_methodic_quicktune_review.collect_dataflash.__globals__["_message_iter"] = fake_iter
    try:
        return analyze_quicktune_review("synthetic.BIN", **kwargs)
    finally:
        ap_common.iter_dataflash_messages = original_common
        ap_methodic_quicktune_review.collect_dataflash.__globals__["_message_iter"] = original_local


def test_completed_tune_with_improved_tracking_passes():
    result = run_case(make_messages())
    assert_true(result["result"] == "pass", result)
    assert_true(result["quicktune_detected"] is True, result)
    assert_true(result["completion_status"] == "completed", result)


def test_missing_pid_evidence_limits_confidence():
    result = run_case(make_messages(missing_pid=True))
    assert_true(result["result"] in {"conditional_pass", "inconclusive"}, result)
    assert_true(any("PID" in item for item in result["missing_evidence"]), result)


def test_post_tune_oscillation_fails():
    result = run_case(make_messages(post_oscillation=True))
    assert_true(result["result"] == "fail", result)
    assert_true(any("oscillation" in item["finding"].lower() for item in result["findings"]), result)


def test_external_before_after_params_enable_manual_review_mode():
    with tempfile.TemporaryDirectory() as tmp:
        before = Path(tmp) / "before.param"
        after = Path(tmp) / "after.param"
        before.write_text("ATC_RAT_RLL_P 0.10\nATC_RAT_PIT_P 0.10\n", encoding="utf-8")
        after.write_text("ATC_RAT_RLL_P 0.12\nATC_RAT_PIT_P 0.12\n", encoding="utf-8")
        result = run_case(make_messages(no_msgs=True), before_params=before, after_params=after)
    assert_true(result["quicktune_detected"] is True, result)
    assert_true(any(change["source"] == "external_param_files" for change in result["parameters_changed"]["changes"]), result)
    assert_true(result["result"] in {"pass", "conditional_pass"}, result)


def test_methodic_step_dispatches_8_5_and_9_2():
    import ap_methodic_step

    assert_true(ap_methodic_step.STEP_IMPLEMENTATIONS.get("8.5") == "analyze_8_5", ap_methodic_step.STEP_IMPLEMENTATIONS)
    assert_true(ap_methodic_step.STEP_IMPLEMENTATIONS.get("9.2") == "analyze_9_2", ap_methodic_step.STEP_IMPLEMENTATIONS)
    result = run_case(make_messages(), methodic_step="9.2")
    assert_true(result["methodic_step"] == "9.2", result)
    assert_true(result["next_step"] == "9.3", result)


def main():
    test_completed_tune_with_improved_tracking_passes()
    test_missing_pid_evidence_limits_confidence()
    test_post_tune_oscillation_fails()
    test_external_before_after_params_enable_manual_review_mode()
    test_methodic_step_dispatches_8_5_and_9_2()
    print("methodic QuikTune/manual PID review tests passed")


if __name__ == "__main__":
    main()
