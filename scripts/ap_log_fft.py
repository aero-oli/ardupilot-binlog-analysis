#!/usr/bin/env python3
from __future__ import annotations
import argparse
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ap_common import AnalysisError, ensure_dir, numeric_series, parse_dataflash, rows_to_dataframe, safe_float, safe_int, write_json


CANDIDATE_IMU_MESSAGES = ["GYR", "ACC", "IMU", "IMU_FAST", "RAW_IMU"]
FFT_MIN_ROWS = 128
MAX_JITTER_RATIO = 1.0
MAX_SPARSE_DT_S = 0.05
NEXT_CAPTURE_GUIDANCE = [
    "Use raw/high-rate IMU or batch-sampler logging only for short controlled captures when the aircraft is otherwise stable and controllable.",
    "Check DSF/DMS/logging health after capture for dropouts, gaps, or sparse data before trusting FFT evidence.",
    "Disable high-volume raw/high-rate IMU or batch-sampler logging afterward.",
]


def pick_imu_table(rows):
    for typ in CANDIDATE_IMU_MESSAGES:
        if typ in rows and rows[typ]:
            return typ, rows_to_dataframe(rows[typ])
    return None, None


def _row_get(row, candidates, default=None):
    lower = {str(k).lower(): k for k in row}
    for key in candidates:
        if key in row:
            return row[key]
        if key.lower() in lower:
            return row[lower[key.lower()]]
    return default


def _as_samples(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [safe_float(v, 0.0) or 0.0 for v in value]
    if isinstance(value, str):
        parts = [p for p in value.replace("[", "").replace("]", "").replace(",", " ").split() if p]
        return [safe_float(v, 0.0) or 0.0 for v in parts]
    val = safe_float(value)
    return [] if val is None else [val]


def _sensor_label(sensor_type, instance):
    if sensor_type == 0:
        prefix = "Accel"
    elif sensor_type == 1:
        prefix = "Gyro"
    else:
        prefix = "Sensor"
    return f"{prefix}[{instance}]"


def _time_column(df):
    for col in ["TimeS", "TimeUS", "TimeMS"]:
        if col in df.columns:
            return col
    return None


def _time_seconds(df, col):
    values = numeric_series(df, [col])
    if values is None:
        values = df[col]
    values = values.dropna()
    if col == "TimeUS":
        values = values / 1_000_000.0
    elif col == "TimeMS":
        values = values / 1_000.0
    return values


def _message_diagnostics(name, df):
    diag = {
        "message": name,
        "rows": int(len(df)) if df is not None else 0,
        "time_column_found": None,
        "start_time": None,
        "end_time": None,
        "median_dt": None,
        "dt_p95": None,
        "dt_jitter_estimate": None,
        "monotonic": None,
        "usable": False,
        "problem": None,
    }
    if df is None or df.empty:
        diag["problem"] = "insufficient_rows"
        return diag
    col = _time_column(df)
    diag["time_column_found"] = col
    if col is None:
        diag["problem"] = "unsupported_message_schema"
        return diag
    try:
        import numpy as np
    except Exception as exc:
        raise AnalysisError("numpy is required for FFT. Install dependencies with pip install -r requirements.txt") from exc

    t = _time_seconds(df, col).to_numpy(dtype=float)
    t = t[np.isfinite(t)]
    if len(t):
        diag["start_time"] = float(t[0])
        diag["end_time"] = float(t[-1])
    if len(t) < FFT_MIN_ROWS:
        diag["problem"] = "insufficient_rows"
        return diag
    dt = np.diff(t)
    if len(dt) == 0:
        diag["problem"] = "could_not_determine_sample_interval"
        return diag
    monotonic = bool(np.all(dt > 0))
    diag["monotonic"] = monotonic
    positive_dt = dt[np.isfinite(dt) & (dt > 0)]
    if len(positive_dt):
        median_dt = float(np.median(positive_dt))
        dt_p95 = float(np.percentile(positive_dt, 95))
        diag["median_dt"] = median_dt
        diag["dt_p95"] = dt_p95
        diag["dt_jitter_estimate"] = float((dt_p95 - median_dt) / median_dt) if median_dt > 0 else None
    if not monotonic:
        diag["problem"] = "non_monotonic_timestamps"
        return diag
    if diag["median_dt"] is None or not np.isfinite(diag["median_dt"]) or diag["median_dt"] <= 0:
        diag["problem"] = "could_not_determine_sample_interval"
        return diag
    if diag["dt_jitter_estimate"] is not None and diag["dt_jitter_estimate"] > MAX_JITTER_RATIO:
        diag["problem"] = "excessive_timestamp_jitter"
        return diag
    if diag["median_dt"] > MAX_SPARSE_DT_S:
        diag["problem"] = "logging_dropouts_or_sparse_data"
        return diag
    diag["usable"] = True
    return diag


def _has_signal_fields(df):
    candidates = [c for c in ["GyrX", "GyrY", "GyrZ", "AccX", "AccY", "AccZ", "GX", "GY", "GZ", "AX", "AY", "AZ"] if c in df.columns]
    if candidates:
        return True
    return any(any(k in c.lower() for k in ["gyr", "gyro", "acc"]) and c != "TimeS" for c in df.columns)


def _failure_result(reason, messages_checked=None, data_quality=None, message=None, detail=None):
    return {
        "available": False,
        "fft_available": False,
        "message": message,
        "reason": reason,
        "reason_detail": detail,
        "messages_checked": messages_checked or [],
        "sample_interval_diagnostics": {d["message"]: d for d in messages_checked or []},
        "data_quality": data_quality or {},
        "next_capture_guidance": list(NEXT_CAPTURE_GUIDANCE),
        "plots": [],
        "peaks": [],
    }


def _choose_failure_reason(diagnostics):
    if not diagnostics:
        return "no_raw_or_high_rate_imu_messages"
    problems = [d.get("problem") for d in diagnostics if d.get("problem")]
    for reason in [
        "unsupported_message_schema",
        "non_monotonic_timestamps",
        "could_not_determine_sample_interval",
        "excessive_timestamp_jitter",
        "logging_dropouts_or_sparse_data",
        "insufficient_rows",
    ]:
        if reason in problems:
            return reason
    return "could_not_determine_sample_interval"


def diagnose_fft_inputs(tables):
    diagnostics = []
    data_quality = {"messages_present": sorted(tables), "candidate_messages_present": []}
    for name in CANDIDATE_IMU_MESSAGES:
        df = tables.get(name)
        if df is None or df.empty:
            continue
        data_quality["candidate_messages_present"].append(name)
        diag = _message_diagnostics(name, df)
        if diag["usable"] and not _has_signal_fields(df):
            diag["usable"] = False
            diag["problem"] = "unsupported_message_schema"
        diagnostics.append(diag)
    if not diagnostics:
        return _failure_result(
            "no_raw_or_high_rate_imu_messages",
            [],
            data_quality,
            detail="No GYR, ACC, IMU, IMU_FAST, RAW_IMU, or usable ISBH/ISBD batch-sampler messages were present. VIBE-only logs are insufficient for frequency-domain FFT.",
        )
    usable = [d for d in diagnostics if d.get("usable")]
    if usable:
        return {"available": True, "fft_available": True, "messages_checked": diagnostics, "data_quality": data_quality}
    reason = _choose_failure_reason(diagnostics)
    return _failure_result(reason, diagnostics, data_quality, detail="Raw/high-rate IMU messages were present, but none had enough clean, monotonic, high-rate timing and recognized gyro/accel fields for FFT.")


def fft_from_isb_rows(rows, max_points=200000):
    messages_checked = []
    if rows.get("ISBH"):
        messages_checked.append({"message": "ISBH", "rows": len(rows.get("ISBH", [])), "time_column_found": None, "start_time": None, "end_time": None, "median_dt": None, "dt_p95": None, "dt_jitter_estimate": None, "monotonic": None, "usable": bool(rows.get("ISBD")), "problem": None if rows.get("ISBD") else "insufficient_rows"})
    if rows.get("ISBD"):
        messages_checked.append({"message": "ISBD", "rows": len(rows.get("ISBD", [])), "time_column_found": None, "start_time": None, "end_time": None, "median_dt": None, "dt_p95": None, "dt_jitter_estimate": None, "monotonic": None, "usable": bool(rows.get("ISBH")), "problem": None if rows.get("ISBH") else "unsupported_message_schema"})
    headers = {}
    for row in rows.get("ISBH", []):
        n = safe_int(_row_get(row, ["N", "fftnum"]))
        if n is None:
            continue
        headers[n] = row
    if not headers or not rows.get("ISBD"):
        return _failure_result(
            "no_raw_or_high_rate_imu_messages",
            messages_checked,
            {"messages_present": sorted(k for k, v in rows.items() if v), "candidate_messages_present": [name for name in ["ISBH", "ISBD"] if rows.get(name)]},
            message="ISBH/ISBD",
            detail="ISBH/ISBD batch-sampler messages are not both present.",
        )

    batches = {}
    holes = set()
    for row in rows.get("ISBD", []):
        n = safe_int(_row_get(row, ["N", "fftnum"]))
        if n is None or n not in headers or n in holes:
            continue
        seqno = safe_int(_row_get(row, ["seqno", "SeqNo", "SN"], 0), 0)
        batch = batches.setdefault(n, {"seqno": -1, "X": [], "Y": [], "Z": []})
        if seqno != batch["seqno"] + 1:
            holes.add(n)
            batches.pop(n, None)
            continue
        batch["seqno"] = seqno
        for axis, candidates in {"X": ["x", "X"], "Y": ["y", "Y"], "Z": ["z", "Z"]}.items():
            batch[axis].extend(_as_samples(_row_get(row, candidates)))

    try:
        import numpy as np
    except Exception as exc:
        raise AnalysisError("numpy is required for FFT. Install dependencies with pip install -r requirements.txt") from exc

    peaks = []
    series = []
    for n, batch in batches.items():
        header = headers[n]
        if n in holes:
            continue
        sample_rate = safe_float(_row_get(header, ["smp_rate", "SmpRate", "sample_rate_hz"]), 0.0) or 0.0
        multiplier = safe_float(_row_get(header, ["mul", "Mul", "multiplier"]), 1.0) or 1.0
        sensor_type = safe_int(_row_get(header, ["type", "Type"]), -1)
        instance = safe_int(_row_get(header, ["instance", "Instance", "I"]), 0)
        if sample_rate <= 0:
            continue
        sensor = _sensor_label(sensor_type, instance)
        for axis in ["X", "Y", "Z"]:
            data = batch.get(axis, [])
            if len(data) > max_points:
                data = data[-max_points:]
            if len(data) < 128:
                continue
            y = np.array(data, dtype=float) / float(multiplier)
            y = y - np.nanmean(y)
            y = np.nan_to_num(y)
            spec = np.abs(np.fft.rfft(y))
            freq = np.fft.rfftfreq(len(y), 1.0 / sample_rate)
            series.append({"sensor": sensor, "axis": axis, "frequency_hz": freq.tolist(), "amplitude": spec.tolist(), "units": {"frequency_hz": "Hz", "amplitude": "unknown"}})
            valid = freq > 5
            if valid.any():
                idx = np.argsort(spec[valid])[-5:]
                for f, a in sorted(zip(freq[valid][idx], spec[valid][idx]), key=lambda x: x[1], reverse=True):
                    peaks.append({"field": f"{sensor}.{axis}", "frequency_hz": float(f), "amplitude": float(a), "units": {"frequency_hz": "Hz", "amplitude": "unknown"}})

    if not series:
        return _failure_result(
            "unsupported_message_schema",
            messages_checked,
            {"messages_present": sorted(k for k, v in rows.items() if v), "candidate_messages_present": ["ISBH", "ISBD"], "incomplete_batches": sorted(holes)},
            message="ISBH/ISBD",
            detail="ISBH/ISBD messages were present but no complete batch with usable sample rate and axis data was found.",
        )
    sample_rates = []
    for row in headers.values():
        sr = safe_float(_row_get(row, ["smp_rate", "SmpRate", "sample_rate_hz"]))
        if sr:
            sample_rates.append(sr)
    return {
        "available": True,
        "fft_available": True,
        "message": "ISBH/ISBD",
        "messages_checked": messages_checked,
        "sample_interval_diagnostics": {d["message"]: d for d in messages_checked},
        "data_quality": {"messages_present": sorted(k for k, v in rows.items() if v), "candidate_messages_present": ["ISBH", "ISBD"]},
        "next_capture_guidance": [],
        "sample_rate_hz_estimate": float(max(sample_rates)) if sample_rates else None,
        "units": {"sample_rate_hz_estimate": "Hz", "peaks.frequency_hz": "Hz", "peaks.amplitude": "unknown"},
        "fields": sorted({s["field"] for s in peaks}),
        "plots": [],
        "peaks": peaks[:30],
        "_series": series,
    }


def write_isb_plot(result, out):
    try:
        import plotly.graph_objects as go
    except Exception as exc:
        raise AnalysisError("plotly is required for HTML plots. Install dependencies with pip install -r requirements.txt") from exc
    out = ensure_dir(out)
    fig = go.Figure()
    for s in result.get("_series", []):
        fig.add_trace(go.Scatter(x=s["frequency_hz"], y=s["amplitude"], mode="lines", name=f"{s['sensor']} {s['axis']}"))
    fig.update_layout(title="FFT spectrum from ISBH/ISBD batch sampler", template="plotly_white", xaxis_title="Frequency (Hz)", yaxis_title="Amplitude")
    plot_path = out / "11_fft_noise_spectrum.html"
    fig.write_html(str(plot_path), include_plotlyjs="cdn")
    result["plots"] = [str(plot_path)]
    result.pop("_series", None)
    return result


def _signal_candidates(df):
    candidates = [c for c in ["GyrX", "GyrY", "GyrZ", "AccX", "AccY", "AccZ", "GX", "GY", "GZ", "AX", "AY", "AZ"] if c in df.columns]
    if not candidates:
        candidates = [c for c in df.columns if any(k in c.lower() for k in ["gyr", "gyro", "acc"]) and c != "TimeS"][:6]
    return candidates


def fft_from_tables(tables, out=None, max_points=200000):
    diagnostics = diagnose_fft_inputs(tables)
    if not diagnostics.get("available"):
        return diagnostics
    try:
        import numpy as np
    except Exception as exc:
        raise AnalysisError("numpy is required for FFT. Install dependencies with pip install -r requirements.txt") from exc

    typ = next((d["message"] for d in diagnostics["messages_checked"] if d.get("usable")), None)
    df = tables[typ].copy()
    time_col = _time_column(df)
    candidates = _signal_candidates(df)
    if not candidates:
        return _failure_result("unsupported_message_schema", diagnostics["messages_checked"], diagnostics.get("data_quality"), message=typ, detail=f"{typ} present, but no gyro/accel fields were recognized.")

    df = df.dropna(subset=[time_col])
    if len(df) > max_points:
        df = df.iloc[-max_points:]
    t_series = _time_seconds(df, time_col)
    t = t_series.to_numpy(dtype=float)
    dt = float(np.median(np.diff(t)))
    if not np.isfinite(dt) or dt <= 0:
        return _failure_result("could_not_determine_sample_interval", diagnostics["messages_checked"], diagnostics.get("data_quality"), message=typ, detail="Could not determine valid sample interval.")
    fs = 1.0 / dt
    peaks = []
    traces = []
    for col in candidates:
        y = df[col].to_numpy(dtype=float)
        y = y - np.nanmean(y)
        y = np.nan_to_num(y)
        if len(y) < FFT_MIN_ROWS:
            continue
        window = np.hanning(len(y))
        spec = np.abs(np.fft.rfft(y * window))
        freq = np.fft.rfftfreq(len(y), d=dt)
        traces.append({"field": col, "frequency_hz": freq.tolist(), "amplitude": spec.tolist()})
        if len(spec) > 5:
            valid = freq > 5
            if valid.any():
                idx = np.argsort(spec[valid])[-5:]
                vf = freq[valid][idx]
                va = spec[valid][idx]
                for f, a in sorted(zip(vf, va), key=lambda x: x[1], reverse=True):
                    peaks.append({"field": col, "frequency_hz": float(f), "amplitude": float(a), "units": {"frequency_hz": "Hz", "amplitude": "unknown"}})
    result = {
        "available": True,
        "fft_available": True,
        "message": typ,
        "reason": None,
        "messages_checked": diagnostics["messages_checked"],
        "sample_interval_diagnostics": {d["message"]: d for d in diagnostics["messages_checked"]},
        "data_quality": diagnostics.get("data_quality", {}),
        "next_capture_guidance": [],
        "sample_rate_hz_estimate": float(fs),
        "units": {"sample_rate_hz_estimate": "Hz", "peaks.frequency_hz": "Hz", "peaks.amplitude": "unknown"},
        "fields": candidates,
        "plots": [],
        "peaks": peaks[:30],
    }
    if out:
        try:
            import plotly.graph_objects as go
        except Exception as exc:
            raise AnalysisError("plotly is required for HTML plots. Install dependencies with pip install -r requirements.txt") from exc
        out = ensure_dir(out)
        fig = go.Figure()
        for trace in traces:
            fig.add_trace(go.Scatter(x=trace["frequency_hz"], y=trace["amplitude"], mode="lines", name=trace["field"]))
        fig.update_layout(title=f"FFT spectrum from {typ} ({fs:.1f} Hz estimated sample rate)", template="plotly_white", xaxis_title="Frequency (Hz)", yaxis_title="Amplitude")
        plot_path = out / "11_fft_noise_spectrum.html"
        fig.write_html(str(plot_path), include_plotlyjs="cdn")
        result["plots"] = [str(plot_path)]
    return result


def main() -> int:
    p = argparse.ArgumentParser(description="Generate FFT spectrum from raw/high-rate IMU-like messages if available.")
    p.add_argument("log")
    p.add_argument("--out", default="fft")
    p.add_argument("--json", default="fft.json")
    p.add_argument("--max-points", type=int, default=200000)
    p.add_argument("--max-messages", type=int, default=None)
    p.add_argument("--start-time", type=float, default=None)
    p.add_argument("--end-time", type=float, default=None)
    args = p.parse_args()
    try:
        if args.start_time is not None and args.end_time is not None and args.end_time < args.start_time:
            raise AnalysisError("--end-time must be greater than or equal to --start-time")
        rows = parse_dataflash(
            args.log,
            include=["GYR", "ACC", "IMU", "IMU_FAST", "RAW_IMU", "VIBE", "ISBH", "ISBD"],
            max_messages=args.max_messages,
            start_s=args.start_time,
            end_s=args.end_time,
        )
        if rows.get("ISBH") or rows.get("ISBD"):
            result = fft_from_isb_rows(rows, max_points=args.max_points)
            if result.get("available"):
                result = write_isb_plot(result, args.out)
            write_json(args.json, result)
            print("FFT generated from ISBH/ISBD batch sampler" if result.get("available") else f"FFT unavailable: {result.get('reason')}")
            return 0
        tables = {name: rows_to_dataframe(values) for name, values in rows.items() if values}
        result = fft_from_tables(tables, out=args.out, max_points=args.max_points)
        write_json(args.json, result)
        if result.get("available"):
            print(f"FFT generated from {result.get('message')}; sample rate estimate {result.get('sample_rate_hz_estimate'):.1f} Hz")
        else:
            print(f"FFT unavailable: {result.get('reason')}")
        return 0
    except AnalysisError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
