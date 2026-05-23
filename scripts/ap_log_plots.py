#!/usr/bin/env python3
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ap_common import (
    AXIS_MAP, AnalysisError, clip_columns, ensure_dir, event_markers_from_tables, filter_tables_by_time,
    get_col, load_tables, motor_channels_from_mapping, output_channel_label, output_mapping_from_tables,
    parse_time_window, read_json, write_json
)


def _plotly():
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
        return go, make_subplots
    except Exception as exc:
        raise AnalysisError("plotly is required for HTML plots. Install dependencies with pip install -r requirements.txt") from exc


def add_line(fig, df, y, name=None, row=None, secondary_y=False):
    go, _ = _plotly()
    if df is None or len(df) == 0 or y not in df.columns:
        return False
    x = df["TimeS"] if "TimeS" in df.columns else list(range(len(df)))
    if row is None:
        fig.add_trace(go.Scatter(x=x, y=df[y], name=name or y, mode="lines"))
    else:
        fig.add_trace(go.Scatter(x=x, y=df[y], name=name or y, mode="lines"), row=row, col=1, secondary_y=secondary_y)
    return True


def write_fig(fig, path, title):
    fig.update_layout(title=title, template="plotly_white", hovermode="x unified")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(str(path), include_plotlyjs="cdn")

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


def flight_overview(tables, out, markers=None):
    go, make_subplots = _plotly()
    fig = make_subplots(rows=4, cols=1, shared_xaxes=True, vertical_spacing=0.04, subplot_titles=("Mode/Altitude", "Battery", "Vibration", "Motor outputs"))
    if "CTUN" in tables:
        for c in ["Alt", "BAlt", "DAlt", "ThO", "ThH"]:
            add_line(fig, tables["CTUN"], c, row=1)
    if "BAT" in tables:
        for c in ["Volt", "VoltR", "Curr"]:
            add_line(fig, tables["BAT"], c, row=2)
    if "VIBE" in tables:
        for c in ["VibeX", "VibeY", "VibeZ", *clip_columns(tables["VIBE"])]:
            add_line(fig, tables["VIBE"], c, row=3)
    if "RCOU" in tables:
        mapping = output_mapping_from_tables(tables)
        motor_channels = motor_channels_from_mapping(mapping, [c for c in tables["RCOU"].columns if c.startswith("C") and c[1:].isdigit()])
        for c in [c for c in tables["RCOU"].columns if c.startswith("C") and c[1:].isdigit()][:12]:
            if c in motor_channels:
                add_line(fig, tables["RCOU"], c, name=output_channel_label(c, mapping), row=4)
    add_event_markers(fig, markers)
    write_fig(fig, out / "00_flight_overview.html", "ArduPilot flight overview")


def attitude_tracking(tables, out, markers=None):
    if "ATT" not in tables:
        return
    go, make_subplots = _plotly()
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, subplot_titles=("Roll attitude", "Pitch attitude", "Yaw heading"))
    pairs = [("DesRoll", "Roll"), ("DesPitch", "Pitch"), ("DesYaw", "Yaw")]
    for i, (des, act) in enumerate(pairs, start=1):
        add_line(fig, tables["ATT"], des, row=i)
        add_line(fig, tables["ATT"], act, row=i)
    add_event_markers(fig, markers)
    write_fig(fig, out / "02_attitude_tracking_roll_pitch_yaw.html", "Desired vs achieved attitude")


def rate_tracking(tables, out, markers=None):
    if "RATE" not in tables:
        return
    go, make_subplots = _plotly()
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.04, subplot_titles=("Roll rate", "Pitch rate", "Yaw rate"))
    pairs = [("RDes", "R", "ROut"), ("PDes", "P", "POut"), ("YDes", "Y", "YOut")]
    for i, (des, act, outcol) in enumerate(pairs, start=1):
        add_line(fig, tables["RATE"], des, row=i)
        add_line(fig, tables["RATE"], act, row=i)
        add_line(fig, tables["RATE"], outcol, row=i, secondary_y=False)
    add_event_markers(fig, markers)
    write_fig(fig, out / "03_rate_tracking_roll_pitch_yaw.html", "Desired vs achieved attitude rates")


def pid_terms(tables, out, markers=None):
    go, make_subplots = _plotly()
    for pid in ["PIDR", "PIDP", "PIDY"]:
        if pid not in tables:
            continue
        fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.04, subplot_titles=(f"{pid} target/actual/error", f"{pid} P/I/D/FF", f"{pid} limiting"))
        for c in ["Tar", "Act", "Err"]:
            add_line(fig, tables[pid], c, row=1)
        for c in ["P", "I", "D", "FF", "DFF"]:
            add_line(fig, tables[pid], c, row=2)
        for c in ["Dmod", "SRate", "Flags"]:
            add_line(fig, tables[pid], c, row=3)
        add_event_markers(fig, markers)
        write_fig(fig, out / f"04_{pid.lower()}_terms.html", f"{pid} PID terms")


def health_plots(tables, out, markers=None):
    go, make_subplots = _plotly()
    if "RCOU" in tables:
        fig = go.Figure()
        channels = [c for c in tables["RCOU"].columns if c.startswith("C") and c[1:].isdigit()]
        mapping = output_mapping_from_tables(tables)
        motor_channels = motor_channels_from_mapping(mapping, channels)
        for c in [c for c in channels if c in motor_channels]:
            add_line(fig, tables["RCOU"], c, name=output_channel_label(c, mapping))
        add_event_markers(fig, markers)
        write_fig(fig, out / "05_motor_outputs_rcou.html", "RCOU motor/servo outputs")
    if "ESC" in tables:
        fig = make_subplots(rows=4, cols=1, shared_xaxes=True, vertical_spacing=0.04, subplot_titles=("ESC RPM", "ESC current", "ESC voltage", "ESC errors/temp"))
        esc = tables["ESC"]
        inst_col = get_col(esc, ["Instance", "I"])
        if inst_col:
            for inst, g in esc.groupby(inst_col):
                for row, col in [(1, "RPM"), (2, "Curr"), (3, "Volt"), (4, "Err")]:
                    if col in g.columns:
                        x = g["TimeS"] if "TimeS" in g.columns else list(range(len(g)))
                        fig.add_trace(go.Scatter(x=x, y=g[col], name=f"ESC{inst} {col}", mode="lines"), row=row, col=1)
        add_event_markers(fig, markers)
        write_fig(fig, out / "06_esc_telemetry.html", "ESC telemetry")
    if "ESCX" in tables:
        fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.04, subplot_titles=("ESCX duty cycle", "ESCX power", "ESCX flags"))
        escx = tables["ESCX"]
        inst_col = get_col(escx, ["Instance", "I"])
        groups = escx.groupby(inst_col) if inst_col else [(None, escx)]
        for inst, g in groups:
            label = f"ESCX{inst} " if inst is not None else "ESCX "
            x = g["TimeS"] if "TimeS" in g.columns else list(range(len(g)))
            for col in ["inpct", "outpct"]:
                if col in g.columns:
                    fig.add_trace(go.Scatter(x=x, y=g[col], name=f"{label}{col}", mode="lines"), row=1, col=1)
            if "Pwr" in g.columns:
                fig.add_trace(go.Scatter(x=x, y=g["Pwr"], name=f"{label}Pwr", mode="lines"), row=2, col=1)
            if "flags" in g.columns:
                fig.add_trace(go.Scatter(x=x, y=g["flags"], name=f"{label}flags", mode="lines"), row=3, col=1)
        add_event_markers(fig, markers)
        write_fig(fig, out / "06b_escx_extended_telemetry.html", "ESCX extended telemetry")
    if "BAT" in tables:
        fig = make_subplots(rows=2, cols=1, shared_xaxes=True, subplot_titles=("Voltage", "Current/capacity"))
        for c in ["Volt", "VoltR"]:
            add_line(fig, tables["BAT"], c, row=1)
        for c in ["Curr", "CurrTot", "EnrgTot"]:
            add_line(fig, tables["BAT"], c, row=2)
        add_event_markers(fig, markers)
        write_fig(fig, out / "07_battery_voltage_current_sag.html", "Battery voltage/current")
    if "VIBE" in tables:
        fig = make_subplots(rows=2, cols=1, shared_xaxes=True, subplot_titles=("Vibration", "Clipping"))
        for c in ["VibeX", "VibeY", "VibeZ"]:
            add_line(fig, tables["VIBE"], c, row=1)
        for c in clip_columns(tables["VIBE"]):
            add_line(fig, tables["VIBE"], c, row=2)
        add_event_markers(fig, markers)
        write_fig(fig, out / "08_vibration_vibe_imu.html", "Vibration and clipping")
    # EKF/GPS
    if "GPS" in tables or "XKF4" in tables or "NKF4" in tables:
        fig = make_subplots(rows=3, cols=1, shared_xaxes=True, subplot_titles=("GPS HDop/HAcc", "GPS satellites", "EKF ratios"))
        if "GPS" in tables:
            for c in ["HDop", "HDOP", "HAcc"]:
                add_line(fig, tables["GPS"], c, row=1)
            for c in ["NSats", "Sats"]:
                add_line(fig, tables["GPS"], c, row=2)
        ekf = tables["XKF4"] if "XKF4" in tables else (tables["NKF4"] if "NKF4" in tables else None)
        if ekf is not None:
            for c in ["SV", "SP", "SH", "SM"]:
                add_line(fig, ekf, c, row=3)
        add_event_markers(fig, markers)
        write_fig(fig, out / "09_ekf_gps_health.html", "GPS and EKF health")


def autotune_plot(tables, out, markers=None):
    if "ATUN" not in tables:
        return
    go, make_subplots = _plotly()
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, subplot_titles=("AutoTune target/min/max", "AutoTune gains", "Axis/step"))
    for c in ["Targ", "Min", "Max"]:
        add_line(fig, tables["ATUN"], c, row=1)
    for c in ["RP", "RD", "SP"]:
        add_line(fig, tables["ATUN"], c, row=2)
    for c in ["Axis", "TuneStep"]:
        add_line(fig, tables["ATUN"], c, row=3)
    add_event_markers(fig, markers)
    write_fig(fig, out / "10_autotune_atun.html", "AutoTune ATUN")


def main() -> int:
    p = argparse.ArgumentParser(description="Generate interactive HTML plot pack from ArduPilot log tables.")
    p.add_argument("--tables", required=True)
    p.add_argument("--metrics", default=None)
    p.add_argument("--out", default="plots")
    p.add_argument("--manifest", default=None)
    p.add_argument("--window", default=None, help="Optional TimeS window as START:END or around:CENTER:RADIUS")
    p.add_argument("--events", action="store_true", help="Overlay MODE/ERR/EV/MSG markers on generated plots")
    args = p.parse_args()
    try:
        tables = load_tables(args.tables)
        window = parse_time_window(args.window)
        tables = filter_tables_by_time(tables, **window)
        out = ensure_dir(args.out)
        before = set(out.glob("*.html"))
        markers = event_markers_from_tables(tables) if args.events else []
        flight_overview(tables, out, markers)
        attitude_tracking(tables, out, markers)
        rate_tracking(tables, out, markers)
        pid_terms(tables, out, markers)
        health_plots(tables, out, markers)
        autotune_plot(tables, out, markers)
        generated = sorted(str(p) for p in out.glob("*.html"))
        manifest = {"tables": args.tables, "analysis_window": window, "events_overlay": bool(args.events), "plot_count": len(generated), "plots": generated}
        write_json(args.manifest or out / "manifest.json", manifest)
        print(f"Generated {len(generated)} plots in {out}")
        return 0
    except AnalysisError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
