#!/usr/bin/env python3
from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from ap_methodic_711_motor_oscillation import REQUIRED_MANUAL_OBSERVATIONS, analyze_motor_oscillation_711


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


def make_messages(
    *,
    roll_pitch_oscillatory=False,
    yaw_bias=False,
    rc_contaminated=False,
    esc_absent=False,
    severe_vibe=False,
    missing_rate=False,
):
    messages = [
        FakeMessage("PARM", TimeS=0.0, Name="RCMAP_ROLL", Value=1),
        FakeMessage("PARM", TimeS=0.0, Name="RCMAP_PITCH", Value=2),
        FakeMessage("PARM", TimeS=0.0, Name="RCMAP_THROTTLE", Value=3),
        FakeMessage("PARM", TimeS=0.0, Name="RCMAP_YAW", Value=4),
        FakeMessage("PARM", TimeS=0.0, Name="RC1_TRIM", Value=1500),
        FakeMessage("PARM", TimeS=0.0, Name="RC2_TRIM", Value=1500),
        FakeMessage("PARM", TimeS=0.0, Name="RC3_TRIM", Value=1500),
        FakeMessage("PARM", TimeS=0.0, Name="RC4_TRIM", Value=1500),
        FakeMessage("PARM", TimeS=0.0, Name="SERVO1_FUNCTION", Value=33),
        FakeMessage("PARM", TimeS=0.0, Name="SERVO2_FUNCTION", Value=34),
        FakeMessage("PARM", TimeS=0.0, Name="SERVO3_FUNCTION", Value=35),
        FakeMessage("PARM", TimeS=0.0, Name="SERVO4_FUNCTION", Value=36),
        FakeMessage("MODE", TimeS=0.0, Mode="STABILIZE"),
        FakeMessage("MODE", TimeS=8.0, Mode="ALTHOLD"),
        FakeMessage("MODE", TimeS=38.0, Mode="LAND"),
        FakeMessage("ARM", TimeS=2.0, ArmState=1),
        FakeMessage("ARM", TimeS=42.0, ArmState=0),
    ]
    for i in range(220):
        t = i * 0.2
        if t < 7:
            alt = 0.0
            tho = 0.12
            pwm = 1120
        elif t < 11:
            alt = (t - 7.0) * 0.35
            tho = 0.55
            pwm = 1450
        elif t < 35:
            alt = 1.45 + 0.03 * math.sin(t)
            tho = 0.48 + 0.01 * math.sin(t / 2.0)
            pwm = 1460
        elif t < 39:
            alt = max(0.0, 1.45 - (t - 35.0) * 0.35)
            tho = 0.35
            pwm = 1320
        else:
            alt = 0.0
            tho = 0.10
            pwm = 1080
        rcin_roll = 1650 if rc_contaminated and 11 <= t <= 35 else 1500
        vibe = 75.0 if severe_vibe and 14 <= t <= 24 else 8.0
        clip = 5 if severe_vibe and t >= 18 else 0
        messages.extend([
            FakeMessage("CTUN", TimeS=t, Alt=alt, ThO=tho, ThH=0.48),
            FakeMessage("ATT", TimeS=t, Roll=1.0, Pitch=-1.0),
            FakeMessage("RCIN", TimeS=t, C1=rcin_roll, C2=1500, C3=1500, C4=1500),
            FakeMessage("RCOU", TimeS=t, C1=pwm, C2=pwm + 8, C3=pwm - 6, C4=pwm + 4),
            FakeMessage("VIBE", TimeS=t, VibeX=vibe, VibeY=vibe * 0.7, VibeZ=vibe * 0.5, Clip0=clip),
            FakeMessage("BAT", TimeS=t, Volt=15.2, Curr=12.0),
            FakeMessage("POWR", TimeS=t, Vcc=5.05),
        ])
        if not esc_absent:
            messages.append(FakeMessage("ESC", TimeS=t, Instance=0, Temp=38.0, MotTemp=40.0, Err=0, Status=0, RPM=5200, Curr=3.0))
        if not missing_rate:
            base = 0.02 * math.sin(t)
            rout = 0.24 * math.sin(2 * math.pi * 2.0 * t) if roll_pitch_oscillatory else base
            pout = 0.23 * math.sin(2 * math.pi * 2.2 * t) if roll_pitch_oscillatory else -base
            yout = 0.22 if yaw_bias else 0.015 * math.sin(t / 2.0)
            messages.append(FakeMessage("RATE", TimeS=t, RDes=0, R=0, ROut=rout, PDes=0, P=0, POut=pout, YDes=0, Y=0, YOut=yout, AOut=0.02))
            messages.append(FakeMessage("PIDR", TimeS=t, P=rout * 0.4, I=0.01, D=rout * 0.1, FF=0, DFF=0, Dmod=1, SRate=400, Flags=0))
            messages.append(FakeMessage("PIDP", TimeS=t, P=pout * 0.4, I=0.01, D=pout * 0.1, FF=0, DFF=0, Dmod=1, SRate=400, Flags=0))
            messages.append(FakeMessage("PIDY", TimeS=t, P=yout * 0.3, I=0.18 if yaw_bias else 0.01, D=0, FF=0, DFF=0, Dmod=1, SRate=400, Flags=0))
    return sorted(messages, key=lambda msg: (msg.fields.get("TimeS", 0.0), msg.typ))


def run_case(messages, observations=None):
    import ap_common
    import ap_methodic_711_motor_oscillation

    original_common = ap_common.iter_dataflash_messages
    original_local = ap_methodic_711_motor_oscillation.collect_dataflash.__globals__["_message_iter"]

    def fake_iter(_source, max_messages=None):
        yield from messages[:max_messages]

    ap_common.iter_dataflash_messages = fake_iter
    ap_methodic_711_motor_oscillation.collect_dataflash.__globals__["_message_iter"] = fake_iter
    try:
        return analyze_motor_oscillation_711("synthetic.BIN", manual_observations=observations or REQUIRED_MANUAL_OBSERVATIONS)
    finally:
        ap_common.iter_dataflash_messages = original_common
        ap_methodic_711_motor_oscillation.collect_dataflash.__globals__["_message_iter"] = original_local


def test_clean_pass():
    result = run_case(make_messages())
    assert_true(result["result"] == "pass", result)
    assert_true(result["next_methodic_step"] == "8.1", result)


def test_roll_pitch_oscillatory_fail():
    result = run_case(make_messages(roll_pitch_oscillatory=True))
    assert_true(result["result"] == "fail", result)
    assert_true(any("roll" in item["finding"].lower() or "pitch" in item["finding"].lower() for item in result["findings"]), result)


def test_yaw_steady_bias_conditional():
    result = run_case(make_messages(yaw_bias=True))
    assert_true(result["result"] == "conditional_pass", result)
    assert_true(any("yaw" in item["finding"].lower() and "steady bias" in item["finding"].lower() for item in result["findings"]), result)


def test_rc_contaminated_inconclusive():
    result = run_case(make_messages(rc_contaminated=True))
    assert_true(result["result"] in {"inconclusive", "conditional_pass"}, result)
    assert_true(any("RC input" in item["finding"] or "RC stick" in item["finding"] for item in result["findings"]), result)


def test_esc_absent_requires_manual_observation():
    result = run_case(make_messages(esc_absent=True))
    assert_true(result["result"] == "conditional_pass", result)
    assert_true(any("ESC telemetry is absent" in item["finding"] for item in result["findings"]), result)
    esc_evidence = [item for item in result["evidence_used"] if item.get("type") == "esc_telemetry"][0]["value"]
    assert_true(esc_evidence["log_can_confirm_esc_temp"] is False, result)


def test_severe_vibration_fail():
    result = run_case(make_messages(severe_vibe=True))
    assert_true(result["result"] == "fail", result)
    assert_true(result["safety_gate"] == "bench_check_required", result)


def test_missing_rate_inconclusive():
    result = run_case(make_messages(missing_rate=True))
    assert_true(result["result"] == "inconclusive", result)
    assert_true(any("RATE" in item for item in result["missing_evidence"]), result)


def main():
    test_clean_pass()
    test_roll_pitch_oscillatory_fail()
    test_yaw_steady_bias_conditional()
    test_rc_contaminated_inconclusive()
    test_esc_absent_requires_manual_observation()
    test_severe_vibration_fail()
    test_missing_rate_inconclusive()
    print("methodic 7.1.1 tests passed")


if __name__ == "__main__":
    main()
