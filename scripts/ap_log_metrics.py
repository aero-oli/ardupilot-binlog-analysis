#!/usr/bin/env python3
from __future__ import annotations
import argparse
import math
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ap_common import (
    AnalysisError, AXIS_MAP, clip_columns, combined_rcout_dataframe, df_duration, filter_tables_by_time,
    first_existing, fmt, get_col, load_tables, md_table, motor_channels_from_mapping, numeric_series,
    output_channel_columns, output_mapping_from_tables, parse_time_window,
    percentile, rms, summarise_numeric, write_json
)


def series_values(s):
    if s is None:
        return []
    try:
        return [float(v) for v in s.dropna().tolist()]
    except Exception:
        return []


def compute_metrics(tables, analysis_window=None):
    metrics = {
        "messages_present": sorted(tables.keys()),
        "analysis_window": analysis_window or {"start_s": None, "end_s": None},
        "flight": {},
        "health": {},
        "tuning": {},
        "generic_messages": {},
        "confidence": {"overall": "medium", "reasons": []},
    }

    durations = {name: df_duration(df) for name, df in tables.items() if df_duration(df) is not None}
    if durations:
        metrics["flight"]["duration_s_estimate"] = round(max(durations.values()), 3)
    else:
        metrics["confidence"]["overall"] = "low"
        metrics["confidence"]["reasons"].append("No usable TimeS field found in extracted tables")

    # Events/errors/modes
    if "ERR" in tables:
        err = tables["ERR"]
        metrics["health"]["errors"] = {
            "count": int(len(err)),
            "rows": err.head(100).to_dict(orient="records"),
        }
    if "MODE" in tables:
        mode = tables["MODE"]
        col = get_col(mode, ["Mode", "ModeNum", "Name"])
        metrics["flight"]["modes_seen"] = [] if col is None else [str(x) for x in mode[col].dropna().unique().tolist()[:50]]
    if "MODE" not in tables and "ARM" not in tables:
        metrics["confidence"]["reasons"].append("MODE/ARM missing; armed state, mode timeline and whether this was a flight cannot be confirmed")
    if not any(name in tables for name in ["CTUN", "POS", "GPS", "GPS2", "BARO", "RNGF"]):
        metrics["confidence"]["reasons"].append("No altitude/position context found; bench-only logs and flight logs may not be distinguishable")

    # Battery and power
    batt_name, bat = first_existing(tables, ["BAT", "BCL"])
    if bat is not None:
        volt = numeric_series(bat, ["Volt", "VoltR", "V"])
        curr = numeric_series(bat, ["Curr", "I"])
        currtot = numeric_series(bat, ["CurrTot", "CTot", "mAh"])
        battery = {"message": batt_name}
        if volt is not None and len(volt.dropna()) > 0:
            battery["min_voltage"] = float(volt.min())
            battery["mean_voltage"] = float(volt.mean())
        if curr is not None and len(curr.dropna()) > 0:
            battery["max_current"] = float(curr.max())
            battery["mean_current"] = float(curr.mean())
        if currtot is not None and len(currtot.dropna()) > 0:
            battery["capacity_used_mah_max"] = float(currtot.max())
        metrics["health"]["battery"] = battery
    if "POWR" in tables:
        powr = tables["POWR"]
        vcc = numeric_series(powr, ["Vcc", "VCC"])
        if vcc is not None and len(vcc.dropna()) > 0:
            metrics["health"]["board_power"] = {"vcc_min": float(vcc.min()), "vcc_max": float(vcc.max()), "vcc_span": float(vcc.max()-vcc.min())}

    # GPS
    gps_name, gps = first_existing(tables, ["GPS", "GPS2"])
    if gps is not None:
        hdop = numeric_series(gps, ["HDop", "HDOP", "HAcc"])
        nsats = numeric_series(gps, ["NSats", "Sats", "Satellites"])
        gps_m = {"message": gps_name}
        if hdop is not None and len(hdop.dropna()) > 0:
            gps_m["hdop_or_hacc_max"] = float(hdop.max())
            gps_m["hdop_or_hacc_mean"] = float(hdop.mean())
            gps_m["samples_gt_2"] = int((hdop > 2.0).sum())
        if nsats is not None and len(nsats.dropna()) > 0:
            gps_m["nsats_min"] = float(nsats.min())
            gps_m["nsats_mean"] = float(nsats.mean())
            gps_m["samples_lt_12"] = int((nsats < 12).sum())
        metrics["health"]["gps"] = gps_m

    # EKF innovation ratios
    for ekf_name in ["XKF4", "NKF4"]:
        if ekf_name in tables:
            ekf = tables[ekf_name]
            d = {"message": ekf_name, "test_ratio_gt_1_counts": {}}
            for col in ["SV", "SP", "SH", "SM", "SVT", "errRP", "OFN", "OFE"]:
                if col in ekf.columns:
                    s = numeric_series(ekf, [col])
                    d["test_ratio_gt_1_counts"][col] = int((s > 1.0).sum())
                    d[f"{col}_max"] = float(s.max()) if len(s.dropna()) else None
            metrics["health"]["ekf"] = d
            break

    # Vibration
    if "VIBE" in tables:
        vibe = tables["VIBE"]
        clips = clip_columns(vibe)
        d = summarise_numeric(vibe, ["VibeX", "VibeY", "VibeZ", *clips])
        clip_delta = {}
        for col in clips:
            clip = numeric_series(vibe, [col])
            if clip is not None and len(clip.dropna()) > 1:
                clip_delta[col] = float(clip.max() - clip.min())
        if clip_delta:
            d["clip_delta"] = clip_delta
        metrics["health"]["vibration"] = d

    # Motor outputs saturation/asymmetry
    rcou = combined_rcout_dataframe(tables)
    if rcou is not None:
        channels = output_channel_columns(rcou)
        output_mapping = output_mapping_from_tables(tables)
        motor_channels = motor_channels_from_mapping(output_mapping, channels)
        sat = {}
        means = {}
        for c in channels:
            s = numeric_series(rcou, [c])
            if s is None or len(s.dropna()) == 0:
                continue
            sat[c] = {"pct_low_le_1100": float((s <= 1100).mean()*100), "pct_high_ge_1900": float((s >= 1900).mean()*100), "min": float(s.min()), "max": float(s.max())}
            means[c] = float(s.mean())
        metrics["health"]["motor_outputs"] = {
            "channels": channels,
            "motor_channels": [c for c in motor_channels if c in channels],
            "mapping_available": bool(output_mapping),
            "output_mapping": output_mapping,
            "saturation": sat,
            "mean_outputs": means,
        }
        if not output_mapping:
            metrics["confidence"]["reasons"].append("Output mapping could not be confirmed from parameters; RCOU/RCO2/RCO3 channel interpretation is generic")

    # ESC summary
    if "ESC" in tables:
        esc = tables["ESC"]
        esc_summary = {"rows": int(len(esc))}
        inst_col = get_col(esc, ["Instance", "I"])
        if inst_col:
            esc_summary["instances"] = sorted([int(x) for x in esc[inst_col].dropna().unique().tolist() if str(x) != "nan"])
        esc_summary["numeric"] = summarise_numeric(esc, ["RPM", "RawRPM", "Volt", "Curr", "Temp", "MotTemp", "Err"])
        metrics["health"]["esc"] = esc_summary
    if "EDT2" in tables:
        edt2 = tables["EDT2"]
        status = numeric_series(edt2, ["Status"])
        edt2_summary = {"rows": int(len(edt2)), "numeric": summarise_numeric(edt2, ["Status", "ErrCnt", "Stress", "MaxStress"])}
        if status is not None and len(status.dropna()) > 0:
            s_i = status.fillna(0).astype(int)
            edt2_summary["status_alert_count"] = int(((s_i & 4) != 0).sum())
            edt2_summary["status_warning_count"] = int(((s_i & 8) != 0).sum())
            edt2_summary["status_error_count"] = int(((s_i & 16) != 0).sum())
        metrics["health"]["edt2"] = edt2_summary
    if "ESCX" in tables:
        escx = tables["ESCX"]
        escx_summary = {"rows": int(len(escx))}
        inst_col = get_col(escx, ["Instance", "I"])
        if inst_col:
            escx_summary["instances"] = sorted([int(x) for x in escx[inst_col].dropna().unique().tolist() if str(x) != "nan"])
        escx_summary["numeric"] = summarise_numeric(escx, ["inpct", "outpct", "flags", "Pwr"])
        flags = numeric_series(escx, ["flags"])
        if flags is not None and len(flags.dropna()) > 0:
            escx_summary["nonzero_flags_count"] = int((flags.fillna(0).astype(int) != 0).sum())
        metrics["health"]["escx"] = escx_summary

    # System ID / frequency response
    sysid = {}
    for name in ["SID", "SIDD", "SIDS"]:
        if name in tables:
            df = tables[name]
            sysid[name] = {"rows": int(len(df)), "fields": [str(c) for c in df.columns if c != "TimeS"], "numeric": summarise_numeric(df, [c for c in df.columns if c != "TimeS"])}
    if sysid:
        metrics["system_id"] = {"present": True, **sysid}
    else:
        metrics["system_id"] = {"present": False}

    # Rate and PID tracking
    if "RATE" in tables:
        rate = tables["RATE"]
        for axis, fields in AXIS_MAP.items():
            des_col, act_col, out_col = fields["rate_des"], fields["rate"], fields["out"]
            if des_col in rate.columns and act_col in rate.columns:
                des = numeric_series(rate, [des_col])
                act = numeric_series(rate, [act_col])
                err = des - act
                axis_m = {
                    "rate_error_rms": rms(series_values(err)),
                    "rate_error_p95_abs": percentile([abs(v) for v in series_values(err)], 95),
                    "rate_error_max_abs": max([abs(v) for v in series_values(err)] or [0.0]),
                }
                if out_col in rate.columns:
                    out = numeric_series(rate, [out_col])
                    axis_m["output_abs_p95"] = percentile([abs(v) for v in series_values(out)], 95)
                    axis_m["output_abs_max"] = max([abs(v) for v in series_values(out)] or [0.0])
                metrics["tuning"].setdefault(axis, {}).update(axis_m)
    for axis, fields in AXIS_MAP.items():
        pid_name = fields["pid"]
        if pid_name in tables:
            pid = tables[pid_name]
            pid_m = {"message": pid_name, "terms": summarise_numeric(pid, ["Err", "P", "I", "D", "FF", "DFF", "Dmod", "SRate"])}
            flags = numeric_series(pid, ["Flags"])
            if flags is not None and len(flags.dropna()) > 0:
                pid_m["flag_limit_count"] = int(((flags.astype(int) & 1) != 0).sum())
                pid_m["flag_pd_sum_limit_count"] = int(((flags.astype(int) & 2) != 0).sum())
            metrics["tuning"].setdefault(axis, {}).update({"pid": pid_m})

    handled = {
        "ATT", "RATE", "PIDR", "PIDP", "PIDY", "PIDA", "RCOU", "RCO2", "RCO3", "ESC", "ESCX", "EDT2", "BAT", "BCL",
        "POWR", "GPS", "GPS2", "GPA", "XKF1", "XKF2", "XKF3", "XKF4", "XKFS", "NKF1", "NKF2",
        "NKF3", "NKF4", "VIBE", "PARM", "MODE", "ERR", "EV", "MSG", "ARM", "CTUN", "BARO",
        "POS", "MAG", "RCIN", "ATUN", "SID", "SIDD", "SIDS", "ISBH", "ISBD",
    }
    for name, df in tables.items():
        if name in handled or df is None or len(df) == 0:
            continue
        numeric_cols = [c for c in df.columns if c not in {"TimeS", "TimeUS", "_type"}]
        summary = summarise_numeric(df, numeric_cols[:20])
        if summary:
            metrics["generic_messages"][name] = {"rows": int(len(df)), "numeric": summary}

    # Confidence gating
    if "ATT" not in tables or "RATE" not in tables:
        metrics["confidence"]["overall"] = "low"
        metrics["confidence"]["reasons"].append("ATT and/or RATE missing; attitude/rate tracking conclusions are limited")
    if not any(name in tables for name in ["RCOU", "RCO2", "RCO3"]):
        metrics["confidence"]["reasons"].append("RCOU/RCO2/RCO3 missing; actuator saturation cannot be confirmed")
    if "ESC" not in tables and "ESCX" not in tables and "EDT2" not in tables:
        metrics["confidence"]["reasons"].append("ESC/ESCX/EDT2 telemetry missing; motor/ESC-level confirmation not possible")
    return metrics


def summary_md(metrics):
    lines = ["# ArduPilot log metrics\n"]
    lines.append(f"- Duration estimate: {metrics.get('flight',{}).get('duration_s_estimate','unknown')} s")
    lines.append(f"- Overall confidence: {metrics.get('confidence',{}).get('overall','unknown')}")
    for r in metrics.get("confidence", {}).get("reasons", []):
        lines.append(f"- Confidence note: {r}")
    lines.append("\n## Health summary")
    for k, v in metrics.get("health", {}).items():
        lines.append(f"### {k}\n```json\n{__import__('json').dumps(v, indent=2)[:4000]}\n```")
    lines.append("\n## Tuning summary")
    for k, v in metrics.get("tuning", {}).items():
        lines.append(f"### {k}\n```json\n{__import__('json').dumps(v, indent=2)[:4000]}\n```")
    return "\n".join(lines)+"\n"


def main() -> int:
    p = argparse.ArgumentParser(description="Compute health/tuning metrics from extracted ArduPilot log tables.")
    p.add_argument("--tables", required=True, help="Directory produced by ap_log_extract.py")
    p.add_argument("--json", default="metrics.json")
    p.add_argument("--summary", default=None)
    p.add_argument("--window", default=None, help="Optional TimeS window as START:END or around:CENTER:RADIUS")
    args = p.parse_args()
    try:
        tables = load_tables(args.tables)
        window = parse_time_window(args.window)
        tables = filter_tables_by_time(tables, **window)
        metrics = compute_metrics(tables, analysis_window=window)
        write_json(args.json, metrics)
        if args.summary:
            Path(args.summary).parent.mkdir(parents=True, exist_ok=True)
            Path(args.summary).write_text(summary_md(metrics), encoding="utf-8")
        print(f"Computed metrics from {len(tables)} tables")
        return 0
    except AnalysisError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
