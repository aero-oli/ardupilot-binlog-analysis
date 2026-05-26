#!/usr/bin/env python3
from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from ap_methodic_notch_review import analyze_notch_review


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


def make_messages(*, notch_enabled=True, fft=True, severe_vibe=False, esc=False, raw_logging=False):
    messages = [
        FakeMessage("PARM", TimeS=0.0, Name="INS_HNTCH_ENABLE", Value=1 if notch_enabled else 0),
        FakeMessage("PARM", TimeS=0.0, Name="INS_HNTCH_MODE", Value=3 if esc else 1),
        FakeMessage("PARM", TimeS=0.0, Name="INS_HNTCH_FREQ", Value=90),
        FakeMessage("PARM", TimeS=0.0, Name="INS_HNTCH_BW", Value=35),
        FakeMessage("PARM", TimeS=0.0, Name="INS_HNTCH_ATT", Value=20),
        FakeMessage("PARM", TimeS=0.0, Name="INS_HNTCH_HMNCS", Value=3),
        FakeMessage("PARM", TimeS=0.0, Name="INS_HNTCH_REF", Value=0.35),
        FakeMessage("PARM", TimeS=0.0, Name="INS_HNTCH_OPTS", Value=0),
        FakeMessage("PARM", TimeS=0.0, Name="INS_HNTC2_ENABLE", Value=0),
        FakeMessage("PARM", TimeS=0.0, Name="INS_GYRO_FILTER", Value=80),
        FakeMessage("PARM", TimeS=0.0, Name="INS_RAW_LOG_OPT", Value=9 if raw_logging else 0),
        FakeMessage("PARM", TimeS=0.0, Name="INS_LOG_BAT_MASK", Value=1 if raw_logging else 0),
        FakeMessage("PARM", TimeS=0.0, Name="INS_LOG_BAT_OPT", Value=4 if raw_logging else 0),
        FakeMessage("PARM", TimeS=0.0, Name="LOG_FILE_RATEMAX", Value=0),
        FakeMessage("ARM", TimeS=1.0, ArmState=1),
    ]
    for i in range(320):
        t = i * 0.01
        vibe = 75.0 if severe_vibe and 1.0 <= t <= 2.0 else 8.0
        clip = 4 if severe_vibe and t >= 1.5 else 0
        noise = math.sin(2 * math.pi * 90 * t) + 0.25 * math.sin(2 * math.pi * 180 * t)
        messages.extend([
            FakeMessage("VIBE", TimeS=t, VibeX=vibe, VibeY=vibe * 0.7, VibeZ=vibe * 0.5, Clip0=clip),
            FakeMessage("ATT", TimeS=t, Roll=1.0, Pitch=-1.0, Yaw=45.0),
            FakeMessage("RATE", TimeS=t, ROut=0.03 * math.sin(t), POut=0.02 * math.sin(t), YOut=0.02 * math.sin(t)),
            FakeMessage("RCOU", TimeS=t, C1=1450, C2=1460, C3=1440, C4=1455),
            FakeMessage("PIDR", TimeS=t, D=0.01 * noise, Dmod=1.0, Flags=0, SRate=400),
            FakeMessage("PIDP", TimeS=t, D=0.01 * noise, Dmod=1.0, Flags=0, SRate=400),
            FakeMessage("PIDY", TimeS=t, D=0.005 * noise, Dmod=1.0, Flags=0, SRate=400),
            FakeMessage("BAT", TimeS=t, Volt=15.4, Curr=10.0),
            FakeMessage("POWR", TimeS=t, Vcc=5.1),
        ])
        if fft:
            messages.append(FakeMessage("GYR", TimeS=t, GyrX=noise, GyrY=0.5 * noise, GyrZ=0.2 * noise))
        if esc:
            messages.append(FakeMessage("ESC", TimeS=t, Instance=0, RPM=5400 + 50 * math.sin(t), Temp=42, Curr=4.0))
    return sorted(messages, key=lambda msg: (msg.fields.get("TimeS", 0.0), msg.typ))


def run_case(messages):
    import ap_common
    import ap_methodic_notch_review

    original_common = ap_common.iter_dataflash_messages
    original_local = ap_methodic_notch_review.collect_dataflash.__globals__["_message_iter"]

    def fake_iter(_source, max_messages=None):
        yield from messages[:max_messages]

    ap_common.iter_dataflash_messages = fake_iter
    ap_methodic_notch_review.collect_dataflash.__globals__["_message_iter"] = fake_iter
    try:
        return analyze_notch_review("synthetic.BIN")
    finally:
        ap_common.iter_dataflash_messages = original_common
        ap_methodic_notch_review.collect_dataflash.__globals__["_message_iter"] = original_local


def test_notch_disabled_no_fft_conditional_or_inconclusive():
    result = run_case(make_messages(notch_enabled=False, fft=False))
    assert_true(result["result"] in {"conditional_pass", "inconclusive"}, result)
    assert_true(result["filter_review_ready"] is False, result)
    assert_true(any("FFT" in item for item in result["missing_evidence"]), result)


def test_fft_available_with_peaks_ready():
    result = run_case(make_messages(notch_enabled=True, fft=True, esc=True))
    assert_true(result["filter_review_ready"] is True, result)
    assert_true(result["dominant_peaks"], result)
    assert_true(result["result"] in {"pass", "conditional_pass"}, result)


def test_severe_vibe_clipping_fail():
    result = run_case(make_messages(severe_vibe=True, fft=True))
    assert_true(result["result"] == "fail", result)
    assert_true(result["safety_gate"] == "bench_check_required", result)


def test_esc_telemetry_missing_caveat_not_crash():
    result = run_case(make_messages(esc=False, fft=True))
    assert_true(result["notch_source_recommendation"] in {"fft", "throttle"}, result)
    assert_true(any("ESC/RPM telemetry is unavailable" in item["finding"] for item in result["findings"]), result)


def test_raw_imu_logging_high_volume_warning_present():
    result = run_case(make_messages(raw_logging=True, fft=True))
    assert_true(any("High-volume" in item for item in result["confidence_limits"]), result)
    assert_true(any("high-volume" in item.lower() for item in result["what_not_to_do"]), result)


def main():
    test_notch_disabled_no_fft_conditional_or_inconclusive()
    test_fft_available_with_peaks_ready()
    test_severe_vibe_clipping_fail()
    test_esc_telemetry_missing_caveat_not_crash()
    test_raw_imu_logging_high_volume_warning_present()
    print("methodic notch review tests passed")


if __name__ == "__main__":
    main()
