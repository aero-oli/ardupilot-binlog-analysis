#!/usr/bin/env python3
from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from ap_methodic_pid_notch_review import analyze_pid_notch_review


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


def make_messages(*, resonance=False, severe_vibe=False, short=False):
    messages = [
        FakeMessage("PARM", TimeS=0.0, Name="INS_HNTCH_ENABLE", Value=1),
        FakeMessage("PARM", TimeS=0.0, Name="INS_HNTCH_MODE", Value=1),
        FakeMessage("PARM", TimeS=0.0, Name="INS_HNTCH_FREQ", Value=90),
        FakeMessage("PARM", TimeS=0.0, Name="INS_HNTCH_BW", Value=35),
        FakeMessage("PARM", TimeS=0.0, Name="INS_GYRO_FILTER", Value=80),
        FakeMessage("PARM", TimeS=0.0, Name="FILT1_TYPE", Value=1),
        FakeMessage("PARM", TimeS=0.0, Name="FILT1_NOTCH_FREQ", Value=46),
        FakeMessage("PARM", TimeS=0.0, Name="FILT1_NOTCH_Q", Value=5),
        FakeMessage("PARM", TimeS=0.0, Name="FILT1_NOTCH_ATT", Value=15),
        FakeMessage("ARM", TimeS=0.1, ArmState=1),
    ]
    count = 40 if short else 1000
    for i in range(count):
        t = i * 0.005
        base = 0.01 * math.sin(2 * math.pi * 2.0 * t)
        resonance_signal = math.sin(2 * math.pi * 45.0 * t)
        rout = base + (0.08 * resonance_signal if resonance else 0.0)
        pout = -base
        yout = 0.005 * math.sin(t)
        pidr_d = 0.006 * math.sin(2 * math.pi * 3.0 * t) + (0.09 * resonance_signal if resonance else 0.0)
        motor = 1500 + (50 * resonance_signal if resonance else 4 * math.sin(t))
        vibe = 75.0 if severe_vibe and 1.0 <= t <= 3.0 else 8.0
        clip = 5 if severe_vibe and t >= 1.5 else 0
        messages.extend([
            FakeMessage("RATE", TimeS=t, ROut=rout, POut=pout, YOut=yout, R=0.5 * rout, P=0.5 * pout, Y=0.5 * yout, RDes=0, PDes=0, YDes=0),
            FakeMessage("ATT", TimeS=t, Roll=1.0, Pitch=-1.0, Yaw=45.0),
            FakeMessage("PIDR", TimeS=t, P=0.02 * rout, I=0.01, D=pidr_d, Dmod=1.0, Flags=0, SRate=400),
            FakeMessage("PIDP", TimeS=t, P=0.02 * pout, I=0.01, D=0.003 * math.sin(t), Dmod=1.0, Flags=0, SRate=400),
            FakeMessage("PIDY", TimeS=t, P=0.02 * yout, I=0.01, D=0.002 * math.sin(t), Dmod=1.0, Flags=0, SRate=400),
            FakeMessage("RCOU", TimeS=t, C1=motor, C2=1500 - (motor - 1500), C3=motor + 3, C4=1498),
            FakeMessage("GYR", TimeS=t, GyrX=(0.4 * resonance_signal if resonance else 0.0), GyrY=0.0, GyrZ=0.0),
            FakeMessage("VIBE", TimeS=t, VibeX=vibe, VibeY=vibe * 0.7, VibeZ=vibe * 0.5, Clip0=clip),
        ])
    return sorted(messages, key=lambda msg: (msg.fields.get("TimeS", 0.0), msg.typ))


def run_case(messages):
    import ap_common
    import ap_methodic_pid_notch_review

    original_common = ap_common.iter_dataflash_messages
    original_local = ap_methodic_pid_notch_review.collect_dataflash.__globals__["_message_iter"]

    def fake_iter(_source, max_messages=None):
        yield from messages[:max_messages]

    ap_common.iter_dataflash_messages = fake_iter
    ap_methodic_pid_notch_review.collect_dataflash.__globals__["_message_iter"] = fake_iter
    try:
        return analyze_pid_notch_review("synthetic.BIN")
    finally:
        ap_common.iter_dataflash_messages = original_common
        ap_methodic_pid_notch_review.collect_dataflash.__globals__["_message_iter"] = original_local


def test_no_resonance_not_needed():
    result = run_case(make_messages())
    assert_true(result["result"] == "not_needed", result)
    assert_true(result["affected_axis"] is None, result)


def test_strong_frequency_peak_candidate():
    result = run_case(make_messages(resonance=True))
    assert_true(result["result"] == "candidate", result)
    assert_true(result["affected_axis"] == "roll", result)
    assert_true(40.0 <= result["resonance_frequency_hz"] <= 50.0, result)


def test_severe_vibration_unsafe():
    result = run_case(make_messages(severe_vibe=True))
    assert_true(result["result"] == "unsafe_to_attempt", result)
    assert_true(result["safety_gate"] == "bench_check_required", result)


def test_missing_frequency_data_inconclusive():
    result = run_case(make_messages(short=True))
    assert_true(result["result"] == "inconclusive", result)
    assert_true(result["evidence_quality"] == "low", result)


def main():
    test_no_resonance_not_needed()
    test_strong_frequency_peak_candidate()
    test_severe_vibration_unsafe()
    test_missing_frequency_data_inconclusive()
    print("methodic PID notch review tests passed")


if __name__ == "__main__":
    main()
