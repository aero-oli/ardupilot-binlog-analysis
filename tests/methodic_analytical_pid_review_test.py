#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from ap_methodic_analytical_pid_review import analyze_analytical_pid_review


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


def good_sysid():
    return {
        "methodic_step": "11.1",
        "result": "ready_for_model",
        "sysid_data_quality": "good",
        "frequency_response_ready": True,
        "axis": {"axis": "roll", "source": "SIDS.Ax", "raw_value": 7},
        "parameter_context": {
            "present": {
                "ATC_RAT_RLL_P": 0.10,
                "ATC_RAT_RLL_I": 0.10,
                "ATC_RAT_RLL_D": 0.005,
                "ATC_ANG_RLL_P": 8.0,
            }
        },
    }


def write_inputs(tmp: Path, *, sysid=None, param_text="ATC_RAT_RLL_P 0.12\nATC_RAT_RLL_I 0.12\nATC_RAT_RLL_D 0.006\nATC_ANG_RLL_P 8.5\n"):
    sysid_path = tmp / "methodic_11_1.json"
    params_path = tmp / "proposed.param"
    sysid_path.write_text(json.dumps(good_sysid() if sysid is None else sysid), encoding="utf-8")
    params_path.write_text(param_text, encoding="utf-8")
    return sysid_path, params_path


def validation_messages(*, oscillation=False):
    messages = [
        FakeMessage("PARM", TimeS=0.0, Name="SERVO1_FUNCTION", Value=33),
        FakeMessage("PARM", TimeS=0.0, Name="SERVO2_FUNCTION", Value=34),
        FakeMessage("PARM", TimeS=0.0, Name="SERVO3_FUNCTION", Value=35),
        FakeMessage("PARM", TimeS=0.0, Name="SERVO4_FUNCTION", Value=36),
        FakeMessage("MODE", TimeS=0.0, Mode="ALTHOLD"),
    ]
    dt = 0.02
    for i in range(700):
        t = i * dt
        rout = 0.35 * math.sin(t * 18.0) if oscillation else 0.04 * math.sin(t * 1.5)
        messages.extend([
            FakeMessage("RATE", TimeS=t, RDes=10.0 * math.sin(t), R=9.0 * math.sin(t), PDes=0.0, P=0.0, YDes=0.0, Y=0.0, ROut=rout, POut=0.03, YOut=0.02),
            FakeMessage("ATT", TimeS=t, DesRoll=2.0, Roll=1.8, DesPitch=0.0, Pitch=0.0, DesYaw=0.0, Yaw=0.0),
            FakeMessage("RCOU", TimeS=t, C1=1500 + 20 * math.sin(t), C2=1500 + 18 * math.sin(t * 1.1), C3=1500 + 16 * math.sin(t * 0.9), C4=1500 + 14 * math.sin(t * 0.8)),
            FakeMessage("VIBE", TimeS=t, VibeX=7.0, VibeY=6.0, VibeZ=5.0, Clip0=0, Clip1=0, Clip2=0),
            FakeMessage("BAT", TimeS=t, Volt=23.2, Curr=18.0),
        ])
    return sorted(messages, key=lambda msg: (msg.fields.get("TimeS", 0.0), msg.typ))


def run_with_fake_log(tmp: Path, messages):
    import ap_common
    import ap_methodic_analytical_pid_review

    sysid_path, params_path = write_inputs(tmp)
    original_common = ap_common.iter_dataflash_messages
    original_local = ap_methodic_analytical_pid_review.collect_dataflash.__globals__["_message_iter"]

    def fake_iter(_source, max_messages=None):
        yield from messages[:max_messages]

    ap_common.iter_dataflash_messages = fake_iter
    ap_methodic_analytical_pid_review.collect_dataflash.__globals__["_message_iter"] = fake_iter
    try:
        return analyze_analytical_pid_review(sysid_path=sysid_path, proposed_params_path=params_path, after_log="validation.BIN")
    finally:
        ap_common.iter_dataflash_messages = original_common
        ap_methodic_analytical_pid_review.collect_dataflash.__globals__["_message_iter"] = original_local


def test_missing_sysid_quality_is_inconclusive():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        sysid_path, params_path = write_inputs(tmp, sysid={"methodic_step": "11.1", "result": "inconclusive"})
        result = analyze_analytical_pid_review(sysid_path=sysid_path, proposed_params_path=params_path)
        assert_true(result["result"] == "inconclusive", result)
        assert_true(any(flag["type"] == "invalid_sysid_inputs" for flag in result["risk_flags"]), result)


def test_extreme_proposed_gain_change_has_risk_flag():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        sysid_path, params_path = write_inputs(tmp, param_text="ATC_RAT_RLL_P 0.50\nATC_RAT_RLL_I 0.10\n")
        result = analyze_analytical_pid_review(sysid_path=sysid_path, proposed_params_path=params_path)
        assert_true(any(flag["type"] == "extreme_relative_change" for flag in result["risk_flags"]), result)
        assert_true(result["result"] == "revise_model", result)


def test_validation_log_oscillation_is_do_not_apply():
    with tempfile.TemporaryDirectory() as td:
        result = run_with_fake_log(Path(td), validation_messages(oscillation=True))
        assert_true(result["result"] == "do_not_apply", result)
        assert_true(any(flag["type"] == "validation_blocker" for flag in result["risk_flags"]), result)


def test_reasonable_change_without_validation_log_is_ready_for_careful_test():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        sysid_path, params_path = write_inputs(tmp)
        result = analyze_analytical_pid_review(sysid_path=sysid_path, proposed_params_path=params_path)
        assert_true(result["result"] == "ready_for_careful_test", result)
        assert_true(result["validation_required"] is True, result)
        assert_true(any("validation" in item.lower() for item in result["missing_evidence"]), result)


def test_methodic_step_dispatches_11_2():
    import ap_methodic_step

    assert_true(ap_methodic_step.STEP_IMPLEMENTATIONS.get("11.2") == "analyze_11_2", ap_methodic_step.STEP_IMPLEMENTATIONS)


def main():
    test_missing_sysid_quality_is_inconclusive()
    test_extreme_proposed_gain_change_has_risk_flag()
    test_validation_log_oscillation_is_do_not_apply()
    test_reasonable_change_without_validation_log_is_ready_for_careful_test()
    test_methodic_step_dispatches_11_2()
    print("methodic analytical PID review tests passed")


if __name__ == "__main__":
    main()
