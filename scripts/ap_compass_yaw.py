#!/usr/bin/env python3
from __future__ import annotations

import math
from typing import Any, Dict, List

from ap_common import (
    battery_instance_groups,
    combined_rcout_dataframe,
    ekf_instance_groups,
    ensure_dir,
    event_markers_from_tables,
    get_col,
    gps_instance_groups,
    numeric_series,
    output_channel_columns,
    percentile,
    safe_float,
)


def _vals(s):
    if s is None:
        return []
    try:
        return [float(v) for v in s.dropna().tolist()]
    except Exception:
        return []


def _add_context(context, source, detail):
    if detail:
        context.append({"source": source, "detail": detail})


def _add_finding(findings, rank, cause, severity, confidence, evidence, interpretation, checks):
    evidence = [e for e in evidence if e]
    if evidence:
        findings.append({
            "rank": rank,
            "possible_cause": cause,
            "severity": severity,
            "confidence": confidence,
            "evidence": evidence,
            "interpretation": interpretation,
            "recommended_checks": checks,
        })


def _mag_axis_columns(df):
    x = get_col(df, ["MagX", "MX", "X", "OfsX"])
    y = get_col(df, ["MagY", "MY", "Y", "OfsY"])
    z = get_col(df, ["MagZ", "MZ", "Z", "OfsZ"])
    if x and y and z:
        return x, y, z
    return None


def mag_field_frame(tables):
    mag = tables.get("MAG")
    if mag is None or len(mag) == 0:
        return None
    cols = _mag_axis_columns(mag)
    if not cols:
        return None
    x = numeric_series(mag, [cols[0]])
    y = numeric_series(mag, [cols[1]])
    z = numeric_series(mag, [cols[2]])
    if x is None or y is None or z is None:
        return None
    out = mag.copy()
    out["mag_field"] = (x * x + y * y + z * z).pow(0.5)
    return out


def yaw_error_frame(tables):
    att = tables.get("ATT")
    if att is None or not all(c in att.columns for c in ["DesYaw", "Yaw"]):
        return None
    des = numeric_series(att, ["DesYaw"])
    yaw = numeric_series(att, ["Yaw"])
    if des is None or yaw is None:
        return None
    out = att.copy()
    out["yaw_error"] = ((des - yaw + 180.0) % 360.0) - 180.0
    out["yaw_error_abs"] = out["yaw_error"].abs()
    return out


def _as_time_value_frame(df, value_col, label):
    if df is None or value_col not in df.columns:
        return None
    if "TimeS" not in df.columns:
        return None
    out = df[["TimeS", value_col]].copy()
    out = out.dropna(subset=["TimeS", value_col]).sort_values("TimeS")
    out = out.rename(columns={value_col: label})
    return out


def _series_frame(tables, source):
    if source == "throttle":
        ctun = tables.get("CTUN")
        if ctun is not None:
            col = get_col(ctun, ["ThO", "ThH", "ThI"])
            if col:
                return _as_time_value_frame(ctun, col, "throttle")
        rcou = combined_rcout_dataframe(tables)
        if rcou is not None:
            channels = output_channel_columns(rcou)
            if channels:
                out = rcou[["TimeS", *channels]].copy() if "TimeS" in rcou.columns else rcou[channels].copy()
                if "TimeS" not in out.columns:
                    out.insert(0, "TimeS", range(len(out)))
                out["throttle"] = out[channels].mean(axis=1)
                return out[["TimeS", "throttle"]].dropna().sort_values("TimeS")
    if source == "current":
        frames = []
        for group in battery_instance_groups(tables):
            bat = group["df"]
            col = get_col(bat, ["Curr", "I"])
            frame = _as_time_value_frame(bat, col, "current") if col else None
            if frame is not None:
                frames.append(frame)
        if frames:
            return frames[0]
    if source == "yaw_error":
        return _as_time_value_frame(yaw_error_frame(tables), "yaw_error_abs", "yaw_error_abs")
    return None


def _aligned_correlation(left, left_col, right, right_col, tolerance=0.35):
    if left is None or right is None or "TimeS" not in left.columns or "TimeS" not in right.columns:
        return None
    try:
        import pandas as pd
        left_frame = left[["TimeS", left_col]].dropna().copy()
        right_frame = right[["TimeS", right_col]].dropna().copy()
        left_frame["TimeS"] = pd.to_numeric(left_frame["TimeS"], errors="coerce").astype(float)
        right_frame["TimeS"] = pd.to_numeric(right_frame["TimeS"], errors="coerce").astype(float)
        merged = pd.merge_asof(
            left_frame.dropna().sort_values("TimeS"),
            right_frame.dropna().sort_values("TimeS"),
            on="TimeS",
            direction="nearest",
            tolerance=tolerance,
        ).dropna()
        if len(merged) < 4:
            return None
        corr = merged[left_col].corr(merged[right_col])
        if corr is None or math.isnan(float(corr)):
            return None
        return float(corr)
    except Exception:
        return None


def _aligned_angle_error_p95(left, left_col, right, right_col, tolerance=0.35):
    if left is None or right is None or "TimeS" not in left.columns or "TimeS" not in right.columns:
        return None
    try:
        import pandas as pd
        left_frame = left[["TimeS", left_col]].dropna().copy()
        right_frame = right[["TimeS", right_col]].dropna().copy()
        left_frame["TimeS"] = pd.to_numeric(left_frame["TimeS"], errors="coerce").astype(float)
        right_frame["TimeS"] = pd.to_numeric(right_frame["TimeS"], errors="coerce").astype(float)
        merged = pd.merge_asof(
            left_frame.dropna().sort_values("TimeS"),
            right_frame.dropna().sort_values("TimeS"),
            on="TimeS",
            direction="nearest",
            tolerance=tolerance,
        ).dropna()
        if len(merged) < 4:
            return None
        err = ((merged[left_col] - merged[right_col] + 180.0) % 360.0) - 180.0
        return percentile([abs(v) for v in _vals(err)], 95)
    except Exception:
        return None


def _mode_dependency(tables, yaw_frame):
    mode = tables.get("MODE")
    if mode is None or yaw_frame is None or "TimeS" not in yaw_frame.columns:
        return None
    mode_col = get_col(mode, ["Mode", "Name", "ModeNum"])
    if not mode_col:
        return None
    rows = mode.dropna(subset=["TimeS"]).sort_values("TimeS").to_dict(orient="records")
    nav_vals = []
    manual_vals = []
    for row in yaw_frame.dropna(subset=["TimeS", "yaw_error_abs"]).to_dict(orient="records"):
        active = None
        row_time = safe_float(row.get("TimeS"))
        if row_time is None:
            continue
        for mode_row in rows:
            mode_time = safe_float(mode_row.get("TimeS"))
            if mode_time is None:
                continue
            if mode_time <= row_time:
                active = str(mode_row.get(mode_col, "")).upper()
            else:
                break
        if active in {"LOITER", "AUTO", "RTL", "GUIDED", "POSHOLD"}:
            nav_vals.append(float(row["yaw_error_abs"]))
        elif active in {"STABILIZE", "ALTHOLD", "ACRO"}:
            manual_vals.append(float(row["yaw_error_abs"]))
    if len(nav_vals) < 2 or len(manual_vals) < 2:
        return None
    nav_p95 = percentile(nav_vals, 95)
    manual_p95 = percentile(manual_vals, 95)
    if nav_p95 is None or manual_p95 is None:
        return None
    return nav_p95, manual_p95


def build_compass_yaw_investigation(tables: Dict[str, Any]) -> Dict[str, Any]:
    findings: List[Dict[str, Any]] = []
    context: List[Dict[str, str]] = []
    checked: List[Dict[str, str]] = []

    mag_field = mag_field_frame(tables)
    yaw_frame = yaw_error_frame(tables)
    mag_evidence = []
    estimator_evidence = []

    if mag_field is not None and len(mag_field.dropna(subset=["mag_field"])) > 0:
        m = mag_field["mag_field"].dropna()
        mmin = float(m.min())
        mmax = float(m.max())
        span_pct = ((mmax - mmin) / mmax * 100.0) if mmax > 0 else 0.0
        _add_context(context, "MAG", f"MAG mag field magnitude min={mmin:.2f}, max={mmax:.2f}, span={span_pct:.1f}%")
        if span_pct > 10.0:
            mag_span_detail = f"mag field magnitude changed {span_pct:.1f}%"
        else:
            mag_span_detail = None
        mag_series = _as_time_value_frame(mag_field, "mag_field", "mag_field")
        for source, label in [("throttle", "throttle"), ("current", "battery current"), ("yaw_error", "yaw error")]:
            other = _series_frame(tables, source)
            corr = _aligned_correlation(mag_series, "mag_field", other, other.columns[-1] if other is not None else "", tolerance=0.5)
            if corr is None:
                checked.append({"check": f"MAG correlation with {label}", "result": f"Could not correlate magnetic field with {label}; required aligned data was missing or sparse"})
                continue
            _add_context(context, "MAG", f"MAG field correlation with {label}: r={corr:.2f}")
            if abs(corr) >= 0.70 and span_pct > 10.0 and source in {"throttle", "current"}:
                mag_evidence.append(f"{mag_span_detail}; mag field magnitude correlates with {label} (r={corr:.2f})")
    elif "MAG" in tables:
        checked.append({"check": "MAG axes", "result": "MAG present but no recognized MagX/MagY/MagZ-style axis fields for field magnitude"})
    else:
        checked.append({"check": "MAG availability", "result": "MAG unavailable; compass field magnitude and interference correlation cannot be checked"})

    for group in ekf_instance_groups(tables, ("XKF3", "NKF3", "XKF4", "NKF4")):
        ekf = group["df"]
        label = group["label"] if group.get("instance_certain") else group["message"]
        for col in [c for c in ["SM", "SH", "SV", "SVT", "Yaw", "IYAW", "IVN", "IVE", "IMX", "IMY", "IMZ"] if c in ekf.columns]:
            s = numeric_series(ekf, [col])
            if s is None or len(s.dropna()) == 0:
                continue
            if col in {"SM", "SH", "SV", "SVT"} and float(s.max()) > 1.0:
                estimator_evidence.append(f"{label}.{col} max={float(s.max()):.2f}, samples >1={int((s > 1.0).sum())}")
            elif col not in {"SM", "SH", "SV", "SVT"}:
                _add_context(context, label, f"{label}.{col}: min={float(s.min()):.2f}, max={float(s.max()):.2f}")

    att = tables.get("ATT")
    gps_yaw_seen = False
    for group in gps_instance_groups(tables):
        gps = group["df"]
        label = group["label"]
        yaw_col = get_col(gps, ["Yaw", "GYaw", "Hdg", "Heading", "GCrs"])
        if not yaw_col:
            continue
        gps_yaw_seen = True
        s = numeric_series(gps, [yaw_col])
        if s is not None and len(s.dropna()) > 0:
            _add_context(context, label, f"{label}.{yaw_col}: min={float(s.min()):.2f}, max={float(s.max()):.2f}")
        if att is not None and "Yaw" in att.columns:
            p95 = _aligned_angle_error_p95(gps, yaw_col, att, "Yaw", tolerance=0.5)
            if p95 is not None:
                _add_context(context, label, f"{label}.{yaw_col} vs ATT.Yaw p95 absolute difference={p95:.1f} deg")
                if p95 > 20:
                    estimator_evidence.append(f"{label}.{yaw_col} differs from ATT.Yaw p95={p95:.1f} deg")
    if not gps_yaw_seen:
        checked.append({"check": "GPS yaw/heading source", "result": "No GPS yaw/heading-style field found in GPS/GPS2 tables"})

    if yaw_frame is not None and len(yaw_frame) > 1:
        yaw_delta = numeric_series(yaw_frame, ["Yaw"]).diff().abs() if "Yaw" in yaw_frame.columns else None
        rate = tables.get("RATE")
        rate_abs_max = None
        out_p95 = None
        if rate is not None:
            rate_y = numeric_series(rate, ["Y"])
            yout = numeric_series(rate, ["YOut"])
            rate_abs_max = max([abs(v) for v in _vals(rate_y)] or [0.0]) if rate_y is not None else None
            out_p95 = percentile([abs(v) for v in _vals(yout)], 95) if yout is not None else None
        if yaw_delta is not None and len(yaw_delta.dropna()) > 0 and float(yaw_delta.max()) > 25 and (rate_abs_max is None or rate_abs_max < 20) and (out_p95 is None or out_p95 < 0.4):
            estimator_evidence.append(f"ATT.Yaw jump max={float(yaw_delta.max()):.1f} deg without matching RATE.Y/YOut evidence")
        dependency = _mode_dependency(tables, yaw_frame)
        if dependency:
            nav_p95, manual_p95 = dependency
            _add_context(context, "MODE", f"yaw error p95 nav modes={nav_p95:.1f} deg, manual/alt-hold modes={manual_p95:.1f} deg")
            if nav_p95 > max(20.0, manual_p95 * 2.0):
                estimator_evidence.append(f"yaw/heading error mainly in navigation modes: nav p95={nav_p95:.1f} deg vs manual/alt-hold p95={manual_p95:.1f} deg")
    else:
        checked.append({"check": "ATT yaw estimator jump", "result": "ATT.DesYaw/ATT.Yaw unavailable; heading jumps cannot be checked"})

    if mag_evidence:
        _add_finding(
            findings, 2, "Compass/yaw-source interference hypothesis", "safety-critical", "medium",
            mag_evidence[:8],
            "Magnetic field movement is treated as a hypothesis only when it correlates with throttle/current or other context; MAG magnitude alone is not enough to declare interference.",
            ["Inspect compass placement, current-carrying wiring, power distribution and compass orientation", "Correlate with flight mode and yaw-source EKF innovations before changing parameters"],
        )
    else:
        checked.append({"check": "MAG load correlation", "result": "No magnetic-field correlation with throttle/current strong enough to support an interference hypothesis"})

    if estimator_evidence:
        _add_finding(
            findings, 2, "Yaw estimator or magnetic innovation evidence", "safety-critical", "medium",
            estimator_evidence[:10],
            "Yaw-source and magnetic innovation evidence can explain heading drift, toilet bowling, and Loiter/Auto position problems when controller/actuator evidence does not explain the symptom.",
            ["Compare Stabilize/AltHold against Loiter/Auto/RTL", "Inspect MAG, XKF3 innovations, XKF4/NKF4 test ratios, GPS status and ERR/MSG timeline"],
        )
    elif not mag_evidence:
        checked.append({"check": "Compass/yaw-source investigation", "result": "No compass/yaw-source issue detected by heuristic"})

    return {"findings": findings, "context": context, "checked": checked}


def _add_line(fig, go, df, col, name, row, secondary_y=False):
    if df is None or col not in df.columns:
        return
    x = df["TimeS"] if "TimeS" in df.columns else list(range(len(df)))
    fig.add_trace(go.Scatter(x=x, y=df[col], mode="lines", name=name), row=row, col=1, secondary_y=secondary_y)


def write_compass_yaw_plots(tables: Dict[str, Any], plots_dir, events=False) -> List[str]:
    generated: List[str] = []
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except Exception:
        return generated
    out = ensure_dir(plots_dir)
    markers = event_markers_from_tables(tables) if events else []

    fig = make_subplots(
        rows=4,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.04,
        specs=[[{}], [{}], [{"secondary_y": True}], [{}]],
        subplot_titles=("Yaw attitude", "Yaw rate", "Mag field vs throttle/current", "Yaw error vs mag field"),
    )
    att = tables.get("ATT")
    if att is not None:
        _add_line(fig, go, att, "DesYaw", "ATT.DesYaw", 1)
        _add_line(fig, go, att, "Yaw", "ATT.Yaw", 1)
    rate = tables.get("RATE")
    if rate is not None:
        _add_line(fig, go, rate, "YDes", "RATE.YDes", 2)
        _add_line(fig, go, rate, "Y", "RATE.Y", 2)
        _add_line(fig, go, rate, "YOut", "RATE.YOut", 2)
    mag = mag_field_frame(tables)
    if mag is not None:
        _add_line(fig, go, mag, "mag_field", "MAG field", 3)
        _add_line(fig, go, mag, "mag_field", "MAG field", 4)
    throttle = _series_frame(tables, "throttle")
    current = _series_frame(tables, "current")
    yaw_err = yaw_error_frame(tables)
    if throttle is not None:
        _add_line(fig, go, throttle, "throttle", "throttle", 3, secondary_y=True)
    if current is not None:
        _add_line(fig, go, current, "current", "battery current", 3, secondary_y=True)
    if yaw_err is not None:
        _add_line(fig, go, yaw_err, "yaw_error_abs", "abs yaw error", 4, secondary_y=True)
    if markers:
        shapes = []
        annotations = []
        for marker in markers[:80]:
            color = "#dc2626" if marker["source"] == "ERR" else ("#2563eb" if marker["source"] == "MODE" else "#64748b")
            shapes.append({"type": "line", "xref": "x", "yref": "paper", "x0": marker["time_s"], "x1": marker["time_s"], "y0": 0, "y1": 1, "line": {"color": color, "width": 1, "dash": "dot"}})
            annotations.append({"xref": "x", "yref": "paper", "x": marker["time_s"], "y": 1.02, "text": marker["label"], "showarrow": False, "textangle": -45, "font": {"size": 9, "color": color}})
        fig.update_layout(shapes=shapes, annotations=annotations)
    fig.update_layout(title="Compass and yaw-source investigation", template="plotly_white", hovermode="x unified")
    p = out / "compass_yaw_source_investigation.html"
    fig.write_html(str(p), include_plotlyjs="cdn")
    generated.append(str(p))

    if any(name in tables for name in ["XKF3", "NKF3", "XKF4", "NKF4"]):
        fig = make_subplots(rows=2, cols=1, shared_xaxes=True, subplot_titles=("XKF magnetic/yaw innovations", "XKF test ratios"))
        for group in ekf_instance_groups(tables, ("XKF3", "NKF3", "XKF4", "NKF4")):
            ekf = group["df"]
            label = group["label"] if group.get("instance_certain") else group["message"]
            for col in ["Yaw", "IYAW", "IVN", "IVE", "IMX", "IMY", "IMZ"]:
                _add_line(fig, go, ekf, col, f"{label}.{col}", 1)
            for col in ["SM", "SH", "SV", "SVT"]:
                _add_line(fig, go, ekf, col, f"{label}.{col}", 2)
        fig.update_layout(title="EKF magnetic/yaw innovations and test ratios", template="plotly_white", hovermode="x unified")
        p = out / "ekf_mag_yaw_innovations.html"
        fig.write_html(str(p), include_plotlyjs="cdn")
        generated.append(str(p))
    return generated
