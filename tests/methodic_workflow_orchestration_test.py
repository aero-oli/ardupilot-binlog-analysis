#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import ap_methodic_compare
from ap_methodic_progress import build_progress


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


def write_step(tmp: Path, name: str, *, step: str, result: str, gate: str, next_step=None, missing=None, blocker=None):
    path = tmp / name
    findings = []
    if blocker:
        findings.append({"severity": "critical", "finding": blocker, "safety_gate": gate})
    path.write_text(json.dumps({
        "methodic_step": step,
        "title": f"Step {step}",
        "result": result,
        "safety_gate": gate,
        "analysis_window": {"log_path": f"log_{step}.BIN", "start_s": 1.0, "end_s": 10.0},
        "missing_evidence": missing or [],
        "findings": findings,
        "next_methodic_step": next_step,
    }), encoding="utf-8")
    return path


def test_combine_three_step_jsons_into_progress():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        paths = [
            write_step(tmp, "7_1.json", step="7.1", result="pass", gate="proceed", next_step="7.1.1"),
            write_step(tmp, "7_1_1.json", step="7.1.1", result="conditional_pass", gate="proceed_with_caution", next_step="8.1"),
            write_step(tmp, "8_1.json", step="8.1", result="pass", gate="proceed", next_step="8.2"),
        ]
        progress = build_progress(paths)
    assert_true(progress["progress_status"] == "conditional", progress)
    assert_true(len(progress["completed_steps"]) == 2, progress)
    assert_true(len(progress["conditional_steps"]) == 1, progress)
    assert_true(progress["recommended_next_methodic_step"] == "8.2", progress)


def test_failed_step_blocks_later_step():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        paths = [
            write_step(tmp, "7_1.json", step="7.1", result="pass", gate="proceed", next_step="7.1.1"),
            write_step(tmp, "7_1_1.json", step="7.1.1", result="fail", gate="do_not_proceed", blocker="Motor output oscillation unresolved."),
            write_step(tmp, "8_1.json", step="8.1", result="pass", gate="proceed", next_step="8.2"),
        ]
        progress = build_progress(paths)
    assert_true(progress["progress_status"] == "blocked", progress)
    assert_true(progress["current_blocker"]["step_id"] == "7.1.1", progress)
    assert_true(progress["recommended_next_methodic_step"] == "7.1.1", progress)


def test_clean_pass_progression_suggests_next_step():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        paths = [
            write_step(tmp, "7_1.json", step="7.1", result="pass", gate="proceed", next_step="7.1.1"),
            write_step(tmp, "7_1_1.json", step="7.1.1", result="pass", gate="proceed", next_step="8.1"),
            write_step(tmp, "8_1.json", step="8.1", result="pass", gate="proceed", next_step="8.2"),
        ]
        progress = build_progress(paths)
    assert_true(progress["progress_status"] == "ready_for_next_step", progress)
    assert_true(progress["recommended_next_methodic_step"] == "8.2", progress)


def test_before_after_compare_with_non_comparable_windows_warns():
    before = {
        "methodic_step": "8.1",
        "title": "Harmonic notch / filter review",
        "result": "conditional_pass",
        "safety_gate": "proceed_with_caution",
        "analysis_window": {"selection": "hover", "start_s": 0.0, "end_s": 10.0},
        "missing_evidence": [],
        "confidence_limits": [],
        "findings": [],
        "vibe": {"p95": 8.0},
    }
    after = {
        "methodic_step": "8.1",
        "title": "Harmonic notch / filter review",
        "result": "pass",
        "safety_gate": "proceed",
        "analysis_window": {"selection": "whole_log", "start_s": 0.0, "end_s": 100.0},
        "missing_evidence": ["FFT missing"],
        "confidence_limits": [],
        "findings": [],
        "vibe": {"p95": 5.0},
    }
    original = ap_methodic_compare.run_step_analysis

    def fake_run(log_path, step_id, plots_dir=None):
        return before if "before" in str(log_path) else after

    ap_methodic_compare.run_step_analysis = fake_run
    try:
        result = ap_methodic_compare.compare_methodic_step("before.BIN", "after.BIN", step_id="8.1")
    finally:
        ap_methodic_compare.run_step_analysis = original
    assert_true(result["comparison_result"] == "not_comparable", result)
    assert_true(result["segments_comparable"]["comparable"] is False, result)
    assert_true(any("Do not claim improvement" in item for item in result["confidence_limits"]), result)


def main():
    test_combine_three_step_jsons_into_progress()
    test_failed_step_blocks_later_step()
    test_clean_pass_progression_suggests_next_step()
    test_before_after_compare_with_non_comparable_windows_warns()
    print("methodic workflow orchestration tests passed")


if __name__ == "__main__":
    main()
