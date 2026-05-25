#!/usr/bin/env python3
from __future__ import annotations

import gzip
import json
import subprocess
import tempfile
from pathlib import Path

import pandas as pd
import yaml

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import ap_common
from ap_common import AnalysisError
from ap_log_compare import metric_differences
from ap_compass_yaw import build_compass_yaw_investigation, mag_field_frame, write_compass_yaw_plots
from ap_log_custom_plot import make_custom_plot
from ap_log_diagnose import diagnosis_missing
from ap_log_diagnose import build_cannot_conclude
from ap_log_diagnose import diagnose_by_class
from ap_log_diagnose import diagnose_yaw
from ap_log_extract import write_jsonl_stream
from ap_log_diagnose import make_targeted_plots_from_tables
from ap_log_fft import fft_from_isb_rows, fft_from_tables
from ap_log_investigation_manifest import build_manifest_from_index, validate_recommended_plot_groups
from ap_log_metrics import compute_metrics
from ap_log_mode_compare import compare_modes
from ap_next_step_helpers import build_diagnosis_action_plan
from ap_param_context import merge_external_parameters, parse_param_file
from ap_param_lookup import lookup_parameters
from ap_log_plots import health_plots
from update_parameter_metadata import compact_from_raw
from ap_log_plots import main as plots_main
from ap_parameters import decode_bitmask, enrich_parameter_entry, select_relevant_parameters
from ap_rcin import build_command_response_investigation, rc_channel_mapping, summarize_rcin
from ap_log_validate import log_quality_status, module_availability
from ap_skill_doctor import run_doctor
from ap_symptom_map import load_symptom_map
from ap_vibration import build_vibration_assessment
from ap_window_select import select_analysis_window


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


class FakeMsg:
    def __init__(self, typ, **fields):
        self.typ = typ
        self.fields = fields

    def get_type(self):
        return self.typ

    def to_dict(self):
        return dict(self.fields)


def test_stream_dataflash_counts_without_storing_unselected_rows():
    messages = [FakeMsg("RATE", TimeUS=i * 100000, R=i) for i in range(5000)]
    messages.extend(FakeMsg("ATT", TimeUS=i * 100000, Roll=i) for i in range(5))

    rows, index, stats = ap_common.collect_dataflash(messages, include=["ATT"], source="synthetic")

    assert_true(len(rows["ATT"]) == 5, "selected ATT rows should be collected")
    assert_true("RATE" not in rows, "unselected RATE rows should not be stored")
    assert_true(index["messages"]["RATE"]["count"] == 5000, "stream index should still count unselected RATE rows")
    assert_true(index["messages"]["ATT"]["count"] == 5, "stream index should count selected ATT rows")
    assert_true(stats["total_messages_read"] == 5005, "stream stats should count all messages read")


def test_stream_dataflash_respects_time_window_and_max_messages():
    messages = [FakeMsg("ATT", TimeUS=i * 1000000, Roll=i) for i in range(10)]

    rows, index, stats = ap_common.collect_dataflash(messages, include=["ATT"], source="synthetic", start_s=3.0, end_s=5.0, max_messages=7)

    assert_true([row["Roll"] for row in rows["ATT"]] == [3, 4, 5], "time window should limit collected rows")
    assert_true(index["messages"]["ATT"]["count"] == 7, "max_messages should stop stream after seven messages")
    assert_true(stats["max_messages_reached"] is True, "stream stats should report max message truncation")


def test_extract_jsonl_stream_respects_message_and_time_filters():
    messages = [
        FakeMsg("ATT", TimeUS=0, Roll=0),
        FakeMsg("RATE", TimeUS=500000, R=1),
        FakeMsg("ATT", TimeUS=1000000, Roll=10),
        FakeMsg("ATT", TimeUS=2000000, Roll=20),
        FakeMsg("RATE", TimeUS=2500000, R=2),
    ]

    with tempfile.TemporaryDirectory() as tmp:
        tables, _index, stats = write_jsonl_stream(
            messages,
            Path(tmp),
            include=["ATT"],
            start_s=0.5,
            end_s=1.5,
            source="synthetic",
        )
        path = Path(tables["ATT"]["path"])
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    assert_true(stats["total_messages_read"] == 5, "JSONL extraction should stream across every input message")
    assert_true(tables["ATT"]["rows"] == 1, "JSONL row count should reflect filters")
    assert_true(records[0]["message_type"] == "ATT", "JSONL should preserve message type")
    assert_true(records[0]["timestamp_s"] == 1.0, "JSONL should preserve normalized timestamp")
    assert_true(records[0]["fields"]["Roll"] == 10, "JSONL should preserve message fields")


def test_extract_jsonl_stream_supports_gzip_and_armed_filter():
    messages = [
        FakeMsg("ATT", TimeUS=0, Roll=0),
        FakeMsg("ARM", TimeUS=500000, Armed=1),
        FakeMsg("ATT", TimeUS=1000000, Roll=10),
    ]

    with tempfile.TemporaryDirectory() as tmp:
        tables, _index, stats = write_jsonl_stream(
            messages,
            Path(tmp),
            include=["ATT"],
            armed_only=True,
            gzip_output=True,
            source="synthetic",
        )
        path = Path(tables["ATT"]["path"])
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            records = [json.loads(line) for line in fh]

    assert_true(path.name == "ATT.jsonl.gz", "gzip JSONL output should use .jsonl.gz suffix")
    assert_true(stats["armed_filter_supported"] is True, "ARM messages should make armed filtering supported")
    assert_true(len(records) == 1 and records[0]["fields"]["Roll"] == 10, "armed-only JSONL should keep only armed rows")


def test_stream_index_reports_logging_dropouts():
    messages = [FakeMsg("DSF", TimeUS=1000000, Dp=3), FakeMsg("ATT", TimeUS=2000000, Roll=1)]

    _rows, index, _stats = ap_common.collect_dataflash(messages, include=[], source="synthetic")

    assert_true(index["logging_dropouts"], "DSF dropout evidence should be reported in the index")
    assert_true(index["logging_dropouts"][0]["fields"]["Dp"] == 3.0, "drop count field should be retained")
    assert_true(index["logging_health"]["confirmed_dropouts"][0]["fields"]["Dp"] == 3.0, "DSF.Dp should be confirmed dropout evidence")
    assert_true(index["logging_health"]["possible_dropouts"] == [], "confirmed DSF.Dp should not also be possible evidence")
    assert_true(index["logging_health"]["dropouts_detected"] is True, "logging health should flag DSF dropouts")
    assert_true(index["logging_health"]["limits_diagnosis"] is True, "dropouts should limit diagnosis confidence")


def test_non_logging_drop_field_is_possible_not_confirmed_dropout():
    messages = [
        FakeMsg("FOO", TimeUS=1000000, VoltageDrop=4),
        FakeMsg("ATT", TimeUS=1000000, Roll=0),
        FakeMsg("ATT", TimeUS=2000000, Roll=1),
    ]

    _rows, index, _stats = ap_common.collect_dataflash(messages, include=[], source="synthetic")
    health = index["logging_health"]

    assert_true(index["logging_dropouts"] == [], "non-logging drop-like fields should not become confirmed dropouts")
    assert_true(health["confirmed_dropouts"] == [], "non-logging drop-like fields should not be confirmed dropout evidence")
    assert_true(health["dropouts_detected"] is False, "possible-only dropout context should not set confirmed dropout flag")
    assert_true(health["possible_dropouts"][0]["message"] == "FOO", "possible dropout evidence should remain visible for inspection")
    assert_true(health["possible_dropout_count"] == 1, "possible dropout evidence should be counted separately")
    assert_true(health["limits_diagnosis"] is False, "possible-only dropout context should not reduce confidence by itself")


def test_logging_related_unknown_drop_field_is_possible_dropout_context():
    messages = [
        FakeMsg("LOGX", TimeUS=1000000, Drops=2),
        FakeMsg("ATT", TimeUS=1000000, Roll=0),
        FakeMsg("ATT", TimeUS=2000000, Roll=1),
    ]

    _rows, index, _stats = ap_common.collect_dataflash(messages, include=[], source="synthetic")
    health = index["logging_health"]

    assert_true(health["confirmed_dropouts"] == [], "unknown logging-like messages should not be confirmed without a known message/field pair")
    assert_true(health["possible_dropouts"][0]["fields"]["Drops"] == 2.0, "logging-like drop fields should be retained as possible context")
    assert_true("possible logging dropout" in health["confidence_impact"].lower(), "possible-only dropout context should be mentioned without a confidence downgrade")


def test_logging_health_clean_log_has_no_limits():
    messages = [FakeMsg("ATT", TimeUS=i * 1000000, Roll=i) for i in range(5)]
    messages.extend(FakeMsg("RATE", TimeUS=i * 1000000, R=i) for i in range(5))

    _rows, index, _stats = ap_common.collect_dataflash(messages, include=[], source="synthetic")
    health = index["logging_health"]
    assert_true(health["dropouts_detected"] is False, "clean log should not flag dropouts")
    assert_true(health["confirmed_dropouts"] == [], "clean log should have no confirmed dropouts")
    assert_true(health["possible_dropouts"] == [], "clean log should have no possible dropouts")
    assert_true(health["limits_diagnosis"] is False, "clean log should not limit diagnosis")
    assert_true(health["max_time_gap_s"] == 1.0, "clean log should still report max normal gap")


def test_logging_health_detects_timestamp_gap_and_reset():
    messages = [
        FakeMsg("ATT", TimeUS=0, Roll=0),
        FakeMsg("ATT", TimeUS=1000000, Roll=1),
        FakeMsg("ATT", TimeUS=10000000, Roll=2),
        FakeMsg("RATE", TimeUS=5000000, R=1),
        FakeMsg("RATE", TimeUS=4000000, R=2),
    ]

    _rows, index, _stats = ap_common.collect_dataflash(messages, include=[], source="synthetic")
    health = index["logging_health"]
    affected = "\n".join(f"{m.get('message')} {m.get('reason')}" for m in health["affected_messages"])
    assert_true(health["max_time_gap_s"] == 9.0, "logging health should report max timestamp gap")
    assert_true("ATT timestamp_gap" in affected, "ATT gap should be listed as affected")
    assert_true("RATE timestamp_reset" in affected, "RATE reset should be listed as affected")
    assert_true(health["limits_diagnosis"] is True, "gaps/resets should limit diagnosis")


def test_logging_health_detects_missing_core_messages_after_arm():
    messages = [
        FakeMsg("ARM", TimeUS=0, Armed=1),
        FakeMsg("ATT", TimeUS=1000000, Roll=1),
        FakeMsg("RATE", TimeUS=1000000, R=1),
    ]

    _rows, index, _stats = ap_common.collect_dataflash(messages, include=[], source="synthetic")
    health = index["logging_health"]
    assert_true("RCOU/RCO2/RCO3" in health["missing_core_messages_after_arm"], "missing actuator output messages after arm should be reported")
    assert_true(health["limits_diagnosis"], "missing armed core messages should limit diagnosis")


def test_logging_health_manifest_and_diagnosis_confidence_limit():
    index = {
        "messages": {"ATT": {}, "RATE": {}, "RCOU": {}},
        "errors": [],
        "events": [],
        "modes": [],
        "logging_health": {
            "dropouts_detected": False,
            "max_time_gap_s": 8.0,
            "affected_messages": [{"message": "RATE", "reason": "timestamp_gap"}],
            "confidence_impact": "Timestamp gaps may hide short events.",
            "limits_diagnosis": True,
        },
    }
    manifest = build_manifest_from_index(index, "yaw issue", "flight.bin")
    assert_true(manifest["logging_health"]["limits_diagnosis"], "manifest should expose logging health")
    assert_true(any("Logging health limits" in w for w in manifest["warnings"]), "manifest should warn about logging health")

    tables = {
        "ATT": pd.DataFrame({"TimeS": [0, 1, 2], "DesYaw": [0, 0, 0], "Yaw": [0, 30, 45]}),
        "RATE": pd.DataFrame({"TimeS": [0, 1, 2], "YDes": [0, 0, 0], "Y": [0, 80, 90], "YOut": [0.8, 0.9, 0.95]}),
        "RCOU": pd.DataFrame({"TimeS": [0, 1, 2], "C1": [1900, 1950, 1960]}),
    }
    findings, _context, checked, *_ = diagnose_yaw(tables, index)
    yaw_finding = next(f for f in findings if "Yaw authority limited" in f.get("possible_cause", ""))
    checked_text = "\n".join(c.get("result", "") for c in checked)
    assert_true(yaw_finding["confidence"] == "medium", "logging health should lower high-confidence diagnosis")
    assert_true("Timestamp gaps may hide short events" in checked_text, "diagnosis should state logging-health confidence impact")


def test_log_quality_status_flags_logging_dropouts_as_confidence_limit():
    index = {
        "file": "flight.bin",
        "messages": {"FMT": {"count": 1}, "ATT": {"count": 10}},
        "parameters": {},
        "duration_s": 10.0,
        "start_time_s": 0.0,
        "end_time_s": 10.0,
        "logging_health": {
            "confirmed_dropouts": [{"message": "DSF", "fields": {"Dp": 3}}],
            "dropouts_detected": True,
            "confidence_impact": "Log dropout/drop-count evidence is present; conclusions that rely on exact timing or missing rows are reduced confidence.",
        },
        "parser_stats": {},
    }
    quality = log_quality_status(index)
    issue_codes = {issue["code"] for issue in quality["issues"]}
    assert_true(quality["status"] == "limited", f"dropout quality should be limited: {quality}")
    assert_true("logging_dropouts" in issue_codes, "confirmed dropouts should become a log quality issue")
    assert_true(any("reduced confidence" in item for item in quality["confidence_limits"]), "dropouts should create confidence limit")


def test_log_quality_status_reports_missing_timebase():
    index = {"file": "flight.bin", "messages": {"FMT": {"count": 1}, "ATT": {"count": 3}}, "parameters": {"FRAME_CLASS": 1}, "duration_s": None, "start_time_s": None, "end_time_s": None, "logging_health": {}, "parser_stats": {}}
    quality = log_quality_status(index)
    assert_true(any(issue["code"] == "no_usable_timebase" for issue in quality["issues"]), "missing timebase should be reported")
    assert_true(any("Time-window" in item for item in quality["confidence_limits"]), "missing timebase should limit time/correlation claims")


def test_no_parm_is_reported_as_parameter_context_limitation():
    index = {"messages": {"ATT": {}, "RATE": {}}, "parameters": {}, "errors": [], "events": [], "modes": []}
    manifest = build_manifest_from_index(index, "yaw issue", "flight.bin")
    quality = log_quality_status({**index, "duration_s": 1.0, "start_time_s": 0.0, "end_time_s": 1.0, "logging_health": {}, "parser_stats": {}})
    assert_true(manifest["parameter_context"]["limitation"].startswith("No PARM/index parameter values"), "manifest should report missing parameter context")
    assert_true(any(issue["code"] == "no_parm" for issue in quality["issues"]), "validation quality should report no PARM")


def test_corrupt_incomplete_reference_exists_and_is_linked_from_skill():
    ref = Path("references/corrupt-or-incomplete-log.md")
    skill = Path("SKILL.md").read_text(encoding="utf-8")
    assert_true(ref.exists(), "corrupt/incomplete log reference should exist")
    text = ref.read_text(encoding="utf-8")
    assert_true("Do not attempt to repair logs automatically" in text, "reference should forbid automatic repair")
    assert_true("references/corrupt-or-incomplete-log.md" in skill, "SKILL should link corrupt/incomplete log guidance")


def test_load_tables_fails_on_unreadable_table():
    with tempfile.TemporaryDirectory() as tmp:
        table = Path(tmp) / "RATE.csv"
        table.write_text("TimeS,RDes,R\n0,1,1\n", encoding="utf-8")
        original = ap_common.read_table
        try:
            def fail_read(_path):
                raise ap_common.AnalysisError("synthetic read failure")

            ap_common.read_table = fail_read
            try:
                ap_common.load_tables(tmp)
            except ap_common.AnalysisError as exc:
                assert_true("synthetic read failure" in str(exc), "load_tables should surface table read failures")
            else:
                raise AssertionError("load_tables should not return success when a table cannot be read")
        finally:
            ap_common.read_table = original


def test_time_window_filters_tables_inclusively():
    tables = {
        "RATE": pd.DataFrame({"TimeS": [0.0, 1.0, 2.0, 3.0], "R": [0, 1, 2, 3]}),
        "MSG": pd.DataFrame({"TimeS": [0.5, 2.5], "Message": ["before", "inside"]}),
    }
    filtered = ap_common.filter_tables_by_time(tables, start_s=1.0, end_s=2.5)
    assert_true(filtered["RATE"]["R"].tolist() == [1, 2], "RATE should be clipped to requested window")
    assert_true(filtered["MSG"]["Message"].tolist() == ["inside"], "MSG should be clipped to requested window")


def test_window_selector_mode_intervals():
    tables = {"MODE": pd.DataFrame({"TimeS": [0.0, 10.0, 30.0], "Mode": ["STABILIZE", "LOITER", "RTL"]})}
    selection = select_analysis_window(tables, mode="LOITER", log_end_s=40.0)
    assert_true(selection["start_s"] == 10.0 and selection["end_s"] == 30.0, "mode selector should return active interval")
    assert_true(selection["rule"] == "mode", "selection should record mode rule")


def test_copter_numeric_mode_matches_named_query():
    tables = {"MODE": pd.DataFrame({"TimeS": [0.0, 10.0, 20.0], "Mode": [0, 3, 16]})}
    selection = select_analysis_window(tables, mode="AUTO", log_end_s=30.0, vehicle_scope={"primary_vehicle": "Copter"})

    assert_true(selection["start_s"] == 10.0 and selection["end_s"] == 20.0, "MODE.Mode=3 should match --mode AUTO")
    assert_true(selection["decoded_source"] == "AUTO", "mode selector should record decoded source")
    assert_true(selection["mode_intervals"][0]["decoded_mode"] == "AUTO", "selected numeric interval should include decoded mode")


def test_copter_poshold_numeric_mode_matches_named_query():
    tables = {"MODE": pd.DataFrame({"TimeS": [0.0, 10.0, 25.0], "Mode": [3, 16, 6]})}
    selection = select_analysis_window(tables, mode="POSHOLD", log_end_s=35.0, vehicle_scope={"primary_vehicle": "Copter"})

    assert_true(selection["start_s"] == 10.0 and selection["end_s"] == 25.0, "MODE.Mode=16 should match --mode POSHOLD")


def test_copter_mode_aliases_match_numeric_modes():
    tables = {"MODE": pd.DataFrame({"TimeS": [0.0, 8.0, 16.0, 24.0], "Mode": [2, 16, 20, 21]})}

    alt_hold = select_analysis_window(tables, mode="ALT_HOLD", log_end_s=30.0, vehicle_scope={"primary_vehicle": "Copter"})
    guided_no_gps = select_analysis_window(tables, mode="GUIDED_NO_GPS", log_end_s=30.0, vehicle_scope={"primary_vehicle": "Copter"})
    smart_rtl = select_analysis_window(tables, mode="SMARTRTL", log_end_s=30.0, vehicle_scope={"primary_vehicle": "Copter"})

    assert_true(alt_hold["start_s"] == 0.0 and alt_hold["decoded_source"] == "ALTHOLD", "ALT_HOLD should alias ALTHOLD")
    assert_true(guided_no_gps["start_s"] == 16.0 and guided_no_gps["decoded_source"] == "GUIDED_NOGPS", "GUIDED_NO_GPS should alias GUIDED_NOGPS")
    assert_true(smart_rtl["start_s"] == 24.0 and smart_rtl["decoded_source"] == "SMART_RTL", "SMARTRTL should alias SMART_RTL")


def test_copter_named_mode_matches_numeric_query():
    tables = {"MODE": pd.DataFrame({"TimeS": [0.0, 5.0, 15.0], "Name": ["STABILIZE", "AUTO", "RTL"]})}
    selection = select_analysis_window(tables, mode="3", log_end_s=30.0, vehicle_scope={"primary_vehicle": "Copter"})

    assert_true(selection["start_s"] == 5.0 and selection["end_s"] == 15.0, 'MODE.Name="AUTO" should match --mode 3')


def test_decoded_mode_selection_preserves_multiple_intervals():
    tables = {
        "MODE": pd.DataFrame({
            "TimeS": [0.0, 10.0, 20.0, 30.0, 40.0],
            "Mode": [0, 3, 0, 3, 16],
        })
    }
    selection = select_analysis_window(tables, mode="AUTO", log_end_s=50.0, vehicle_scope={"primary_vehicle": "Copter"})

    assert_true(selection["intervals_found"] == [{"start_s": 10.0, "end_s": 20.0}, {"start_s": 30.0, "end_s": 40.0}], "decoded numeric mode intervals should remain split")
    assert_true([m["decoded_mode"] for m in selection["mode_intervals"]] == ["AUTO", "AUTO"], "mode interval metadata should carry decoded names")


def test_unknown_numeric_mode_is_labelled_without_crashing():
    tables = {"MODE": pd.DataFrame({"TimeS": [0.0, 10.0], "Mode": [99, 3]})}
    segments = ap_common.mode_segments_from_tables(tables, log_end_s=20.0)

    assert_true(segments[0]["raw_mode"] == 99, "unknown numeric raw mode should be preserved")
    assert_true(segments[0]["decoded_mode"] == "UNKNOWN_COPTER_MODE_99", "unknown numeric mode should be labelled")
    assert_true(segments[1]["decoded_mode"] == "AUTO", "known numeric mode should still decode")


def test_index_summary_includes_decoded_mode_timeline_and_caveat():
    index = {
        "file_name": "flight.bin",
        "vehicle": None,
        "firmware": None,
        "duration_s": 30.0,
        "end_time_s": 30.0,
        "parameter_count": 0,
        "messages": {"MODE": {"count": 3, "fields": ["TimeS", "Mode"]}},
        "modes": [
            {"time_s": 0.0, "raw_mode": 0, "decoded_mode": "STABILIZE"},
            {"time_s": 10.0, "raw_mode": 3, "decoded_mode": "AUTO"},
            {"time_s": 20.0, "raw_mode": 16, "decoded_mode": "POSHOLD"},
        ],
        "errors": [],
        "parameters": {},
    }

    summary = ap_common.message_inventory_markdown(index)
    assert_true("| 3 | AUTO | 10.000 | 20.000 | 10.000 |" in summary, "index summary should include raw and decoded mode timeline")
    assert_true("not confirmed Copter" in summary, "unknown vehicle mode decoding should be caveated")


def test_active_flight_filter_excludes_ground_spool_from_auto_window():
    tables = {
        "MODE": pd.DataFrame({"TimeS": [0.0], "Mode": [3]}),
        "ARM": pd.DataFrame({"TimeS": [0.0], "Armed": [1]}),
        "CTUN": pd.DataFrame({"TimeS": [0.0, 1.0, 2.0, 10.0, 11.0, 12.0], "Alt": [0.0, 0.1, 0.1, 3.0, 3.2, 3.1], "ThO": [0.08, 0.10, 0.10, 0.45, 0.46, 0.44]}),
        "RCOU": pd.DataFrame({"TimeS": [0.0, 1.0, 2.0, 10.0, 11.0, 12.0], "C1": [1950, 1960, 1950, 1500, 1510, 1500], "C2": [1500, 1500, 1500, 1500, 1510, 1500]}),
        "PARM": pd.DataFrame({"Name": ["SERVO1_FUNCTION", "SERVO2_FUNCTION"], "Value": [33, 34]}),
    }
    selection = select_analysis_window(tables, mode="AUTO", log_end_s=12.0, vehicle_scope={"primary_vehicle": "Copter"})
    selected = ap_common.filter_tables_by_time(tables, start_s=selection["start_s"], end_s=selection["end_s"], intervals=selection["intervals_used"])

    filtered_selection, profile = ap_common.apply_active_flight_filter(selection, selected, active_flight_only=True)
    filtered = ap_common.filter_tables_by_time(selected, start_s=filtered_selection["start_s"], end_s=filtered_selection["end_s"], intervals=filtered_selection["intervals_used"])

    assert_true(filtered["RCOU"]["TimeS"].tolist() == [10.0, 11.0, 12.0], "active-flight filter should exclude low-altitude spool rows")
    assert_true(filtered_selection["ground_spool_excluded"] is True, "selection should record ground/spool exclusion")
    assert_true(filtered_selection["rule"] == "mode+active_flight", "selection rule should record active-flight filtering")
    assert_true(profile["quality"]["ground_spool_rows_included"] is True, "window quality should report excluded ground/spool contamination")
    assert_true(profile["criteria"]["min_alt_m"] == 1.0 and profile["criteria"]["min_throttle_normalized"] == 0.15, "active-flight criteria should be recorded")


def test_ground_only_saturation_is_not_active_flight_motor_finding():
    tables = {
        "MODE": pd.DataFrame({"TimeS": [0.0], "Mode": [3]}),
        "ARM": pd.DataFrame({"TimeS": [0.0], "Armed": [1]}),
        "CTUN": pd.DataFrame({"TimeS": [0.0, 1.0, 2.0, 10.0, 11.0, 12.0], "Alt": [0.0, 0.1, 0.1, 3.0, 3.2, 3.1], "ThO": [0.08, 0.10, 0.10, 0.45, 0.46, 0.44]}),
        "RCOU": pd.DataFrame({"TimeS": [0.0, 1.0, 2.0, 10.0, 11.0, 12.0], "C1": [1950, 1960, 1950, 1500, 1510, 1500], "C2": [1500, 1500, 1500, 1500, 1510, 1500]}),
        "PARM": pd.DataFrame({"Name": ["SERVO1_FUNCTION", "SERVO2_FUNCTION"], "Value": [33, 34]}),
    }
    index = {"messages": {name: {} for name in tables}, "parameters": {"SERVO1_FUNCTION": 33, "SERVO2_FUNCTION": 34}, "errors": [], "events": [], "modes": []}
    unfiltered_findings, *_ = diagnose_by_class("motor_esc_issue", tables, index)
    selection = {"start_s": 0.0, "end_s": 12.0, "rule": "window", "intervals_used": [{"start_s": 0.0, "end_s": 12.0}], "warnings": []}
    filtered_selection, _profile = ap_common.apply_active_flight_filter(selection, tables, active_flight_only=True)
    filtered = ap_common.filter_tables_by_time(tables, start_s=filtered_selection["start_s"], end_s=filtered_selection["end_s"], intervals=filtered_selection["intervals_used"])
    filtered_findings, *_ = diagnose_by_class("motor_esc_issue", filtered, index)

    assert_true(any("Motor output saturation" in f.get("possible_cause", "") for f in unfiltered_findings), "unfiltered ground spool saturation should be visible to the existing heuristic")
    assert_true(not any("Motor output saturation" in f.get("possible_cause", "") for f in filtered_findings), "ground-only saturation should not become active-flight saturation")


def test_active_flight_filter_warns_when_evidence_is_insufficient():
    tables = {
        "ATT": pd.DataFrame({"TimeS": [0.0, 1.0, 2.0], "Roll": [0.0, 0.0, 0.0]}),
        "RATE": pd.DataFrame({"TimeS": [0.0, 1.0, 2.0], "R": [0.0, 0.0, 0.0]}),
    }
    selection = {"start_s": 0.0, "end_s": 2.0, "rule": "window", "intervals_used": [{"start_s": 0.0, "end_s": 2.0}], "warnings": []}
    filtered_selection, profile = ap_common.apply_active_flight_filter(selection, tables, active_flight_only=True)

    warning_text = "\n".join(filtered_selection["warnings"])
    assert_true("no usable altitude signal" in warning_text, "missing altitude should be warned")
    assert_true("no usable throttle/output signal" in warning_text, "missing throttle should be warned")
    assert_true(profile["quality"]["active_flight_confidence"] == "low", "insufficient evidence should not be presented as certain")
    assert_true(filtered_selection["intervals_used"] == [{"start_s": 0.0, "end_s": 2.0}], "insufficient evidence should retain the original window")


def test_mode_window_filters_disjoint_intervals_without_intervening_modes():
    tables = {
        "MODE": pd.DataFrame({
            "TimeS": [0.0, 10.0, 20.0, 30.0, 40.0],
            "Mode": ["STABILIZE", "LOITER", "STABILIZE", "LOITER", "RTL"],
        }),
        "RATE": pd.DataFrame({
            "TimeS": [12.0, 16.0, 24.0, 32.0, 36.0],
            "YDes": [5.0, 5.0, 99.0, 7.0, 7.0],
            "Y": [4.5, 4.7, 98.0, 6.5, 6.8],
            "YOut": [0.2, 0.2, 0.95, 0.3, 0.3],
        }),
    }
    selection = select_analysis_window(tables, mode="LOITER", log_end_s=45.0)

    filtered = ap_common.filter_tables_by_time(
        tables,
        start_s=selection["start_s"],
        end_s=selection["end_s"],
        intervals=selection.get("intervals_used"),
    )

    assert_true(selection["start_s"] == 10.0 and selection["end_s"] == 40.0, "mode metadata should record bounding selected span")
    assert_true(selection["intervals_found"] == [{"start_s": 10.0, "end_s": 20.0}, {"start_s": 30.0, "end_s": 40.0}], "all matching mode intervals should be recorded")
    assert_true(selection["intervals_used"] == selection["intervals_found"], "all matching mode intervals should be used")
    assert_true(selection["non_matching_gaps_excluded"] is True, "split mode selection should report excluded gaps")
    assert_true(filtered["RATE"]["TimeS"].tolist() == [12.0, 16.0, 32.0, 36.0], "intervening STABILIZE telemetry should be excluded")


def test_mode_window_diagnosis_uses_only_selected_intervals():
    tables = {
        "MODE": pd.DataFrame({
            "TimeS": [0.0, 10.0, 20.0, 30.0, 40.0],
            "Mode": ["STABILIZE", "LOITER", "STABILIZE", "LOITER", "RTL"],
        }),
        "ATT": pd.DataFrame({"TimeS": [12.0, 24.0, 32.0], "DesYaw": [0.0, 0.0, 0.0], "Yaw": [1.0, 90.0, 2.0]}),
        "RATE": pd.DataFrame({"TimeS": [12.0, 24.0, 32.0], "YDes": [0.0, 0.0, 0.0], "Y": [1.0, 120.0, 2.0], "YOut": [0.1, 0.99, 0.1]}),
        "PIDY": pd.DataFrame({"TimeS": [12.0, 24.0, 32.0], "Tar": [0.0, 0.0, 0.0], "Act": [1.0, 120.0, 2.0], "Err": [1.0, 120.0, 2.0]}),
        "RCOU": pd.DataFrame({"TimeS": [12.0, 24.0, 32.0], "C1": [1500, 2000, 1500], "C2": [1500, 2000, 1500]}),
    }
    selection = select_analysis_window(tables, mode="LOITER", log_end_s=45.0)
    filtered = ap_common.filter_tables_by_time(tables, start_s=selection["start_s"], end_s=selection["end_s"], intervals=selection["intervals_used"])

    findings, _context, _checked, *_missing = diagnose_yaw(filtered, {"messages": {"ATT": {}, "RATE": {}, "PIDY": {}, "RCOU": {}}})

    finding_text = "\n".join(str(f) for f in findings)
    assert_true("Yaw tracking error" not in finding_text, "diagnosis should not see yaw error from intervening non-LOITER interval")
    assert_true(filtered["RATE"]["TimeS"].tolist() == [12.0, 32.0], "diagnosis input should only contain LOITER RATE rows")


def test_custom_plot_manifest_records_split_mode_intervals(tmp_path=None):
    with tempfile.TemporaryDirectory() as tmp:
        tables = {
            "MODE": pd.DataFrame({
                "TimeS": [0.0, 10.0, 20.0, 30.0, 40.0],
                "Mode": ["STABILIZE", "LOITER", "STABILIZE", "LOITER", "RTL"],
            }),
            "RATE": pd.DataFrame({"TimeS": [12.0, 24.0, 32.0], "Y": [1.0, 99.0, 2.0]}),
        }
        selection = select_analysis_window(tables, mode="LOITER", log_end_s=45.0)
        filtered = ap_common.filter_tables_by_time(tables, start_s=selection["start_s"], end_s=selection["end_s"], intervals=selection["intervals_used"])
        manifest = make_custom_plot(filtered, ["RATE.Y"], Path(tmp) / "plot.html", analysis_window=selection)

    assert_true(manifest["analysis_window"]["rule"] == "mode", "plot manifest should record mode window rule")
    assert_true(manifest["analysis_window"]["source"] == "LOITER", "plot manifest should record selected mode")
    assert_true(manifest["analysis_window"]["intervals_found"] == [{"start_s": 10.0, "end_s": 20.0}, {"start_s": 30.0, "end_s": 40.0}], "plot manifest should record candidate mode intervals")
    assert_true(manifest["analysis_window"]["intervals_used"] == manifest["analysis_window"]["intervals_found"], "plot manifest should record used mode intervals")
    assert_true(manifest["analysis_window"]["non_matching_gaps_excluded"] is True, "plot manifest should state that non-matching gaps were excluded")


def test_window_selector_around_msg_event_and_error():
    tables = {
        "MSG": pd.DataFrame({"TimeS": [5.0, 20.0], "Message": ["startup", "yaw issue seen"]}),
        "EV": pd.DataFrame({"TimeS": [40.0], "Id": ["TAKEOFF"]}),
        "ERR": pd.DataFrame({"TimeS": [60.0], "Subsys": [2], "ECode": [1]}),
    }
    msg = select_analysis_window(tables, around_msg="yaw issue", around_radius_s=3.0)
    event = select_analysis_window(tables, around_event="takeoff", around_radius_s=5.0)
    err = select_analysis_window(tables, around_error=True, around_radius_s=2.0)
    assert_true(msg["start_s"] == 17.0 and msg["end_s"] == 23.0, "around-msg should center on matching MSG")
    assert_true(event["start_s"] == 35.0 and event["end_s"] == 45.0, "around-event should center on matching EV")
    assert_true(err["start_s"] == 58.0 and err["end_s"] == 62.0, "around-error should center on first ERR")


def test_window_selector_takeoff_hover_and_high_throttle():
    tables = {
        "CTUN": pd.DataFrame({
            "TimeS": [0, 5, 10, 15, 20, 25, 30],
            "Alt": [0, 0.2, 2.0, 5.0, 5.1, 5.0, 5.1],
            "ThO": [0.2, 0.35, 0.65, 0.55, 0.5, 0.52, 0.51],
        }),
        "GPS": pd.DataFrame({"TimeS": [15, 20, 25, 30], "Spd": [0.2, 0.1, 0.15, 0.1]}),
        "ATT": pd.DataFrame({"TimeS": [15, 20, 25, 30], "Roll": [1, 2, 1, 2], "Pitch": [1, 1, 2, 1]}),
        "RCOU": pd.DataFrame({"TimeS": [0, 5, 10, 15, 20], "C1": [1200, 1300, 1900, 1950, 1500]}),
    }
    takeoff = select_analysis_window(tables, takeoff_only=True)
    hover = select_analysis_window(tables, hover_candidates=True)
    high = select_analysis_window(tables, high_throttle_only=True, high_throttle_threshold=0.55)
    assert_true(takeoff["start_s"] == 10.0 and takeoff["end_s"] == 15.0, f"unexpected takeoff window {takeoff}")
    assert_true(hover["start_s"] >= 15.0 and hover["end_s"] <= 30.0, f"unexpected hover window {hover}")
    assert_true(high["start_s"] == 10.0 and high["end_s"] == 15.0, f"unexpected high-throttle window {high}")


def test_hover_selector_uses_duration_based_window_on_high_rate_data():
    times = [i * 0.5 for i in range(25)]
    tables = {
        "CTUN": pd.DataFrame({
            "TimeS": times,
            "Alt": [2.0 + (0.05 if i % 2 else 0.0) for i in range(25)],
            "ThO": [0.45 for _ in times],
        }),
        "GPS": pd.DataFrame({"TimeS": times, "Spd": [0.25 for _ in times]}),
        "ATT": pd.DataFrame({"TimeS": times, "Roll": [1.5 for _ in times], "Pitch": [-1.0 for _ in times]}),
    }

    hover = select_analysis_window(tables, hover_candidates=True, hover_min_duration_s=5.0)

    assert_true(hover["end_s"] - hover["start_s"] >= 5.0, f"hover window should last at least min duration: {hover}")
    assert_true(hover["criteria"]["min_duration_s"] == 5.0, "hover criteria should record minimum duration")
    assert_true(hover["criteria"]["alt_span_max_m"] == 0.75, "hover criteria should record altitude span limit")
    assert_true(hover["intervals_found"], "hover selector should record candidate intervals")


def test_hover_selector_rejects_unstable_altitude():
    tables = {
        "CTUN": pd.DataFrame({
            "TimeS": [0, 1, 2, 3, 4, 5, 6],
            "Alt": [0.0, 0.4, 0.9, 1.4, 2.0, 2.5, 3.0],
            "ThO": [0.45, 0.45, 0.45, 0.45, 0.45, 0.45, 0.45],
        }),
    }
    try:
        select_analysis_window(tables, hover_candidates=True, hover_min_duration_s=5.0, hover_alt_span_max_m=0.75)
    except AnalysisError as exc:
        assert_true("no stable-altitude moderate-throttle window" in str(exc), f"unexpected hover rejection error: {exc}")
    else:
        raise AssertionError("unstable altitude should not produce a hover candidate")


def test_hover_selector_rejects_throttle_outside_hover_band():
    tables = {
        "CTUN": pd.DataFrame({
            "TimeS": [0, 1, 2, 3, 4, 5, 6],
            "Alt": [2.0, 2.05, 2.0, 2.05, 2.0, 2.05, 2.0],
            "ThO": [0.82, 0.83, 0.84, 0.82, 0.83, 0.84, 0.82],
        }),
    }
    try:
        select_analysis_window(tables, hover_candidates=True, hover_min_duration_s=5.0, hover_throttle_min=0.25, hover_throttle_max=0.75)
    except AnalysisError as exc:
        assert_true("no stable-altitude moderate-throttle window" in str(exc), f"unexpected throttle rejection error: {exc}")
    else:
        raise AssertionError("throttle outside hover band should not produce a hover candidate")


def test_window_selector_fails_requested_missing_selector():
    try:
        select_analysis_window({}, mode="LOITER")
    except AnalysisError as exc:
        assert_true("MODE" in str(exc), f"unexpected mode selector error: {exc}")
    else:
        raise AssertionError("requested mode selector should fail when MODE is missing")
    try:
        select_analysis_window({}, hover_candidates=True)
    except AnalysisError as exc:
        assert_true("CTUN" in str(exc), f"unexpected hover selector error: {exc}")
    else:
        raise AssertionError("requested hover selector should fail when CTUN is missing")


def test_parse_time_window_accepts_start_end_and_around():
    assert_true(ap_common.parse_time_window("10:20") == {"start_s": 10.0, "end_s": 20.0}, "start:end window should parse")
    assert_true(ap_common.parse_time_window("around:100:5") == {"start_s": 95.0, "end_s": 105.0}, "around window should parse")


def test_metrics_can_be_computed_from_filtered_window():
    tables = {"RATE": pd.DataFrame({"TimeS": [0.0, 1.0, 2.0], "RDes": [0.0, 10.0, 20.0], "R": [0.0, 8.0, 10.0]})}
    filtered = ap_common.filter_tables_by_time(tables, start_s=1.0, end_s=2.0)
    metrics = compute_metrics(filtered, analysis_window={"start_s": 1.0, "end_s": 2.0})
    assert_true(metrics["flight"]["duration_s_estimate"] == 1.0, "filtered metrics should use filtered duration")
    assert_true(metrics["analysis_window"] == {"start_s": 1.0, "end_s": 2.0}, "metrics should record analysis window")


def test_nested_numeric_summary_units_use_message_and_field_context():
    tables = {
        "ESC": pd.DataFrame({
            "TimeS": [0.0, 1.0],
            "RPM": [4200.0, 4300.0],
            "Curr": [3.2, 4.1],
            "Temp": [34.0, 36.0],
            "MotTemp": [39.0, 42.0],
        }),
        "ESCX": pd.DataFrame({
            "TimeS": [0.0, 1.0],
            "inpct": [20.0, 65.0],
            "outpct": [18.0, 60.0],
        }),
        "RATE": pd.DataFrame({
            "TimeS": [0.0, 1.0],
            "YDes": [0.0, 90.0],
            "Y": [0.0, 30.0],
            "YOut": [0.0, 0.8],
        }),
        "PIDY": pd.DataFrame({
            "TimeS": [0.0, 1.0],
            "Err": [0.0, 60.0],
        }),
        "VIBE": pd.DataFrame({
            "TimeS": [0.0, 1.0],
            "VibeX": [4.0, 6.0],
            "VibeY": [5.0, 7.0],
        }),
        "IMU": pd.DataFrame({
            "TimeS": [0.0, 1.0],
            "AccX": [0.1, 0.2],
            "GyrX": [1.0, 2.0],
        }),
        "XUNK": pd.DataFrame({
            "TimeS": [0.0, 1.0],
            "Mystery": [1.0, 2.0],
        }),
    }

    metrics = compute_metrics(tables)

    esc_units = metrics["health"]["esc"]["numeric_units"]
    assert_true(esc_units["RPM"]["min"] == "rpm", "ESC.RPM nested summary should use rpm")
    assert_true(esc_units["Curr"]["max"] == "A", "ESC.Curr nested summary should use current units")
    assert_true(esc_units["Temp"]["mean"] == "degC", "ESC.Temp nested summary should use temperature units")
    assert_true(esc_units["MotTemp"]["p95"] == "degC", "ESC.MotTemp nested summary should use temperature units")

    escx_units = metrics["health"]["escx"]["numeric_units"]
    assert_true(escx_units["inpct"]["max"] == "%", "ESCX.inpct nested summary should use percent units")
    assert_true(escx_units["outpct"]["p99"] == "%", "ESCX.outpct nested summary should use percent units")

    assert_true(metrics["tuning"]["yaw"]["units"]["rate_error_rms"] == "deg/s", "RATE error metrics should use deg/s")
    assert_true(metrics["tuning"]["yaw"]["pid"]["term_units"]["Err"]["max"] == "deg/s", "PID error summary should use deg/s")
    assert_true(metrics["health"]["vibration"]["units"]["VibeX"]["max"] == "m/s/s", "VIBE nested summary should use acceleration units")
    imu_units = metrics["health"]["instances"]["imu"]["IMU"]["numeric_units"]
    assert_true(imu_units["AccX"]["mean"] == "m/s/s", "IMU acceleration summary should use acceleration units")
    assert_true(metrics["generic_messages"]["XUNK"]["numeric_units"]["Mystery"]["max"] == "unknown", "unknown nested fields should remain unknown")


def test_output_mapping_reads_servo_function_parameters():
    params = {"SERVO1_FUNCTION": 33, "SERVO2_FUNCTION": 34, "SERVO3_FUNCTION": 1, "SERVO4_FUNCTION": 0}
    mapping = ap_common.output_mapping_from_params(params)
    assert_true(mapping["C1"]["function_id"] == 33 and mapping["C1"]["role"] == "motor1", "SERVO1 motor mapping should be detected")
    assert_true(mapping["C1"]["category"] == "motor", "SERVO1 motor mapping should be categorized as motor")
    assert_true(mapping["C2"]["role"] == "motor2", "SERVO2 motor mapping should be detected")
    assert_true(mapping["C3"]["role"] == "rc_passthrough", "passthrough role should be detected")
    assert_true(mapping["C4"]["role"] == "disabled", "disabled role should be detected")


def test_copter_output_mapping_handles_motor9_to_motor12_and_tilt_roles():
    params = {
        "SERVO1_FUNCTION": 41,
        "SERVO2_FUNCTION": 45,
        "SERVO9_FUNCTION": 82,
        "SERVO10_FUNCTION": 83,
        "SERVO11_FUNCTION": 84,
        "SERVO12_FUNCTION": 85,
    }
    mapping = ap_common.output_mapping_from_params(params)
    assert_true(mapping["C1"]["role"] == "motor_tilt" and mapping["C1"]["category"] == "tilt", "function 41 should be tilt, not motor9")
    assert_true(mapping["C2"]["role"] == "tilt_motor_rear" and mapping["C2"]["category"] == "tilt", "function 45 should be tilt, not a normal motor")
    assert_true(mapping["C9"]["role"] == "motor9", "function 82 should map to motor9")
    assert_true(mapping["C10"]["role"] == "motor10", "function 83 should map to motor10")
    assert_true(mapping["C11"]["role"] == "motor11", "function 84 should map to motor11")
    assert_true(mapping["C12"]["role"] == "motor12", "function 85 should map to motor12")
    assert_true(ap_common.motor_channels_from_mapping(mapping, ["C1", "C2"]) == ["C9", "C10", "C11", "C12"], "tilt outputs should not be treated as motor channels")


def test_motor_output_metrics_are_mapping_aware():
    tables = {
        "RCOU": pd.DataFrame({"TimeS": [0.0, 1.0], "C1": [1000, 1200], "C2": [1900, 1800], "C3": [1000, 2000]}),
        "PARM": pd.DataFrame({"Name": ["SERVO1_FUNCTION", "SERVO2_FUNCTION", "SERVO3_FUNCTION"], "Value": [33, 34, 1]}),
    }
    metrics = compute_metrics(tables)
    channels = metrics["health"]["motor_outputs"]["motor_channels"]
    assert_true(channels == ["C1", "C2"], "only mapped motor channels should be treated as motors")


def test_windowed_tables_preserve_boot_only_parameter_context():
    full_tables = {
        "PARM": pd.DataFrame({
            "TimeS": [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8],
            "Name": [
                "SERVO1_FUNCTION", "SERVO2_FUNCTION", "SERVO3_FUNCTION",
                "RCMAP_ROLL", "RCMAP_PITCH", "RCMAP_THROTTLE", "RCMAP_YAW", "RC2_TRIM",
            ],
            "Value": [33, 34, 1, 1, 3, 4, 2, 1490],
        }),
        "RCOU": pd.DataFrame({"TimeS": [20.0, 21.0], "C1": [1900, 1910], "C2": [1700, 1710], "C3": [1950, 1960]}),
        "RCIN": pd.DataFrame({"TimeS": [20.0, 21.0], "C1": [1500, 1500], "C2": [1490, 1710], "C4": [1100, 1900]}),
        "ATT": pd.DataFrame({"TimeS": [20.0, 21.0], "DesYaw": [0.0, 5.0], "Yaw": [0.0, 4.0]}),
        "RATE": pd.DataFrame({"TimeS": [20.0, 21.0], "YDes": [0.0, 30.0], "Y": [0.0, 25.0], "YOut": [0.0, 0.2]}),
    }
    index = {"messages": {name: {} for name in full_tables}, "parameters": ap_common.params_from_tables(full_tables), "errors": [], "events": [], "modes": []}

    window_tables = ap_common.filter_tables_by_time(full_tables, start_s=20.0, end_s=22.0)
    mapping = ap_common.output_mapping_from_tables(window_tables, parameters=index["parameters"])
    metrics = compute_metrics(window_tables, analysis_window={"start_s": 20.0, "end_s": 22.0})
    rcin = summarize_rcin(window_tables, index)
    command_response = build_command_response_investigation(window_tables, index, axes=("yaw",))

    assert_true(len(window_tables["PARM"]) == len(full_tables["PARM"]), "boot-only PARM rows should survive dynamic telemetry windowing")
    assert_true(ap_common.motor_channels_from_mapping(mapping, ["C1", "C2", "C3"]) == ["C1", "C2"], "windowed motor outputs should still use SERVOx_FUNCTION mapping")
    assert_true(metrics["health"]["motor_outputs"]["motor_channels"] == ["C1", "C2"], "windowed metrics should not fall back to generic output channels when PARM exists")
    confidence_reasons = "\n".join(metrics["confidence"]["reasons"])
    assert_true("Output mapping could not be confirmed" not in confidence_reasons, "windowed metrics should not warn about generic output mapping when full-log PARM exists")
    assert_true(rcin["mapping"]["axes"]["yaw"]["channel"] == 2, "windowed RCIN summary should use RCMAP_YAW from full-log parameters")
    assert_true(rcin["mapping"]["limitation"] is None, "complete full-log RCMAP parameters should avoid fallback mapping warnings")
    command_context = "\n".join(item.get("detail", "") for item in command_response["context"])
    assert_true("RCIN yaw channel 2" in command_context, "windowed command-vs-response should use mapped RCMAP_YAW channel")

    with tempfile.TemporaryDirectory() as tmp:
        plots = make_targeted_plots_from_tables(window_tables, "motor_esc_issue", Path(tmp), index=index)
        motor_plot = next(path for path in plots if path.endswith("motor_outputs_symptom.html"))
        html = Path(motor_plot).read_text(encoding="utf-8")
    assert_true("C1 motor1" in html and "C2 motor2" in html, "windowed mapped motor-output plot should label motor channels from SERVOx_FUNCTION")
    assert_true("C3 rc_passthrough" not in html, "windowed mapped motor-output plot should not treat passthrough outputs as motors")


def test_motor_output_metrics_include_rco2_and_rco3_channels():
    tables = {
        "RCOU": pd.DataFrame({"TimeS": [0.0, 1.0], "C1": [1200, 1300]}),
        "RCO2": pd.DataFrame({"TimeS": [0.0, 1.0], "C15": [1900, 1950]}),
        "RCO3": pd.DataFrame({"TimeS": [0.0, 1.0], "C19": [1000, 1050]}),
        "PARM": pd.DataFrame({"Name": ["SERVO15_FUNCTION", "SERVO19_FUNCTION"], "Value": [82, 83]}),
    }
    metrics = compute_metrics(tables)
    motor_outputs = metrics["health"]["motor_outputs"]
    assert_true("C15" in motor_outputs["channels"] and "C19" in motor_outputs["channels"], "RCO2/RCO3 channels should be included")
    assert_true(motor_outputs["motor_channels"] == ["C15", "C19"], "mapped high-numbered motor outputs should be motor channels")
    assert_true(motor_outputs["saturation"]["C15"]["pct_high_ge_1900"] == 100.0, "RCO2 saturation should be summarized")
    assert_true(motor_outputs["saturation"]["C19"]["pct_low_le_1100"] == 100.0, "RCO3 saturation should be summarized")


def test_parameter_context_uses_yaml_selectors_and_servo_wildcards():
    index = {
        "parameters": {
            "FRAME_CLASS": 1,
            "SERVO1_FUNCTION": 33,
            "SERVO2_FUNCTION": 34,
            "MOT_SPIN_MIN": 0.12,
            "INS_HNTCH_ENABLE": 0,
            "ATC_RAT_YAW_P": 0.18,
        },
        "parameter_defaults": {
            "FRAME_CLASS": 1,
            "INS_HNTCH_ENABLE": 0,
        },
    }
    context = select_relevant_parameters("yaw_misbehaviour", index=index)
    names = [item["name"] for item in context["selected"]]
    assert_true("SERVO1_FUNCTION" in names and "SERVO2_FUNCTION" in names, "SERVO*_FUNCTION should expand to present servo parameters")
    assert_true("ATC_RAT_YAW_D" in context["missing"], "absent exact yaw parameter should be listed as missing")
    flagged = {item["name"]: item["reasons"] for item in context["default_or_zero"]}
    assert_true(flagged["INS_HNTCH_ENABLE"] == ["zero", "matches_default"], "zero/default parameters should be flagged separately from missing")
    assert_true("SERVO3_FUNCTION" not in context["missing"], "wildcard selectors should not generate noisy missing servo slots")
    servo = next(item for item in context["selected"] if item["name"] == "SERVO1_FUNCTION")
    assert_true(servo["metadata_missing"] is False, "SERVO wildcard metadata should enrich concrete SERVO function parameters")
    assert_true(servo["enum_value"] == "Motor1", "SERVO function metadata should decode known motor function values")


def test_stream_index_preserves_parameter_defaults_for_context():
    rows, index, _stats = ap_common.collect_dataflash([
        FakeMsg("PARM", TimeUS=0, Name="INS_HNTCH_ENABLE", Value=0, Default=0),
        FakeMsg("PARM", TimeUS=1000000, Name="ATC_RAT_YAW_P", Value=0.2, Default=0.18),
    ], include=[])
    assert_true(rows == {}, "index-only collection should not store rows")
    context = select_relevant_parameters("yaw_misbehaviour", index=index)
    notch = next(item for item in context["selected"] if item["name"] == "INS_HNTCH_ENABLE")
    yaw_p = next(item for item in context["selected"] if item["name"] == "ATC_RAT_YAW_P")
    assert_true(notch["is_default"] is True and notch["is_zero"] is True, "streamed PARM defaults should be available")
    assert_true(yaw_p["is_default"] is False, "non-default parameter values should be distinguished")


def test_manifest_includes_symptom_parameter_context():
    index = {
        "messages": {"ATT": {}, "RATE": {}, "PARM": {}},
        "parameters": {"FRAME_CLASS": 1, "SERVO1_FUNCTION": 33, "ATC_RAT_YAW_P": 0.2},
        "parameter_defaults": {"FRAME_CLASS": 1},
    }
    manifest = build_manifest_from_index(index, "yaw issue", "flight.bin")
    params = manifest["parameter_context"]
    names = [item["name"] for item in params["selected"]]
    assert_true(params["symptom_class"] == "yaw_misbehaviour", "manifest parameter context should match classified symptom")
    assert_true("SERVO1_FUNCTION" in names, "manifest should include selected servo function context")
    assert_true("MOT_SPIN_MIN" in params["missing"], "manifest should list missing exact relevant parameters")
    assert_true(params["note"].startswith("Parameter values are context"), "manifest should not turn parameters into tuning advice")
    assert_true("metadata_caveat" in params and "may not exactly match" in params["metadata_caveat"], "manifest parameter context should include metadata caveat")


def test_param_lookup_known_parameter_returns_metadata():
    index = {"parameters": {"WP_YAW_BEHAVIOR": 2}, "parameter_defaults": {"WP_YAW_BEHAVIOR": 1}}
    with tempfile.TemporaryDirectory() as tmp:
        index_path = Path(tmp) / "index.json"
        index_path.write_text(json.dumps(index), encoding="utf-8")
        result = lookup_parameters(index_path=index_path, names="WP_YAW_BEHAVIOR")
    entry = result["parameters"][0]
    assert_true(entry["metadata_missing"] is False, "known parameter should return metadata")
    assert_true(entry["display_name"] == "Yaw behaviour during missions", "known parameter should include display name")
    assert_true(entry["enum_value"] == "Face next waypoint except RTL", "known enum value should be decoded")
    assert_true("metadata_caveat" in result and "may not exactly match" in result["metadata_caveat"], "lookup result should always include metadata caveat")


def test_param_lookup_unknown_parameter_preserves_logged_value():
    index = {"parameters": {"MY_CUSTOM_PARAM": 42}, "parameter_defaults": {"MY_CUSTOM_PARAM": 0}}
    with tempfile.TemporaryDirectory() as tmp:
        index_path = Path(tmp) / "index.json"
        index_path.write_text(json.dumps(index), encoding="utf-8")
        result = lookup_parameters(index_path=index_path, names="MY_CUSTOM_PARAM")
    entry = result["parameters"][0]
    assert_true(entry["logged_value"] == 42, "unknown parameter should preserve logged value")
    assert_true(entry["metadata_missing"] is True, "unknown parameter should be marked metadata_missing")
    assert_true(entry["metadata_caveat"], "unknown lookup should still include caveat")


def test_mission_planner_style_param_file_is_parsed():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "vehicle.param"
        path.write_text("SERVO1_FUNCTION,33\nRCMAP_YAW,4\n", encoding="utf-8")
        context = parse_param_file(path)
    assert_true(context["format_detected"] == "comma_separated", f"unexpected param format: {context}")
    assert_true(context["parameters"]["SERVO1_FUNCTION"] == 33.0, "Mission Planner comma params should parse numeric values")
    assert_true(context["parameters"]["RCMAP_YAW"] == 4.0, "Mission Planner comma params should parse RCMAP")


def test_qgc_style_param_file_is_parsed():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "vehicle.params"
        path.write_text("# Vehicle-Id Component-Id Name Value Type\n1 1 SERVO1_FUNCTION 33 6\n1\t1\tRCMAP_YAW\t2\t6\n", encoding="utf-8")
        context = parse_param_file(path)
    assert_true(context["format_detected"] == "qgroundcontrol", f"unexpected QGC format: {context}")
    assert_true(context["parameters"]["SERVO1_FUNCTION"] == 33.0, "QGC params should parse SERVO function")
    assert_true(context["parameters"]["RCMAP_YAW"] == 2.0, "QGC params should parse RCMAP")


def test_mavproxy_name_value_param_file_is_parsed():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "vehicle.param"
        path.write_text("SERVO1_FUNCTION 33\nRCMAP_YAW 3\n", encoding="utf-8")
        context = parse_param_file(path)
    assert_true(context["format_detected"] == "name_value", f"unexpected MAVProxy format: {context}")
    assert_true(context["parameters"]["SERVO1_FUNCTION"] == 33.0, "NAME VALUE params should parse numeric values")


def test_external_param_conflict_preserves_logged_value_and_warns():
    external = {"parameters": {"WP_YAW_BEHAVIOR": 3, "MOT_YAW_HEADROOM": 200}, "source_file": "vehicle.param", "format_detected": "name_value", "warnings": []}
    merged = merge_external_parameters({"parameters": {"WP_YAW_BEHAVIOR": 1}}, external)
    assert_true(merged["parameters"]["WP_YAW_BEHAVIOR"] == 1, "logged PARM should remain primary on conflict")
    assert_true(merged["parameters"]["MOT_YAW_HEADROOM"] == 200, "external params should supplement missing log params")
    assert_true(merged["parameter_conflicts"][0]["name"] == "WP_YAW_BEHAVIOR", "conflicts should be explicit")
    manifest = build_manifest_from_index({"messages": {"ATT": {}, "RATE": {}}, "parameters": {"WP_YAW_BEHAVIOR": 1}, "errors": [], "events": [], "modes": []}, "yaw issue", "flight.bin", external_parameter_context=external)
    assert_true(manifest["parameter_conflicts"][0]["external_value"] == 3, "manifest should report external/log parameter conflicts")
    assert_true("Log PARM" in manifest["parameter_source_precedence"], "manifest should state logged parameter precedence")


def test_external_servo_function_enables_motor_mapping_without_log_parm():
    tables = {"RCOU": pd.DataFrame({"TimeS": [0.0, 1.0], "C1": [1200, 1300], "C2": [1300, 1400]})}
    external = {"parameters": {"SERVO1_FUNCTION": 33, "SERVO2_FUNCTION": 34}, "source_file": "vehicle.param", "format_detected": "name_value", "warnings": []}
    merged = merge_external_parameters({}, external)
    mapping = ap_common.output_mapping_from_tables(tables, parameters=merged["parameters"])
    metrics = compute_metrics(tables, parameters=merged["parameters"])
    assert_true(ap_common.motor_channels_from_mapping(mapping, ["C1", "C2"]) == ["C1", "C2"], "external SERVOx_FUNCTION should enable motor mapping")
    assert_true(metrics["health"]["motor_outputs"]["motor_channels"] == ["C1", "C2"], "metrics should use external motor mapping when log PARM is missing")


def test_external_rcmap_yaw_enables_rcin_mapping_without_log_parm():
    tables = {"RCIN": pd.DataFrame({"TimeS": [0.0, 1.0], "C2": [1500, 1600], "C4": [1500, 1500]})}
    external = {"parameters": {"RCMAP_YAW": 2}, "source_file": "vehicle.param", "format_detected": "name_value", "warnings": []}
    merged = merge_external_parameters({}, external)
    summary = summarize_rcin(tables, merged["index"])
    assert_true(summary["mapping"]["axes"]["yaw"]["channel"] == 2, "external RCMAP_YAW should set yaw channel")
    assert_true(summary["axes"]["yaw"]["field"] == "C2", "RCIN summary should use external yaw mapping")


def test_param_lookup_symptom_returns_enriched_relevant_parameters():
    index = {"parameters": {"WP_YAW_BEHAVIOR": 3, "ATC_RATE_Y_MAX": 120, "MOT_YAW_HEADROOM": 200}, "parameter_defaults": {}}
    with tempfile.TemporaryDirectory() as tmp:
        index_path = Path(tmp) / "index.json"
        index_path.write_text(json.dumps(index), encoding="utf-8")
        result = lookup_parameters(index_path=index_path, symptom="yaw_misbehaviour")
    names = {entry["name"] for entry in result["parameters"]}
    wp = next(entry for entry in result["parameters"] if entry["name"] == "WP_YAW_BEHAVIOR")
    assert_true({"WP_YAW_BEHAVIOR", "ATC_RATE_Y_MAX", "MOT_YAW_HEADROOM"}.issubset(names), "symptom lookup should include relevant logged yaw parameters")
    assert_true(wp["enum_value"] == "Face along GPS course", "symptom lookup should enrich relevant parameters")
    assert_true(result["symptom_context"]["selected"][0]["metadata_caveat"], "symptom context should include metadata caveat")


def test_generic_bitmask_decode_returns_enabled_labels():
    decoded = decode_bitmask(5, {"0": "First", "1": "Second", "2": "Third"})
    assert_true(decoded == ["First", "Third"], f"unexpected bitmask decode: {decoded}")


def test_unknown_bitmask_metadata_does_not_crash():
    assert_true(decode_bitmask(7, None) == [], "missing bitmask metadata should decode to an empty list")
    enriched = enrich_parameter_entry({"name": "UNKNOWN_BITMASK", "value": 7}, metadata={"caveat": "test caveat", "parameters": []})
    assert_true(enriched["metadata_missing"] is True, "unknown metadata should be marked missing")
    assert_true("decoded_bits" not in enriched, "unknown metadata should not invent decoded bits")


def test_log_bitmask_missing_pid_bit_reports_pid_guidance():
    index = {
        "messages": {"ATT": {}, "RATE": {}},
        "parameters": {"LOG_BITMASK": 65535 - 4096},
        "parameter_defaults": {},
    }
    with tempfile.TemporaryDirectory() as tmp:
        index_path = Path(tmp) / "index.json"
        index_path.write_text(json.dumps(index), encoding="utf-8")
        result = lookup_parameters(index_path=index_path, names="LOG_BITMASK", symptom="yaw_misbehaviour")
    entry = result["parameters"][0]
    missing = entry["possibly_missing_for_symptom"]
    assert_true("PID" not in entry["decoded_bits"], "synthetic LOG_BITMASK should not decode PID bit")
    assert_true(any(item["message"] == "PIDY" and "PID" in item["absent_logging_families"] for item in missing), f"missing PID guidance should mention PIDY: {missing}")
    assert_true("Bit definitions may vary" in entry["bitmask_caveat"], "bitmask output should include caveat")
    assert_true("Bit definitions may vary" in result["bitmask_caveat"], "lookup output should include top-level bitmask caveat")


def test_manifest_next_evidence_uses_log_bitmask_pid_context():
    index = {"messages": {"ATT": {}, "RATE": {}}, "parameters": {"LOG_BITMASK": 65535 - 4096}, "errors": [], "events": [], "modes": []}
    manifest = build_manifest_from_index(index, "yaw issue", "flight.bin")
    hints = "\n".join(manifest["next_evidence_gathering"]["logging_profile_hints"])
    assert_true("LOG_BITMASK" in hints and "PID logging" in hints, "missing PIDY guidance should mention LOG_BITMASK/PID logging when available")


def test_parameter_metadata_fetch_compactor_uses_machine_readable_shape():
    raw = {
        "Copter": {
            "WP_YAW_BEHAVIOR": {
                "DisplayName": "Yaw behaviour during missions",
                "Description": "Determines how yaw is controlled in missions",
                "Values": {"0": "Never change yaw", "1": "Face next waypoint"},
                "User": "Standard",
            },
            "LOG_BITMASK": {
                "DisplayName": "Log bitmask",
                "Description": "Bitmap of onboard log types",
                "Bitmask": {"0": "Fast Attitude"},
                "User": "Standard",
            },
        },
        "ATC_": {
            "ATC_RATE_Y_MAX": {
                "DisplayName": "Maximum yaw rate",
                "Description": "Maximum yaw rate target",
                "Units": "deg/s",
                "Range": {"low": "0", "high": "500"},
                "User": "Standard",
            },
        },
        "RCMAP_": {
            "RCMAP_YAW": {
                "DisplayName": "Yaw channel",
                "Description": "Yaw RC input channel",
                "Range": {"low": "1", "high": "16"},
            }
        },
    }
    compact = compact_from_raw(raw, vehicle="ArduCopter", source_url="https://example.test/apm.pdef.json", docs_url="https://example.test/params.html")
    entries = {entry["name"]: entry for entry in compact["parameters"]}
    assert_true(entries["WP_YAW_BEHAVIOR"]["values"]["1"] == "Face next waypoint", "compactor should retain enum values from apm.pdef.json")
    assert_true(entries["LOG_BITMASK"]["bitmask"]["0"] == "Fast Attitude", "compactor should retain bitmask values")
    assert_true(entries["ATC_RATE_Y_MAX"]["range"] == [0.0, 500.0], "compactor should normalize range dictionaries")
    assert_true("RCMAP_*" in entries, "compactor should synthesize wildcard family metadata")
    assert_true("may not exactly match" in compact["caveat"], "compacted web metadata should retain firmware caveat")


def test_parameter_context_yaw_includes_mission_yaw_parameters():
    index = {
        "parameters": {
            "WP_YAW_BEHAVIOR": 2,
            "WPNAV_SPEED": 500,
            "WPNAV_ACCEL": 250,
            "WPNAV_ACCEL_C": 100,
        },
    }
    context = select_relevant_parameters("yaw_misbehaviour", index=index)
    names = {item["name"] for item in context["selected"]}

    assert_true("WP_YAW_BEHAVIOR" in names, "yaw parameter context should include mission yaw behaviour")
    assert_true({"WPNAV_SPEED", "WPNAV_ACCEL", "WPNAV_ACCEL_C"}.issubset(names), "yaw parameter context should include navigation speed/accel context")


def test_parameter_context_mission_yaw_includes_rate_accel_and_headroom():
    index = {
        "parameters": {
            "ATC_RATE_Y_MAX": 18000,
            "ATC_ACCEL_Y_MAX": 36000,
            "MOT_YAW_HEADROOM": 200,
        },
    }
    context = select_relevant_parameters("yaw_misbehaviour", index=index)
    names = {item["name"] for item in context["selected"]}

    assert_true({"ATC_RATE_Y_MAX", "ATC_ACCEL_Y_MAX", "MOT_YAW_HEADROOM"}.issubset(names), "mission-yaw context should include yaw rate/accel limits and motor yaw headroom")


def test_manifest_questions_include_mission_yaw_context_for_auto_symptom():
    index = {"messages": {"ATT": {}, "RATE": {}, "MODE": {}}, "parameters": {"WP_YAW_BEHAVIOR": 2}, "errors": [], "events": [], "modes": []}
    manifest = build_manifest_from_index(index, "yaw problem in AUTO mission", "flight.bin")
    questions = "\n".join(manifest["questions_to_answer"])

    assert_true("Is the yaw issue mostly in AUTO/mission?" in questions, "mission yaw manifest should ask whether symptom is mission-specific")
    assert_true("Is RATE.YDes unusually high or continuous in AUTO?" in questions, "mission yaw manifest should ask about commanded yaw demand")
    assert_true("Does WP_YAW_BEHAVIOR explain mission yaw demands?" in questions, "mission yaw manifest should ask about mission yaw behaviour")


def test_mode_compare_ranks_auto_worse_for_yaw_tracking_with_numeric_modes():
    tables = {
        "MODE": pd.DataFrame({"TimeS": [0.0, 10.0, 20.0], "Mode": [3, 16, 3]}),
        "ARM": pd.DataFrame({"TimeS": [0.0], "Armed": [1]}),
        "CTUN": pd.DataFrame({"TimeS": [0.0, 1.0, 2.0, 11.0, 12.0, 21.0, 22.0], "Alt": [0.0, 0.1, 0.1, 3.0, 3.1, 3.0, 3.2], "ThO": [0.08, 0.10, 0.10, 0.42, 0.43, 0.44, 0.45]}),
        "ATT": pd.DataFrame({"TimeS": [1.0, 2.0, 11.0, 12.0, 21.0, 22.0], "DesYaw": [0, 0, 0, 0, 0, 0], "Yaw": [80, 90, 2, 3, 60, 70]}),
        "RATE": pd.DataFrame({"TimeS": [1.0, 2.0, 11.0, 12.0, 21.0, 22.0], "YDes": [0, 0, 0, 0, 0, 0], "Y": [90, 100, 2, 3, 70, 80], "YOut": [0.9, 0.95, 0.1, 0.1, 0.85, 0.9]}),
        "RCOU": pd.DataFrame({"TimeS": [1.0, 2.0, 11.0, 12.0, 21.0, 22.0], "C1": [1950, 1960, 1500, 1510, 1900, 1910], "C2": [1500, 1500, 1500, 1510, 1500, 1510]}),
        "PARM": pd.DataFrame({"Name": ["SERVO1_FUNCTION", "SERVO2_FUNCTION"], "Value": [33, 34]}),
    }

    result = compare_modes(
        tables,
        symptom="yaw_misbehaviour",
        compare_modes=["3", "16"],
        active_flight_only=True,
        index={"parameters": {"SERVO1_FUNCTION": 33, "SERVO2_FUNCTION": 34}},
    )

    assert_true(result["decoded_modes"] == ["AUTO", "POSHOLD"], "numeric mode ids should decode in mode comparison")
    assert_true(result["ranking"][0]["decoded_mode"] == "AUTO", "AUTO should rank worse for yaw tracking")
    auto = next(item for item in result["mode_comparisons"] if item["decoded_mode"] == "AUTO")
    poshold = next(item for item in result["mode_comparisons"] if item["decoded_mode"] == "POSHOLD")
    assert_true(auto["metrics"]["rate_y_error"]["p95_abs"] > poshold["metrics"]["rate_y_error"]["p95_abs"], "AUTO yaw rate error should be larger than POSHOLD")
    assert_true(auto["intervals_used"][0]["start_s"] >= 21.0, "active-flight filtering should exclude AUTO ground rows")
    assert_true(auto["window_quality"]["ground_spool_rows_included"] is True, "mode comparison should record ground/spool contamination")


def test_manifest_recommends_mode_compare_for_mission_symptom():
    index = {"messages": {"ATT": {}, "RATE": {}, "MODE": {}, "CTUN": {}, "RCOU": {}}, "parameters": {}, "errors": [], "events": [], "modes": []}
    manifest = build_manifest_from_index(index, "yaw problem during missions", "flight.bin")

    assert_true(any("ap_log_mode_compare.py" in cmd for cmd in manifest["recommended_next_commands"]), "manifest should recommend mode comparison for mission/manual symptoms")


def test_event_markers_collect_mode_err_ev_msg():
    tables = {
        "MODE": pd.DataFrame({"TimeS": [10.0], "Mode": ["LOITER"]}),
        "ERR": pd.DataFrame({"TimeS": [12.0], "Subsys": [16], "ECode": [2]}),
        "EV": pd.DataFrame({"TimeS": [14.0], "Id": [10]}),
    }
    markers = ap_common.event_markers_from_tables(tables)
    labels = [m["label"] for m in markers]
    assert_true(any("LOITER" in x for x in labels), "mode marker should be present")
    assert_true(any("ERR" in x for x in labels), "ERR marker should be present")
    assert_true(any("EV" in x for x in labels), "EV marker should be present")


def test_mode_segments_are_derived_from_mode_rows():
    tables = {"MODE": pd.DataFrame({"TimeS": [0.0, 10.0, 25.0], "Mode": ["STABILIZE", "LOITER", "RTL"]})}
    segments = ap_common.mode_segments_from_tables(tables, log_end_s=40.0)
    assert_true(segments[1]["mode"] == "LOITER", "second segment should be LOITER")
    assert_true(segments[1]["start_s"] == 10.0 and segments[1]["end_s"] == 25.0, "segment bounds should come from next mode")


def test_validate_marks_non_copter_scope_as_partial():
    index = {"messages": {"ATT": {}, "RATE": {}}, "parameters": {}, "vehicle": "ArduPlane likely"}
    scope = ap_common.vehicle_scope(index)
    assert_true(scope["primary_vehicle"] == "Plane", "vehicle scope should detect Plane")
    assert_true(scope["copter_heuristics_confidence"] == "low", "Copter-specific heuristics should be low confidence for Plane")


def test_vibe_clip_variants_are_detected():
    tables = {
        "VIBE": pd.DataFrame({
            "TimeS": [0.0, 1.0, 2.0],
            "VibeX": [10.0, 35.0, 20.0],
            "VibeY": [5.0, 7.0, 8.0],
            "VibeZ": [4.0, 5.0, 6.0],
            "Clip0": [0, 0, 3],
            "Clip1": [0, 2, 2],
            "Clip2": [0, 0, 0],
        })
    }
    metrics = compute_metrics(tables)
    vibration = metrics["health"]["vibration"]
    assert_true("Clip0" in vibration and "Clip1" in vibration, "metrics should summarize Clip0/Clip1 fields")
    assert_true(vibration["clip_delta"]["Clip0"] == 3.0, "metrics should report Clip0 delta")
    assert_true(vibration["units"]["VibeX"]["max"] == "m/s/s", "VIBE metrics should carry acceleration units")
    assert_true(vibration["clip_delta_units"]["Clip0"] == "count", "clip deltas should carry count units")

    index = {"messages": {"VIBE": {}}, "errors": [], "events": [], "modes": []}
    findings, _context, _checked, missing, missing_strongly, _missing_optional = diagnose_by_class("vibration_issue", tables, index)
    evidence = "\n".join("\n".join(f.get("evidence", [])) for f in findings)
    assert_true("VIBE.Clip0 increased by 3" in evidence, "diagnosis should include Clip0 clipping evidence")
    assert_true("RATE" in missing_strongly, "diagnosis should still report missing strongly recommended symptom messages")


def test_high_vibration_outside_symptom_window_is_context_not_finding():
    full_tables = {
        "VIBE": pd.DataFrame({"TimeS": [0, 1, 2, 10, 11, 12], "VibeX": [55, 60, 58, 8, 9, 8], "VibeY": [5, 5, 5, 4, 4, 4], "VibeZ": [4, 4, 4, 3, 3, 3]}),
        "ATT": pd.DataFrame({"TimeS": [10, 11, 12], "DesYaw": [0, 0, 0], "Yaw": [0, 25, 35]}),
        "RATE": pd.DataFrame({"TimeS": [10, 11, 12], "YDes": [0, 0, 0], "Y": [0, 20, 30], "YOut": [0.1, 0.2, 0.2]}),
    }
    window_tables = ap_common.filter_tables_by_time(full_tables, start_s=10.0, end_s=12.0)
    assessment = build_vibration_assessment(full_tables, "yaw_misbehaviour", window_tables=window_tables, analysis_window={"start_s": 10.0, "end_s": 12.0})
    index = {"messages": {"ATT": {}, "RATE": {}, "VIBE": {}}, "errors": [], "events": [], "modes": []}
    findings, _context, checked, *_ = diagnose_by_class("attitude_rate_issue", window_tables, index, vibration_assessment=assessment)
    causes = "\n".join(f.get("possible_cause", "") for f in findings)
    checked_text = "\n".join(c.get("result", "") for c in checked)
    assert_true(assessment["vibration_context"]["above_warning_threshold"], "whole-log high VIBE should remain context")
    assert_true(not assessment["vibration_relevance_to_symptom"]["evidence"], "outside-window high VIBE should not be symptom evidence")
    assert_true("Vibration/clipping is relevant" not in causes, "outside-window high VIBE should not create a vibration finding")
    assert_true("whole-log max axis" in checked_text, "diagnosis should explain that whole-log VIBE was retained as context")


def test_high_vibration_during_symptom_window_becomes_supporting_evidence():
    full_tables = {
        "VIBE": pd.DataFrame({"TimeS": [0, 1, 2, 10, 11, 12], "VibeX": [8, 9, 8, 45, 55, 50], "VibeY": [5, 5, 5, 4, 4, 4], "VibeZ": [4, 4, 4, 3, 3, 3]}),
        "ATT": pd.DataFrame({"TimeS": [10, 11, 12], "DesYaw": [0, 0, 0], "Yaw": [0, 25, 35]}),
        "RATE": pd.DataFrame({"TimeS": [10, 11, 12], "YDes": [0, 0, 0], "Y": [0, 20, 30], "YOut": [0.1, 0.2, 0.2]}),
    }
    window_tables = ap_common.filter_tables_by_time(full_tables, start_s=10.0, end_s=12.0)
    assessment = build_vibration_assessment(full_tables, "yaw_misbehaviour", window_tables=window_tables, analysis_window={"start_s": 10.0, "end_s": 12.0})
    index = {"messages": {"ATT": {}, "RATE": {}, "VIBE": {}}, "errors": [], "events": [], "modes": []}
    findings, _context, _checked, *_ = diagnose_by_class("attitude_rate_issue", window_tables, index, vibration_assessment=assessment)
    evidence = "\n".join("\n".join(f.get("evidence", [])) for f in findings)
    assert_true("within analysis window" in evidence, "windowed high VIBE should become supporting evidence")


def test_missing_vibe_limits_vibration_confidence_without_guessing():
    assessment = build_vibration_assessment({}, "yaw_misbehaviour", window_tables={}, analysis_window={"start_s": 1.0, "end_s": 2.0})
    index = {"messages": {"ATT": {}, "RATE": {}}, "errors": [], "events": [], "modes": []}
    findings, _context, checked, _missing_required, _missing_strongly, missing_optional = diagnose_yaw({}, index, vibration_assessment=assessment)
    causes = "\n".join(f.get("possible_cause", "") for f in findings)
    checked_text = "\n".join(c.get("result", "") for c in checked)
    assert_true("VIBE" in missing_optional, "missing VIBE should remain optional evidence for yaw diagnosis")
    assert_true("Vibration/clipping is relevant" not in causes, "missing VIBE must not create a guessed vibration conclusion")
    assert_true("VIBE missing" in checked_text, "missing VIBE should be explicit in checked output")


def test_non_yaw_symptoms_get_targeted_findings():
    index = {
        "messages": {"GPS": {}, "XKF4": {}, "MODE": {}, "MSG": {}, "EV": {}, "ERR": {}},
        "errors": [],
        "events": [],
        "modes": [],
    }
    tables = {
        "GPS": pd.DataFrame({
            "TimeS": [0.0, 1.0, 2.0],
            "Status": [3, 2, 3],
            "NSats": [14, 9, 10],
            "HDop": [1.2, 2.5, 1.7],
        }),
        "XKF4": pd.DataFrame({
            "TimeS": [0.0, 1.0, 2.0],
            "SV": [0.2, 1.2, 0.4],
            "SP": [0.2, 0.8, 0.4],
            "SH": [0.2, 0.4, 0.5],
            "SM": [0.2, 0.4, 1.3],
        }),
    }
    findings, _context, checked, missing, missing_strongly, _missing_optional = diagnose_by_class("ekf_gps_issue", tables, index)
    causes = "\n".join(f.get("possible_cause", "") for f in findings)
    evidence = "\n".join("\n".join(f.get("evidence", [])) for f in findings)
    assert_true("GPS/EKF" in causes, "EKF/GPS symptom should produce a targeted GPS/EKF finding")
    assert_true("GPS.Status minimum=2" in evidence, "GPS fix status should be used as evidence")
    assert_true("SV max=1.20" in evidence and "SM max=1.30" in evidence, "EKF test ratios should be used as evidence")
    assert_true("XKF1" in missing_strongly and "XKF3" in missing_strongly, "missing data should reflect the selected symptom class")
    assert_true(checked, "diagnosis should record checks that were not supported")


def test_toilet_bowling_prefers_ekf_gps_when_navigation_context_is_present():
    symptom = ap_common.classify_symptom("toilet bowling in loiter after a GPS glitch")
    assert_true(symptom == "ekf_gps_issue", f"expected ekf_gps_issue, got {symptom}")


def test_yaml_aliases_drive_symptom_classification():
    symptom = ap_common.classify_symptom("yaw twitching on takeoff")
    assert_true(symptom == "yaw_misbehaviour", f"expected yaw_misbehaviour from YAML alias, got {symptom}")


def test_rc_prearm_aliases_drive_symptom_classification():
    symptom = ap_common.classify_symptom("it would not arm")
    assert_true(symptom == "rc_failsafe_prearm_issue", f"expected rc_failsafe_prearm_issue, got {symptom}")
    symptom = ap_common.classify_symptom("radio failsafe in flight")
    assert_true(symptom == "rc_failsafe_prearm_issue", f"expected rc_failsafe_prearm_issue, got {symptom}")


def test_explicit_compass_and_baro_aliases_drive_symptom_classification():
    symptom = ap_common.classify_symptom("compass interference during loiter")
    assert_true(symptom == "compass_yaw_source_issue", f"expected compass_yaw_source_issue, got {symptom}")
    symptom = ap_common.classify_symptom("rangefinder altitude jumps near the ground")
    assert_true(symptom == "baro_rangefinder_altitude_issue", f"expected baro_rangefinder_altitude_issue, got {symptom}")


def test_rc_prearm_required_messages_and_parameters():
    missing_required, _missing_strongly, _missing_optional = diagnosis_missing({"messages": {}}, "rc_failsafe_prearm_issue")
    assert_true(missing_required == ["MSG", "ERR"], f"MSG/ERR should be required for rc_failsafe_prearm_issue, got {missing_required}")

    manifest = build_manifest_from_index({"messages": {}, "parameters": {}, "errors": [], "events": [], "modes": []}, "pre-arm error", "flight.bin")
    params = manifest["parameter_context"]
    assert_true("LOG_DISARMED" in params["selectors"], "LOG_DISARMED should be in parameter selectors")
    assert_true("LOG_DISARMED" in params["missing"], "LOG_DISARMED should appear as missing when not logged")
    plan = manifest["next_evidence_gathering"]
    assert_true(plan["recommended_next_step_type"] == "ground_test", "pre-arm evidence should be a ground capture, not a flight requirement")
    assert_true(plan["safe_to_request_flight"] is False, "pre-arm evidence should not request flight")


def test_rc_prearm_diagnosis_routes_context_without_bypassing_checks():
    index = {
        "messages": {"MSG": {}, "ERR": {}, "EV": {}, "ARM": {}, "MODE": {}, "RCIN": {}, "PARM": {}, "BAT": {}, "POWR": {}, "GPS": {}, "XKF4": {}, "MAG": {}},
        "errors": [{"time_s": 1.2, "subsys": 2, "ecode": 4}],
        "events": [{"time_s": 1.0, "id": 10}],
        "modes": [{"time_s": 0.0, "mode": "STABILIZE"}],
        "parameters": {
            "LOG_DISARMED": 1,
            "ARMING_CHECK": 1,
            "BRD_SAFETYENABLE": 1,
            "RCMAP_ROLL": 1,
            "RCMAP_PITCH": 2,
            "RCMAP_THROTTLE": 3,
            "RCMAP_YAW": 4,
        },
    }
    tables = {
        "MSG": pd.DataFrame({"TimeS": [0.5], "Message": ["PreArm: Hardware safety switch"]}),
        "ERR": pd.DataFrame({"TimeS": [1.2], "Subsys": [2], "ECode": [4]}),
        "ARM": pd.DataFrame({"TimeS": [1.3], "ArmState": [0], "Reason": ["prearm failed"]}),
        "MODE": pd.DataFrame({"TimeS": [0.0], "Mode": ["STABILIZE"]}),
        "RCIN": pd.DataFrame({
            "TimeS": [0.0, 1.0, 2.0],
            "C1": [1500, 1600, 1500],
            "C2": [1500, 1500, 1400],
            "C3": [1000, 1050, 1100],
            "C4": [1500, 1500, 1550],
        }),
        "BAT": pd.DataFrame({"TimeS": [0.0, 1.0], "Volt": [16.2, 15.9], "Curr": [0.1, 0.2]}),
        "POWR": pd.DataFrame({"TimeS": [0.0, 1.0], "Vcc": [5.1, 5.0], "Flags": [0, 0]}),
        "GPS": pd.DataFrame({"TimeS": [0.0, 1.0], "Status": [3, 3], "NSats": [14, 14], "HDop": [1.1, 1.0]}),
        "XKF4": pd.DataFrame({"TimeS": [0.0, 1.0], "SV": [0.2, 0.3], "SP": [0.2, 0.3], "SH": [0.2, 0.3], "SM": [0.2, 0.3]}),
        "MAG": pd.DataFrame({"TimeS": [0.0, 1.0], "MagX": [100, 101], "MagY": [20, 20], "MagZ": [300, 300]}),
    }
    findings, context, checked, missing_required, _missing_strongly, _missing_optional = diagnose_by_class("rc_failsafe_prearm_issue", tables, index)
    causes = "\n".join(f.get("possible_cause", "") for f in findings)
    evidence = "\n".join("\n".join(f.get("evidence", [])) for f in findings)
    context_text = "\n".join(c.get("detail", "") for c in context)
    checked_text = "\n".join(c.get("result", "") for c in checked)
    recommendation_text = "\n".join("\n".join(f.get("recommended_checks", [])) for f in findings)

    assert_true(missing_required == [], "MSG/ERR should not be missing in synthetic rc/prearm diagnosis")
    assert_true("Pre-arm, arming, or failsafe timeline evidence" in causes, "rc/prearm diagnosis should produce timeline finding")
    assert_true("PreArm: Hardware safety switch" in evidence, "MSG pre-arm text should be evidence")
    assert_true("RCIN roll channel" in context_text and "RCIN throttle channel" in context_text, "RCIN context should be summarized")
    assert_true("No GPS fix" in checked_text or "GPS/EKF health" in checked_text, "GPS/EKF context should be checked")
    assert_true("disable ARMING_CHECK" not in recommendation_text, "diagnosis must not recommend disabling ARMING_CHECK as a routine fix")


def test_new_yaml_alias_does_not_need_python_change():
    source = Path("references/symptom-diagnosis-map.yaml")
    data = yaml.safe_load(source.read_text(encoding="utf-8"))
    for item in data["symptom_classes"]:
        if item["name"] == "battery_power_issue":
            item["aliases"].append("pack droop")
            break
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "symptom-map.yaml"
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        symptom = ap_common.classify_symptom("pack droop under throttle", map_path=path)
    assert_true(symptom == "battery_power_issue", f"expected battery_power_issue from injected YAML alias, got {symptom}")


def test_unmatched_symptom_returns_general_investigation():
    symptom = ap_common.classify_symptom("please look at this flight")
    assert_true(symptom == "general_investigation", f"unmatched symptoms should be conservative, got {symptom}")


def test_malformed_symptom_yaml_fails_clearly():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bad-map.yaml"
        path.write_text(
            yaml.safe_dump({
                "version": 1,
                "default_class": "general_investigation",
                "symptom_classes": [{"name": "bad_issue", "aliases": ["bad"]}],
            }),
            encoding="utf-8",
        )
        try:
            load_symptom_map(path)
        except AnalysisError as exc:
            assert_true("missing required field 'required_messages'" in str(exc), f"unexpected validation error: {exc}")
        else:
            raise AssertionError("malformed symptom YAML should fail validation")


def test_edt2_status_is_used_for_motor_esc_findings():
    index = {"messages": {"EDT2": {}, "RCOU": {}, "RATE": {}, "BAT": {}}, "errors": [], "events": [], "modes": []}
    tables = {
        "EDT2": pd.DataFrame({
            "TimeS": [0.0, 1.0, 2.0],
            "Instance": [0, 0, 0],
            "Status": [0, 8, 16],
            "Stress": [1, 2, 3],
            "MaxStress": [1, 3, 4],
        })
    }
    findings, _context, _checked, _missing_required, _missing_strongly, missing_optional = diagnose_by_class("motor_esc_issue", tables, index)
    evidence = "\n".join("\n".join(f.get("evidence", [])) for f in findings)
    assert_true("EDT2 status alert/warning/error counts=0/1/1" in evidence, "EDT2 status bits should be diagnosed")
    assert_true("ESC" not in missing_optional, "EDT2 should satisfy ESC-status confirmation for motor diagnostics")


def test_escx_is_used_for_motor_esc_metrics_and_findings():
    index = {"messages": {"ESCX": {}, "RCOU": {}, "RATE": {}, "BAT": {}}, "errors": [], "events": [], "modes": []}
    tables = {
        "ESCX": pd.DataFrame({
            "TimeS": [0.0, 1.0, 2.0],
            "Instance": [0, 0, 0],
            "inpct": [15.0, 45.0, 60.0],
            "outpct": [12.0, 40.0, 59.0],
            "flags": [0, 2, 0],
            "Pwr": [20.0, 55.0, 70.0],
        })
    }
    metrics = compute_metrics(tables)
    escx = metrics["health"]["escx"]
    assert_true(escx["instances"] == [0], "ESCX instances should be summarized")
    assert_true(escx["nonzero_flags_count"] == 1, "ESCX nonzero flags should be counted")
    assert_true("ESC/ESCX/EDT2 telemetry missing" not in "\n".join(metrics["confidence"]["reasons"]), "ESCX should satisfy ESC telemetry confidence")

    findings, context, _checked, _missing_required, _missing_strongly, missing_optional = diagnose_by_class("motor_esc_issue", tables, index)
    evidence = "\n".join("\n".join(f.get("evidence", [])) for f in findings)
    context_text = "\n".join(c.get("detail", "") for c in context)
    assert_true("ESCX flags nonzero samples=1" in evidence, "ESCX flags should be diagnostic evidence")
    assert_true("ESCX inpct: min=15.00 %, max=60.00 %" in context_text, "ESCX duty cycle should be retained as context with units")
    assert_true("ESC" not in missing_optional, "ESCX should satisfy ESC-status confirmation for motor diagnostics")


def test_multi_instance_gps_battery_esc_and_ekf_are_summarized_separately():
    tables = {
        "GPS": pd.DataFrame({"TimeS": [0.0, 1.0], "Status": [3, 3], "NSats": [15, 14], "HDop": [0.8, 0.9]}),
        "GPS2": pd.DataFrame({"TimeS": [0.0, 1.0], "Status": [2, 2], "NSats": [8, 7], "HDop": [2.5, 2.7]}),
        "BAT": pd.DataFrame({"TimeS": [0.0, 1.0, 0.0, 1.0], "Instance": [0, 0, 1, 1], "Volt": [16.8, 16.6, 14.8, 13.9], "Curr": [5, 6, 8, 9]}),
        "ESC": pd.DataFrame({"TimeS": [0.0, 1.0, 0.0, 1.0], "Instance": [0, 0, 1, 1], "RPM": [4000, 4100, 2500, 2400], "Curr": [4, 4.2, 7, 8], "Err": [0, 0, 0, 2]}),
        "XKF4": pd.DataFrame({"TimeS": [0.0, 1.0, 0.0, 1.0], "C": [0, 0, 1, 1], "SV": [0.2, 0.3, 1.4, 1.5], "SM": [0.3, 0.4, 1.2, 1.3]}),
    }
    metrics = compute_metrics(tables)
    instances = metrics["health"]["instances"]
    assert_true("GPS[0]" in instances["gps"] and "GPS[1]" in instances["gps"], "GPS and GPS2 should be separate instances")
    assert_true(instances["gps"]["GPS[1]"]["status_min"] == 2.0, "GPS2 degraded status should be retained")
    assert_true("BAT[0]" in instances["battery"] and "BAT[1]" in instances["battery"], "BAT instances should be separate")
    assert_true(instances["battery"]["BAT[1]"]["min_voltage"] == 13.9, "battery instance sag should be retained")
    assert_true(instances["battery"]["BAT[1]"]["units"]["min_voltage"] == "V", "battery voltage metrics should carry units")
    assert_true(metrics["health"]["battery"]["units"]["max_current"] == "A", "aggregate battery current should carry units")
    assert_true(instances["esc"]["ESC[1]"]["err_max"] == 2.0, "ESC instance error should be retained")
    assert_true(instances["ekf"]["XKF4[1]"]["SV_gt_1_count"] == 2, "EKF core instance should retain test-ratio exceedances")


def test_multi_instance_diagnosis_flags_degraded_gps_and_esc_instances():
    index = {"messages": {"GPS": {}, "GPS2": {}, "ESC": {}, "RATE": {}, "RCOU": {}}, "errors": [], "events": [], "modes": []}
    tables = {
        "GPS": pd.DataFrame({"TimeS": [0.0, 1.0], "Status": [3, 3], "NSats": [15, 14], "HDop": [0.8, 0.9]}),
        "GPS2": pd.DataFrame({"TimeS": [0.0, 1.0], "Status": [2, 2], "NSats": [8, 7], "HDop": [2.5, 2.7]}),
        "ESC": pd.DataFrame({"TimeS": [0.0, 1.0, 0.0, 1.0], "Instance": [0, 0, 1, 1], "RPM": [4000, 4100, 2500, 2400], "Curr": [4, 4.2, 7, 8], "Err": [0, 0, 0, 2]}),
    }
    findings, context, checked, _missing_required, _missing_strongly, _missing_optional = diagnose_by_class("ekf_gps_issue", tables, index)
    evidence = "\n".join("\n".join(f.get("evidence", [])) for f in findings)
    assert_true("GPS[1].Status minimum=2" in evidence, "degraded second GPS should be diagnostic evidence")
    assert_true("GPS[0]" not in evidence, "healthy first GPS should not be collapsed into degraded evidence")

    findings, context, checked, _missing_required, _missing_strongly, _missing_optional = diagnose_by_class("motor_esc_issue", tables, index)
    evidence = "\n".join("\n".join(f.get("evidence", [])) for f in findings)
    context_text = "\n".join(c.get("detail", "") for c in context)
    assert_true("ESC[1].Err max=2.00" in evidence, "ESC instance error should be diagnostic evidence")
    assert_true("ESC[0] RPM" in context_text and "ESC[1] RPM" in context_text, "ESC ranges should be contextualized per instance")


def test_normal_compass_data_is_context_not_interference_finding():
    tables = {
        "MAG": pd.DataFrame({"TimeS": [0, 1, 2, 3], "MagX": [100, 101, 100, 99], "MagY": [20, 21, 20, 19], "MagZ": [350, 351, 350, 349]}),
        "ATT": pd.DataFrame({"TimeS": [0, 1, 2, 3], "DesYaw": [10, 10, 10, 10], "Yaw": [10, 11, 10, 10]}),
        "RATE": pd.DataFrame({"TimeS": [0, 1, 2, 3], "YDes": [0, 0, 0, 0], "Y": [0, 1, 0, 0], "YOut": [0.01, 0.01, 0.01, 0.01]}),
        "CTUN": pd.DataFrame({"TimeS": [0, 1, 2, 3], "ThO": [0.3, 0.4, 0.5, 0.4]}),
        "BAT": pd.DataFrame({"TimeS": [0, 1, 2, 3], "Curr": [5, 6, 7, 6]}),
        "MODE": pd.DataFrame({"TimeS": [0], "Mode": ["STABILIZE"]}),
    }
    result = build_compass_yaw_investigation(tables)
    evidence = "\n".join("\n".join(f.get("evidence", [])) for f in result["findings"])
    context = "\n".join(c.get("detail", "") for c in result["context"])
    checks = "\n".join(c.get("result", "") for c in result["checked"])
    assert_true("mag field magnitude" in context, "normal MAG magnitude should be retained as context")
    assert_true("magnetic interference" not in evidence.lower(), "MAG data alone should not become an interference finding")
    assert_true("No compass/yaw-source issue" in checks or "No magnetic-field correlation" in checks, "normal compass data should be checked but not flagged")


def test_mag_field_magnitude_uses_measured_components_only():
    tables = {
        "MAG": pd.DataFrame({"TimeS": [0, 1], "MagX": [3, 0], "MagY": [4, 0], "MagZ": [12, 5]}),
    }
    frame = mag_field_frame(tables)
    assert_true(frame is not None, "measured MAG components should produce a field magnitude frame")
    assert_true(frame["mag_field"].round(3).tolist() == [13.0, 5.0], "field magnitude should be computed from MagX/MagY/MagZ")


def test_mag_offsets_are_context_not_field_magnitude_or_interference():
    tables = {
        "MAG": pd.DataFrame({"TimeS": [0, 1, 2, 3, 4], "OfsX": [100, 130, 160, 190, 220], "OfsY": [20, 25, 30, 35, 40], "OfsZ": [350, 390, 430, 470, 510]}),
        "ATT": pd.DataFrame({"TimeS": [0, 1, 2, 3, 4], "DesYaw": [0, 0, 0, 0, 0], "Yaw": [0, 5, 12, 20, 30]}),
        "RATE": pd.DataFrame({"TimeS": [0, 1, 2, 3, 4], "YDes": [0, 0, 0, 0, 0], "Y": [0, 1, 1, 2, 1], "YOut": [0.05, 0.05, 0.06, 0.05, 0.04]}),
        "CTUN": pd.DataFrame({"TimeS": [0, 1, 2, 3, 4], "ThO": [0.2, 0.35, 0.5, 0.65, 0.8]}),
        "BAT": pd.DataFrame({"TimeS": [0, 1, 2, 3, 4], "Curr": [5, 10, 15, 20, 25]}),
    }
    result = build_compass_yaw_investigation(tables)
    evidence = "\n".join("\n".join(f.get("evidence", [])) for f in result["findings"])
    context = "\n".join(c.get("detail", "") for c in result["context"])
    checks = "\n".join(c.get("result", "") for c in result["checked"])

    assert_true(mag_field_frame(tables) is None, "offset-only MAG data must not produce measured field magnitude")
    assert_true("measured magnetic field components" in checks, "offset-only MAG should explain that measured components are unavailable")
    assert_true("MAG compass offsets" in context, "offset fields should still be retained as context")
    assert_true("mag field magnitude" not in context, "offset fields should not be summarized as field magnitude")
    assert_true("Compass/yaw-source interference hypothesis" not in "\n".join(f.get("possible_cause", "") for f in result["findings"]), "offset-only MAG data must not create interference finding")
    assert_true("mag field magnitude correlates" not in evidence, "offset-only MAG data must not create magnetic-field correlation evidence")


def test_magnetic_interference_hypothesis_requires_correlation():
    tables = {
        "MAG": pd.DataFrame({"TimeS": [0, 1, 2, 3, 4], "MagX": [100, 130, 160, 190, 220], "MagY": [20, 25, 30, 35, 40], "MagZ": [350, 390, 430, 470, 510]}),
        "ATT": pd.DataFrame({"TimeS": [0, 1, 2, 3, 4], "DesYaw": [0, 0, 0, 0, 0], "Yaw": [0, 5, 12, 20, 30]}),
        "RATE": pd.DataFrame({"TimeS": [0, 1, 2, 3, 4], "YDes": [0, 0, 0, 0, 0], "Y": [0, 1, 1, 2, 1], "YOut": [0.05, 0.05, 0.06, 0.05, 0.04]}),
        "CTUN": pd.DataFrame({"TimeS": [0, 1, 2, 3, 4], "ThO": [0.2, 0.35, 0.5, 0.65, 0.8]}),
        "BAT": pd.DataFrame({"TimeS": [0, 1, 2, 3, 4], "Curr": [5, 10, 15, 20, 25]}),
        "XKF4": pd.DataFrame({"TimeS": [0, 1, 2, 3, 4], "SM": [0.2, 0.4, 1.2, 1.4, 1.6]}),
        "MODE": pd.DataFrame({"TimeS": [0], "Mode": ["LOITER"]}),
    }
    result = build_compass_yaw_investigation(tables)
    evidence = "\n".join("\n".join(f.get("evidence", [])) for f in result["findings"])
    assert_true("mag field magnitude changed" in evidence, "magnetic field change should be evidence when correlated with load")
    assert_true("correlates with battery current" in evidence or "correlates with throttle" in evidence, "interference hypothesis should require correlation")
    assert_true("XKF4.SM max=1.60" in evidence, "magnetic/yaw EKF test-ratio evidence should be included")


def test_yaw_diagnosis_separates_yaw_control_from_yaw_estimator_evidence():
    index = {"messages": {name: {} for name in ["ATT", "RATE", "MAG", "XKF4", "CTUN", "BAT"]}, "errors": [], "events": [], "modes": []}
    tables = {
        "MAG": pd.DataFrame({"TimeS": [0, 1, 2, 3, 4], "MagX": [100, 130, 160, 190, 220], "MagY": [20, 25, 30, 35, 40], "MagZ": [350, 390, 430, 470, 510]}),
        "ATT": pd.DataFrame({"TimeS": [0, 1, 2, 3, 4], "DesYaw": [0, 0, 0, 0, 0], "Yaw": [0, 5, 12, 20, 30]}),
        "RATE": pd.DataFrame({"TimeS": [0, 1, 2, 3, 4], "YDes": [0, 0, 0, 0, 0], "Y": [0, 1, 1, 2, 1], "YOut": [0.05, 0.05, 0.06, 0.05, 0.04]}),
        "CTUN": pd.DataFrame({"TimeS": [0, 1, 2, 3, 4], "ThO": [0.2, 0.35, 0.5, 0.65, 0.8]}),
        "BAT": pd.DataFrame({"TimeS": [0, 1, 2, 3, 4], "Curr": [5, 10, 15, 20, 25]}),
        "XKF4": pd.DataFrame({"TimeS": [0, 1, 2, 3, 4], "SM": [0.2, 0.4, 1.2, 1.4, 1.6]}),
    }
    findings, context, checked, *_ = diagnose_yaw(tables, index)
    causes = "\n".join(f.get("possible_cause", "") for f in findings)
    evidence = "\n".join("\n".join(f.get("evidence", [])) for f in findings)
    assert_true("Compass/yaw-source interference hypothesis" in causes, "yaw diagnosis should include yaw-estimator evidence")
    assert_true("Yaw authority limited" not in causes, "low RATE.Y/YOut should not be reported as yaw authority saturation")
    assert_true("mag field magnitude correlates" in evidence, "yaw estimator evidence should retain MAG/load correlation")


def _yaw_authority_finding_for_output_message(output_message=None):
    index_messages = {"RATE": {}, "PIDY": {}, "MODE": {}}
    tables = {
        "RATE": pd.DataFrame({
            "TimeS": [0.0, 1.0, 2.0, 3.0],
            "YDes": [0.0, 120.0, 120.0, 120.0],
            "Y": [0.0, 0.0, 5.0, 0.0],
            "YOut": [0.0, 0.9, 0.95, 0.9],
        }),
        "PIDY": pd.DataFrame({"TimeS": [1.0, 2.0, 3.0], "Err": [120.0, 115.0, 120.0]}),
        "MODE": pd.DataFrame({"TimeS": [0.0], "Mode": ["LOITER"]}),
    }
    if output_message:
        index_messages[output_message] = {}
        tables[output_message] = pd.DataFrame({"TimeS": [1.0, 2.0, 3.0], "C1": [1900, 1950, 1960], "C2": [1100, 1050, 1040]})
    findings, _context, _checked, *_ = diagnose_yaw(tables, {"messages": index_messages, "errors": [], "events": [], "modes": []})
    return next(f for f in findings if f.get("possible_cause") == "Yaw authority limited or yaw controller output saturated")


def test_yaw_authority_confidence_is_high_with_rcou_rco2_or_rco3_outputs():
    for output_message in ["RCOU", "RCO2", "RCO3"]:
        finding = _yaw_authority_finding_for_output_message(output_message)
        assert_true(finding["confidence"] == "high", f"{output_message} output evidence should give high yaw-authority confidence")


def test_yaw_authority_confidence_remains_medium_without_actuator_outputs():
    finding = _yaw_authority_finding_for_output_message()
    assert_true(finding["confidence"] == "medium", "missing actuator output evidence should keep yaw-authority confidence medium")


def test_cannot_conclude_treats_rco2_as_actuator_output_evidence():
    cannot = build_cannot_conclude(
        "yaw_misbehaviour",
        missing_required=[],
        missing_strongly_recommended=["RCOU"],
        missing_optional=[],
        tables={"RCO2": pd.DataFrame({"TimeS": [1.0], "C9": [1900]})},
        index={"messages": {"RCO2": {}}},
    )
    text = "\n".join(cannot)
    assert_true("RCOU/RCO2/RCO3 is missing" not in text, "RCO2 output rows should avoid missing actuator-output caveat")
    assert_true("Strongly recommended message `RCOU` is missing" not in text, "RCO2 output rows should avoid false RCOU-only missing tier wording")


def test_rcin_summary_uses_parameter_channel_mapping():
    tables = {
        "PARM": pd.DataFrame({
            "Name": ["RCMAP_ROLL", "RCMAP_PITCH", "RCMAP_THROTTLE", "RCMAP_YAW", "RC2_TRIM"],
            "Value": [1, 3, 4, 2, 1490],
        }),
        "RCIN": pd.DataFrame({"TimeS": [0.0, 1.0, 2.0], "C1": [1500, 1600, 1500], "C2": [1490, 1700, 1490], "C3": [1500, 1500, 1400], "C4": [1100, 1400, 1800]}),
    }
    mapping = rc_channel_mapping(tables)
    summary = summarize_rcin(tables)
    assert_true(mapping["axes"]["yaw"]["channel"] == 2, "RCMAP_YAW should select the yaw input channel")
    assert_true(mapping["axes"]["pitch"]["channel"] == 3, "RCMAP_PITCH should select the pitch input channel")
    assert_true(summary["axes"]["yaw"]["field"] == "C2", "RCIN summary should use mapped yaw field")
    assert_true(summary["axes"]["yaw"]["trim"] == 1490.0, "RCIN summary should use RC channel trim when available")
    assert_true(summary["mapping"]["limitation"] is None, "complete RCMAP parameters should avoid default-mapping limitation")


def test_yaw_rcin_commanded_motion_is_context_not_fault():
    index = {"messages": {"ATT": {}, "RATE": {}, "RCIN": {}, "PARM": {}}, "parameters": {"RCMAP_YAW": 4}, "errors": [], "events": [], "modes": []}
    tables = {
        "RCIN": pd.DataFrame({"TimeS": [0.0, 1.0, 2.0], "C4": [1500, 1700, 1700]}),
        "ATT": pd.DataFrame({"TimeS": [0.0, 1.0, 2.0], "DesYaw": [0.0, 5.0, 10.0], "Yaw": [0.0, 4.0, 9.0]}),
        "RATE": pd.DataFrame({"TimeS": [0.0, 1.0, 2.0], "YDes": [0.0, 25.0, 20.0], "Y": [0.0, 22.0, 19.0], "YOut": [0.0, 0.2, 0.2]}),
    }
    result = build_command_response_investigation(tables, index, axes=("yaw",))
    checks = "\n".join(c.get("result", "") for c in result["checked"])
    assert_true(result["findings"] == [], "commanded yaw should not become a fault finding")
    assert_true("commanded manoeuvre" in checks, "RCIN command before yaw motion should be recorded as commanded context")


def test_yaw_without_rcin_or_desired_command_flags_uncommanded_motion():
    index = {"messages": {"ATT": {}, "RATE": {}, "RCIN": {}}, "parameters": {"RCMAP_YAW": 4}, "errors": [], "events": [], "modes": []}
    tables = {
        "RCIN": pd.DataFrame({"TimeS": [0.0, 1.0, 2.0], "C4": [1500, 1500, 1500]}),
        "ATT": pd.DataFrame({"TimeS": [0.0, 1.0, 2.0], "DesYaw": [0.0, 0.0, 0.0], "Yaw": [0.0, 8.0, 16.0]}),
        "RATE": pd.DataFrame({"TimeS": [0.0, 1.0, 2.0], "YDes": [0.0, 0.0, 0.0], "Y": [0.0, 18.0, 22.0], "YOut": [0.0, 0.05, 0.05]}),
    }
    findings, context, checked, *_ = diagnose_yaw(tables, index)
    causes = "\n".join(f.get("possible_cause", "") for f in findings)
    assert_true("yaw motion without RCIN or desired command" in causes, "uncommanded yaw motion should be diagnostic evidence")
    assert_true(any(c.get("source") == "RCIN" for c in context), "RCIN range summary should be retained as context")


def test_roll_pitch_rcin_command_response_checks_are_added():
    index = {"messages": {"ATT": {}, "RATE": {}, "RCIN": {}}, "parameters": {"RCMAP_ROLL": 1, "RCMAP_PITCH": 2}, "errors": [], "events": [], "modes": []}
    tables = {
        "RCIN": pd.DataFrame({"TimeS": [0.0, 1.0, 2.0], "C1": [1500, 1650, 1650], "C2": [1500, 1500, 1500]}),
        "ATT": pd.DataFrame({"TimeS": [0.0, 1.0, 2.0], "DesRoll": [0.0, 8.0, 12.0], "Roll": [0.0, 7.0, 11.0], "DesPitch": [0.0, 0.0, 0.0], "Pitch": [0.0, 8.0, 12.0]}),
        "RATE": pd.DataFrame({"TimeS": [0.0, 1.0, 2.0], "RDes": [0.0, 20.0, 15.0], "R": [0.0, 18.0, 13.0], "PDes": [0.0, 0.0, 0.0], "P": [0.0, 18.0, 20.0]}),
    }
    findings, _context, checked, *_ = diagnose_by_class("attitude_rate_issue", tables, index)
    causes = "\n".join(f.get("possible_cause", "") for f in findings)
    checks = "\n".join(c.get("result", "") for c in checked)
    assert_true("commanded manoeuvre" in checks, "roll RCIN command should be checked against response")
    assert_true("pitch motion without RCIN or desired command" in causes, "pitch response without RCIN/desired command should be flagged")


def test_manifest_includes_rcin_plot_presets_when_supported():
    index = {
        "messages": {
            "ATT": {},
            "RATE": {},
            "RCIN": {"fields": ["TimeS", "C1", "C2", "C3", "C4"]},
            "CTUN": {},
            "BAT": {},
        },
        "errors": [],
        "events": [],
        "modes": [],
    }
    manifest = build_manifest_from_index(index, "yaw issue", "flight.bin")
    commands = "\n".join(manifest["recommended_next_commands"])
    assert_true("RCIN.C4=RC yaw input" in commands, "yaw manifest should include RCIN yaw command-response custom plot")
    assert_true("rcin_yaw_rate" in manifest["recommended_plots"], "RCIN yaw plot preset should be listed as recommended when data exists")
    assert_true(any("default ArduPilot channel order" in limit for limit in manifest["confidence_limits"]), "manifest should state when default RC mapping was assumed")


def test_manifest_rcin_plot_uses_rcmap_parameters_when_available():
    index = {
        "messages": {
            "ATT": {},
            "RATE": {},
            "RCIN": {"fields": ["TimeS", "C1", "C2", "C3", "C4"]},
        },
        "parameters": {
            "RCMAP_ROLL": 1,
            "RCMAP_PITCH": 3,
            "RCMAP_THROTTLE": 4,
            "RCMAP_YAW": 2,
        },
        "errors": [],
        "events": [],
        "modes": [],
    }
    manifest = build_manifest_from_index(index, "yaw issue", "flight.bin")
    commands = "\n".join(manifest["recommended_next_commands"])

    assert_true("RCIN.C2=RC yaw input" in commands, "RCMAP_YAW=2 should make yaw plot use RCIN.C2")
    assert_true("RCIN.C4=RC yaw input" not in commands, "mapped yaw plot should not use default RCIN.C4")
    assert_true(not any("default ArduPilot channel order" in limit for limit in manifest["confidence_limits"]), "complete RCMAP parameters should avoid default-mapping limitation")


def test_manifest_suppresses_rcin_plot_when_rcin_inventory_missing():
    index = {"messages": {"ATT": {}, "RATE": {}}, "errors": [], "events": [], "modes": []}
    manifest = build_manifest_from_index(index, "yaw issue", "flight.bin")
    commands = "\n".join(manifest["recommended_next_commands"])

    assert_true("RCIN." not in commands, "manifest should not recommend RCIN custom plots when RCIN is absent")
    assert_true("rcin_yaw_rate" not in manifest["recommended_plots"], "missing RCIN should keep RCIN plot group unavailable")


def test_manifest_plot_group_validation_covers_yaml_and_unknown_groups():
    validate_recommended_plot_groups()

    try:
        validate_recommended_plot_groups({
            "synthetic_symptom": {
                "recommended_plot_groups": ["does_not_exist"],
            },
        })
    except AnalysisError as exc:
        text = str(exc)
        assert_true("unknown recommended_plot_groups" in text, "unknown plot group validation should explain the failure")
        assert_true("synthetic_symptom" in text and "does_not_exist" in text, "unknown plot group validation should identify class and group")
    else:
        raise AssertionError("unknown recommended_plot_groups entry should fail validation")


def test_manifest_recommends_fft_workflow_for_vibration_raw_imu_evidence():
    index = {"messages": {"VIBE": {}, "RATE": {}, "ISBH": {}, "ISBD": {}}, "errors": [], "events": [], "modes": []}
    manifest = build_manifest_from_index(index, "vibration problem", "flight.bin")
    commands = "\n".join(manifest["recommended_next_commands"])

    assert_true("fft" in manifest["recommended_plots"], "FFT plot group should be recommended when raw/batch IMU evidence exists")
    assert_true("python scripts/ap_log_fft.py flight.bin --out out/fft --json out/fft.json" in commands, "vibration manifest should recommend the FFT workflow")


def test_manifest_recommends_mag_yaw_source_plot_for_ekf_gps_evidence():
    index = {"messages": {"GPS": {}, "XKF1": {}, "XKF3": {}, "XKF4": {}, "MODE": {}, "MAG": {}}, "errors": [], "events": [], "modes": []}
    manifest = build_manifest_from_index(index, "gps glitch", "flight.bin")
    commands = "\n".join(manifest["recommended_next_commands"])

    assert_true("mag" in manifest["recommended_plots"], "EKF/GPS manifest should expose the mag/yaw-source plot group when evidence exists")
    assert_true("EKF yaw/mag test ratios" in commands, "EKF/GPS manifest should recommend the intended mag/yaw-source custom plot")


def test_manifest_recommends_targeted_plots_for_compass_and_baro_classes():
    compass_index = {"messages": {"ATT": {}, "RATE": {}, "MAG": {}, "XKF3": {}, "XKF4": {}, "MODE": {}, "GPS": {}, "BAT": {}, "VIBE": {}}, "errors": [], "events": [], "modes": []}
    compass_manifest = build_manifest_from_index(compass_index, "compass interference", "flight.bin")
    compass_commands = "\n".join(compass_manifest["recommended_next_commands"])
    assert_true(compass_manifest["symptom_class"] == "compass_yaw_source_issue", "compass prompt should use explicit compass/yaw-source class")
    assert_true("mag" in compass_manifest["recommended_plots"], "compass/yaw-source manifest should recommend mag/yaw-source plots")
    assert_true("EKF yaw/mag test ratios" in compass_commands, "compass/yaw-source commands should include yaw/mag test-ratio plotting")

    baro_index = {"messages": {"CTUN": {}, "BARO": {}, "XKF4": {}, "VIBE": {}, "BAT": {}, "RCOU": {}}, "errors": [], "events": [], "modes": []}
    baro_manifest = build_manifest_from_index(baro_index, "rangefinder altitude jumps", "flight.bin")
    baro_commands = "\n".join(baro_manifest["recommended_next_commands"])
    assert_true(baro_manifest["symptom_class"] == "baro_rangefinder_altitude_issue", "rangefinder prompt should use explicit baro/rangefinder class")
    assert_true("baro_altitude" in baro_manifest["recommended_plots"], "baro/rangefinder manifest should recommend barometer altitude plots")
    assert_true("Barometer and altitude estimate" in baro_commands, "baro/rangefinder commands should include barometer altitude plotting")


def test_escx_generates_plots_and_avoids_missing_telemetry_caveat():
    tables = {
        "ESCX": pd.DataFrame({
            "TimeS": [0.0, 1.0],
            "Instance": [0, 0],
            "inpct": [15.0, 45.0],
            "outpct": [12.0, 40.0],
            "flags": [0, 2],
            "Pwr": [20.0, 55.0],
        })
    }
    with tempfile.TemporaryDirectory() as tmp:
        health_plots(tables, Path(tmp))
        assert_true((Path(tmp) / "06b_escx_extended_telemetry.html").exists(), "standard plot pack should include ESCX plot")
        targeted = make_targeted_plots_from_tables(tables, "motor_esc_issue", Path(tmp) / "targeted")
        assert_true(any("esc_escx_edt2" in p for p in targeted), "targeted motor/ESC plots should include ESCX")


def test_normal_telemetry_is_context_not_findings():
    index = {"messages": {"BAT": {}, "ESC": {}, "CTUN": {}, "BARO": {}}, "errors": [], "events": [], "modes": []}
    tables = {
        "BAT": pd.DataFrame({
            "TimeS": [0.0, 1.0, 2.0],
            "Volt": [16.8, 16.5, 16.4],
            "Curr": [5.0, 8.0, 6.0],
        }),
        "ESC": pd.DataFrame({
            "TimeS": [0.0, 1.0, 2.0],
            "Instance": [0, 0, 0],
            "RPM": [4100, 4300, 4200],
            "Curr": [3.0, 3.5, 3.2],
            "Temp": [32.0, 34.0, 33.0],
            "Err": [0, 0, 0],
        }),
        "CTUN": pd.DataFrame({
            "TimeS": [0.0, 1.0, 2.0],
            "Alt": [10.0, 10.2, 10.1],
            "DAlt": [10.1, 10.1, 10.0],
            "ThO": [0.35, 0.37, 0.36],
        }),
        "BARO": pd.DataFrame({
            "TimeS": [0.0, 1.0, 2.0],
            "Alt": [10.0, 10.1, 10.1],
            "Press": [101325, 101320, 101318],
        }),
    }

    findings, context, checked, _missing_required, _missing_strongly, _missing_optional = diagnose_by_class("altitude_throttle_issue", tables, index)
    evidence = "\n".join("\n".join(f.get("evidence", [])) for f in findings)
    context_text = "\n".join(c.get("detail", "") for c in context)
    assert_true("BAT voltage min" not in evidence, "normal battery ranges should not be findings")
    assert_true("ESC RPM" not in evidence, "normal ESC ranges should not be findings")
    assert_true("CTUN.Alt" not in evidence, "normal CTUN ranges should not be findings")
    assert_true("BAT voltage min=16.40 V, max=16.80 V" in context_text, "battery range should be retained as context")
    assert_true("ESC RPM: min=4100.00 rpm, max=4300.00 rpm" in context_text, "ESC range should be retained as context with units")
    assert_true("CTUN.Alt: min=10.00 m, max=10.20 m" in context_text, "altitude range should be retained as context with units")
    assert_true(checked, "normal telemetry checks should still be recorded as checked")


def test_yaw_pid_error_below_threshold_is_checked_not_finding():
    index = {"messages": {"ATT": {}, "RATE": {}, "PIDY": {}, "RCOU": {}, "MODE": {}, "MSG": {}, "EV": {}, "ERR": {}}, "errors": [], "events": [], "modes": []}
    tables = {
        "PIDY": pd.DataFrame({
            "TimeS": [0.0, 1.0, 2.0],
            "Err": [1.0, -2.0, 1.5],
            "Flags": [0, 0, 0],
            "Dmod": [1.0, 0.95, 0.9],
        })
    }

    findings, _context, checked, _missing_required, _missing_strongly, _missing_optional = diagnose_yaw(tables, index)
    evidence = "\n".join("\n".join(f.get("evidence", [])) for f in findings)
    checked_text = "\n".join(c.get("result", "") for c in checked)
    assert_true("PIDY.Err p95" not in evidence, "low PIDY.Err should not create finding evidence")
    assert_true("PIDY.Err p95 abs=" in checked_text, "low PIDY.Err should be recorded as checked context")


def test_yaw_diagnosis_requires_only_att_and_rate():
    index = {"messages": {"ATT": {}, "RATE": {}}, "errors": [], "events": [], "modes": []}
    tables = {
        "ATT": pd.DataFrame({
            "TimeS": [0.0, 1.0, 2.0],
            "DesYaw": [10.0, 12.0, 14.0],
            "Yaw": [10.5, 11.5, 14.5],
        }),
        "RATE": pd.DataFrame({
            "TimeS": [0.0, 1.0, 2.0],
            "YDes": [0.0, 5.0, -5.0],
            "Y": [0.0, 4.0, -4.0],
            "YOut": [0.1, 0.2, -0.2],
        }),
    }

    findings, _context, checked, missing_required, missing_strongly, missing_optional = diagnose_yaw(tables, index)
    assert_true(missing_required == [], f"ATT/RATE-only yaw should not miss required data, got {missing_required}")
    assert_true(missing_strongly == ["PIDY", "RCOU", "MODE"], f"PIDY/RCOU/MODE should be strongly recommended, got {missing_strongly}")
    assert_true("MSG" in missing_optional and "ERR" in missing_optional, "timeline messages should be optional yaw context")
    assert_true(findings == [], "normal ATT/RATE-only yaw data should not create findings")
    assert_true(checked, "ATT/RATE-only yaw should still run checks")


def test_cannot_conclude_preserves_missing_evidence_tiers_for_yaw():
    index = {"messages": {"ATT": {}, "RATE": {}, "RCOU": {}, "MODE": {}, "ESCX": {}, "XKF4": {}}, "errors": [], "events": [], "modes": []}
    tables = {
        "ATT": pd.DataFrame({"TimeS": [0.0], "DesYaw": [0.0], "Yaw": [0.0]}),
        "RATE": pd.DataFrame({"TimeS": [0.0], "YDes": [0.0], "Y": [0.0], "YOut": [0.0]}),
        "RCOU": pd.DataFrame({"TimeS": [0.0], "C1": [1500]}),
        "ESCX": pd.DataFrame({"TimeS": [0.0], "Instance": [0], "Pwr": [0.0]}),
        "XKF4": pd.DataFrame({"TimeS": [0.0], "SV": [0.1]}),
    }
    missing_required, missing_strongly, missing_optional = diagnosis_missing(index, "yaw_misbehaviour")
    cannot = build_cannot_conclude(
        "yaw_misbehaviour",
        missing_required=missing_required,
        missing_strongly_recommended=missing_strongly,
        missing_optional=missing_optional,
        tables=tables,
        index=index,
    )
    text = "\n".join(cannot)

    assert_true("Strongly recommended message `PIDY` is missing; confidence is reduced." in text, "PIDY should be described as strongly recommended")
    assert_true("Required message `PIDY`" not in text, "PIDY should not be overstated as required")
    assert_true("Optional context message `VIBE` is missing; this limits supporting context only." in text, "VIBE should be described as optional context")
    assert_true("Required message `VIBE`" not in text, "VIBE should not be overstated as required")
    assert_true("ESC-level motor/ESC confirmation is not possible because ESC/ESCX/EDT2 telemetry is missing." not in text, "ESCX should avoid the all-ESC-telemetry-missing caveat")

    missing_required, missing_strongly, missing_optional = diagnosis_missing({"messages": {"RATE": {}}}, "yaw_misbehaviour")
    cannot = build_cannot_conclude(
        "yaw_misbehaviour",
        missing_required=missing_required,
        missing_strongly_recommended=missing_strongly,
        missing_optional=missing_optional,
        tables={"RATE": tables["RATE"]},
        index={"messages": {"RATE": {}}},
    )
    text = "\n".join(cannot)
    assert_true("Required message `ATT` is missing; core diagnosis may not be possible." in text, "ATT should remain required yaw evidence")


def test_yaw_with_pidy_missing_other_strong_data_downgrades_confidence():
    index = {"messages": {"ATT": {}, "RATE": {}, "PIDY": {}}, "errors": [], "events": [], "modes": []}
    tables = {
        "ATT": pd.DataFrame({
            "TimeS": [0.0, 1.0, 2.0],
            "DesYaw": [0.0, 0.0, 0.0],
            "Yaw": [0.0, 0.0, 0.0],
        }),
        "RATE": pd.DataFrame({
            "TimeS": [0.0, 1.0, 2.0],
            "YDes": [0.0, 0.0, 0.0],
            "Y": [0.0, 0.0, 0.0],
            "YOut": [0.1, 0.1, 0.1],
        }),
        "PIDY": pd.DataFrame({
            "TimeS": [0.0, 1.0, 2.0],
            "Err": [35.0, 40.0, 45.0],
            "Flags": [1, 1, 1],
            "Dmod": [0.75, 0.7, 0.72],
        }),
    }

    findings, _context, _checked, missing_required, missing_strongly, _missing_optional = diagnose_yaw(tables, index)
    assert_true(missing_required == [], "ATT/RATE/PIDY yaw should have core required data")
    assert_true(missing_strongly == ["RCOU", "MODE"], f"RCOU/MODE should remain strongly recommended, got {missing_strongly}")
    assert_true(findings, "PIDY limits should still create a finding")
    assert_true(all(f.get("confidence") != "high" for f in findings), "missing strongly recommended yaw data should prevent high-confidence findings")


def test_yaw_full_evidence_has_no_missing_evidence_tiers():
    messages = {name: {} for name in ["ATT", "RATE", "PIDY", "RCOU", "MODE", "MSG", "EV", "ERR", "RCIN", "MAG", "XKF3", "XKF4", "VIBE", "BAT", "POWR", "ESC", "ESCX", "EDT2"]}
    required, strongly, optional = diagnosis_missing({"messages": messages}, "yaw_misbehaviour")
    assert_true(required == [], f"full yaw evidence should satisfy required messages, got {required}")
    assert_true(strongly == [], f"full yaw evidence should satisfy strongly recommended messages, got {strongly}")
    assert_true(optional == [], f"full yaw evidence should satisfy optional context messages, got {optional}")


def test_investigation_manifest_yaw_inventory_plans_next_steps():
    index = {"messages": {"ATT": {}, "RATE": {}, "PIDY": {}, "BAT": {}, "MSG": {}}, "errors": [], "events": [], "modes": []}
    manifest = build_manifest_from_index(index, "yaw issue", "flight.bin")
    assert_true(manifest["symptom_class"] == "yaw_misbehaviour", "manifest should classify yaw symptom from YAML")
    assert_true("ATT" in manifest["available_evidence"]["core"], "ATT should be listed as core evidence")
    assert_true("RATE" in manifest["available_evidence"]["controller"], "RATE should be listed as controller evidence")
    assert_true("PIDY" in manifest["available_evidence"]["controller"], "PIDY should be listed as controller evidence")
    assert_true("BAT" in manifest["available_evidence"]["power"], "BAT should be listed as power evidence")
    assert_true(manifest["missing_evidence"]["required"] == [], "ATT/RATE should satisfy yaw required evidence")
    assert_true("RCOU" in manifest["missing_evidence"]["strongly_recommended"], "missing RCOU should be strongly recommended")
    assert_true(any("ap_log_diagnose.py" in cmd for cmd in manifest["recommended_next_commands"]), "manifest should suggest diagnosis command")
    assert_true(any("ap_log_custom_plot.py" in cmd and "RATE.YDes" in cmd for cmd in manifest["recommended_next_commands"]), "manifest should suggest concrete yaw custom plot")
    assert_true(any("Was yaw commanded or uncommanded?" in q for q in manifest["questions_to_answer"]), "manifest should include yaw questions")
    assert_true(any("strongly recommended" in limit for limit in manifest["confidence_limits"]), "missing strong evidence should limit confidence")


def test_investigation_manifest_suggests_extract_when_core_evidence_missing():
    index = {"messages": {"MSG": {}}, "errors": [], "events": [], "modes": []}
    manifest = build_manifest_from_index(index, "yaw issue", "flight.bin")
    assert_true("ATT" in manifest["missing_evidence"]["required"], "missing ATT should be required yaw evidence")
    assert_true("RATE" in manifest["missing_evidence"]["required"], "missing RATE should be required yaw evidence")
    assert_true(any("ap_log_extract.py" in cmd and "--messages ATT,RATE" in cmd for cmd in manifest["recommended_next_commands"]), "manifest should suggest extracting missing core evidence")
    assert_true(any("Cannot answer core diagnosis" in limit for limit in manifest["confidence_limits"]), "manifest should state required evidence limit")


def test_manifest_next_evidence_yaw_missing_pidy_outputs():
    index = {"messages": {"ATT": {}, "RATE": {}}, "errors": [], "events": [], "modes": []}
    manifest = build_manifest_from_index(index, "yaw issue", "flight.bin")
    plan = manifest["next_evidence_gathering"]
    assert_true(plan["safe_to_request_flight"] is True, "yaw missing strong evidence can allow controlled capture after checks")
    assert_true(plan["recommended_next_step_type"] == "controlled_flight", f"unexpected yaw next step: {plan}")
    assert_true("PIDY" in plan["messages_to_capture"], "yaw plan should ask for missing PIDY")
    assert_true("RCOU" in plan["messages_to_capture"], "yaw plan should ask for actuator outputs")
    assert_true(any("Capture PIDY" in item for item in plan["missing_critical_message_guidance"]), "yaw plan should explain why PIDY is needed")
    assert_true(any("yaw authority" in item for item in plan["missing_critical_message_guidance"]), "yaw plan should explain why actuator outputs are needed")
    assert_true(any("PIDY" in item for item in plan["logging_profile_hints"]), "yaw logging hints should mention PIDY")
    assert_true(any("stable hover" in item for item in plan["suggested_safe_capture"]), "yaw plan should suggest a cautious hover capture")
    assert_true(plan["recommended_next_step_type"] != "do_not_fly_until_checked", "ordinary yaw missing-evidence plan should not become crash no-fly guidance")


def test_manifest_next_evidence_yaw_missing_esc_telemetry_is_support_dependent():
    index = {"messages": {"ATT": {}, "RATE": {}, "PIDY": {}, "RCOU": {}, "BAT": {}, "POWR": {}}, "errors": [], "events": [], "modes": []}
    manifest = build_manifest_from_index(index, "yaw issue", "flight.bin")
    plan = manifest["next_evidence_gathering"]
    assert_true(any("Enable ESC telemetry" in item for item in plan["hardware_support_dependent_evidence"]), "yaw plan should make ESC telemetry hardware-support dependent")
    assert_true(any("proxy evidence" in item for item in plan["hardware_support_dependent_evidence"]), "yaw plan should name actuator/power proxy evidence when ESC telemetry is unavailable")
    assert_true(any("ESC-level confirmation is limited" in item for item in plan["confidence_limits"]), "missing ESC telemetry should limit yaw confidence")


def test_manifest_next_evidence_motor_esc_missing_outputs_prefers_bench():
    index = {"messages": {"RATE": {}, "BAT": {}}, "errors": [], "events": [], "modes": []}
    manifest = build_manifest_from_index(index, "motor pulsing", "flight.bin")
    plan = manifest["next_evidence_gathering"]
    assert_true(plan["safe_to_request_flight"] is False, "motor/ESC missing evidence should not request flight first")
    assert_true(plan["recommended_next_step_type"] == "bench_check", f"unexpected motor/ESC next step: {plan}")
    assert_true("RCOU" in plan["messages_to_capture"], "motor/ESC plan should request actuator output evidence")
    assert_true(any("actuator-output logging" in item for item in plan["missing_critical_message_guidance"]), "motor/ESC plan should request actuator-output logging")
    assert_true(any("bench checks" in item or "restrained ground checks" in item for item in plan["logging_profile_hints"]), "motor/ESC logging hints should be bench/ground-first")
    assert_true(any("do not claim ESC-level" in item for item in plan["hardware_support_dependent_evidence"]), "motor/ESC plan should avoid unsupported ESC-level claims")
    assert_true(any("Enable ESC telemetry" in item for item in plan["suggested_safe_capture"]), "missing ESC telemetry should be called out")
    assert_true(any("proxy evidence" in item for item in plan["suggested_safe_capture"]), "plan should identify RCOU/BAT/POWR proxy evidence when ESC telemetry is unavailable")


def test_manifest_next_evidence_crash_is_do_not_fly():
    index = {"messages": {"MSG": {}, "BAT": {}}, "errors": [], "events": [], "modes": []}
    manifest = build_manifest_from_index(index, "crash loss of control", "flight.bin")
    plan = manifest["next_evidence_gathering"]
    assert_true(plan["safe_to_request_flight"] is False, "crash manifest should not request flight")
    assert_true(plan["recommended_next_step_type"] == "do_not_fly_until_checked", f"unexpected crash next step: {plan}")
    assert_true(any("Do not repeat flight" in item for item in plan["do_not_attempt"]), "crash plan should forbid repeat flight until checks")
    assert_true("LOG_DISARMED" in plan["logging_settings_to_review"], "crash repair/startup evidence may need disarmed logging")


def test_manifest_next_evidence_vibration_missing_raw_imu_short_controlled_capture():
    index = {"messages": {"VIBE": {}, "RATE": {}}, "errors": [], "events": [], "modes": []}
    manifest = build_manifest_from_index(index, "vibration filter issue", "flight.bin")
    plan = manifest["next_evidence_gathering"]
    assert_true(plan["safe_to_request_flight"] is True, "vibration raw-IMU capture can be controlled if vehicle is otherwise controllable")
    assert_true(plan["recommended_next_step_type"] == "controlled_flight", f"unexpected vibration next step: {plan}")
    assert_true("INS_RAW_LOG_OPT" in plan["logging_settings_to_review"], "vibration plan should review raw IMU logging")
    assert_true(any("short" in item and "raw/high-rate IMU" in item for item in plan["suggested_safe_capture"]), "vibration plan should recommend short high-rate capture cautiously")
    assert_true(any("dropouts" in item for item in plan["suggested_safe_capture"]), "vibration plan should require dropout checks")
    assert_true(any("fft_available=false" in item for item in plan["suggested_safe_capture"]), "vibration plan should route unusable FFT diagnostics back to ap_log_fft output")
    assert_true(any("FFT/filter conclusions are limited" in item for item in plan["missing_critical_message_guidance"]), "vibration plan should explain raw/high-rate IMU limits")
    assert_true(any("short targeted capture" in item for item in plan["logging_profile_hints"]), "vibration logging hints should keep raw/high-rate capture short")
    assert_true(any("unstable" in item for item in plan["do_not_attempt"]), "vibration plan should forbid raw IMU capture if unstable or unsafe")
    assert_true(any("usable FFT evidence" in item for item in plan["confidence_limits"]), "vibration plan should state missing usable FFT evidence")
    assert_true(any("Reset high-volume" in item or "Clear INS_RAW_LOG_OPT" in item for item in plan["reset_after_test"]), "vibration plan should reset high-volume logging")


def test_manifest_next_evidence_prearm_boot_uses_log_disarmed_without_flight():
    index = {"messages": {"MSG": {}, "ERR": {}}, "errors": [], "events": [], "modes": []}
    manifest = build_manifest_from_index(index, "pre-arm boot arming failure", "flight.bin")
    plan = manifest["next_evidence_gathering"]
    assert_true(plan["safe_to_request_flight"] is False, "pre-arm/boot issue should not require flight")
    assert_true(plan["recommended_next_step_type"] == "ground_test", f"unexpected pre-arm next step: {plan}")
    assert_true("LOG_DISARMED" in plan["logging_settings_to_review"], "pre-arm/boot evidence should include LOG_DISARMED")
    assert_true(any("boot/pre-arm/arming ground capture" in item for item in plan["suggested_safe_capture"]), "pre-arm plan should request boot/pre-arm ground logging")
    assert_true(any("bypass arming checks" in item for item in plan["do_not_attempt"]), "pre-arm plan should not bypass arming checks")


def test_next_steps_yaw_auto_worse_with_vibration_limits_missions():
    plan = build_diagnosis_action_plan(
        symptom_class="yaw_misbehaviour",
        symptom_text="yaw wobble is worse in AUTO mission than POSHOLD",
        findings=[
            {"possible_cause": "Yaw rate tracking error", "severity": "likely-issue", "evidence": ["RATE yaw error p95 is 55 deg/s"]},
            {"possible_cause": "Vibration/clipping is relevant to symptom", "severity": "likely-issue", "evidence": ["VIBE high within analysis window"]},
        ],
        missing_strongly_recommended=["PIDY"],
        missing_optional=["ESC"],
        next_evidence_gathering={
            "suggested_safe_capture": ["Use a short stable hover capture after checks."],
            "hardware_support_dependent_evidence": ["Enable ESC telemetry if hardware and firmware support it."],
        },
    )
    assert_true(plan["flight_status"]["classification"] in {"no_auto_missions", "controlled_hover_only"}, f"unexpected yaw mission status: {plan['flight_status']}")
    actions = "\n".join(step["action"] for step in plan["recommended_next_steps"])
    assert_true("Pause AUTO/mission flying" in actions, "yaw mission next steps should pause AUTO/mission flying")
    assert_true("PIDY" in actions and "ESC telemetry" in actions, "yaw mission next steps should request PIDY and ESC telemetry")
    assert_true("Re-run mode comparison and diagnosis before tuning" in actions, "yaw mission next steps should require reanalysis before tuning")
    assert_true("safe to fly" not in actions.lower(), "next steps must not overclaim safety")


def test_next_steps_crash_loss_of_control_do_not_fly():
    plan = build_diagnosis_action_plan(
        symptom_class="crash_or_loss_of_control",
        symptom_text="crash loss of control",
        findings=[{"possible_cause": "Motor output saturation", "severity": "safety-critical", "evidence": ["RCOU.C1 >=1900us"]}],
    )
    assert_true(plan["flight_status"]["classification"] == "do_not_fly_until_checked", f"unexpected crash status: {plan['flight_status']}")
    actions = "\n".join(step["action"] for step in plan["recommended_next_steps"])
    assert_true("Do not fly until checked" in actions, "crash plan should start with no-fly gate")
    assert_true("Do not repeat the mission" in actions or "Do not repeat the flight" in actions, "crash plan should forbid repeating the flight")


def test_next_steps_motor_esc_missing_outputs_prefers_bench():
    plan = build_diagnosis_action_plan(
        symptom_class="motor_esc_issue",
        symptom_text="motor pulsing",
        findings=[],
        missing_strongly_recommended=["RCOU"],
        missing_optional=["ESC"],
    )
    assert_true(plan["flight_status"]["classification"] == "bench_only", f"unexpected motor/ESC status: {plan['flight_status']}")
    actions = "\n".join(step["action"] for step in plan["recommended_next_steps"])
    assert_true("bench" in actions.lower(), "motor/ESC next steps should start with bench checks")
    assert_true("RCOU" in actions and "ESC telemetry" in actions, "motor/ESC missing evidence should produce logging/capture steps")


def test_next_steps_rc_failsafe_prearm_ground_only():
    plan = build_diagnosis_action_plan(
        symptom_class="rc_failsafe_prearm_issue",
        symptom_text="radio failsafe prearm",
        findings=[{"possible_cause": "Pre-arm, arming, or failsafe timeline evidence", "severity": "safety-critical", "evidence": ["PreArm: Hardware safety switch"]}],
    )
    assert_true(plan["flight_status"]["classification"] == "ground_test_only", f"unexpected rc/prearm status: {plan['flight_status']}")
    actions = "\n".join(step["action"] for step in plan["recommended_next_steps"])
    assert_true("ground" in actions.lower(), "rc/prearm next steps should be ground-test first")
    assert_true("arming checks" in actions.lower() or "failsafe" in actions.lower(), "rc/prearm next steps should check safety warnings/failsafes")


def test_next_steps_missing_pid_esc_evidence_adds_logging_capture_steps():
    plan = build_diagnosis_action_plan(
        symptom_class="yaw_misbehaviour",
        symptom_text="yaw wobble",
        findings=[{"possible_cause": "Yaw rate tracking error", "severity": "likely-issue", "evidence": ["RATE yaw error p95 is 45 deg/s"]}],
        missing_strongly_recommended=["PIDY"],
        missing_optional=["ESC"],
    )
    steps = plan["recommended_next_steps"]
    actions = "\n".join(step["action"] for step in steps)
    priorities = [step["priority"] for step in steps]
    assert_true(priorities == sorted(priorities), f"next steps should be ordered by priority: {priorities}")
    assert_true("PIDY" in actions, "missing PIDY should produce logging/check next step")
    assert_true("ESC telemetry" in actions, "missing ESC evidence should produce hardware-support-dependent next step")
    assert_true(any(step["type"] == "controlled_evidence_capture" for step in steps), "missing evidence should produce controlled capture step")
    assert_true(any(step["type"] == "what_not_to_do" for step in steps), "next steps should include what-not-to-do")


def test_next_steps_use_mode_comparison_when_auto_ranks_worse():
    plan = build_diagnosis_action_plan(
        symptom_class="yaw_misbehaviour",
        symptom_text="yaw issue",
        findings=[{"possible_cause": "Yaw rate tracking error", "severity": "likely-issue", "evidence": ["RATE yaw error"]}],
        mode_comparison={"ranking": [{"decoded_mode": "AUTO", "ranking_score": 10}, {"decoded_mode": "POSHOLD", "ranking_score": 2}]},
    )
    assert_true(plan["flight_status"]["classification"] == "no_auto_missions", f"mode comparison should gate AUTO missions: {plan['flight_status']}")
    assert_true(any("mode_comparison" in step["source_evidence"] for step in plan["recommended_next_steps"]), "mode comparison should be recorded as source evidence")


def test_manifest_next_evidence_field_shape_for_all_symptom_classes():
    required_keys = {
        "safe_to_request_flight",
        "recommended_next_step_type",
        "reason",
        "bench_checks_first",
        "logging_settings_to_review",
        "messages_to_capture",
        "suggested_safe_capture",
        "do_not_attempt",
        "reset_after_test",
        "confidence_limits",
    }
    symptom_map = load_symptom_map()
    for symptom_class in symptom_map["ordered_class_names"]:
        manifest = build_manifest_from_index({"messages": {}, "errors": [], "events": [], "modes": []}, symptom_class, "flight.bin")
        plan = manifest.get("next_evidence_gathering")
        assert_true(isinstance(plan, dict), f"{symptom_class} should include next_evidence_gathering")
        assert_true(required_keys <= set(plan), f"{symptom_class} next_evidence_gathering missing keys: {required_keys - set(plan)}")
        assert_true(plan["recommended_next_step_type"] in {
            "existing_log_analysis",
            "parameter_review",
            "bench_check",
            "ground_test",
            "controlled_flight",
            "do_not_fly_until_checked",
        }, f"{symptom_class} has invalid next step type")


def test_validate_module_availability_separates_required_and_optional_messages():
    modules = module_availability({"messages": {"ATT": {}, "RATE": {}, "PIDY": {}, "RCOU": {}, "MODE": {}, "MSG": {}, "EV": {}, "ERR": {}}})
    yaw = modules["yaw_diagnosis"]
    assert_true(yaw["status"] == "available", f"yaw should be available from primary messages, got {yaw}")
    assert_true(yaw["missing_strongly_recommended"] == [], "yaw strong recommendations should be satisfied")
    assert_true("MAG" in yaw["missing_optional_context"], "missing optional yaw context should be reported separately")


def test_compare_summarizes_metric_deltas():
    before = {"flight": {"duration_s_estimate": 10}, "health": {"battery": {"min_voltage": 15.0}}, "tuning": {"yaw": {"rate_error_p95_abs": 20.0}}}
    after = {"flight": {"duration_s_estimate": 11}, "health": {"battery": {"min_voltage": 14.5}}, "tuning": {"yaw": {"rate_error_p95_abs": 12.0}}}
    diffs = metric_differences(before, after)
    keys = {d["metric"] for d in diffs}
    assert_true("health.battery.min_voltage" in keys, "battery metric delta should be summarized")
    assert_true("tuning.yaw.rate_error_p95_abs" in keys, "tuning metric delta should be summarized")


def test_metric_differences_can_ignore_unrequested_sections():
    before = {"flight": {"duration_s_estimate": 10}, "tuning": {"roll": {"rate_error_p95_abs": 20.0}}}
    after = {"flight": {"duration_s_estimate": 10}, "tuning": {"roll": {"rate_error_p95_abs": 10.0}}}
    diffs = metric_differences(before, after)
    assert_true(any(d["metric"] == "tuning.roll.rate_error_p95_abs" for d in diffs), "segment metrics should remain comparable")


def test_system_id_metrics_are_reported():
    tables = {
        "SIDS": pd.DataFrame({
            "TimeS": [0.0],
            "Ax": [12],
            "Mag": [0.55],
            "FSt": [0.1],
            "FSp": [40.0],
            "TR": [120.0],
        }),
        "SID": pd.DataFrame({
            "TimeS": [0.0, 0.1],
            "Time": [0.0, 0.1],
            "Targ": [0.0, 0.2],
            "F": [0.1, 0.2],
            "Gx": [1.0, 2.0],
            "Gy": [1.5, 2.5],
            "Gz": [2.0, 3.0],
            "Ax": [0.1, 0.2],
            "Ay": [0.2, 0.3],
            "Az": [0.3, 0.4],
        }),
    }
    metrics = compute_metrics(tables)
    sysid = metrics["system_id"]
    assert_true(sysid["present"] is True, "System ID should be reported when SID/SIDS tables are present")
    assert_true(sysid["SIDS"]["rows"] == 1 and sysid["SID"]["rows"] == 2, "System ID row counts should be included")


def test_metrics_flag_missing_flight_context():
    tables = {
        "RATE": pd.DataFrame({"TimeS": [0.0, 1.0], "RDes": [0.0, 1.0], "R": [0.0, 0.8]}),
        "ATT": pd.DataFrame({"TimeS": [0.0, 1.0], "DesRoll": [0.0, 1.0], "Roll": [0.0, 0.8]}),
    }
    metrics = compute_metrics(tables)
    reasons = "\n".join(metrics["confidence"]["reasons"])
    assert_true("whether this was a flight cannot be confirmed" in reasons, "missing MODE/ARM should be called out")
    assert_true("bench-only logs and flight logs may not be distinguishable" in reasons, "missing position/altitude context should be called out")


def test_metrics_include_generic_numeric_summary_for_extra_messages():
    tables = {"CANH": pd.DataFrame({"TimeS": [0, 1], "Health": [0, 2], "Mode": [0, 7]})}
    metrics = compute_metrics(tables)
    assert_true("CANH" in metrics["generic_messages"], "unknown but numeric messages should be summarized")
    assert_true(metrics["generic_messages"]["CANH"]["numeric"]["Health"]["max"] == 2.0, "numeric fields should be summarized")


def test_batch_sampler_isb_fft_rows_are_processed():
    rows = {
        "ISBH": [{"N": 1, "type": 1, "instance": 0, "smp_rate": 1000, "mul": 10}],
        "ISBD": [
            {"N": 1, "seqno": i, "x": [0, 10, 0, -10] * 40, "y": [0, 5, 0, -5] * 40, "z": [0, 2, 0, -2] * 40}
            for i in range(4)
        ],
    }
    result = fft_from_isb_rows(rows)
    assert_true(result["available"] is True, "ISBH/ISBD batch data should produce FFT results")
    assert_true(result["message"] == "ISBH/ISBD", "batch FFT should identify ISBH/ISBD source")
    assert_true(result["sample_rate_hz_estimate"] == 1000.0, "batch FFT should use ISBH sample rate")
    assert_true(result["units"]["sample_rate_hz_estimate"] == "Hz", "FFT sample rate should carry Hz units")
    assert_true(result["peaks"][0]["units"]["frequency_hz"] == "Hz", "FFT peak frequencies should carry Hz units")
    assert_true(result["peaks"], "batch FFT should report dominant peaks")


def test_fft_reports_no_raw_or_high_rate_imu_messages():
    result = fft_from_tables({"VIBE": pd.DataFrame({"TimeS": [0.0, 1.0], "VibeX": [1.0, 2.0]})})
    assert_true(result["fft_available"] is False, "FFT should be marked unavailable without raw/high-rate IMU evidence")
    assert_true(result["reason"] == "no_raw_or_high_rate_imu_messages", f"unexpected FFT reason: {result}")
    assert_true(result["messages_checked"] == [], "no candidate IMU messages should produce an empty checked list")
    assert_true(any("raw/high-rate IMU" in item for item in result["next_capture_guidance"]), "failure output should include capture guidance")


def test_fft_reports_sparse_or_insufficient_timestamps():
    result = fft_from_tables({"GYR": pd.DataFrame({"TimeS": [0.0, 0.5, 1.0], "GyrX": [0.0, 1.0, 0.0]})})
    assert_true(result["fft_available"] is False, "FFT should not run on sparse short evidence")
    assert_true(result["reason"] in {"insufficient_rows", "could_not_determine_sample_interval"}, f"unexpected sparse FFT reason: {result}")
    diag = result["sample_interval_diagnostics"]["GYR"]
    assert_true(diag["rows"] == 3, "diagnostics should include checked row count")
    assert_true(diag["usable"] is False, "sparse input should be unusable")


def test_fft_reports_non_monotonic_timestamps():
    times = [i * 0.01 for i in range(140)]
    times[80] = times[79] - 0.01
    result = fft_from_tables({"GYR": pd.DataFrame({"TimeS": times, "GyrX": [0.0, 1.0] * 70})})
    assert_true(result["fft_available"] is False, "FFT should reject non-monotonic timestamps")
    assert_true(result["reason"] == "non_monotonic_timestamps", f"unexpected non-monotonic FFT reason: {result}")
    assert_true(result["sample_interval_diagnostics"]["GYR"]["monotonic"] is False, "diagnostics should record monotonic=false")


def test_fft_valid_synthetic_high_rate_data_is_available():
    times = [i * 0.01 for i in range(256)]
    signal = [0.0, 1.0, 0.0, -1.0] * 64
    with tempfile.TemporaryDirectory() as tmp:
        result = fft_from_tables({"GYR": pd.DataFrame({"TimeS": times, "GyrX": signal, "GyrY": signal})}, out=tmp)
    assert_true(result["fft_available"] is True, f"valid high-rate data should produce FFT: {result}")
    assert_true(result["available"] is True, "legacy available flag should remain true")
    assert_true(abs(result["sample_rate_hz_estimate"] - 100.0) < 1e-6, "synthetic sample rate should be 100 Hz")
    assert_true(result["plots"], "valid FFT should write an HTML plot when output directory is provided")
    assert_true(result["sample_interval_diagnostics"]["GYR"]["usable"] is True, "diagnostics should mark valid GYR data usable")


def test_non_yaw_symptom_plots_are_generated_when_data_exists():
    tables = {
        "GPS": pd.DataFrame({"TimeS": [0.0, 1.0], "HDop": [1.0, 2.5], "NSats": [14, 9]}),
        "XKF4": pd.DataFrame({"TimeS": [0.0, 1.0], "SV": [0.2, 1.2], "SP": [0.1, 0.7], "SH": [0.2, 0.3], "SM": [0.2, 1.1]}),
    }
    with tempfile.TemporaryDirectory() as tmp:
        plots = make_targeted_plots_from_tables(tables, "ekf_gps_issue", tmp)
        assert_true(any("ekf_gps" in p for p in plots), "EKF/GPS symptom should generate a targeted plot")


def test_skill_doctor_passes_with_current_test_environment():
    with tempfile.TemporaryDirectory() as tmp:
        result = run_doctor(Path(tmp) / "doctor-out")
    assert_true(result["exit_code"] == 0, f"doctor should pass in the regression environment: {result}")
    names = {check["name"] for check in result["checks"] if check["status"] == "pass"}
    assert_true("package:pymavlink" in names, "doctor should check pymavlink")
    assert_true("package:PyYAML" in names, "doctor should check PyYAML via yaml import")
    assert_true("module:ap_log_diagnose" in names, "doctor should check core diagnosis module imports")
    assert_true("synthetic_dataframe_path" in names, "doctor should verify a no-log synthetic data path")


def test_skill_doctor_cli_writes_json():
    with tempfile.TemporaryDirectory() as tmp:
        json_path = Path(tmp) / "skill_doctor.json"
        out_dir = Path(tmp) / "doctor-out"
        proc = subprocess.run(
            [sys.executable, "scripts/ap_skill_doctor.py", "--json", str(json_path), "--out-dir", str(out_dir)],
            cwd=Path(__file__).resolve().parents[1],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        assert_true(proc.returncode == 0, f"doctor CLI should pass, stdout={proc.stdout}, stderr={proc.stderr}")
        result = json.loads(json_path.read_text(encoding="utf-8"))
        assert_true(result["exit_code"] == 0, "doctor JSON should report success in the regression environment")
        assert_true(any(c["name"] == "requirements_txt" for c in result["checks"]), "doctor JSON should include requirements.txt check")


def test_compass_yaw_plots_generate_with_full_synthetic_evidence():
    tables = {
        "ATT": pd.DataFrame({"TimeS": [0.0, 1.0, 2.0], "DesYaw": [10.0, 20.0, 30.0], "Yaw": [9.0, 18.0, 29.0]}),
        "RATE": pd.DataFrame({"TimeS": [0.0, 1.0, 2.0], "YDes": [0.0, 5.0, -5.0], "Y": [0.0, 4.0, -4.0], "YOut": [0.1, 0.2, -0.2]}),
        "MAG": pd.DataFrame({"TimeS": [0.0, 1.0, 2.0], "MagX": [100.0, 110.0, 105.0], "MagY": [20.0, 22.0, 24.0], "MagZ": [40.0, 41.0, 43.0]}),
        "CTUN": pd.DataFrame({"TimeS": [0.0, 1.0, 2.0], "ThO": [0.2, 0.4, 0.6]}),
        "BAT": pd.DataFrame({"TimeS": [0.0, 1.0, 2.0], "Curr": [5.0, 8.0, 12.0]}),
        "XKF4": pd.DataFrame({"TimeS": [0.0, 1.0, 2.0], "SM": [0.2, 0.4, 0.6], "SH": [0.1, 0.2, 0.3]}),
    }
    with tempfile.TemporaryDirectory() as tmp:
        plots = write_compass_yaw_plots(tables, tmp, events=True)
        names = {Path(p).name for p in plots}
        assert_true("compass_yaw_source_investigation.html" in names, "compass/yaw investigation plot should be written")
        assert_true("ekf_mag_yaw_innovations.html" in names, "EKF magnetic/yaw plot should be written when XKF data is present")
        assert_true((Path(tmp) / "compass_yaw_source_investigation.html").exists(), "compass/yaw HTML should exist")
        assert_true((Path(tmp) / "ekf_mag_yaw_innovations.html").exists(), "EKF HTML should exist")


def test_compass_yaw_plots_tolerate_missing_optional_battery_and_throttle():
    tables = {
        "ATT": pd.DataFrame({"TimeS": [0.0, 1.0, 2.0], "DesYaw": [10.0, 20.0, 30.0], "Yaw": [9.0, 18.0, 29.0]}),
        "RATE": pd.DataFrame({"TimeS": [0.0, 1.0, 2.0], "YDes": [0.0, 5.0, -5.0], "Y": [0.0, 4.0, -4.0], "YOut": [0.1, 0.2, -0.2]}),
        "MAG": pd.DataFrame({"TimeS": [0.0, 1.0, 2.0], "MagX": [100.0, 110.0, 105.0], "MagY": [20.0, 22.0, 24.0], "MagZ": [40.0, 41.0, 43.0]}),
    }
    with tempfile.TemporaryDirectory() as tmp:
        plots = write_compass_yaw_plots(tables, tmp, events=True)
        names = {Path(p).name for p in plots}
        assert_true("compass_yaw_source_investigation.html" in names, "missing BAT/CTUN should not block the main compass/yaw plot")
        assert_true("ekf_mag_yaw_innovations.html" not in names, "missing XKF data should skip EKF plot")


def test_custom_plot_supports_arbitrary_fields_and_secondary_axis():
    tables = {
        "GPS": pd.DataFrame({"TimeS": [0.0, 1.0, 2.0], "Alt": [83.5, 84.0, 85.0]}),
        "BARO": pd.DataFrame({"TimeS": [0.0, 1.0, 2.0], "Press": [101162.5, 101160.0, 101155.0]}),
    }
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "gps_alt_pressure.html"
        manifest = make_custom_plot(
            tables,
            ["GPS.Alt=GPS altitude", "BARO.Press=Barometric pressure"],
            out,
            title="GPS altitude and pressure",
            secondary=["BARO.Press"],
        )
        html = out.read_text(encoding="utf-8")
        assert_true(out.exists(), "custom plot should write the requested HTML file")
        assert_true("GPS altitude" in html and "Barometric pressure" in html, "custom plot should include requested series labels")
        assert_true(manifest["secondary"] == ["BARO.PRESS"], "custom plot should record right-axis series")
        units = {series["label"]: series["unit"] for series in manifest["series"]}
        assert_true(units["GPS altitude"] == "m", "custom plot manifest should unit GPS altitude")
        assert_true(units["Barometric pressure"] == "unknown", "custom plot should mark uncertain pressure units unknown")


def test_custom_plot_rejects_secondary_series_not_in_plot():
    tables = {
        "GPS": pd.DataFrame({"TimeS": [0.0, 1.0], "Alt": [83.5, 84.0]}),
        "BARO": pd.DataFrame({"TimeS": [0.0, 1.0], "Press": [101162.5, 101160.0]}),
    }
    with tempfile.TemporaryDirectory() as tmp:
        try:
            make_custom_plot(tables, ["GPS.Alt"], Path(tmp) / "plot.html", secondary=["BARO.Press"])
        except ap_common.AnalysisError as exc:
            assert_true("must also be present as --series" in str(exc), "secondary validation should explain the issue")
        else:
            raise AssertionError("custom plot should reject secondary fields that are not plotted")


def test_custom_plot_missing_message_suggests_extracting_all_messages():
    tables = {"GPS": pd.DataFrame({"TimeS": [0.0, 1.0], "Alt": [83.5, 84.0]})}
    with tempfile.TemporaryDirectory() as tmp:
        try:
            make_custom_plot(tables, ["CUSTOM.Value"], Path(tmp) / "plot.html")
        except ap_common.AnalysisError as exc:
            msg = str(exc)
            assert_true("--messages ALL" in msg and "--messages CUSTOM" in msg, "missing custom plot messages should explain how to re-extract")
        else:
            raise AssertionError("custom plot should reject messages that were not extracted")


def test_custom_plot_supports_simple_derived_expression():
    tables = {
        "GPS": pd.DataFrame({"TimeS": [0.0, 1.0], "Alt": [100.0, 102.0]}),
        "BARO": pd.DataFrame({"TimeS": [0.0, 1.0], "Alt": [98.0, 99.0]}),
    }
    with tempfile.TemporaryDirectory() as tmp:
        manifest = make_custom_plot(
            tables,
            ["GPS.Alt", "BARO.Alt", "GPS.Alt-BARO.Alt=GPS minus baro"],
            Path(tmp) / "derived.html",
        )
        labels = [s["label"] for s in manifest["series"]]
        assert_true("GPS minus baro" in labels, "derived expression should appear as a plotted series")


def test_custom_plot_expression_alignment_tolerance_drops_unmatched_rows():
    tables = {
        "GPS": pd.DataFrame({"TimeS": [0.0, 1.0, 2.0], "Alt": [100.0, 102.0, 104.0]}),
        "BARO": pd.DataFrame({"TimeS": [0.02, 4.0], "Alt": [98.0, 99.0]}),
    }
    with tempfile.TemporaryDirectory() as tmp:
        manifest = make_custom_plot(
            tables,
            ["GPS.Alt-BARO.Alt=GPS minus baro"],
            Path(tmp) / "aligned.html",
            align_tolerance=0.05,
        )
        alignment = manifest["series"][0]["alignment"]
        assert_true(alignment["align_tolerance_s"] == 0.05, "alignment tolerance should be recorded")
        assert_true(alignment["rows_before_alignment"] == 3, "base GPS rows should be counted")
        assert_true(alignment["rows_after_alignment"] == 1, "only one GPS row should align within tolerance")
        assert_true(alignment["rows_dropped_for_alignment"] == 2, "unmatched rows should be reported")


def test_plot_manifest_uses_metrics_argument():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        tables_dir = tmp_path / "tables"
        tables_dir.mkdir()
        pd.DataFrame({"TimeS": [0.0, 1.0], "C1": [1200, 1300]}).to_csv(tables_dir / "RCOU.csv", index=False)
        metrics_path = tmp_path / "metrics.json"
        metrics_path.write_text(
            '{"analysis_window":{"start_s":1.0,"end_s":2.0},"health":{"motor_outputs":{"mapping_available":true}}}',
            encoding="utf-8",
        )
        manifest_path = tmp_path / "manifest.json"
        argv = sys.argv
        try:
            sys.argv = [
                "ap_log_plots.py",
                "--tables", str(tables_dir),
                "--metrics", str(metrics_path),
                "--out", str(tmp_path / "plots"),
                "--manifest", str(manifest_path),
            ]
            rc = plots_main()
        finally:
            sys.argv = argv
        assert_true(rc == 0, "plot generation should succeed")
        manifest = ap_common.read_json(manifest_path)
        assert_true(manifest["metrics_file"] == str(metrics_path), "manifest should record metrics file")
        assert_true(manifest["metrics_analysis_window"] == {"start_s": 1.0, "end_s": 2.0}, "manifest should include metrics window")
        assert_true(manifest["motor_mapping_available"] is True, "manifest should include motor mapping availability")


def main():
    test_stream_dataflash_counts_without_storing_unselected_rows()
    test_stream_dataflash_respects_time_window_and_max_messages()
    test_extract_jsonl_stream_respects_message_and_time_filters()
    test_extract_jsonl_stream_supports_gzip_and_armed_filter()
    test_stream_index_reports_logging_dropouts()
    test_non_logging_drop_field_is_possible_not_confirmed_dropout()
    test_logging_related_unknown_drop_field_is_possible_dropout_context()
    test_logging_health_clean_log_has_no_limits()
    test_logging_health_detects_timestamp_gap_and_reset()
    test_logging_health_detects_missing_core_messages_after_arm()
    test_logging_health_manifest_and_diagnosis_confidence_limit()
    test_log_quality_status_flags_logging_dropouts_as_confidence_limit()
    test_log_quality_status_reports_missing_timebase()
    test_no_parm_is_reported_as_parameter_context_limitation()
    test_corrupt_incomplete_reference_exists_and_is_linked_from_skill()
    test_load_tables_fails_on_unreadable_table()
    test_time_window_filters_tables_inclusively()
    test_window_selector_mode_intervals()
    test_copter_numeric_mode_matches_named_query()
    test_copter_poshold_numeric_mode_matches_named_query()
    test_copter_mode_aliases_match_numeric_modes()
    test_copter_named_mode_matches_numeric_query()
    test_decoded_mode_selection_preserves_multiple_intervals()
    test_unknown_numeric_mode_is_labelled_without_crashing()
    test_index_summary_includes_decoded_mode_timeline_and_caveat()
    test_active_flight_filter_excludes_ground_spool_from_auto_window()
    test_ground_only_saturation_is_not_active_flight_motor_finding()
    test_active_flight_filter_warns_when_evidence_is_insufficient()
    test_mode_window_filters_disjoint_intervals_without_intervening_modes()
    test_mode_window_diagnosis_uses_only_selected_intervals()
    test_custom_plot_manifest_records_split_mode_intervals()
    test_window_selector_around_msg_event_and_error()
    test_window_selector_takeoff_hover_and_high_throttle()
    test_hover_selector_uses_duration_based_window_on_high_rate_data()
    test_hover_selector_rejects_unstable_altitude()
    test_hover_selector_rejects_throttle_outside_hover_band()
    test_window_selector_fails_requested_missing_selector()
    test_parse_time_window_accepts_start_end_and_around()
    test_metrics_can_be_computed_from_filtered_window()
    test_nested_numeric_summary_units_use_message_and_field_context()
    test_output_mapping_reads_servo_function_parameters()
    test_copter_output_mapping_handles_motor9_to_motor12_and_tilt_roles()
    test_motor_output_metrics_are_mapping_aware()
    test_windowed_tables_preserve_boot_only_parameter_context()
    test_motor_output_metrics_include_rco2_and_rco3_channels()
    test_parameter_context_uses_yaml_selectors_and_servo_wildcards()
    test_stream_index_preserves_parameter_defaults_for_context()
    test_manifest_includes_symptom_parameter_context()
    test_param_lookup_known_parameter_returns_metadata()
    test_param_lookup_unknown_parameter_preserves_logged_value()
    test_mission_planner_style_param_file_is_parsed()
    test_qgc_style_param_file_is_parsed()
    test_mavproxy_name_value_param_file_is_parsed()
    test_external_param_conflict_preserves_logged_value_and_warns()
    test_external_servo_function_enables_motor_mapping_without_log_parm()
    test_external_rcmap_yaw_enables_rcin_mapping_without_log_parm()
    test_param_lookup_symptom_returns_enriched_relevant_parameters()
    test_generic_bitmask_decode_returns_enabled_labels()
    test_unknown_bitmask_metadata_does_not_crash()
    test_log_bitmask_missing_pid_bit_reports_pid_guidance()
    test_manifest_next_evidence_uses_log_bitmask_pid_context()
    test_parameter_metadata_fetch_compactor_uses_machine_readable_shape()
    test_parameter_context_yaw_includes_mission_yaw_parameters()
    test_parameter_context_mission_yaw_includes_rate_accel_and_headroom()
    test_manifest_questions_include_mission_yaw_context_for_auto_symptom()
    test_mode_compare_ranks_auto_worse_for_yaw_tracking_with_numeric_modes()
    test_manifest_recommends_mode_compare_for_mission_symptom()
    test_event_markers_collect_mode_err_ev_msg()
    test_mode_segments_are_derived_from_mode_rows()
    test_validate_marks_non_copter_scope_as_partial()
    test_vibe_clip_variants_are_detected()
    test_high_vibration_outside_symptom_window_is_context_not_finding()
    test_high_vibration_during_symptom_window_becomes_supporting_evidence()
    test_missing_vibe_limits_vibration_confidence_without_guessing()
    test_non_yaw_symptoms_get_targeted_findings()
    test_toilet_bowling_prefers_ekf_gps_when_navigation_context_is_present()
    test_yaml_aliases_drive_symptom_classification()
    test_rc_prearm_aliases_drive_symptom_classification()
    test_explicit_compass_and_baro_aliases_drive_symptom_classification()
    test_rc_prearm_required_messages_and_parameters()
    test_rc_prearm_diagnosis_routes_context_without_bypassing_checks()
    test_new_yaml_alias_does_not_need_python_change()
    test_unmatched_symptom_returns_general_investigation()
    test_malformed_symptom_yaml_fails_clearly()
    test_edt2_status_is_used_for_motor_esc_findings()
    test_escx_is_used_for_motor_esc_metrics_and_findings()
    test_multi_instance_gps_battery_esc_and_ekf_are_summarized_separately()
    test_multi_instance_diagnosis_flags_degraded_gps_and_esc_instances()
    test_normal_compass_data_is_context_not_interference_finding()
    test_mag_field_magnitude_uses_measured_components_only()
    test_mag_offsets_are_context_not_field_magnitude_or_interference()
    test_magnetic_interference_hypothesis_requires_correlation()
    test_yaw_diagnosis_separates_yaw_control_from_yaw_estimator_evidence()
    test_yaw_authority_confidence_is_high_with_rcou_rco2_or_rco3_outputs()
    test_yaw_authority_confidence_remains_medium_without_actuator_outputs()
    test_cannot_conclude_treats_rco2_as_actuator_output_evidence()
    test_rcin_summary_uses_parameter_channel_mapping()
    test_yaw_rcin_commanded_motion_is_context_not_fault()
    test_yaw_without_rcin_or_desired_command_flags_uncommanded_motion()
    test_roll_pitch_rcin_command_response_checks_are_added()
    test_manifest_includes_rcin_plot_presets_when_supported()
    test_manifest_rcin_plot_uses_rcmap_parameters_when_available()
    test_manifest_suppresses_rcin_plot_when_rcin_inventory_missing()
    test_manifest_plot_group_validation_covers_yaml_and_unknown_groups()
    test_manifest_recommends_fft_workflow_for_vibration_raw_imu_evidence()
    test_manifest_recommends_mag_yaw_source_plot_for_ekf_gps_evidence()
    test_manifest_recommends_targeted_plots_for_compass_and_baro_classes()
    test_escx_generates_plots_and_avoids_missing_telemetry_caveat()
    test_normal_telemetry_is_context_not_findings()
    test_yaw_pid_error_below_threshold_is_checked_not_finding()
    test_yaw_diagnosis_requires_only_att_and_rate()
    test_cannot_conclude_preserves_missing_evidence_tiers_for_yaw()
    test_yaw_with_pidy_missing_other_strong_data_downgrades_confidence()
    test_yaw_full_evidence_has_no_missing_evidence_tiers()
    test_investigation_manifest_yaw_inventory_plans_next_steps()
    test_investigation_manifest_suggests_extract_when_core_evidence_missing()
    test_manifest_next_evidence_yaw_missing_pidy_outputs()
    test_manifest_next_evidence_yaw_missing_esc_telemetry_is_support_dependent()
    test_manifest_next_evidence_motor_esc_missing_outputs_prefers_bench()
    test_manifest_next_evidence_crash_is_do_not_fly()
    test_manifest_next_evidence_vibration_missing_raw_imu_short_controlled_capture()
    test_manifest_next_evidence_prearm_boot_uses_log_disarmed_without_flight()
    test_next_steps_yaw_auto_worse_with_vibration_limits_missions()
    test_next_steps_crash_loss_of_control_do_not_fly()
    test_next_steps_motor_esc_missing_outputs_prefers_bench()
    test_next_steps_rc_failsafe_prearm_ground_only()
    test_next_steps_missing_pid_esc_evidence_adds_logging_capture_steps()
    test_next_steps_use_mode_comparison_when_auto_ranks_worse()
    test_manifest_next_evidence_field_shape_for_all_symptom_classes()
    test_validate_module_availability_separates_required_and_optional_messages()
    test_compare_summarizes_metric_deltas()
    test_metric_differences_can_ignore_unrequested_sections()
    test_system_id_metrics_are_reported()
    test_metrics_flag_missing_flight_context()
    test_metrics_include_generic_numeric_summary_for_extra_messages()
    test_batch_sampler_isb_fft_rows_are_processed()
    test_fft_reports_no_raw_or_high_rate_imu_messages()
    test_fft_reports_sparse_or_insufficient_timestamps()
    test_fft_reports_non_monotonic_timestamps()
    test_fft_valid_synthetic_high_rate_data_is_available()
    test_non_yaw_symptom_plots_are_generated_when_data_exists()
    test_skill_doctor_passes_with_current_test_environment()
    test_skill_doctor_cli_writes_json()
    test_compass_yaw_plots_generate_with_full_synthetic_evidence()
    test_compass_yaw_plots_tolerate_missing_optional_battery_and_throttle()
    test_custom_plot_supports_arbitrary_fields_and_secondary_axis()
    test_custom_plot_rejects_secondary_series_not_in_plot()
    test_custom_plot_missing_message_suggests_extracting_all_messages()
    test_custom_plot_supports_simple_derived_expression()
    test_custom_plot_expression_alignment_tolerance_drops_unmatched_rows()
    test_plot_manifest_uses_metrics_argument()
    print("regression tests passed")


if __name__ == "__main__":
    main()
