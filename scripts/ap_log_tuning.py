#!/usr/bin/env python3
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ap_common import (
    AXIS_MAP, AnalysisError, ensure_dir, filter_tables_by_time, get_col, load_tables, numeric_series,
    output_mapping_from_tables, parse_time_window, percentile, rms, write_json
)

def vals(s):
    if s is None:
        return []
    try:
        return [float(v) for v in s.dropna().tolist()]
    except Exception:
        return []

def classify_axis(axis, data):
    findings = []
    p95 = data.get("rate_error_p95_abs")
    out95 = data.get("output_abs_p95")
    pid = data.get("pid", {})
    flag_limit = pid.get("flag_limit_count", 0)
    if p95 is not None and p95 > 30 and out95 is not None and out95 > 0.7:
        findings.append({
            "severity": "safety-critical",
            "finding": f"{axis} rate tracking error is high while controller output is high",
            "confidence": "medium",
            "interpretation": "This can indicate actuator authority limitation, saturation, frame/motor issue, or a tune unable to command enough response.",
            "recommended_checks": ["Check motor outputs for saturation", "Check frame class/type and motor order", "Check props/motors/ESC health before tuning gains"],
        })
    if flag_limit and flag_limit > 0:
        findings.append({
            "severity": "likely-issue",
            "finding": f"{axis} PID limit flag active",
            "confidence": "high",
            "interpretation": "PID output saturated or anti-windup active for part of the log.",
            "recommended_checks": ["Check RCOU saturation at the same time", "Do not increase gains until actuator headroom is understood"],
        })
    terms = pid.get("terms", {})
    dmod = terms.get("Dmod", {}) if isinstance(terms, dict) else {}
    if dmod and dmod.get("min") is not None and dmod.get("min") < 0.8:
        findings.append({
            "severity": "worth-checking",
            "finding": f"{axis} Dmod reduced below 0.8",
            "confidence": "medium",
            "interpretation": "Dynamic D-term reduction occurred; this can be associated with noise or limit-cycle protection.",
            "recommended_checks": ["Review gyro noise/filtering", "Check harmonic notch setup before increasing D gain"],
        })
    return findings

def analyze_tuning(tables, analysis_window=None):
    out = {"analysis_window": analysis_window or {"start_s": None, "end_s": None}, "axis": {}, "findings": [], "autotune": {}, "confidence": {"overall": "medium", "reasons": []}}
    if "RATE" not in tables:
        out["confidence"]["overall"] = "low"
        out["confidence"]["reasons"].append("RATE missing; rate tracking cannot be evaluated")
        return out
    rate = tables["RATE"]
    for axis, f in AXIS_MAP.items():
        axis_m = {}
        if f["rate_des"] in rate.columns and f["rate"] in rate.columns:
            err = numeric_series(rate, [f["rate_des"]]) - numeric_series(rate, [f["rate"]])
            axis_m["rate_error_rms"] = rms(vals(err))
            axis_m["rate_error_p95_abs"] = percentile([abs(x) for x in vals(err)], 95)
            axis_m["rate_error_max_abs"] = max([abs(x) for x in vals(err)] or [0.0])
        if f["out"] in rate.columns:
            s = numeric_series(rate, [f["out"]])
            axis_m["output_abs_p95"] = percentile([abs(x) for x in vals(s)], 95)
            axis_m["output_abs_max"] = max([abs(x) for x in vals(s)] or [0.0])
        pid_name = f["pid"]
        if pid_name in tables:
            pid = tables[pid_name]
            pid_m = {"message": pid_name, "flag_limit_count": 0, "flag_pd_sum_limit_count": 0, "terms": {}}
            flags = numeric_series(pid, ["Flags"])
            if flags is not None and len(flags.dropna()) > 0:
                flags_i = flags.fillna(0).astype(int)
                pid_m["flag_limit_count"] = int(((flags_i & 1) != 0).sum())
                pid_m["flag_pd_sum_limit_count"] = int(((flags_i & 2) != 0).sum())
            for col in ["Err", "P", "I", "D", "FF", "DFF", "Dmod", "SRate"]:
                if col in pid.columns:
                    s = numeric_series(pid, [col])
                    pid_m["terms"][col] = {"min": float(s.min()), "max": float(s.max()), "mean": float(s.mean()), "p95_abs": percentile([abs(x) for x in vals(s)], 95)}
            axis_m["pid"] = pid_m
        out["axis"][axis] = axis_m
        for finding in classify_axis(axis, axis_m):
            out["findings"].append({"axis": axis, **finding})
    if "ATUN" in tables:
        atun = tables["ATUN"]
        out["autotune"] = {
            "present": True,
            "rows": int(len(atun)),
            "axes": sorted([str(x) for x in atun[get_col(atun, ["Axis"])] .dropna().unique().tolist()]) if get_col(atun, ["Axis"]) else [],
            "last_rows": atun.tail(20).to_dict(orient="records"),
        }
    else:
        out["autotune"] = {"present": False}
    if "RCOU" not in tables:
        out["confidence"]["reasons"].append("RCOU missing; actuator saturation cannot be correlated with tracking error")
    elif not output_mapping_from_tables(tables):
        out["confidence"]["reasons"].append("Output mapping could not be confirmed from parameters; RCOU channel interpretation is generic")
    if "VIBE" not in tables:
        out["confidence"]["reasons"].append("VIBE missing; vibration contribution cannot be assessed")
    return out


def make_tuning_plots(tables, result, plots_dir):
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except Exception as exc:
        raise AnalysisError("plotly is required for tuning plots. Install dependencies with pip install -r requirements.txt") from exc
    out = ensure_dir(plots_dir)
    generated = []
    if "RATE" in tables:
        rate = tables["RATE"]
        fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.04, subplot_titles=("Roll rate", "Pitch rate", "Yaw rate"))
        x = rate["TimeS"] if "TimeS" in rate.columns else list(range(len(rate)))
        for row, cols in enumerate([("RDes", "R", "ROut"), ("PDes", "P", "POut"), ("YDes", "Y", "YOut")], start=1):
            for col in cols:
                if col in rate.columns:
                    fig.add_trace(go.Scatter(x=x, y=rate[col], mode="lines", name=col), row=row, col=1)
        fig.update_layout(title="Tuning rate tracking", template="plotly_white", hovermode="x unified")
        path = out / "tuning_rate_tracking.html"
        fig.write_html(str(path), include_plotlyjs="cdn")
        generated.append(str(path))
    for pid_name in ["PIDR", "PIDP", "PIDY"]:
        if pid_name not in tables:
            continue
        pid = tables[pid_name]
        fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.04, subplot_titles=(f"{pid_name} target/actual/error", f"{pid_name} terms", f"{pid_name} limiting"))
        x = pid["TimeS"] if "TimeS" in pid.columns else list(range(len(pid)))
        for col in ["Tar", "Act", "Err"]:
            if col in pid.columns:
                fig.add_trace(go.Scatter(x=x, y=pid[col], mode="lines", name=col), row=1, col=1)
        for col in ["P", "I", "D", "FF", "DFF"]:
            if col in pid.columns:
                fig.add_trace(go.Scatter(x=x, y=pid[col], mode="lines", name=col), row=2, col=1)
        for col in ["Dmod", "SRate", "Flags"]:
            if col in pid.columns:
                fig.add_trace(go.Scatter(x=x, y=pid[col], mode="lines", name=col), row=3, col=1)
        fig.update_layout(title=f"{pid_name} tuning terms", template="plotly_white", hovermode="x unified")
        path = out / f"tuning_{pid_name.lower()}_terms.html"
        fig.write_html(str(path), include_plotlyjs="cdn")
        generated.append(str(path))
    if "ATUN" in tables:
        atun = tables["ATUN"]
        fig = make_subplots(rows=3, cols=1, shared_xaxes=True, subplot_titles=("AutoTune target/min/max", "AutoTune gains", "Axis/step"))
        x = atun["TimeS"] if "TimeS" in atun.columns else list(range(len(atun)))
        for row, cols in enumerate([("Targ", "Min", "Max"), ("RP", "RD", "SP"), ("Axis", "TuneStep")], start=1):
            for col in cols:
                if col in atun.columns:
                    fig.add_trace(go.Scatter(x=x, y=atun[col], mode="lines", name=col), row=row, col=1)
        fig.update_layout(title="AutoTune tuning progress", template="plotly_white", hovermode="x unified")
        path = out / "tuning_autotune.html"
        fig.write_html(str(path), include_plotlyjs="cdn")
        generated.append(str(path))
    result["plots"] = generated
    return generated

def main() -> int:
    p = argparse.ArgumentParser(description="Analyze ArduCopter tuning-relevant log tables.")
    p.add_argument("--tables", required=True)
    p.add_argument("--out", default="tuning.json")
    p.add_argument("--plots", default=None, help="Directory for focused tuning plots")
    p.add_argument("--window", default=None, help="Optional TimeS window as START:END or around:CENTER:RADIUS")
    args = p.parse_args()
    try:
        tables = load_tables(args.tables)
        window = parse_time_window(args.window)
        tables = filter_tables_by_time(tables, **window)
        result = analyze_tuning(tables, analysis_window=window)
        if args.plots:
            make_tuning_plots(tables, result, args.plots)
        write_json(args.out, result)
        print(f"Tuning findings: {len(result['findings'])}")
        return 0
    except AnalysisError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
