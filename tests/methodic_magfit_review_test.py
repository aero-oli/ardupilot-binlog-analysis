#!/usr/bin/env python3
from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from ap_methodic_magfit_review import analyze_magfit_review


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


def make_messages(*, missing_mag=False, current_correlation=False, high_ekf_ratio=False):
    messages = [
        FakeMessage("PARM", TimeS=0.0, Name="COMPASS_USE", Value=1),
        FakeMessage("PARM", TimeS=0.0, Name="COMPASS_USE2", Value=1),
        FakeMessage("PARM", TimeS=0.0, Name="COMPASS_USE3", Value=0),
        FakeMessage("PARM", TimeS=0.0, Name="COMPASS_ORIENT", Value=0),
        FakeMessage("PARM", TimeS=0.0, Name="COMPASS_OFS_X", Value=10),
        FakeMessage("PARM", TimeS=0.0, Name="COMPASS_OFS_Y", Value=-5),
        FakeMessage("PARM", TimeS=0.0, Name="COMPASS_OFS_Z", Value=20),
        FakeMessage("PARM", TimeS=0.0, Name="COMPASS_MOT_X", Value=0),
        FakeMessage("PARM", TimeS=0.0, Name="EK3_SRC1_YAW", Value=1),
        FakeMessage("PARM", TimeS=0.0, Name="GPS_TYPE", Value=1),
        FakeMessage("PARM", TimeS=0.0, Name="GPS_TYPE2", Value=0),
        FakeMessage("PARM", TimeS=0.0, Name="GPS_AUTO_SWITCH", Value=1),
        FakeMessage("PARM", TimeS=0.0, Name="LOG_BITMASK", Value=131071),
        FakeMessage("MODE", TimeS=0.0, Mode="AUTO", ModeNum=10),
        FakeMessage("MSG", TimeS=2.0, Message="MagFit mission started"),
        FakeMessage("EV", TimeS=4.0, Id=1),
    ]
    for i in range(360):
        t = i * 0.2
        yaw = (t * 8.0) % 360.0
        current = 12.0 + 8.0 * math.sin(t * 0.25)
        strength = 500.0 + (current * 12.0 if current_correlation else 4.0 * math.sin(t * 0.7))
        rad = math.radians(yaw)
        sm = 1.25 if high_ekf_ratio and 25.0 <= t <= 45.0 else 0.25
        if not missing_mag:
            messages.append(FakeMessage("MAG", TimeS=t, I=0, MagX=strength * math.cos(rad), MagY=strength * math.sin(rad), MagZ=100.0, OfsX=10, OfsY=-5, OfsZ=20))
        messages.extend([
            FakeMessage("ATT", TimeS=t, Roll=8.0 * math.sin(t * 0.4), Pitch=6.0 * math.sin(t * 0.5), Yaw=yaw),
            FakeMessage("RATE", TimeS=t, Y=35.0 * math.sin(t * 0.8), YDes=35.0 * math.sin(t * 0.8), R=12.0 * math.sin(t), P=10.0 * math.sin(t * 1.1)),
            FakeMessage("XKF3", TimeS=t, IMX=0.02, IMY=0.01, IMZ=0.03),
            FakeMessage("XKF4", TimeS=t, SM=sm, SH=0.2, SV=0.2, SP=0.2),
            FakeMessage("GPS", TimeS=t, Status=3, NSats=14, HDop=0.7, Alt=100.0),
            FakeMessage("GPA", TimeS=t, VAcc=0.8),
            FakeMessage("BAT", TimeS=t, Volt=15.2, Curr=current),
            FakeMessage("POWR", TimeS=t, Vcc=5.05),
            FakeMessage("RCIN", TimeS=t, C1=1500, C2=1500, C3=1500, C4=1500),
            FakeMessage("RCOU", TimeS=t, C1=1500 + current * 4, C2=1500 + current * 4, C3=1500 + current * 4, C4=1500 + current * 4),
        ])
    return sorted(messages, key=lambda msg: (msg.fields.get("TimeS", 0.0), msg.typ))


def run_case(messages):
    import ap_common
    import ap_methodic_magfit_review

    original_common = ap_common.iter_dataflash_messages
    original_local = ap_methodic_magfit_review.collect_dataflash.__globals__["_message_iter"]

    def fake_iter(_source, max_messages=None):
        yield from messages[:max_messages]

    ap_common.iter_dataflash_messages = fake_iter
    ap_methodic_magfit_review.collect_dataflash.__globals__["_message_iter"] = fake_iter
    try:
        return analyze_magfit_review("synthetic.BIN")
    finally:
        ap_common.iter_dataflash_messages = original_common
        ap_methodic_magfit_review.collect_dataflash.__globals__["_message_iter"] = original_local


def test_good_yaw_diversity_clean_ekf_ready():
    result = run_case(make_messages())
    assert_true(result["result"] == "ready_for_magfit", result)
    assert_true(result["magfit_evidence_quality"] == "good", result)
    assert_true(result["ekf_yaw_mag_health"]["status"] == "healthy", result)


def test_no_mag_inconclusive():
    result = run_case(make_messages(missing_mag=True))
    assert_true(result["result"] == "inconclusive", result)
    assert_true(any("MAG" in item for item in result["missing_evidence"]), result)


def test_magnetic_correlation_with_current_blocks_or_repeats():
    result = run_case(make_messages(current_correlation=True))
    assert_true(result["result"] in {"fix_hardware_first", "repeat_flight"}, result)
    assert_true(result["magnetic_interference"]["assessment"] in {"suspect", "likely"}, result)


def test_high_ekf_mag_test_ratio_blocks():
    result = run_case(make_messages(high_ekf_ratio=True))
    assert_true(result["result"] == "fix_hardware_first", result)
    assert_true(result["ekf_yaw_mag_health"]["status"] == "fail", result)


def test_dispatcher_maps_9_1():
    import ap_methodic_step

    assert_true(ap_methodic_step.STEP_IMPLEMENTATIONS.get("9.1") == "analyze_9_1", ap_methodic_step.STEP_IMPLEMENTATIONS)


def main():
    test_good_yaw_diversity_clean_ekf_ready()
    test_no_mag_inconclusive()
    test_magnetic_correlation_with_current_blocks_or_repeats()
    test_high_ekf_mag_test_ratio_blocks()
    test_dispatcher_maps_9_1()
    print("methodic MagFit review tests passed")


if __name__ == "__main__":
    main()
