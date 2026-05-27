#!/usr/bin/env python3
from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from ap_methodic_position_controller_review import analyze_position_controller_review


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


def make_messages(*, poor_gps=False, poor_ekf=False, inner_loop_poor=False, missing_position=False):
    messages = [
        FakeMessage("PARM", TimeS=0.0, Name="PSC_POSXY_P", Value=1.0),
        FakeMessage("PARM", TimeS=0.0, Name="PSC_VELXY_P", Value=2.0),
        FakeMessage("PARM", TimeS=0.0, Name="PSC_VELXY_I", Value=1.0),
        FakeMessage("PARM", TimeS=0.0, Name="PSC_ACCXY_P", Value=0.5),
        FakeMessage("PARM", TimeS=0.0, Name="LOIT_BRK_ACCEL", Value=250),
        FakeMessage("PARM", TimeS=0.0, Name="WPNAV_SPEED", Value=500),
        FakeMessage("PARM", TimeS=0.0, Name="SERVO1_FUNCTION", Value=33),
        FakeMessage("PARM", TimeS=0.0, Name="SERVO2_FUNCTION", Value=34),
        FakeMessage("PARM", TimeS=0.0, Name="SERVO3_FUNCTION", Value=35),
        FakeMessage("PARM", TimeS=0.0, Name="SERVO4_FUNCTION", Value=36),
        FakeMessage("MODE", TimeS=0.0, Mode="LOITER"),
        FakeMessage("ARM", TimeS=1.0, ArmState=1),
    ]
    for i in range(260):
        t = i * 0.1
        pos = 1.2 * math.sin(t * 0.25)
        vel = 0.3 * math.cos(t * 0.25)
        rout = 0.28 * math.sin(t * 12.0) if inner_loop_poor else 0.035 * math.sin(t * 0.8)
        status = 2 if poor_gps and 6.0 < t < 14.0 else 3
        sp = 1.35 if poor_ekf and 6.0 < t < 16.0 else 0.22
        lat = int((51.0 + 0.000002 * math.sin(t * 0.1)) * 1e7)
        lng = int((-1.0 + 0.000002 * math.cos(t * 0.1)) * 1e7)
        messages.extend([
            FakeMessage("GPS", TimeS=t, Status=status, NSats=12, HDop=0.7 if not poor_gps else 2.8, HAcc=0.6 if not poor_gps else 3.0, VAcc=0.8, Lat=lat, Lng=lng, Alt=105.0, Spd=0.3),
            FakeMessage("GPA", TimeS=t, HAcc=0.6 if not poor_gps else 3.0, VAcc=0.8),
            FakeMessage("XKF4", TimeS=t, SP=sp, SV=0.2, SH=0.2, SM=0.2),
            FakeMessage("XKF3", TimeS=t, IVN=0.05, IVE=0.04, IPN=0.02, IPE=0.02),
            FakeMessage("ATT", TimeS=t, DesRoll=1.5 * math.sin(t * 0.3), Roll=1.4 * math.sin(t * 0.3), DesPitch=1.2 * math.cos(t * 0.3), Pitch=1.1 * math.cos(t * 0.3), DesYaw=0.0, Yaw=0.0),
            FakeMessage("RATE", TimeS=t, RDes=5.0 * math.sin(t), R=4.8 * math.sin(t), PDes=4.0 * math.cos(t), P=3.8 * math.cos(t), YDes=0.0, Y=0.0, ROut=rout, POut=0.03, YOut=0.02),
            FakeMessage("CTUN", TimeS=t, DAlt=4.0, Alt=4.0 + 0.05 * math.sin(t * 0.2), ThO=0.48, ThH=0.47),
            FakeMessage("RCIN", TimeS=t, C1=1500, C2=1500, C3=1500, C4=1500),
            FakeMessage("VIBE", TimeS=t, VibeX=7.0, VibeY=6.0, VibeZ=5.0, Clip0=0, Clip1=0, Clip2=0),
            FakeMessage("BAT", TimeS=t, Volt=22.8, Curr=17.0),
            FakeMessage("POWR", TimeS=t, Vcc=5.05, Vservo=5.1, Flags=0),
            FakeMessage("RCOU", TimeS=t, C1=1500 + 18 * math.sin(t), C2=1500 + 16 * math.sin(t * 0.9), C3=1500 + 15 * math.sin(t * 1.1), C4=1500 + 14 * math.sin(t * 0.7)),
        ])
        if not missing_position:
            messages.extend([
                FakeMessage("PSC", TimeS=t, DVelX=vel, VelX=vel + 0.05 * math.sin(t), DVelY=0.1 * vel, VelY=0.1 * vel + 0.04 * math.cos(t), DPosX=pos, PosX=pos + 0.25 * math.sin(t * 0.4), DPosY=0.5 * pos, PosY=0.5 * pos + 0.2 * math.cos(t * 0.4)),
                FakeMessage("NTUN", TimeS=t, DVelX=vel, VelX=vel + 0.05, DVelY=0.0, VelY=0.03),
            ])
    return sorted(messages, key=lambda msg: (msg.fields.get("TimeS", 0.0), msg.typ))


def run_case(messages):
    import ap_common
    import ap_methodic_position_controller_review

    original_common = ap_common.iter_dataflash_messages
    original_local = ap_methodic_position_controller_review.collect_dataflash.__globals__["_message_iter"]

    def fake_iter(_source, max_messages=None):
        yield from messages[:max_messages]

    ap_common.iter_dataflash_messages = fake_iter
    ap_methodic_position_controller_review.collect_dataflash.__globals__["_message_iter"] = fake_iter
    try:
        return analyze_position_controller_review("synthetic.BIN")
    finally:
        ap_common.iter_dataflash_messages = original_common
        ap_methodic_position_controller_review.collect_dataflash.__globals__["_message_iter"] = original_local


def test_poor_gps_ekf_blocks_position_tuning():
    result = run_case(make_messages(poor_gps=True, poor_ekf=True))
    assert_true(result["result"] == "fix_ekf_gps_first", result)
    assert_true(result["safety_gate"] == "do_not_proceed", result)


def test_inner_loop_poor_blocks_outer_loop():
    result = run_case(make_messages(inner_loop_poor=True))
    assert_true(result["result"] == "collect_better_log", result)
    assert_true(result["inner_loop_prerequisite_status"]["status"] == "poor", result)


def test_clean_position_response_passes():
    result = run_case(make_messages())
    assert_true(result["result"] == "pass", result)
    assert_true(result["position_control_quality"]["quality"] == "good", result)
    assert_true(result["inner_loop_prerequisite_status"]["status"] == "acceptable", result)


def test_missing_position_messages_is_inconclusive():
    result = run_case(make_messages(missing_position=True))
    assert_true(result["result"] == "inconclusive", result)
    assert_true(any("POS" in item or "NTUN" in item or "PSC" in item for item in result["missing_evidence"]), result)


def test_methodic_step_dispatches_12_1():
    import ap_methodic_step

    assert_true(ap_methodic_step.STEP_IMPLEMENTATIONS.get("12.1") == "analyze_12_1", ap_methodic_step.STEP_IMPLEMENTATIONS)


def main():
    test_poor_gps_ekf_blocks_position_tuning()
    test_inner_loop_poor_blocks_outer_loop()
    test_clean_position_response_passes()
    test_missing_position_messages_is_inconclusive()
    test_methodic_step_dispatches_12_1()
    print("methodic position-controller review tests passed")


if __name__ == "__main__":
    main()
