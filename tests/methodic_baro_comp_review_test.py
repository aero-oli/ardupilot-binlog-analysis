#!/usr/bin/env python3
from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from ap_methodic_baro_comp_review import analyze_baro_comp_review


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


def make_messages(*, hover_only=False, correlated=False, missing_baro=False, severe_vibe=False):
    messages = [
        FakeMessage("PARM", TimeS=0.0, Name="BARO1_WCF_FWD", Value=0.0),
        FakeMessage("PARM", TimeS=0.0, Name="EK3_SRC1_POSZ", Value=1),
        FakeMessage("MODE", TimeS=0.0, Mode="ALTHOLD"),
        FakeMessage("ARM", TimeS=0.0, ArmState=1),
    ]
    for i in range(240):
        t = i * 0.2
        speed = 1.2 + 0.2 * math.sin(t * 0.2) if hover_only else 2.0 + 4.0 * abs(math.sin(t * 0.13))
        ctun_alt = 20.0 + 0.08 * math.sin(t * 0.1)
        dalt = 20.0
        baro_alt = 100.0 + ctun_alt + 0.03 * math.sin(t * 0.5)
        if correlated:
            baro_alt += 0.22 * speed
        gps_alt = 120.0 + ctun_alt + 0.1 * math.sin(t * 0.3)
        vibe = 55.0 if severe_vibe else 8.0
        clip = i if severe_vibe else 0
        rows = [
            FakeMessage("CTUN", TimeS=t, DAlt=dalt, Alt=ctun_alt, ThO=0.48),
            FakeMessage("GPS", TimeS=t, Alt=gps_alt, Spd=speed, NSats=18, HDop=0.7),
            FakeMessage("GPA", TimeS=t, VAcc=0.8),
            FakeMessage("XKF4", TimeS=t, SH=0.2, SV=0.2, SP=0.2, SM=0.2),
            FakeMessage("XKF3", TimeS=t, IH=0.03 * math.sin(t)),
            FakeMessage("ATT", TimeS=t, Roll=4.0 * math.sin(t * 0.2), Pitch=5.0 * math.cos(t * 0.2), Yaw=20.0),
            FakeMessage("RATE", TimeS=t, R=8.0 * math.sin(t), P=6.0 * math.cos(t), Y=2.0),
            FakeMessage("VIBE", TimeS=t, VibeX=vibe, VibeY=vibe * 0.8, VibeZ=vibe * 0.6, Clip0=clip, Clip1=0, Clip2=0),
            FakeMessage("BAT", TimeS=t, Volt=22.2, Curr=18.0),
            FakeMessage("POWR", TimeS=t, Vcc=5.05, Vservo=5.1, Flags=0),
            FakeMessage("RNGF", TimeS=t, Dist=ctun_alt, Status=1),
        ]
        if not missing_baro:
            rows.append(FakeMessage("BARO", TimeS=t, Alt=baro_alt, Press=101325.0 - baro_alt * 12.0))
        messages.extend(rows)
    return sorted(messages, key=lambda msg: (msg.fields.get("TimeS", 0.0), msg.typ))


def run_case(messages):
    import ap_common
    import ap_methodic_baro_comp_review

    original_common = ap_common.iter_dataflash_messages
    original_local = ap_methodic_baro_comp_review.collect_dataflash.__globals__["_message_iter"]

    def fake_iter(_source, max_messages=None):
        yield from messages[:max_messages]

    ap_common.iter_dataflash_messages = fake_iter
    ap_methodic_baro_comp_review.collect_dataflash.__globals__["_message_iter"] = fake_iter
    try:
        return analyze_baro_comp_review("synthetic.BIN")
    finally:
        ap_common.iter_dataflash_messages = original_common
        ap_methodic_baro_comp_review.collect_dataflash.__globals__["_message_iter"] = original_local


def test_hover_only_inconclusive():
    result = run_case(make_messages(hover_only=True))
    assert_true(result["result"] == "inconclusive", result)
    assert_true(result["test_segment_quality"]["hover_only"] is True, result)


def test_clear_baro_speed_correlation_needs_compensation():
    result = run_case(make_messages(correlated=True))
    assert_true(result["result"] == "compensation_needed", result)
    assert_true(result["baro_wind_sensitivity"]["status"] == "sensitive", result)


def test_no_baro_inconclusive():
    result = run_case(make_messages(missing_baro=True))
    assert_true(result["result"] == "inconclusive", result)
    assert_true(any("BARO" in item for item in result["missing_evidence"]), result)


def test_high_vibration_reviews_hardware_first():
    result = run_case(make_messages(correlated=True, severe_vibe=True))
    assert_true(result["safety_gate"] == "bench_check_required", result)
    assert_true(result["result"] == "repeat_flight", result)
    assert_true(any("hardware" in item.lower() or "vibration" in item.lower() for item in result["recommended_next_steps"]), result)


def test_methodic_step_dispatches_10_2():
    import ap_methodic_step

    assert_true(ap_methodic_step.STEP_IMPLEMENTATIONS.get("10.2") == "analyze_10_2", ap_methodic_step.STEP_IMPLEMENTATIONS)


def main():
    test_hover_only_inconclusive()
    test_clear_baro_speed_correlation_needs_compensation()
    test_no_baro_inconclusive()
    test_high_vibration_reviews_hardware_first()
    test_methodic_step_dispatches_10_2()
    print("methodic baro compensation review tests passed")


if __name__ == "__main__":
    main()
