#!/usr/bin/env python3
from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from ap_methodic_precision_land_review import analyze_precision_land_review


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


def make_messages(*, precision=True, target_loss=False):
    messages = [
        FakeMessage("PARM", TimeS=0.0, Name="PLND_ENABLED", Value=1 if precision else 0),
        FakeMessage("PARM", TimeS=0.0, Name="RNGFND1_TYPE", Value=24),
        FakeMessage("PARM", TimeS=0.0, Name="LAND_SPEED", Value=50),
        FakeMessage("MODE", TimeS=0.0, Mode="LOITER", ModeNum=5, Rsn=1),
        FakeMessage("MODE", TimeS=5.0, Mode="LAND", ModeNum=9, Rsn=1),
        FakeMessage("MODE", TimeS=24.0, Mode="DISARMED", ModeNum=0, Rsn=1),
    ]
    for i in range(240):
        t = i * 0.1
        alt = max(0.25, 8.0 - max(0.0, t - 5.0) * 0.35)
        valid = 0 if target_loss and 11.0 < t < 15.0 else 1
        errx = 0.12 * math.sin(t * 0.7)
        erry = 0.10 * math.cos(t * 0.7)
        lat = int((51.0 + 0.000001 * math.sin(t * 0.1)) * 1e7)
        lng = int((-1.0 + 0.000001 * math.cos(t * 0.1)) * 1e7)
        messages.extend([
            FakeMessage("GPS", TimeS=t, Status=3, NSats=14, HDop=0.7, HAcc=0.6, VAcc=0.8, Lat=lat, Lng=lng, Alt=105.0 + alt, Spd=0.3),
            FakeMessage("GPA", TimeS=t, HAcc=0.6, VAcc=0.8),
            FakeMessage("XKF4", TimeS=t, SP=0.2, SV=0.2, SH=0.2, SM=0.2),
            FakeMessage("XKF3", TimeS=t, IVN=0.04, IVE=0.04, IPN=0.02, IPE=0.02),
            FakeMessage("ATT", TimeS=t, DesRoll=0.8 * math.sin(t * 0.2), Roll=0.75 * math.sin(t * 0.2), DesPitch=0.8 * math.cos(t * 0.2), Pitch=0.75 * math.cos(t * 0.2), DesYaw=30.0, Yaw=30.1),
            FakeMessage("RATE", TimeS=t, RDes=2.0 * math.sin(t), R=1.9 * math.sin(t), PDes=2.0 * math.cos(t), P=1.9 * math.cos(t), YDes=0.0, Y=0.0, ROut=0.03, POut=0.03, YOut=0.02),
            FakeMessage("CTUN", TimeS=t, DAlt=alt, Alt=alt + 0.03 * math.sin(t), ThO=0.42, ThH=0.47),
            FakeMessage("RNGF", TimeS=t, Dist=alt, Status=1),
            FakeMessage("RCIN", TimeS=t, C1=1500, C2=1500, C3=1500, C4=1500),
            FakeMessage("VIBE", TimeS=t, VibeX=6.0, VibeY=5.0, VibeZ=5.0, Clip0=0, Clip1=0, Clip2=0),
            FakeMessage("BAT", TimeS=t, Volt=22.8, Curr=14.0),
            FakeMessage("POWR", TimeS=t, Vcc=5.05, Vservo=5.1, Flags=0),
        ])
        if precision:
            messages.append(FakeMessage("PL", TimeS=t, Heal=valid, ErrX=errx, ErrY=erry))
    return sorted(messages, key=lambda msg: (msg.fields.get("TimeS", 0.0), msg.typ))


def run_case(messages):
    import ap_common
    import ap_methodic_precision_land_review

    original_common = ap_common.iter_dataflash_messages
    original_local = ap_methodic_precision_land_review.collect_dataflash.__globals__["_message_iter"]

    def fake_iter(_source, max_messages=None):
        yield from messages[:max_messages]

    ap_common.iter_dataflash_messages = fake_iter
    ap_methodic_precision_land_review.collect_dataflash.__globals__["_message_iter"] = fake_iter
    try:
        return analyze_precision_land_review("synthetic.BIN")
    finally:
        ap_common.iter_dataflash_messages = original_common
        ap_methodic_precision_land_review.collect_dataflash.__globals__["_message_iter"] = original_local


def test_no_precision_landing_messages_not_applicable_or_inconclusive():
    result = run_case(make_messages(precision=False))
    assert_true(result["result"] in {"not_applicable", "inconclusive"}, result)
    assert_true(result["target_tracking_quality"]["quality"] == "missing", result)


def test_target_loss_needs_sensor_review():
    result = run_case(make_messages(precision=True, target_loss=True))
    assert_true(result["result"] == "needs_sensor_review", result)
    assert_true(result["safety_gate"] == "bench_check_required", result)
    assert_true(result["target_tracking_quality"]["quality"] == "lost", result)


def test_clean_descent_ready_for_further_tests():
    result = run_case(make_messages(precision=True))
    assert_true(result["result"] == "ready_for_further_precision_land_tests", result)
    assert_true(result["target_tracking_quality"]["quality"] == "good", result)
    assert_true(result["rangefinder_health"]["quality"] == "good", result)


def test_methodic_step_dispatches_12_3():
    import ap_methodic_step

    assert_true(ap_methodic_step.STEP_IMPLEMENTATIONS.get("12.3") == "analyze_12_3", ap_methodic_step.STEP_IMPLEMENTATIONS)


def main():
    test_no_precision_landing_messages_not_applicable_or_inconclusive()
    test_target_loss_needs_sensor_review()
    test_clean_descent_ready_for_further_tests()
    test_methodic_step_dispatches_12_3()
    print("methodic precision-landing review tests passed")


if __name__ == "__main__":
    main()
