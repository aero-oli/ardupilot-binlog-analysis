#!/usr/bin/env python3
from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from ap_methodic_sysid_review import analyze_sysid_review


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


def make_messages(*, no_sid=False, saturation=False, poor_frequency=False):
    messages = [
        FakeMessage("PARM", TimeS=0.0, Name="SID_AXIS", Value=7),
        FakeMessage("PARM", TimeS=0.0, Name="SID_MAGNITUDE", Value=35),
        FakeMessage("PARM", TimeS=0.0, Name="SID_F_START_HZ", Value=0.5),
        FakeMessage("PARM", TimeS=0.0, Name="SID_F_STOP_HZ", Value=12.0),
        FakeMessage("PARM", TimeS=0.0, Name="SID_T_REC", Value=24.0),
        FakeMessage("PARM", TimeS=0.0, Name="ATC_RATE_FF_ENAB", Value=1),
        FakeMessage("MODE", TimeS=0.0, Mode="SYSID"),
        FakeMessage("SIDS", TimeS=0.0, Ax=7, Mag=35, FSt=0.5, FSp=12.0, TR=24.0, TFin=2.0, TFout=2.0),
    ]
    dt = 0.02
    for i in range(1400):
        t = i * dt
        freq = 0.8 if poor_frequency else 0.5 + 10.0 * (i / 1399)
        phase = 2.0 * math.pi * (0.5 * t + 0.18 * t * t)
        target = 0.42 * math.sin(phase)
        rate = 35.0 * math.sin(phase - 0.25)
        rdes = 38.0 * math.sin(phase)
        rout = 0.12 * math.sin(phase + 0.1)
        if not no_sid:
            messages.append(FakeMessage("SID", TimeS=t, Time=t, Targ=target, F=freq, Gx=rate, Gy=0.1, Gz=0.1, Ax=0.0, Ay=0.0, Az=-9.8))
            messages.append(FakeMessage("SIDD", TimeS=t, Time=t, Targ=target, F=freq, Gx=rate, Gy=0.1, Gz=0.1, Ax=0.0, Ay=0.0, Az=-9.8))
        high = 1925 if saturation else 1540
        messages.extend([
            FakeMessage("RATE", TimeS=t, RDes=rdes, R=rate, PDes=0.0, P=0.0, YDes=0.0, Y=0.0, ROut=rout, POut=0.0, YOut=0.0, AOut=0.0),
            FakeMessage("ATT", TimeS=t, DesRoll=rdes / 10.0, Roll=rate / 10.0, DesPitch=0.0, Pitch=0.0, DesYaw=0.0, Yaw=0.0),
            FakeMessage("RCOU", TimeS=t, C1=high, C2=1500 + 20 * math.sin(t), C3=1500 + 18 * math.sin(t * 1.1), C4=1500 + 16 * math.sin(t * 0.9)),
            FakeMessage("VIBE", TimeS=t, VibeX=8.0, VibeY=7.0, VibeZ=6.0, Clip0=0, Clip1=0, Clip2=0),
            FakeMessage("BAT", TimeS=t, Volt=23.5, Curr=22.0),
            FakeMessage("POWR", TimeS=t, Vcc=5.05, Vservo=5.1, Flags=0),
        ])
    return sorted(messages, key=lambda msg: (msg.fields.get("TimeS", 0.0), msg.typ))


def run_case(messages, **kwargs):
    import ap_common
    import ap_methodic_sysid_review

    original_common = ap_common.iter_dataflash_messages
    original_local = ap_methodic_sysid_review.collect_dataflash.__globals__["_message_iter"]

    def fake_iter(_source, max_messages=None):
        yield from messages[:max_messages]

    ap_common.iter_dataflash_messages = fake_iter
    ap_methodic_sysid_review.collect_dataflash.__globals__["_message_iter"] = fake_iter
    try:
        return analyze_sysid_review("synthetic.BIN", **kwargs)
    finally:
        ap_common.iter_dataflash_messages = original_common
        ap_methodic_sysid_review.collect_dataflash.__globals__["_message_iter"] = original_local


def test_no_sid_is_inconclusive():
    result = run_case(make_messages(no_sid=True))
    assert_true(result["result"] == "inconclusive", result)
    assert_true(any("SID or SIDD" in item for item in result["missing_evidence"]), result)


def test_clean_synthetic_excitation_is_ready_for_model():
    result = run_case(make_messages())
    assert_true(result["result"] == "ready_for_model", result)
    assert_true(result["frequency_response_ready"] is True, result)
    assert_true(result["axis"]["axis"] == "roll", result)
    assert_true(result["sysid_data_quality"] == "good", result)


def test_saturation_is_do_not_use():
    result = run_case(make_messages(saturation=True))
    assert_true(result["result"] == "do_not_use", result)
    assert_true(result["safety_gate"] == "do_not_proceed", result)
    assert_true(result["saturation"]["present"] is True, result)


def test_poor_frequency_content_repeats_sysid():
    result = run_case(make_messages(poor_frequency=True))
    assert_true(result["result"] == "repeat_sysid", result)
    assert_true(result["sysid_data_quality"] in {"marginal", "poor"}, result)
    assert_true(any("Frequency sweep" in reason for reason in result["excitation_quality"]["reasons"]), result)


def test_methodic_step_dispatches_11_1():
    import ap_methodic_step

    assert_true(ap_methodic_step.STEP_IMPLEMENTATIONS.get("11.1") == "analyze_11_1", ap_methodic_step.STEP_IMPLEMENTATIONS)


def main():
    test_no_sid_is_inconclusive()
    test_clean_synthetic_excitation_is_ready_for_model()
    test_saturation_is_do_not_use()
    test_poor_frequency_content_repeats_sysid()
    test_methodic_step_dispatches_11_1()
    print("methodic System ID review tests passed")


if __name__ == "__main__":
    main()
