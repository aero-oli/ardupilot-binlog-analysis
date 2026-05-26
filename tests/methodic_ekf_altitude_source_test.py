#!/usr/bin/env python3
from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from ap_methodic_ekf_altitude_source import analyze_ekf_altitude_source


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


def make_messages(*, missing_core=False, high_ratio=False, range_dropout=False, severe_vibe=False):
    messages = [
        FakeMessage("PARM", TimeS=0.0, Name="EK3_SRC1_POSZ", Value=1),
        FakeMessage("PARM", TimeS=0.0, Name="EK3_OGN_HGT_MASK", Value=0),
        FakeMessage("PARM", TimeS=0.0, Name="EK3_PRIMARY", Value=0),
        FakeMessage("PARM", TimeS=0.0, Name="RNGFND1_TYPE", Value=1),
        FakeMessage("PARM", TimeS=0.0, Name="RNGFND1_ORIENT", Value=25),
        FakeMessage("PARM", TimeS=0.0, Name="RNGFND1_MIN_CM", Value=20),
        FakeMessage("PARM", TimeS=0.0, Name="RNGFND1_MAX_CM", Value=700),
        FakeMessage("PARM", TimeS=0.0, Name="PSC_POSZ_P", Value=1.0),
        FakeMessage("PARM", TimeS=0.0, Name="PSC_VELZ_P", Value=5.0),
        FakeMessage("PARM", TimeS=0.0, Name="PSC_VELZ_I", Value=1.0),
        FakeMessage("PARM", TimeS=0.0, Name="PSC_ACCZ_P", Value=0.5),
        FakeMessage("PARM", TimeS=0.0, Name="PSC_ACCZ_I", Value=1.0),
        FakeMessage("PARM", TimeS=0.0, Name="PSC_ACCZ_D", Value=0.0),
        FakeMessage("MODE", TimeS=0.0, Mode="ALTHOLD"),
        FakeMessage("ARM", TimeS=1.0, ArmState=1),
    ]
    for i in range(160):
        t = i * 0.2
        alt = 1.5 + 0.05 * math.sin(t)
        dalt = 1.5
        baro_alt = 100.0 + alt + 0.02 * math.sin(t * 0.7)
        gps_alt = 120.0 + alt + 0.4 * math.sin(t * 0.2)
        sh = 1.25 if high_ratio and 8.0 <= t <= 18.0 else 0.25
        rng = 0.0 if range_dropout and i % 5 == 0 else alt
        vibe = 75.0 if severe_vibe and 6.0 <= t <= 14.0 else 8.0
        clip = 4 if severe_vibe and t >= 8.0 else 0
        common = [
            FakeMessage("GPS", TimeS=t, Alt=gps_alt, Status=3, NSats=12, HDop=0.8, VAcc=0.8),
            FakeMessage("GPA", TimeS=t, VAcc=0.8),
            FakeMessage("XKF4", TimeS=t, SH=sh, SV=0.2, SP=0.2, SM=0.2),
            FakeMessage("XKF3", TimeS=t, IH=0.05 * math.sin(t)),
            FakeMessage("VIBE", TimeS=t, VibeX=vibe, VibeY=vibe * 0.7, VibeZ=vibe * 0.5, Clip0=clip),
            FakeMessage("BAT", TimeS=t, Volt=15.2, Curr=10.0),
            FakeMessage("POWR", TimeS=t, Vcc=5.05),
            FakeMessage("RNGF", TimeS=t, Dist=rng, Status=0 if rng <= 0 else 1),
            FakeMessage("ATT", TimeS=t, Roll=1.0, Pitch=1.0),
            FakeMessage("RATE", TimeS=t, ROut=0.01, POut=0.01),
        ]
        if not missing_core:
            common.extend([
                FakeMessage("CTUN", TimeS=t, Alt=alt, DAlt=dalt, ThO=0.48),
                FakeMessage("BARO", TimeS=t, Alt=baro_alt, Press=101325.0 - baro_alt * 12.0),
            ])
        messages.extend(common)
    return sorted(messages, key=lambda msg: (msg.fields.get("TimeS", 0.0), msg.typ))


def run_case(messages):
    import ap_common
    import ap_methodic_ekf_altitude_source

    original_common = ap_common.iter_dataflash_messages
    original_local = ap_methodic_ekf_altitude_source.collect_dataflash.__globals__["_message_iter"]

    def fake_iter(_source, max_messages=None):
        yield from messages[:max_messages]

    ap_common.iter_dataflash_messages = fake_iter
    ap_methodic_ekf_altitude_source.collect_dataflash.__globals__["_message_iter"] = fake_iter
    try:
        return analyze_ekf_altitude_source("synthetic.BIN")
    finally:
        ap_common.iter_dataflash_messages = original_common
        ap_methodic_ekf_altitude_source.collect_dataflash.__globals__["_message_iter"] = original_local


def test_good_baro_height_pass():
    result = run_case(make_messages())
    assert_true(result["result"] == "pass", result)
    assert_true(result["height_source_assessment"]["baro"] == "usable", result)


def test_high_ekf_height_ratio_review_or_fail():
    result = run_case(make_messages(high_ratio=True))
    assert_true(result["result"] in {"review_required", "fail"}, result)
    assert_true(any("height test ratio" in item["finding"].lower() for item in result["findings"]), result)


def test_rangefinder_dropout_suspect():
    result = run_case(make_messages(range_dropout=True))
    assert_true(result["height_source_assessment"]["rangefinder"] == "suspect", result)
    assert_true(result["result"] == "review_required", result)


def test_severe_vibration_blocks_conclusion():
    result = run_case(make_messages(severe_vibe=True))
    assert_true(result["result"] == "fail", result)
    assert_true(result["safety_gate"] == "bench_check_required", result)


def test_missing_ctun_baro_inconclusive():
    result = run_case(make_messages(missing_core=True))
    assert_true(result["result"] == "inconclusive", result)
    assert_true(any("CTUN" in item or "BARO" in item for item in result["missing_evidence"]), result)


def main():
    test_good_baro_height_pass()
    test_high_ekf_height_ratio_review_or_fail()
    test_rangefinder_dropout_suspect()
    test_severe_vibration_blocks_conclusion()
    test_missing_ctun_baro_inconclusive()
    print("methodic EKF altitude-source tests passed")


if __name__ == "__main__":
    main()
