#!/usr/bin/env python3
from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from ap_methodic_guided_operation_review import analyze_guided_operation_review


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


def make_messages(*, guided=True, ekf_failsafe=False):
    messages = [
        FakeMessage("PARM", TimeS=0.0, Name="GUID_OPTIONS", Value=0),
        FakeMessage("PARM", TimeS=0.0, Name="FS_GCS_ENABLE", Value=1),
        FakeMessage("PARM", TimeS=0.0, Name="FENCE_ENABLE", Value=1),
        FakeMessage("MODE", TimeS=0.0, Mode="LOITER", ModeNum=5, Rsn=1),
    ]
    if guided:
        messages.append(FakeMessage("MODE", TimeS=5.0, Mode="GUIDED", ModeNum=4, Rsn=2))
    if ekf_failsafe:
        messages.append(FakeMessage("MODE", TimeS=12.0, Mode="LAND", ModeNum=9, Rsn=6))
        messages.append(FakeMessage("ERR", TimeS=12.0, Subsys="EKF", ECode=1))
        messages.append(FakeMessage("MSG", TimeS=12.0, Message="EKF failsafe"))
    messages.append(FakeMessage("MODE", TimeS=24.0, Mode="LOITER", ModeNum=5, Rsn=1))
    for i in range(260):
        t = i * 0.1
        pos = 1.0 * math.sin(t * 0.25)
        vel = 0.25 * math.cos(t * 0.25)
        lat = int((51.0 + 0.000002 * math.sin(t * 0.1)) * 1e7)
        lng = int((-1.0 + 0.000002 * math.cos(t * 0.1)) * 1e7)
        sp = 1.35 if ekf_failsafe and 10.0 < t < 14.0 else 0.2
        messages.extend([
            FakeMessage("GPS", TimeS=t, Status=3, NSats=14, HDop=0.7, HAcc=0.6, VAcc=0.8, Lat=lat, Lng=lng, Alt=105.0, Spd=0.4),
            FakeMessage("GPA", TimeS=t, HAcc=0.6, VAcc=0.8),
            FakeMessage("XKF4", TimeS=t, SP=sp, SV=0.2, SH=0.2, SM=0.2),
            FakeMessage("XKF3", TimeS=t, IVN=0.04, IVE=0.04, IPN=0.02, IPE=0.02),
            FakeMessage("ATT", TimeS=t, DesRoll=1.0 * math.sin(t * 0.3), Roll=0.95 * math.sin(t * 0.3), DesPitch=1.0 * math.cos(t * 0.3), Pitch=0.95 * math.cos(t * 0.3), DesYaw=30.0, Yaw=30.2),
            FakeMessage("RATE", TimeS=t, RDes=3.0 * math.sin(t), R=2.9 * math.sin(t), PDes=3.0 * math.cos(t), P=2.9 * math.cos(t), YDes=0.0, Y=0.0, ROut=0.03, POut=0.03, YOut=0.02),
            FakeMessage("CTUN", TimeS=t, DAlt=5.0, Alt=5.0 + 0.05 * math.sin(t * 0.2), ThO=0.48, ThH=0.47),
            FakeMessage("RCIN", TimeS=t, C1=1500, C2=1500, C3=1500, C4=1500),
            FakeMessage("VIBE", TimeS=t, VibeX=6.0, VibeY=5.0, VibeZ=5.0, Clip0=0, Clip1=0, Clip2=0),
            FakeMessage("BAT", TimeS=t, Volt=22.8, Curr=16.0),
            FakeMessage("POWR", TimeS=t, Vcc=5.05, Vservo=5.1, Flags=0),
            FakeMessage("PSC", TimeS=t, DVelX=vel, VelX=vel + 0.03 * math.sin(t), DVelY=0.1 * vel, VelY=0.1 * vel + 0.03 * math.cos(t), DPosX=pos, PosX=pos + 0.15 * math.sin(t * 0.4), DPosY=0.5 * pos, PosY=0.5 * pos + 0.1 * math.cos(t * 0.4)),
            FakeMessage("CMD", TimeS=t, CNum=16, Frame=3),
        ])
    return sorted(messages, key=lambda msg: (msg.fields.get("TimeS", 0.0), msg.typ))


def run_case(messages):
    import ap_common
    import ap_methodic_guided_operation_review

    original_common = ap_common.iter_dataflash_messages
    original_local = ap_methodic_guided_operation_review.collect_dataflash.__globals__["_message_iter"]

    def fake_iter(_source, max_messages=None):
        yield from messages[:max_messages]

    ap_common.iter_dataflash_messages = fake_iter
    ap_methodic_guided_operation_review.collect_dataflash.__globals__["_message_iter"] = fake_iter
    try:
        return analyze_guided_operation_review("synthetic.BIN")
    finally:
        ap_common.iter_dataflash_messages = original_common
        ap_methodic_guided_operation_review.collect_dataflash.__globals__["_message_iter"] = original_local


def test_no_guided_not_applicable():
    result = run_case(make_messages(guided=False))
    assert_true(result["result"] == "not_applicable", result)
    assert_true(result["guided_segments"]["present"] is False, result)


def test_guided_with_ekf_failsafe_not_ready():
    result = run_case(make_messages(guided=True, ekf_failsafe=True))
    assert_true(result["result"] == "not_ready", result)
    assert_true(result["safety_gate"] == "do_not_proceed", result)
    assert_true(result["failsafe_context"]["issues_detected"] is True, result)


def test_clean_guided_segment_ready_for_checks():
    result = run_case(make_messages(guided=True))
    assert_true(result["result"] == "ready_for_guided_checks", result)
    assert_true(result["safety_gate"] == "proceed_with_caution", result)
    assert_true(result["tracking_quality"]["quality"] == "good", result)


def test_methodic_step_dispatches_12_2():
    import ap_methodic_step

    assert_true(ap_methodic_step.STEP_IMPLEMENTATIONS.get("12.2") == "analyze_12_2", ap_methodic_step.STEP_IMPLEMENTATIONS)


def main():
    test_no_guided_not_applicable()
    test_guided_with_ekf_failsafe_not_ready()
    test_clean_guided_segment_ready_for_checks()
    test_methodic_step_dispatches_12_2()
    print("methodic guided-operation review tests passed")


if __name__ == "__main__":
    main()
