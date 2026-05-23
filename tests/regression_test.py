#!/usr/bin/env python3
from __future__ import annotations

import tempfile
from pathlib import Path

import pandas as pd

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import ap_common
from ap_log_compare import metric_differences
from ap_log_custom_plot import make_custom_plot
from ap_log_diagnose import diagnose_by_class
from ap_log_diagnose import make_targeted_plots_from_tables
from ap_log_fft import fft_from_isb_rows
from ap_log_metrics import compute_metrics
from ap_log_plots import health_plots
from ap_report_pack import render as render_report
from ap_log_validate import module_availability


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


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


def test_parse_time_window_accepts_start_end_and_around():
    assert_true(ap_common.parse_time_window("10:20") == {"start_s": 10.0, "end_s": 20.0}, "start:end window should parse")
    assert_true(ap_common.parse_time_window("around:100:5") == {"start_s": 95.0, "end_s": 105.0}, "around window should parse")


def test_metrics_can_be_computed_from_filtered_window():
    tables = {"RATE": pd.DataFrame({"TimeS": [0.0, 1.0, 2.0], "RDes": [0.0, 10.0, 20.0], "R": [0.0, 8.0, 10.0]})}
    filtered = ap_common.filter_tables_by_time(tables, start_s=1.0, end_s=2.0)
    metrics = compute_metrics(filtered, analysis_window={"start_s": 1.0, "end_s": 2.0})
    assert_true(metrics["flight"]["duration_s_estimate"] == 1.0, "filtered metrics should use filtered duration")
    assert_true(metrics["analysis_window"] == {"start_s": 1.0, "end_s": 2.0}, "metrics should record analysis window")


def test_output_mapping_reads_servo_function_parameters():
    params = {"SERVO1_FUNCTION": 33, "SERVO2_FUNCTION": 34, "SERVO3_FUNCTION": 1, "SERVO4_FUNCTION": 0}
    mapping = ap_common.output_mapping_from_params(params)
    assert_true(mapping["C1"]["function_id"] == 33 and mapping["C1"]["role"] == "motor1", "SERVO1 motor mapping should be detected")
    assert_true(mapping["C2"]["role"] == "motor2", "SERVO2 motor mapping should be detected")
    assert_true(mapping["C3"]["role"] == "rc_passthrough", "passthrough role should be detected")
    assert_true(mapping["C4"]["role"] == "disabled", "disabled role should be detected")


def test_motor_output_metrics_are_mapping_aware():
    tables = {
        "RCOU": pd.DataFrame({"TimeS": [0.0, 1.0], "C1": [1000, 1200], "C2": [1900, 1800], "C3": [1000, 2000]}),
        "PARM": pd.DataFrame({"Name": ["SERVO1_FUNCTION", "SERVO2_FUNCTION", "SERVO3_FUNCTION"], "Value": [33, 34, 1]}),
    }
    metrics = compute_metrics(tables)
    channels = metrics["health"]["motor_outputs"]["motor_channels"]
    assert_true(channels == ["C1", "C2"], "only mapped motor channels should be treated as motors")


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

    index = {"messages": {"VIBE": {}}, "errors": [], "events": [], "modes": []}
    findings, _checked, missing = diagnose_by_class("vibration_issue", tables, index)
    evidence = "\n".join("\n".join(f.get("evidence", [])) for f in findings)
    assert_true("VIBE.Clip0 increased by 3" in evidence, "diagnosis should include Clip0 clipping evidence")
    assert_true("RATE" in missing, "diagnosis should still report missing symptom-relevant messages")


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
    findings, checked, missing = diagnose_by_class("ekf_gps_issue", tables, index)
    causes = "\n".join(f.get("possible_cause", "") for f in findings)
    evidence = "\n".join("\n".join(f.get("evidence", [])) for f in findings)
    assert_true("GPS/EKF" in causes, "EKF/GPS symptom should produce a targeted GPS/EKF finding")
    assert_true("GPS.Status minimum=2" in evidence, "GPS fix status should be used as evidence")
    assert_true("SV max=1.20" in evidence and "SM max=1.30" in evidence, "EKF test ratios should be used as evidence")
    assert_true("XKF1" in missing and "XKF3" in missing, "missing data should reflect the selected symptom class")
    assert_true(checked, "diagnosis should record checks that were not supported")


def test_toilet_bowling_prefers_ekf_gps_when_navigation_context_is_present():
    symptom = ap_common.classify_symptom("toilet bowling in loiter after a GPS glitch")
    assert_true(symptom == "ekf_gps_issue", f"expected ekf_gps_issue, got {symptom}")


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
    findings, _checked, missing = diagnose_by_class("motor_esc_issue", tables, index)
    evidence = "\n".join("\n".join(f.get("evidence", [])) for f in findings)
    assert_true("EDT2 status alert/warning/error counts=0/1/1" in evidence, "EDT2 status bits should be diagnosed")
    assert_true("ESC" not in missing, "EDT2 should satisfy ESC-status confirmation for motor diagnostics")


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

    findings, _checked, missing = diagnose_by_class("motor_esc_issue", tables, index)
    evidence = "\n".join("\n".join(f.get("evidence", [])) for f in findings)
    assert_true("ESCX flags nonzero samples=1" in evidence, "ESCX flags should be diagnostic evidence")
    assert_true("ESCX inpct: min=15.00, max=60.00" in evidence, "ESCX duty cycle should be diagnostic evidence")
    assert_true("ESC" not in missing, "ESCX should satisfy ESC-status confirmation for motor diagnostics")


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
    metrics = {"messages_present": ["ESCX"], "health": {}, "tuning": {}, "confidence": {"overall": "medium", "reasons": []}}
    report = render_report(metrics=metrics)
    assert_true("ESC/ESCX/EDT2 telemetry is missing" not in report, "report should not claim telemetry is missing when ESCX exists")


def test_validate_module_availability_separates_required_and_optional_messages():
    modules = module_availability({"messages": {"ATT": {}, "RATE": {}, "PIDY": {}, "RCOU": {}, "MODE": {}, "MSG": {}, "EV": {}, "ERR": {}}})
    yaw = modules["yaw_diagnosis"]
    assert_true(yaw["status"] == "available", f"yaw should be available from primary messages, got {yaw}")
    assert_true("MAG" in yaw["missing_optional"], "missing optional yaw context should be reported separately")


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
    assert_true(result["peaks"], "batch FFT should report dominant peaks")


def test_non_yaw_symptom_plots_are_generated_when_data_exists():
    tables = {
        "GPS": pd.DataFrame({"TimeS": [0.0, 1.0], "HDop": [1.0, 2.5], "NSats": [14, 9]}),
        "XKF4": pd.DataFrame({"TimeS": [0.0, 1.0], "SV": [0.2, 1.2], "SP": [0.1, 0.7], "SH": [0.2, 0.3], "SM": [0.2, 1.1]}),
    }
    with tempfile.TemporaryDirectory() as tmp:
        plots = make_targeted_plots_from_tables(tables, "ekf_gps_issue", tmp)
        assert_true(any("ekf_gps" in p for p in plots), "EKF/GPS symptom should generate a targeted plot")


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


def main():
    test_load_tables_fails_on_unreadable_table()
    test_time_window_filters_tables_inclusively()
    test_parse_time_window_accepts_start_end_and_around()
    test_metrics_can_be_computed_from_filtered_window()
    test_output_mapping_reads_servo_function_parameters()
    test_motor_output_metrics_are_mapping_aware()
    test_event_markers_collect_mode_err_ev_msg()
    test_mode_segments_are_derived_from_mode_rows()
    test_validate_marks_non_copter_scope_as_partial()
    test_vibe_clip_variants_are_detected()
    test_non_yaw_symptoms_get_targeted_findings()
    test_toilet_bowling_prefers_ekf_gps_when_navigation_context_is_present()
    test_edt2_status_is_used_for_motor_esc_findings()
    test_escx_is_used_for_motor_esc_metrics_and_findings()
    test_escx_generates_plots_and_avoids_missing_telemetry_caveat()
    test_validate_module_availability_separates_required_and_optional_messages()
    test_compare_summarizes_metric_deltas()
    test_metric_differences_can_ignore_unrequested_sections()
    test_system_id_metrics_are_reported()
    test_metrics_flag_missing_flight_context()
    test_metrics_include_generic_numeric_summary_for_extra_messages()
    test_batch_sampler_isb_fft_rows_are_processed()
    test_non_yaw_symptom_plots_are_generated_when_data_exists()
    test_custom_plot_supports_arbitrary_fields_and_secondary_axis()
    test_custom_plot_rejects_secondary_series_not_in_plot()
    test_custom_plot_supports_simple_derived_expression()
    print("regression tests passed")


if __name__ == "__main__":
    main()
