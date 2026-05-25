#!/usr/bin/env python3
from __future__ import annotations
import argparse
import math
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ap_common import (
    AnalysisError, AXIS_MAP, battery_instance_groups, classify_symptom, clip_columns, collect_dataflash,
    apply_active_flight_filter, combined_rcout_dataframe, ekf_instance_groups, ensure_dir, esc_instance_groups, event_markers_from_tables,
    filter_tables_by_time, get_col, gps_instance_groups, missing_messages, motor_channels_from_mapping,
    numeric_series, output_channel_columns, output_channel_label, output_mapping_from_tables,
    parse_time_window, percentile, rms, rows_to_dataframe, safe_float, severity_rank, vehicle_scope, write_json
)
from ap_diag_helpers import add_motor_esc_findings, add_power_findings, vals
from ap_diag_requirements import missing_by_tier
from ap_symptom_map import requirement_spec
from ap_window_select import select_analysis_window
from ap_compass_yaw import build_compass_yaw_investigation, write_compass_yaw_plots
from ap_param_context import merge_external_parameters, parse_param_file
from ap_parameters import select_relevant_parameters
from ap_rcin import build_command_response_investigation, rcin_channel_col, rc_channel_mapping, summarize_rcin
from ap_units import value_with_unit
from ap_vibration import add_vibration_assessment_findings, build_vibration_assessment


ACTUATOR_OUTPUT_MESSAGES = ("RCOU", "RCO2", "RCO3")


def actuator_output_evidence_sources(tables):
    sources = []
    for name in ACTUATOR_OUTPUT_MESSAGES:
        df = tables.get(name)
        if df is None:
            continue
        try:
            has_rows = len(df) > 0
        except Exception:
            has_rows = False
        if has_rows and output_channel_columns(df):
            sources.append(name)
    if sources:
        return sources
    return ["RCOUT"] if combined_rcout_dataframe(tables) is not None else []


def has_actuator_output_evidence(tables):
    return bool(actuator_output_evidence_sources(tables))


def missing_strongly_with_available_alternatives(missing_strongly, tables):
    missing = list(missing_strongly or [])
    if has_actuator_output_evidence(tables):
        missing = [msg for msg in missing if msg not in ACTUATOR_OUTPUT_MESSAGES]
    return missing


def add_event_markers(fig, markers):
    if not markers:
        return
    shapes = []
    annotations = []
    for marker in markers[:80]:
        x = marker["time_s"]
        color = "#dc2626" if marker["source"] == "ERR" else ("#2563eb" if marker["source"] == "MODE" else "#64748b")
        shapes.append({"type": "line", "xref": "x", "yref": "paper", "x0": x, "x1": x, "y0": 0, "y1": 1, "line": {"color": color, "width": 1, "dash": "dot"}})
        annotations.append({"xref": "x", "yref": "paper", "x": x, "y": 1.02, "text": marker["label"], "showarrow": False, "textangle": -45, "font": {"size": 9, "color": color}})
    fig.update_layout(shapes=shapes, annotations=annotations)


def make_targeted_plots_from_tables(tables, symptom_class, plots_dir, events=False, index=None, parameters=None):
    generated = []
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except Exception:
        return generated
    out = ensure_dir(plots_dir)
    markers = event_markers_from_tables(tables) if events else []
    if symptom_class == "yaw_misbehaviour":
        if "RCIN" in tables and "RATE" in tables:
            mapping = rc_channel_mapping(tables, index, parameters=parameters)
            yaw_info = mapping["axes"]["yaw"]
            yaw_col = rcin_channel_col(tables["RCIN"], yaw_info["channel"])
            rate = tables["RATE"]
            if yaw_col and all(c in rate.columns for c in ["YDes", "Y"]):
                fig = make_subplots(rows=2, cols=1, shared_xaxes=True, subplot_titles=("RC yaw input", "Yaw rate response"))
                rcin = tables["RCIN"]; x = rcin["TimeS"] if "TimeS" in rcin.columns else list(range(len(rcin)))
                fig.add_trace(go.Scatter(x=x, y=rcin[yaw_col], mode="lines", name=f"RCIN.{yaw_col} yaw"), row=1, col=1)
                x = rate["TimeS"] if "TimeS" in rate.columns else list(range(len(rate)))
                for c in ["YDes", "Y"]:
                    fig.add_trace(go.Scatter(x=x, y=rate[c], mode="lines", name=f"RATE.{c}"), row=2, col=1)
                fig.update_layout(title="RCIN yaw command vs yaw rate response", template="plotly_white", hovermode="x unified")
                add_event_markers(fig, markers)
                p = out / "rcin_yaw_rate_command_response.html"; fig.write_html(str(p), include_plotlyjs="cdn"); generated.append(str(p))
        if "ATT" in tables:
            fig = go.Figure()
            att = tables["ATT"]
            x = att["TimeS"] if "TimeS" in att.columns else list(range(len(att)))
            for c in ["DesYaw", "Yaw"]:
                if c in att.columns:
                    fig.add_trace(go.Scatter(x=x, y=att[c], mode="lines", name=c))
            fig.update_layout(title="Yaw desired vs achieved attitude", template="plotly_white", hovermode="x unified")
            add_event_markers(fig, markers)
            p = out / "yaw_attitude_desired_vs_actual.html"; fig.write_html(str(p), include_plotlyjs="cdn"); generated.append(str(p))
        if "RATE" in tables:
            rate = tables["RATE"]
            fig = make_subplots(rows=2, cols=1, shared_xaxes=True, subplot_titles=("Yaw rate", "Yaw output"))
            x = rate["TimeS"] if "TimeS" in rate.columns else list(range(len(rate)))
            for c in ["YDes", "Y"]:
                if c in rate.columns:
                    fig.add_trace(go.Scatter(x=x, y=rate[c], mode="lines", name=c), row=1, col=1)
            if "YOut" in rate.columns:
                fig.add_trace(go.Scatter(x=x, y=rate["YOut"], mode="lines", name="YOut"), row=2, col=1)
            fig.update_layout(title="Yaw rate tracking and output", template="plotly_white", hovermode="x unified")
            add_event_markers(fig, markers)
            p = out / "yaw_rate_desired_vs_actual.html"; fig.write_html(str(p), include_plotlyjs="cdn"); generated.append(str(p))
        if "PIDY" in tables:
            pid = tables["PIDY"]
            fig = make_subplots(rows=3, cols=1, shared_xaxes=True, subplot_titles=("Target/actual/error", "PID terms", "Limits"))
            x = pid["TimeS"] if "TimeS" in pid.columns else list(range(len(pid)))
            for c in ["Tar", "Act", "Err"]:
                if c in pid.columns: fig.add_trace(go.Scatter(x=x, y=pid[c], mode="lines", name=c), row=1, col=1)
            for c in ["P", "I", "D", "FF", "DFF"]:
                if c in pid.columns: fig.add_trace(go.Scatter(x=x, y=pid[c], mode="lines", name=c), row=2, col=1)
            for c in ["Dmod", "SRate", "Flags"]:
                if c in pid.columns: fig.add_trace(go.Scatter(x=x, y=pid[c], mode="lines", name=c), row=3, col=1)
            fig.update_layout(title="Yaw PID terms", template="plotly_white", hovermode="x unified")
            add_event_markers(fig, markers)
            p = out / "yaw_pid_terms.html"; fig.write_html(str(p), include_plotlyjs="cdn"); generated.append(str(p))
        rcou = combined_rcout_dataframe(tables)
        if rcou is not None:
            fig = go.Figure()
            x = rcou["TimeS"] if "TimeS" in rcou.columns else list(range(len(rcou)))
            channels = output_channel_columns(rcou)
            mapping = output_mapping_from_tables(tables, index=index, parameters=parameters)
            motor_channels = motor_channels_from_mapping(mapping, channels)
            for c in [c for c in channels if c in motor_channels]:
                fig.add_trace(go.Scatter(x=x, y=rcou[c], mode="lines", name=output_channel_label(c, mapping)))
            fig.update_layout(title="Motor outputs during yaw diagnosis", template="plotly_white", hovermode="x unified")
            add_event_markers(fig, markers)
            p = out / "motor_outputs_during_yaw_error.html"; fig.write_html(str(p), include_plotlyjs="cdn"); generated.append(str(p))
        generated.extend(write_compass_yaw_plots(tables, out, events=events))
    if symptom_class in {"attitude_rate_issue", "crash_or_loss_of_control", "general_investigation", "rc_failsafe_prearm_issue"}:
        if "RCIN" in tables and "ATT" in tables:
            mapping = rc_channel_mapping(tables, index, parameters=parameters)
            rcin = tables["RCIN"]
            att = tables["ATT"]
            for axis, des_col, actual_col in [("roll", "DesRoll", "Roll"), ("pitch", "DesPitch", "Pitch")]:
                info = mapping["axes"][axis]
                rc_col = rcin_channel_col(rcin, info["channel"])
                if not rc_col or des_col not in att.columns or actual_col not in att.columns:
                    continue
                fig = make_subplots(rows=2, cols=1, shared_xaxes=True, subplot_titles=(f"RC {axis} input", f"{axis.title()} attitude response"))
                x = rcin["TimeS"] if "TimeS" in rcin.columns else list(range(len(rcin)))
                fig.add_trace(go.Scatter(x=x, y=rcin[rc_col], mode="lines", name=f"RCIN.{rc_col} {axis}"), row=1, col=1)
                x = att["TimeS"] if "TimeS" in att.columns else list(range(len(att)))
                for c in [des_col, actual_col]:
                    fig.add_trace(go.Scatter(x=x, y=att[c], mode="lines", name=f"ATT.{c}"), row=2, col=1)
                fig.update_layout(title=f"RCIN {axis} command vs attitude response", template="plotly_white", hovermode="x unified")
                add_event_markers(fig, markers)
                p = out / f"rcin_{axis}_attitude_command_response.html"; fig.write_html(str(p), include_plotlyjs="cdn"); generated.append(str(p))
        if "ATT" in tables:
            att = tables["ATT"]
            fig = make_subplots(rows=3, cols=1, shared_xaxes=True, subplot_titles=("Roll", "Pitch", "Yaw"))
            x = att["TimeS"] if "TimeS" in att.columns else list(range(len(att)))
            for row, cols in enumerate([("DesRoll", "Roll"), ("DesPitch", "Pitch"), ("DesYaw", "Yaw")], start=1):
                for c in cols:
                    if c in att.columns:
                        fig.add_trace(go.Scatter(x=x, y=att[c], mode="lines", name=c), row=row, col=1)
            fig.update_layout(title="Attitude desired vs achieved", template="plotly_white", hovermode="x unified")
            add_event_markers(fig, markers)
            p = out / "attitude_tracking_symptom.html"; fig.write_html(str(p), include_plotlyjs="cdn"); generated.append(str(p))
        if "RATE" in tables:
            rate = tables["RATE"]
            fig = make_subplots(rows=3, cols=1, shared_xaxes=True, subplot_titles=("Roll rate", "Pitch rate", "Yaw rate"))
            x = rate["TimeS"] if "TimeS" in rate.columns else list(range(len(rate)))
            for row, cols in enumerate([("RDes", "R", "ROut"), ("PDes", "P", "POut"), ("YDes", "Y", "YOut")], start=1):
                for c in cols:
                    if c in rate.columns:
                        fig.add_trace(go.Scatter(x=x, y=rate[c], mode="lines", name=c), row=row, col=1)
            fig.update_layout(title="Rate tracking symptom plot", template="plotly_white", hovermode="x unified")
            add_event_markers(fig, markers)
            p = out / "rate_tracking_symptom.html"; fig.write_html(str(p), include_plotlyjs="cdn"); generated.append(str(p))
    if symptom_class in {"ekf_gps_issue", "compass_yaw_source_issue", "crash_or_loss_of_control", "general_investigation", "rc_failsafe_prearm_issue"}:
        if "GPS" in tables or "GPS2" in tables or "XKF4" in tables or "NKF4" in tables:
            fig = make_subplots(rows=3, cols=1, shared_xaxes=True, subplot_titles=("GPS quality", "GPS satellites/status", "EKF test ratios"))
            for group in gps_instance_groups(tables):
                gps = group["df"]
                label = group["label"]
                x = gps["TimeS"] if "TimeS" in gps.columns else list(range(len(gps)))
                for c in ["HDop", "HDOP", "HAcc", "VAcc"]:
                    if c in gps.columns:
                        fig.add_trace(go.Scatter(x=x, y=gps[c], mode="lines", name=f"{label} {c}"), row=1, col=1)
                for c in ["NSats", "Sats", "Status"]:
                    if c in gps.columns:
                        fig.add_trace(go.Scatter(x=x, y=gps[c], mode="lines", name=f"{label} {c}"), row=2, col=1)
            for group in ekf_instance_groups(tables):
                ekf = group["df"]
                label = group["label"]
                x = ekf["TimeS"] if "TimeS" in ekf.columns else list(range(len(ekf)))
                for c in ["SV", "SP", "SH", "SM", "SVT"]:
                    if c in ekf.columns:
                        fig.add_trace(go.Scatter(x=x, y=ekf[c], mode="lines", name=f"{label} {c}"), row=3, col=1)
            fig.update_layout(title="EKF/GPS symptom plot", template="plotly_white", hovermode="x unified")
            add_event_markers(fig, markers)
            p = out / "ekf_gps_symptom.html"; fig.write_html(str(p), include_plotlyjs="cdn"); generated.append(str(p))
            generated.extend(write_compass_yaw_plots(tables, out, events=events))
    if symptom_class in {"vibration_issue", "compass_yaw_source_issue", "baro_rangefinder_altitude_issue", "crash_or_loss_of_control", "general_investigation"} and "VIBE" in tables:
        vibe = tables["VIBE"]
        fig = make_subplots(rows=2, cols=1, shared_xaxes=True, subplot_titles=("Vibration", "Clipping"))
        x = vibe["TimeS"] if "TimeS" in vibe.columns else list(range(len(vibe)))
        for c in ["VibeX", "VibeY", "VibeZ"]:
            if c in vibe.columns:
                fig.add_trace(go.Scatter(x=x, y=vibe[c], mode="lines", name=c), row=1, col=1)
        for c in clip_columns(vibe):
            fig.add_trace(go.Scatter(x=x, y=vibe[c], mode="lines", name=c), row=2, col=1)
        fig.update_layout(title="Vibration symptom plot", template="plotly_white", hovermode="x unified")
        add_event_markers(fig, markers)
        p = out / "vibration_symptom.html"; fig.write_html(str(p), include_plotlyjs="cdn"); generated.append(str(p))
    if symptom_class in {"battery_power_issue", "compass_yaw_source_issue", "baro_rangefinder_altitude_issue", "crash_or_loss_of_control", "general_investigation", "rc_failsafe_prearm_issue"}:
        if "BAT" in tables or "POWR" in tables:
            fig = make_subplots(rows=2, cols=1, shared_xaxes=True, subplot_titles=("Battery", "Board power"))
            for group in battery_instance_groups(tables):
                bat = group["df"]; x = bat["TimeS"] if "TimeS" in bat.columns else list(range(len(bat)))
                for c in ["Volt", "VoltR", "Curr", "CurrTot"]:
                    if c in bat.columns:
                        fig.add_trace(go.Scatter(x=x, y=bat[c], mode="lines", name=f"{group['label']} {c}"), row=1, col=1)
            if "POWR" in tables:
                powr = tables["POWR"]; x = powr["TimeS"] if "TimeS" in powr.columns else list(range(len(powr)))
                for c in ["Vcc", "VCC", "Flags", "AccFlags"]:
                    if c in powr.columns:
                        fig.add_trace(go.Scatter(x=x, y=powr[c], mode="lines", name=c), row=2, col=1)
            fig.update_layout(title="Battery and board power symptom plot", template="plotly_white", hovermode="x unified")
            add_event_markers(fig, markers)
            p = out / "battery_power_symptom.html"; fig.write_html(str(p), include_plotlyjs="cdn"); generated.append(str(p))
        if "RCIN" in tables and ("CTUN" in tables or "BAT" in tables):
            mapping = rc_channel_mapping(tables, index, parameters=parameters)
            thr_info = mapping["axes"]["throttle"]
            thr_col = rcin_channel_col(tables["RCIN"], thr_info["channel"])
            if thr_col:
                fig = make_subplots(rows=3, cols=1, shared_xaxes=True, subplot_titles=("RC throttle", "Throttle output", "Battery"))
                rcin = tables["RCIN"]; x = rcin["TimeS"] if "TimeS" in rcin.columns else list(range(len(rcin)))
                fig.add_trace(go.Scatter(x=x, y=rcin[thr_col], mode="lines", name=f"RCIN.{thr_col} throttle"), row=1, col=1)
                if "CTUN" in tables:
                    ctun = tables["CTUN"]; x = ctun["TimeS"] if "TimeS" in ctun.columns else list(range(len(ctun)))
                    if "ThO" in ctun.columns:
                        fig.add_trace(go.Scatter(x=x, y=ctun["ThO"], mode="lines", name="CTUN.ThO"), row=2, col=1)
                if "BAT" in tables:
                    bat = tables["BAT"]; x = bat["TimeS"] if "TimeS" in bat.columns else list(range(len(bat)))
                    for c in ["Curr", "Volt", "VoltR"]:
                        if c in bat.columns:
                            fig.add_trace(go.Scatter(x=x, y=bat[c], mode="lines", name=f"BAT.{c}"), row=3, col=1)
                fig.update_layout(title="RCIN throttle vs throttle output and battery", template="plotly_white", hovermode="x unified")
                add_event_markers(fig, markers)
                p = out / "rcin_throttle_power_command_response.html"; fig.write_html(str(p), include_plotlyjs="cdn"); generated.append(str(p))
    if symptom_class in {"motor_esc_issue", "crash_or_loss_of_control", "general_investigation"}:
        rcou = combined_rcout_dataframe(tables)
        if rcou is not None:
            x = rcou["TimeS"] if "TimeS" in rcou.columns else list(range(len(rcou)))
            fig = go.Figure()
            channels = output_channel_columns(rcou)
            mapping = output_mapping_from_tables(tables, index=index, parameters=parameters)
            motor_channels = motor_channels_from_mapping(mapping, channels)
            for c in [c for c in channels if c in motor_channels]:
                fig.add_trace(go.Scatter(x=x, y=rcou[c], mode="lines", name=output_channel_label(c, mapping)))
            fig.update_layout(title="Motor/servo outputs symptom plot", template="plotly_white", hovermode="x unified")
            add_event_markers(fig, markers)
            p = out / "motor_outputs_symptom.html"; fig.write_html(str(p), include_plotlyjs="cdn"); generated.append(str(p))
        if "ESC" in tables or "ESCX" in tables or "EDT2" in tables:
            fig = make_subplots(rows=4, cols=1, shared_xaxes=True, subplot_titles=("ESC", "ESCX duty/power", "EDT2 status", "EDT2 stress"))
            for group in esc_instance_groups(tables):
                esc = group["df"]; x = esc["TimeS"] if "TimeS" in esc.columns else list(range(len(esc)))
                label = group["label"]
                if group["message"] == "ESC":
                    for c in ["RPM", "RawRPM", "Curr", "Temp", "MotTemp", "Err"]:
                        if c in esc.columns:
                            fig.add_trace(go.Scatter(x=x, y=esc[c], mode="lines", name=f"{label} {c}"), row=1, col=1)
                elif group["message"] == "ESCX":
                    for c in ["inpct", "outpct", "Pwr", "flags"]:
                        if c in esc.columns:
                            fig.add_trace(go.Scatter(x=x, y=esc[c], mode="lines", name=f"{label} {c}"), row=2, col=1)
                elif group["message"] == "EDT2":
                    for c in ["Status", "ErrCnt"]:
                        if c in esc.columns:
                            fig.add_trace(go.Scatter(x=x, y=esc[c], mode="lines", name=f"{label} {c}"), row=3, col=1)
                    for c in ["Stress", "MaxStress"]:
                        if c in esc.columns:
                            fig.add_trace(go.Scatter(x=x, y=esc[c], mode="lines", name=f"{label} {c}"), row=4, col=1)
            fig.update_layout(title="ESC/ESCX/EDT2 symptom plot", template="plotly_white", hovermode="x unified")
            add_event_markers(fig, markers)
            p = out / "esc_escx_edt2_symptom.html"; fig.write_html(str(p), include_plotlyjs="cdn"); generated.append(str(p))
    if symptom_class in {"altitude_throttle_issue", "baro_rangefinder_altitude_issue", "crash_or_loss_of_control"}:
        if "CTUN" in tables or "BARO" in tables:
            fig = make_subplots(rows=2, cols=1, shared_xaxes=True, subplot_titles=("Altitude/throttle", "Barometer"))
            if "CTUN" in tables:
                ctun = tables["CTUN"]; x = ctun["TimeS"] if "TimeS" in ctun.columns else list(range(len(ctun)))
                for c in ["Alt", "DAlt", "BAlt", "ThO", "ThH", "DCRt", "CRt"]:
                    if c in ctun.columns:
                        fig.add_trace(go.Scatter(x=x, y=ctun[c], mode="lines", name=c), row=1, col=1)
            if "BARO" in tables:
                baro = tables["BARO"]; x = baro["TimeS"] if "TimeS" in baro.columns else list(range(len(baro)))
                for c in ["Alt", "Press", "Temp"]:
                    if c in baro.columns:
                        fig.add_trace(go.Scatter(x=x, y=baro[c], mode="lines", name=c), row=2, col=1)
            fig.update_layout(title="Altitude/throttle symptom plot", template="plotly_white", hovermode="x unified")
            add_event_markers(fig, markers)
            p = out / "altitude_throttle_symptom.html"; fig.write_html(str(p), include_plotlyjs="cdn"); generated.append(str(p))
    return generated


def diagnosis_missing(index, symptom_class):
    return missing_by_tier(index, symptom_class, missing_messages)


def add_context(context, source, detail, values=None):
    if detail:
        item = {"source": source, "detail": detail}
        if values:
            item["values"] = values
        context.append(item)


def add_finding(findings, rank, possible_cause, severity, confidence, evidence, interpretation, recommended_checks, evidence_values=None):
    evidence = [e for e in evidence if e]
    if not evidence:
        return
    findings.append({
        "rank": rank,
        "possible_cause": possible_cause,
        "severity": severity,
        "confidence": confidence,
        "evidence": evidence,
        "evidence_values": evidence_values or [],
        "interpretation": interpretation,
        "recommended_checks": recommended_checks,
    })


def limit_confidence_for_missing_strong_evidence(findings, checked, missing_strongly):
    if not missing_strongly:
        return
    changed = False
    for finding in findings:
        if finding.get("confidence") == "high":
            finding["confidence"] = "medium"
            finding["confidence_limited_by_missing_strongly_recommended"] = list(missing_strongly)
            changed = True
    if changed:
        checked.append({
            "check": "Strongly recommended evidence",
            "result": "High confidence limited because these strongly recommended messages are missing: " + ", ".join(missing_strongly),
        })


def limit_confidence_for_logging_health(findings, checked, logging_health):
    if not logging_health or not logging_health.get("limits_diagnosis"):
        return
    changed = False
    for finding in findings:
        if finding.get("confidence") == "high":
            finding["confidence"] = "medium"
            finding["confidence_limited_by_logging_health"] = logging_health.get("confidence_impact")
            changed = True
    checked.append({
        "check": "Logging health",
        "result": logging_health.get("confidence_impact", "Log quality may limit diagnosis confidence."),
    })


def add_event_findings(index, findings, checked):
    errors = index.get("errors", [])
    events = index.get("events", [])
    modes = index.get("modes", [])
    if errors:
        add_finding(
            findings, 1, "Log contains ERR messages/failsafe-related entries", "safety-critical", "high",
            [str(e) for e in errors[:10]],
            "Subsystem errors and failsafe-related entries can explain sudden behaviour changes and should be placed first in the timeline.",
            ["Review ERR Subsys/ECode meanings", "Correlate ERR entries with MODE, EV, MSG, RC input and control symptoms"],
        )
    else:
        checked.append({"check": "ERR timeline", "result": "No ERR rows were indexed"})
    if modes:
        checked.append({"check": "Mode timeline", "result": f"{len(modes)} mode rows indexed; inspect exact transitions around the symptom"})
    if events:
        checked.append({"check": "Event timeline", "result": f"{len(events)} EV rows indexed; correlate with symptom time"})


def _table_text_rows(table, fields):
    if table is None or not hasattr(table, "to_dict"):
        return []
    rows = []
    for row in table.to_dict(orient="records"):
        text_parts = []
        for field in fields:
            value = row.get(field)
            if value is not None and str(value).strip() and str(value).lower() != "nan":
                text_parts.append(str(value).strip())
        if text_parts:
            time_s = safe_float(row.get("TimeS"))
            prefix = f"{time_s:.2f}s " if time_s is not None else ""
            rows.append(prefix + " ".join(text_parts))
    return rows


def add_rc_failsafe_prearm_findings(tables, index, findings, checked, context, rank=1):
    timeline_evidence = []
    timeline_evidence.extend(_table_text_rows(tables.get("MSG"), ["Message", "Msg", "message"])[:12])
    timeline_evidence.extend("ERR " + str(e) for e in index.get("errors", [])[:10])
    timeline_evidence.extend(_table_text_rows(tables.get("ARM"), ["ArmState", "Armed", "Reason", "Method", "State"])[:10])
    keywords = (
        "prearm", "pre-arm", "arm", "arming", "failsafe", "radio", "rc", "throttle",
        "gcs", "safety", "switch", "battery", "gps", "ekf", "compass", "baro", "hardware"
    )
    relevant_timeline = [item for item in timeline_evidence if any(k in item.lower() for k in keywords)]
    if relevant_timeline:
        add_finding(
            findings, rank, "Pre-arm, arming, or failsafe timeline evidence", "safety-critical", "high",
            relevant_timeline[:14],
            "MSG/ERR/EV/ARM/MODE timing is the primary evidence for arming failures and failsafe behaviour. Correlate the exact message with RC input, power, GPS/EKF/compass, and safety-switch context before changing parameters.",
            ["Identify the exact GCS/logged pre-arm or failsafe message", "Determine whether it occurred before arming, during arming, or after arming", "Resolve the implicated check rather than bypassing safety protections"],
        )
    else:
        checked.append({"check": "Pre-arm/failsafe timeline", "result": "No specific pre-arm, arming, or failsafe text was found in MSG/ERR/ARM by heuristic"})

    rcin_summary = summarize_rcin(tables, index)
    if rcin_summary.get("limitation"):
        add_context(context, "RCIN", rcin_summary["limitation"])
    if rcin_summary.get("available"):
        for axis, info in rcin_summary.get("axes", {}).items():
            if not info.get("available"):
                checked.append({"check": f"RCIN {axis}", "result": info.get("limitation", f"RCIN {axis} unavailable")})
                continue
            add_context(context, "RCIN", (
                f"RCIN {axis} channel {info['channel']} ({info['field']}): "
                f"min={info['min']:.0f} PWM us, max={info['max']:.0f} PWM us, "
                f"active={info['active_percent']:.1f}%"
            ), [
                value_with_unit(f"RCIN.{axis}.min", info["min"], "PWM us"),
                value_with_unit(f"RCIN.{axis}.max", info["max"], "PWM us"),
                value_with_unit(f"RCIN.{axis}.active_percent", info["active_percent"], "%"),
            ])
        checked.append({"check": "RCIN command/link context", "result": "RCIN is available; inspect mapped channel ranges and whether inputs disappear or become invalid around the event"})
    else:
        add_finding(
            findings, rank + 1, "RC input evidence is missing for RC/failsafe investigation", "safety-critical", "medium",
            ["RCIN message is missing"],
            "Without RCIN, the log cannot confirm whether the receiver/link disappeared, throttle input went invalid, or mapped channels behaved as expected.",
            ["Capture RCIN in the next ground or bench evidence run", "Review RC_OPTIONS, RC_PROTOCOLS, receiver wiring, receiver failsafe output, and RCMAP_* parameters"],
        )


def add_vibration_findings(tables, findings, checked, rank=4, vibration_assessment=None, symptom_class="general_investigation"):
    assessment = vibration_assessment or build_vibration_assessment(tables, symptom_class)
    add_vibration_assessment_findings(assessment, findings, checked, rank=rank, symptom_class=symptom_class)


def add_ekf_gps_findings(tables, index, findings, checked, rank=2):
    evidence = []
    gps_groups = gps_instance_groups(tables)
    for group in gps_groups:
        gps = group["df"]
        label = group["label"] if len(gps_groups) > 1 else group["message"]
        status = numeric_series(gps, ["Status"])
        if status is not None and len(status.dropna()) > 0 and float(status.min()) < 3:
            evidence.append(f"{label}.Status minimum={float(status.min()):.0f} (<3D fix)")
        nsats = numeric_series(gps, ["NSats", "Sats"])
        if nsats is not None and len(nsats.dropna()) > 0 and float(nsats.min()) < 12:
            evidence.append(f"{label} satellites minimum={float(nsats.min()):.0f}")
        hdop = numeric_series(gps, ["HDop", "HDOP"])
        if hdop is not None and len(hdop.dropna()) > 0 and float(hdop.max()) > 2.0:
            evidence.append(f"{label}.HDop max={float(hdop.max()):.2f}")
    gpa = tables.get("GPA")
    if gpa is not None:
        for col in ["HAcc", "VAcc", "SAcc", "YAcc"]:
            if col in gpa.columns:
                s = numeric_series(gpa, [col])
                if s is not None and len(s.dropna()) > 0:
                    evidence.append(f"GPA.{col}: min={float(s.min()):.2f}, max={float(s.max()):.2f}")
    for group in ekf_instance_groups(tables):
        ekf = group["df"]
        label = group["label"] if group.get("instance_certain") else ""
        for col in ["SV", "SP", "SH", "SM", "SVT"]:
            if col in ekf.columns:
                s = numeric_series(ekf, [col])
                if s is not None and len(s.dropna()) > 0 and float(s.max()) > 1.0:
                    prefix = f"{label}." if label else ""
                    evidence.append(f"{prefix}{col} max={float(s.max()):.2f}, samples >1={int((s > 1.0).sum())}")
    if index.get("errors"):
        ekf_errors = [e for e in index["errors"] if str(e.get("subsys")) in {"7", "11", "16", "17", "24", "31"}]
        if ekf_errors:
            evidence.extend(f"ERR {e}" for e in ekf_errors[:5])
    if evidence:
        add_finding(
            findings, rank, "GPS/EKF evidence may explain navigation or heading behaviour", "safety-critical", "medium", evidence[:14],
            "GPS fix quality, satellite count, HDOP/accuracy, EKF innovation test ratios, and EKF/GPS failsafe errors should be correlated with the symptom time and flight mode.",
            ["Compare Stabilize/AltHold behaviour with Loiter/Auto/RTL", "Inspect XKF3 innovations, XKF4/NKF4 test ratios, GPS status and MSG/ERR timeline", "Check vibration and power as possible secondary causes"],
        )
    else:
        checked.append({"check": "GPS/EKF health", "result": "No GPS fix, HDOP/satellite, or EKF test-ratio issue detected by heuristic"})


def add_attitude_rate_findings(tables, findings, checked, axes=("roll", "pitch", "yaw"), rank=2):
    rate = tables.get("RATE")
    att = tables.get("ATT")
    for axis in axes:
        fields = AXIS_MAP[axis]
        if att is not None and fields["att_des"] in att.columns and fields["att"] in att.columns:
            des = numeric_series(att, [fields["att_des"]])
            actual = numeric_series(att, [fields["att"]])
            err = des - actual
            if axis == "yaw":
                err = ((err + 180) % 360) - 180
            p95 = percentile([abs(v) for v in vals(err)], 95)
            if p95 is not None and p95 > (20 if axis == "yaw" else 10):
                add_finding(
                    findings, rank, f"{axis} attitude tracking error", "likely-issue", "medium",
                    [f"ATT {axis} desired-vs-achieved p95 absolute error={p95:.1f} deg"],
                    "Desired attitude and achieved attitude diverge; use RATE, PID and actuator evidence to separate tune, authority, estimator and external-disturbance causes.",
                    ["Inspect ATT and RATE around the symptom", "Check PID flags/Dmod and mapped output-channel saturation before changing gains"],
                    [value_with_unit(f"ATT.{axis}_p95_abs_error", p95, "deg")],
                )
        if rate is not None and fields["rate_des"] in rate.columns and fields["rate"] in rate.columns:
            des = numeric_series(rate, [fields["rate_des"]])
            actual = numeric_series(rate, [fields["rate"]])
            err = des - actual
            p95 = percentile([abs(v) for v in vals(err)], 95)
            evidence = []
            if p95 is not None and p95 > 30:
                evidence.append(f"RATE {axis} p95 absolute error={p95:.1f} deg/s")
            out_col = fields["out"]
            if out_col in rate.columns:
                out = numeric_series(rate, [out_col])
                out95 = percentile([abs(v) for v in vals(out)], 95)
                if out95 is not None and out95 > 0.65:
                    evidence.append(f"RATE.{out_col} p95 abs={out95:.2f} normalized")
            if evidence:
                add_finding(
                    findings, rank, f"{axis} rate tracking or controller authority issue", "likely-issue", "medium", evidence,
                    "Large rate tracking error, especially with high controller output, points toward authority/headroom, actuator, filter/noise or tune issues.",
                    ["Correlate RATE error with RCOU, PID flags/Dmod, vibration and battery/current", "Do not tune gains until actuator headroom and vibration are understood"],
                    [value_with_unit(f"RATE.{axis}_p95_abs_error", p95, "deg/s")] + ([value_with_unit(f"RATE.{out_col}_p95_abs", out95, "normalized")] if out_col in rate.columns and out95 is not None else []),
                )
        pid_name = fields["pid"]
        if pid_name in tables:
            pid = tables[pid_name]
            evidence = []
            flags = numeric_series(pid, ["Flags"])
            if flags is not None and len(flags.dropna()) > 0:
                f_i = flags.fillna(0).astype(int)
                limit_count = int(((f_i & 1) != 0).sum())
                pd_limit_count = int(((f_i & 2) != 0).sum())
                if limit_count or pd_limit_count:
                    evidence.append(f"{pid_name}.Flags limit count={limit_count}, PD-sum-limit count={pd_limit_count}")
            dmod = numeric_series(pid, ["Dmod"])
            if dmod is not None and len(dmod.dropna()) > 0 and float(dmod.min()) < 0.8:
                evidence.append(f"{pid_name}.Dmod minimum={float(dmod.min()):.2f} normalized")
            if evidence:
                add_finding(
                    findings, rank, f"{axis} PID limiting or D-term reduction", "likely-issue", "high", evidence,
                    "PID flags and Dmod identify saturation/anti-windup, PD-sum limiting, or dynamic D reduction; correlate before changing gains.",
                    ["Overlay PID flags/Dmod with RATE outputs, RCOU and vibration", "Review filter/noise and actuator headroom before gain changes"],
                    ([value_with_unit(f"{pid_name}.Dmod_min", float(dmod.min()), "normalized")] if dmod is not None and len(dmod.dropna()) > 0 else []),
                )
    if not any("RATE" in f.get("evidence", [""])[0] or "ATT" in f.get("evidence", [""])[0] for f in findings):
        checked.append({"check": "Attitude/rate tracking", "result": "No ATT/RATE tracking issue detected by heuristic for requested axes"})


def add_altitude_findings(tables, findings, checked, context=None, rank=2):
    if context is None:
        context = []
    evidence = []
    if "CTUN" in tables:
        ctun = tables["CTUN"]
        for col in ["Alt", "DAlt", "BAlt", "ThO", "ThH", "DCRt", "CRt"]:
            if col in ctun.columns:
                s = numeric_series(ctun, [col])
                if s is not None and len(s.dropna()) > 0:
                    unit = "m" if col in {"Alt", "DAlt", "BAlt"} else ("normalized" if col in {"ThO", "ThH"} else "unknown")
                    add_context(context, "CTUN", f"CTUN.{col}: min={float(s.min()):.2f} {unit}, max={float(s.max()):.2f} {unit}", [
                        value_with_unit(f"CTUN.{col}_min", float(s.min()), unit),
                        value_with_unit(f"CTUN.{col}_max", float(s.max()), unit),
                    ])
        if "Alt" in ctun.columns and "DAlt" in ctun.columns:
            err = numeric_series(ctun, ["DAlt"]) - numeric_series(ctun, ["Alt"])
            p95 = percentile([abs(v) for v in vals(err)], 95)
            if p95 is not None and p95 > 3:
                evidence.append(f"CTUN altitude target-vs-actual p95 abs error={p95:.2f} m")
    if "BARO" in tables:
        baro = tables["BARO"]
        for col in ["Alt", "Press", "Temp"]:
            if col in baro.columns:
                s = numeric_series(baro, [col])
                if s is not None and len(s.dropna()) > 0:
                    unit = "m" if col == "Alt" else ("unknown" if col == "Press" else "degC")
                    add_context(context, "BARO", f"BARO.{col}: min={float(s.min()):.2f} {unit}, max={float(s.max()):.2f} {unit}", [
                        value_with_unit(f"BARO.{col}_min", float(s.min()), unit),
                        value_with_unit(f"BARO.{col}_max", float(s.max()), unit),
                    ])
    if evidence:
        add_finding(
            findings, rank, "Altitude/throttle evidence requires vibration, power and estimator correlation", "likely-issue", "medium", evidence[:14],
            "Altitude symptoms can come from throttle saturation, vibration-driven estimator error, barometer/rangefinder issues, battery sag, or control tuning. CTUN/BARO evidence should not be read in isolation.",
            ["Correlate CTUN with VIBE/clipping, BAT/POWR, GPS/EKF and RCOU", "Check if the issue only appears in AltHold/Loiter/Auto/RTL modes"],
        )
    else:
        checked.append({"check": "Altitude/throttle", "result": "No CTUN/BARO altitude evidence available or no heuristic altitude issue detected"})


def diagnose_yaw(tables, index, vibration_assessment=None):
    findings = []
    context = []
    checked = []
    missing_required, missing_strongly, missing_optional = diagnosis_missing(index, "yaw_misbehaviour")
    missing_strongly = missing_strongly_with_available_alternatives(missing_strongly, tables)

    command_response = build_command_response_investigation(tables, index, axes=("yaw",))
    findings.extend(command_response["findings"])
    context.extend(command_response["context"])
    checked.extend(command_response["checked"])

    # Commanded vs uncommanded yaw
    if "ATT" in tables and all(c in tables["ATT"].columns for c in ["DesYaw", "Yaw"]):
        att = tables["ATT"]
        des = numeric_series(att, ["DesYaw"])
        yaw = numeric_series(att, ["Yaw"])
        err = ((des - yaw + 180) % 360) - 180
        p95 = percentile([abs(v) for v in vals(err)], 95)
        maxabs = max([abs(v) for v in vals(err)] or [0.0])
        if p95 is not None and p95 > 20:
            findings.append({
                "rank": 10,
                "possible_cause": "Yaw attitude not tracking desired heading",
                "severity": "likely-issue",
                "confidence": "medium",
                "evidence": [f"ATT yaw desired-vs-achieved p95 absolute error is {p95:.1f} deg; max {maxabs:.1f} deg"],
                "evidence_values": [
                    value_with_unit("ATT.yaw_p95_abs_error", p95, "deg"),
                    value_with_unit("ATT.yaw_max_abs_error", maxabs, "deg"),
                ],
                "interpretation": "The aircraft heading estimate/response diverges from desired yaw. RATE/PIDY/RCOU decide whether this is controller, actuator, or estimator related.",
                "recommended_checks": ["Inspect RATE.YDes vs RATE.Y", "Inspect PIDY flags and motor output saturation", "Check compass/EKF yaw evidence if the physical aircraft did not actually yaw"],
            })
        else:
            checked.append({"check": "ATT yaw tracking", "result": f"No large heading tracking error detected by heuristic; p95={p95} deg", "values": [value_with_unit("ATT.yaw_p95_abs_error", p95, "deg")]})

    # Rate tracking and output authority
    rate_tracking_bad = False
    output_high = False
    if "RATE" in tables and all(c in tables["RATE"].columns for c in ["YDes", "Y"]):
        rate = tables["RATE"]
        err = numeric_series(rate, ["YDes"]) - numeric_series(rate, ["Y"])
        p95 = percentile([abs(v) for v in vals(err)], 95)
        maxabs = max([abs(v) for v in vals(err)] or [0.0])
        if p95 is not None and p95 > 30:
            rate_tracking_bad = True
        if "YOut" in rate.columns:
            yout = numeric_series(rate, ["YOut"])
            out_p95 = percentile([abs(v) for v in vals(yout)], 95)
            out_max = max([abs(v) for v in vals(yout)] or [0.0])
            output_high = out_p95 is not None and out_p95 > 0.65
        else:
            out_p95 = None; out_max = None
        if rate_tracking_bad and output_high:
            output_sources = actuator_output_evidence_sources(tables)
            evidence = [
                f"RATE yaw error p95 is {p95:.1f} deg/s; max {maxabs:.1f} deg/s",
                f"RATE.YOut p95 abs is {out_p95:.2f} normalized; max abs {out_max:.2f} normalized",
            ]
            if output_sources:
                evidence.append("Actuator output rows are available from " + "/".join(output_sources))
            findings.append({
                "rank": 1,
                "possible_cause": "Yaw authority limited or yaw controller output saturated",
                "severity": "safety-critical",
                "confidence": "high" if output_sources else "medium",
                "evidence": evidence,
                "evidence_values": [
                    value_with_unit("RATE.yaw_p95_abs_error", p95, "deg/s"),
                    value_with_unit("RATE.yaw_max_abs_error", maxabs, "deg/s"),
                    value_with_unit("RATE.YOut_p95_abs", out_p95, "normalized"),
                    value_with_unit("RATE.YOut_max_abs", out_max, "normalized"),
                ],
                "interpretation": "The controller is asking for yaw response but achieved yaw rate is not following well. This points first to yaw authority, actuator saturation, motor/prop/ESC/frame setup, or severe power/throttle limitation rather than simply changing yaw P.",
                "recommended_checks": ["Check motor order and prop direction", "Check frame class/type and motor mapping", "Check mapped output-channel saturation", "Check ESC/motor health and yaw torque asymmetry", "Do not continue flight tests until bench/ground checks are complete"],
            })
        elif rate_tracking_bad:
            findings.append({
                "rank": 3,
                "possible_cause": "Yaw rate tracking error",
                "severity": "likely-issue",
                "confidence": "medium",
                "evidence": [f"RATE yaw error p95 is {p95:.1f} deg/s; max {maxabs:.1f} deg/s"],
                "evidence_values": [
                    value_with_unit("RATE.yaw_p95_abs_error", p95, "deg/s"),
                    value_with_unit("RATE.yaw_max_abs_error", maxabs, "deg/s"),
                ],
                "interpretation": "Yaw rate response is not following target. Output/saturation evidence is incomplete or not high by heuristic.",
                "recommended_checks": ["Check PIDY terms", "Check mapped output channels", "Compare in hover vs high-throttle sections"],
            })
        else:
            checked.append({"check": "RATE yaw tracking", "result": f"Yaw rate tracking not flagged by heuristic; p95={p95} deg/s, YOut p95={out_p95} normalized", "values": [value_with_unit("RATE.yaw_p95_abs_error", p95, "deg/s"), value_with_unit("RATE.YOut_p95_abs", out_p95, "normalized")]})

    # PIDY flags/noise
    if "PIDY" in tables:
        pid = tables["PIDY"]
        evidence = []
        flags = numeric_series(pid, ["Flags"])
        if flags is not None and len(flags.dropna()) > 0:
            f_i = flags.fillna(0).astype(int)
            limit_count = int(((f_i & 1) != 0).sum())
            pd_limit_count = int(((f_i & 2) != 0).sum())
            if limit_count > 0 or pd_limit_count > 0:
                evidence.append(f"PIDY limit flag count={limit_count}, PD-sum-limit count={pd_limit_count}")
        err = numeric_series(pid, ["Err"])
        if err is not None and len(err.dropna()) > 0:
            err_p95 = percentile([abs(v) for v in vals(err)], 95)
            if err_p95 is not None and err_p95 > 30:
                evidence.append(f"PIDY.Err p95 abs={err_p95:.2f} deg/s")
            else:
                checked.append({"check": "PIDY.Err magnitude", "result": f"PIDY.Err p95 abs={err_p95:.2f} deg/s below heuristic threshold", "values": [value_with_unit("PIDY.Err_p95_abs", err_p95, "deg/s")]})
        dmod = numeric_series(pid, ["Dmod"])
        if dmod is not None and len(dmod.dropna()) > 0 and float(dmod.min()) < 0.8:
            evidence.append(f"PIDY.Dmod minimum={float(dmod.min()):.2f} normalized")
        if evidence:
            findings.append({
                "rank": 2,
                "possible_cause": "Yaw PID limiting/noise/anti-windup behaviour",
                "severity": "likely-issue",
                "confidence": "high",
                "evidence": evidence,
                "evidence_values": ([value_with_unit("PIDY.Dmod_min", float(dmod.min()), "normalized")] if dmod is not None and len(dmod.dropna()) > 0 else []),
                "interpretation": "The yaw PID controller logged limiting or protective behaviour. Correlate with RCOU, vibration and power before changing gains.",
                "recommended_checks": ["Overlay PIDY.Flags with RATE.YOut and RCOU", "Check notch/filter setup and vibration", "Check whether high throttle removes yaw authority"],
            })
        else:
            checked.append({"check": "PIDY flags/limits", "result": "No PIDY limit/Dmod issue detected by heuristic"})

    add_motor_esc_findings(tables, findings, checked, context, rank=1, index=index)

    compass_yaw = build_compass_yaw_investigation(tables)
    findings.extend(compass_yaw["findings"])
    context.extend(compass_yaw["context"])
    checked.extend(compass_yaw["checked"])

    add_vibration_findings(tables, findings, checked, rank=4, vibration_assessment=vibration_assessment, symptom_class="yaw_misbehaviour")
    add_power_findings(tables, findings, checked, context, rank=3)

    limit_confidence_for_missing_strong_evidence(findings, checked, missing_strongly)
    limit_confidence_for_logging_health(findings, checked, index.get("logging_health"))
    findings = sorted(findings, key=lambda f: (f.get("rank", 99), 0 if f.get("severity") == "safety-critical" else 1))
    return findings, context, checked, missing_required, missing_strongly, missing_optional


def diagnose_by_class(symptom_class, tables, index, vibration_assessment=None):
    findings = []
    context = []
    checked = []
    missing_required, missing_strongly, missing_optional = diagnosis_missing(index, symptom_class)
    add_event_findings(index, findings, checked)
    if symptom_class in {"attitude_rate_issue", "crash_or_loss_of_control", "general_investigation"}:
        command_response = build_command_response_investigation(tables, index, axes=("roll", "pitch", "yaw"))
        findings.extend(command_response["findings"])
        context.extend(command_response["context"])
        checked.extend(command_response["checked"])

    if symptom_class == "attitude_rate_issue":
        add_attitude_rate_findings(tables, findings, checked, axes=("roll", "pitch"), rank=2)
        add_motor_esc_findings(tables, findings, checked, context, rank=3, index=index)
        add_vibration_findings(tables, findings, checked, rank=3, vibration_assessment=vibration_assessment, symptom_class=symptom_class)
        add_power_findings(tables, findings, checked, context, rank=4)
    elif symptom_class == "ekf_gps_issue":
        add_ekf_gps_findings(tables, index, findings, checked, rank=1)
        compass_yaw = build_compass_yaw_investigation(tables)
        findings.extend(compass_yaw["findings"])
        context.extend(compass_yaw["context"])
        checked.extend(compass_yaw["checked"])
        add_vibration_findings(tables, findings, checked, rank=2, vibration_assessment=vibration_assessment, symptom_class=symptom_class)
        add_power_findings(tables, findings, checked, context, rank=3)
        add_attitude_rate_findings(tables, findings, checked, axes=("roll", "pitch", "yaw"), rank=4)
    elif symptom_class == "compass_yaw_source_issue":
        compass_yaw = build_compass_yaw_investigation(tables)
        findings.extend(compass_yaw["findings"])
        context.extend(compass_yaw["context"])
        checked.extend(compass_yaw["checked"])
        add_ekf_gps_findings(tables, index, findings, checked, rank=2)
        add_attitude_rate_findings(tables, findings, checked, axes=("yaw",), rank=3)
        add_vibration_findings(tables, findings, checked, rank=4, vibration_assessment=vibration_assessment, symptom_class=symptom_class)
        add_power_findings(tables, findings, checked, context, rank=4)
    elif symptom_class == "vibration_issue":
        add_vibration_findings(tables, findings, checked, rank=1, vibration_assessment=vibration_assessment, symptom_class=symptom_class)
        add_attitude_rate_findings(tables, findings, checked, axes=("roll", "pitch", "yaw"), rank=2)
        add_ekf_gps_findings(tables, index, findings, checked, rank=3)
    elif symptom_class == "battery_power_issue":
        add_power_findings(tables, findings, checked, context, rank=1)
        add_motor_esc_findings(tables, findings, checked, context, rank=2, index=index)
        add_attitude_rate_findings(tables, findings, checked, axes=("roll", "pitch", "yaw"), rank=3)
    elif symptom_class == "rc_failsafe_prearm_issue":
        add_rc_failsafe_prearm_findings(tables, index, findings, checked, context, rank=1)
        add_power_findings(tables, findings, checked, context, rank=2)
        add_ekf_gps_findings(tables, index, findings, checked, rank=3)
        compass_yaw = build_compass_yaw_investigation(tables)
        findings.extend(compass_yaw["findings"])
        context.extend(compass_yaw["context"])
        checked.extend(compass_yaw["checked"])
    elif symptom_class == "motor_esc_issue":
        add_motor_esc_findings(tables, findings, checked, context, rank=1, index=index)
        add_attitude_rate_findings(tables, findings, checked, axes=("roll", "pitch", "yaw"), rank=2)
        add_power_findings(tables, findings, checked, context, rank=3)
        add_vibration_findings(tables, findings, checked, rank=4, vibration_assessment=vibration_assessment, symptom_class=symptom_class)
    elif symptom_class == "crash_or_loss_of_control":
        add_motor_esc_findings(tables, findings, checked, context, rank=1, index=index)
        add_attitude_rate_findings(tables, findings, checked, axes=("roll", "pitch", "yaw"), rank=1)
        add_power_findings(tables, findings, checked, context, rank=2)
        add_ekf_gps_findings(tables, index, findings, checked, rank=2)
        add_vibration_findings(tables, findings, checked, rank=3, vibration_assessment=vibration_assessment, symptom_class=symptom_class)
        add_altitude_findings(tables, findings, checked, context, rank=3)
    elif symptom_class == "altitude_throttle_issue":
        add_altitude_findings(tables, findings, checked, context, rank=1)
        add_vibration_findings(tables, findings, checked, rank=2, vibration_assessment=vibration_assessment, symptom_class=symptom_class)
        add_power_findings(tables, findings, checked, context, rank=2)
        add_motor_esc_findings(tables, findings, checked, context, rank=3, index=index)
        add_ekf_gps_findings(tables, index, findings, checked, rank=3)
    elif symptom_class == "baro_rangefinder_altitude_issue":
        add_altitude_findings(tables, findings, checked, context, rank=1)
        add_ekf_gps_findings(tables, index, findings, checked, rank=2)
        add_vibration_findings(tables, findings, checked, rank=2, vibration_assessment=vibration_assessment, symptom_class=symptom_class)
        add_power_findings(tables, findings, checked, context, rank=3)
        add_motor_esc_findings(tables, findings, checked, context, rank=4, index=index)
    else:
        add_attitude_rate_findings(tables, findings, checked, axes=("roll", "pitch", "yaw"), rank=2)
        add_motor_esc_findings(tables, findings, checked, context, rank=2, index=index)
        add_ekf_gps_findings(tables, index, findings, checked, rank=3)
        add_vibration_findings(tables, findings, checked, rank=3, vibration_assessment=vibration_assessment, symptom_class=symptom_class)
        add_power_findings(tables, findings, checked, context, rank=4)

    limit_confidence_for_missing_strong_evidence(findings, checked, missing_strongly)
    limit_confidence_for_logging_health(findings, checked, index.get("logging_health"))
    findings = sorted(findings, key=lambda f: (f.get("rank", 99), severity_rank(f.get("severity", ""))))
    return findings, context, checked, missing_required, missing_strongly, missing_optional


def main() -> int:
    p = argparse.ArgumentParser(description="Symptom-led ArduPilot log diagnosis.")
    p.add_argument("log")
    p.add_argument("--symptom", required=True)
    p.add_argument("--out", default="diagnosis.json")
    p.add_argument("--plots", default=None)
    p.add_argument("--window", default=None, help="Optional TimeS window as START:END or around:CENTER:RADIUS")
    p.add_argument("--start-time", type=float, default=None, help="Optional start TimeS for row collection")
    p.add_argument("--end-time", type=float, default=None, help="Optional end TimeS for row collection")
    p.add_argument("--messages", default=None, help="Comma-separated message names to parse, or ALL. Defaults to symptom-relevant messages.")
    p.add_argument("--max-messages", type=int, default=None, help="Optional parse limit for quick diagnosis")
    p.add_argument("--armed-only", action="store_true", help="Collect rows only while ARM messages indicate armed state when available")
    p.add_argument("--airborne-only", action="store_true", help="Filter the selected window to active airborne-looking rows")
    p.add_argument("--active-flight-only", action="store_true", help="Filter the selected window to rows that conservatively look like active flight")
    p.add_argument("--exclude-ground-spool", action="store_true", help="Remove obvious ground spool, landing, or disarmed rows from the selected window")
    p.add_argument("--min-alt", type=float, default=1.0, help="Minimum relative altitude in metres for active-flight filtering when altitude is available")
    p.add_argument("--min-throttle", type=float, default=0.15, help="Minimum normalized throttle/output for active-flight filtering when throttle is available")
    p.add_argument("--mode", default=None, help="Select active intervals for a flight mode name or numeric Copter mode id")
    p.add_argument("--around-msg", default=None, help="Select a window around the first matching MSG text")
    p.add_argument("--around-event", default=None, help="Select a window around matching EV/MSG/MODE text")
    p.add_argument("--around-error", action="store_true", help="Select a window around the first ERR message")
    p.add_argument("--takeoff-only", action="store_true", help="Select an approximate takeoff climb window")
    p.add_argument("--hover-candidates", action="store_true", help="Select an approximate stable hover candidate window")
    p.add_argument("--hover-min-duration", type=float, default=5.0, help="Minimum duration in seconds for --hover-candidates")
    p.add_argument("--hover-alt-span-max", type=float, default=0.75, help="Maximum altitude span in metres for --hover-candidates")
    p.add_argument("--hover-throttle-min", type=float, default=0.25, help="Minimum CTUN throttle for --hover-candidates when throttle is available")
    p.add_argument("--hover-throttle-max", type=float, default=0.75, help="Maximum CTUN throttle for --hover-candidates when throttle is available")
    p.add_argument("--high-throttle-only", action="store_true", help="Select a high-throttle output/demand window")
    p.add_argument("--around-radius", type=float, default=10.0, help="Seconds before/after around-msg/event/error selectors")
    p.add_argument("--high-throttle-percentile", type=float, default=90.0)
    p.add_argument("--high-throttle-threshold", type=float, default=None)
    p.add_argument("--events", action="store_true", help="Overlay MODE/ERR/EV/MSG markers on generated plots")
    p.add_argument("--params", help="External Mission Planner/QGC/MAVProxy parameter export for configuration context")
    args = p.parse_args()
    try:
        symptom_class = classify_symptom(args.symptom)
        window = {"start_s": args.start_time, "end_s": args.end_time}
        if args.start_time is not None:
            window["start_s"] = args.start_time
        if args.end_time is not None:
            window["end_s"] = args.end_time
        if window["start_s"] is not None and window["end_s"] is not None and window["end_s"] < window["start_s"]:
            raise AnalysisError("--end-time must be greater than or equal to --start-time")
        if args.messages:
            include = None if args.messages.strip().upper() == "ALL" else [m.strip().upper() for m in args.messages.split(",") if m.strip()]
        else:
            spec = requirement_spec(symptom_class)
            include = []
            for msg in spec["required_messages"] + spec["strongly_recommended_messages"] + spec["optional_context_messages"] + ["PARM"]:
                if msg not in include:
                    include.append(msg)
            if any([args.mode, args.around_msg, args.around_event, args.around_error, args.takeoff_only, args.hover_candidates, args.high_throttle_only, args.airborne_only, args.active_flight_only, args.exclude_ground_spool]):
                for msg in ["MODE", "MSG", "EV", "ERR", "ARM", "CTUN", "ATT", "BARO", "GPS", "RCOU", "RCO2", "RCO3"]:
                    if msg not in include:
                        include.append(msg)
        rows, index, stats = collect_dataflash(
            args.log,
            include=include,
            max_messages=args.max_messages,
            start_s=window["start_s"],
            end_s=window["end_s"],
            armed_only=args.armed_only,
        )
        external_parameter_context = parse_param_file(args.params) if args.params else None
        merged_params = merge_external_parameters(index, external_parameter_context)
        parameter_index = merged_params["index"]
        tables = {typ: rows_to_dataframe(data) for typ, data in rows.items() if data and typ not in {"FMT", "FMTU"}}
        selection = select_analysis_window(
            tables,
            window=args.window,
            mode=args.mode,
            armed_only=args.armed_only,
            around_msg=args.around_msg,
            around_event=args.around_event,
            around_error=args.around_error,
            takeoff_only=args.takeoff_only,
            hover_candidates=args.hover_candidates,
            hover_min_duration_s=args.hover_min_duration,
            hover_alt_span_max_m=args.hover_alt_span_max,
            hover_throttle_min=args.hover_throttle_min,
            hover_throttle_max=args.hover_throttle_max,
            high_throttle_only=args.high_throttle_only,
            around_radius_s=args.around_radius,
            high_throttle_percentile=args.high_throttle_percentile,
            high_throttle_threshold=args.high_throttle_threshold,
            log_end_s=index.get("end_time_s"),
            vehicle_scope=vehicle_scope(index),
        )
        if args.start_time is not None or args.end_time is not None:
            selection["start_s"] = window["start_s"] if window["start_s"] is not None else selection.get("start_s")
            selection["end_s"] = window["end_s"] if window["end_s"] is not None else selection.get("end_s")
            selection["rule"] = "start_end" if selection.get("rule") == "whole_log" else selection.get("rule")
        full_tables = tables
        selected_tables = filter_tables_by_time(
            full_tables,
            start_s=selection.get("start_s"),
            end_s=selection.get("end_s"),
            intervals=selection.get("intervals_used"),
        )
        selection, active_profile = apply_active_flight_filter(
            selection,
            selected_tables,
            active_flight_only=args.active_flight_only,
            airborne_only=args.airborne_only,
            exclude_ground_spool=args.exclude_ground_spool,
            min_alt=args.min_alt,
            min_throttle=args.min_throttle,
        )
        tables = filter_tables_by_time(
            selected_tables,
            start_s=selection.get("start_s"),
            end_s=selection.get("end_s"),
            intervals=selection.get("intervals_used"),
        )
        window_quality = active_profile.get("quality", {})
        vibration_assessment = build_vibration_assessment(
            full_tables,
            symptom_class,
            window_tables=tables,
            analysis_window=selection,
        )
        rcin_summary = summarize_rcin(tables, parameter_index)
        parameter_context = select_relevant_parameters(symptom_class, index=parameter_index, tables=full_tables)
        if symptom_class == "yaw_misbehaviour":
            findings, context, checked, missing_required, missing_strongly, missing_optional = diagnose_yaw(tables, parameter_index, vibration_assessment=vibration_assessment)
        else:
            findings, context, checked, missing_required, missing_strongly, missing_optional = diagnose_by_class(symptom_class, tables, parameter_index, vibration_assessment=vibration_assessment)
        plots = make_targeted_plots_from_tables(
            tables,
            symptom_class,
            args.plots,
            events=args.events,
            index=parameter_index,
            parameters=merged_params["parameters"],
        ) if args.plots else []
        warnings = []
        if stats.get("max_messages_reached"):
            warnings.append("Diagnosis stopped at --max-messages; evidence may be partial.")
        if args.messages and args.messages.strip().upper() != "ALL":
            warnings.append("Diagnosis used an explicit --messages filter; unavailable evidence may be due to filtering.")
        if args.armed_only and not stats.get("armed_filter_supported"):
            warnings.append("--armed-only was requested, but ARM state could not be confirmed from ARM messages.")
        warnings.extend(selection.get("warnings", []))
        if window_quality.get("ground_spool_rows_included"):
            warnings.append("Selected analysis window includes rows that look like ground spool, landing, or disarmed time; use --active-flight-only or --exclude-ground-spool when comparing flight behaviour.")
        logging_health = index.get("logging_health", {})
        if logging_health.get("confirmed_dropouts"):
            warnings.append("Confirmed logging dropout/drop count evidence was found; inspect logging_health.confirmed_dropouts.")
        if logging_health.get("possible_dropouts"):
            warnings.append("Possible logging dropout context was found; inspect logging_health.possible_dropouts.")
        if logging_health.get("limits_diagnosis"):
            warnings.append("Logging health limits diagnosis confidence: " + logging_health.get("confidence_impact", "inspect logging_health"))
        result = {
            "symptom_text": args.symptom,
            "symptom_class": symptom_class,
            "analysis_window": selection,
            "analysis_window_units": {"start_s": "s", "end_s": "s"},
            "window_quality": window_quality,
            "log": {"file": args.log, "vehicle": index.get("vehicle"), "firmware": index.get("firmware"), "duration_s": index.get("duration_s")},
            "units": {"log.duration_s": "s"},
            "parser": stats,
            "warnings": warnings,
            "logging_health": logging_health,
            "findings": findings,
            "context": context,
            "checked_but_not_supported": checked,
            "parameter_context": parameter_context,
            "external_parameter_context": merged_params["external_parameter_context"],
            "parameter_conflicts": merged_params["parameter_conflicts"],
            "parameter_source_precedence": merged_params["parameter_source_precedence"],
            "rcin_command_context": rcin_summary,
            "vibration_context": vibration_assessment.get("vibration_context", {}),
            "vibration_relevance_to_symptom": vibration_assessment.get("vibration_relevance_to_symptom", {}),
            "vibration_confidence_limits": vibration_assessment.get("vibration_confidence_limits", []),
            "missing_required": missing_required,
            "missing_strongly_recommended": missing_strongly,
            "missing_optional": missing_optional,
            "plots": plots,
            "logging_dropouts": index.get("logging_dropouts", []),
            "possible_logging_dropouts": index.get("possible_logging_dropouts", []),
            "safety_note": "Do not treat this diagnosis as clearance to fly. Bench and ground checks are required after any configuration, mechanical, power, or tuning changes.",
            "what_cannot_be_concluded": build_cannot_conclude(
                symptom_class,
                missing_required=missing_required,
                missing_strongly_recommended=missing_strongly,
                missing_optional=missing_optional,
                tables=tables,
                index=parameter_index,
            ),
        }
        write_json(args.out, result)
        print(f"Diagnosis class={symptom_class}; findings={len(findings)}; plots={len(plots)}")
        return 0
    except AnalysisError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def build_cannot_conclude(
    symptom_class,
    missing_required=None,
    missing_strongly_recommended=None,
    missing_optional=None,
    tables=None,
    index=None,
):
    tables = tables or {}
    missing_required = missing_required or []
    missing_strongly_recommended = missing_strongly_with_available_alternatives(missing_strongly_recommended, tables)
    missing_strongly_recommended = missing_strongly_recommended or []
    missing_optional = missing_optional or []
    out = []
    if "ESC" not in tables and "ESCX" not in tables and "EDT2" not in tables:
        out.append("ESC-level motor/ESC confirmation is not possible because ESC/ESCX/EDT2 telemetry is missing.")
    elif "ESCX" in tables and "ESC" not in tables and "EDT2" not in tables:
        out.append("ESCX duty/power/flags are available, but ESC RPM/current/temperature/error and EDT2 status confirmation are not available because ESC and EDT2 telemetry are missing.")
    if not has_actuator_output_evidence(tables):
        out.append("Actuator output saturation cannot be confirmed because RCOU/RCO2/RCO3 is missing.")
    elif not output_mapping_from_tables(tables, index=index):
        out.append("Output mapping could not be confirmed from parameters; RCOU/RCO2/RCO3 channel interpretation is generic.")
    if "PIDY" not in tables and symptom_class == "yaw_misbehaviour":
        out.append("Yaw PID limiting, I-term behaviour, and Dmod cannot be confirmed because PIDY is missing.")
    if "XKF4" not in tables and "NKF4" not in tables:
        out.append("EKF test-ratio evidence may be incomplete because XKF4/NKF4 is missing.")
    if "VIBE" not in tables:
        out.append("Vibration contribution cannot be assessed from VIBE because VIBE is missing.")
    for msg in missing_required:
        out.append(f"Required message `{msg}` is missing; core diagnosis may not be possible.")
    for msg in missing_strongly_recommended:
        out.append(f"Strongly recommended message `{msg}` is missing; confidence is reduced.")
    for msg in missing_optional:
        out.append(f"Optional context message `{msg}` is missing; this limits supporting context only.")
    return out

if __name__ == "__main__":
    raise SystemExit(main())
