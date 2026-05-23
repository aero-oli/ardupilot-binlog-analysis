#!/usr/bin/env python3
from __future__ import annotations
import argparse
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ap_common import AnalysisError, ensure_dir, numeric_series, parse_dataflash, rows_to_dataframe, safe_float, safe_int, write_json


def pick_imu_table(rows):
    for typ in ["GYR", "ACC", "IMU", "IMU_FAST", "RAW_IMU"]:
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


def fft_from_isb_rows(rows, max_points=200000):
    headers = {}
    for row in rows.get("ISBH", []):
        n = safe_int(_row_get(row, ["N", "fftnum"]))
        if n is None:
            continue
        headers[n] = row
    if not headers or not rows.get("ISBD"):
        return {"available": False, "message": "ISBH/ISBD", "reason": "ISBH/ISBD batch-sampler messages are not both present.", "plots": [], "peaks": []}

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
            series.append({"sensor": sensor, "axis": axis, "frequency_hz": freq.tolist(), "amplitude": spec.tolist()})
            valid = freq > 5
            if valid.any():
                idx = np.argsort(spec[valid])[-5:]
                for f, a in sorted(zip(freq[valid][idx], spec[valid][idx]), key=lambda x: x[1], reverse=True):
                    peaks.append({"field": f"{sensor}.{axis}", "frequency_hz": float(f), "amplitude": float(a)})

    if not series:
        return {"available": False, "message": "ISBH/ISBD", "reason": "ISBH/ISBD messages were present but no complete batch with usable sample rate and axis data was found.", "plots": [], "peaks": []}
    sample_rates = []
    for row in headers.values():
        sr = safe_float(_row_get(row, ["smp_rate", "SmpRate", "sample_rate_hz"]))
        if sr:
            sample_rates.append(sr)
    return {
        "available": True,
        "message": "ISBH/ISBD",
        "sample_rate_hz_estimate": float(max(sample_rates)) if sample_rates else None,
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


def main() -> int:
    p = argparse.ArgumentParser(description="Generate FFT spectrum from raw/high-rate IMU-like messages if available.")
    p.add_argument("log")
    p.add_argument("--out", default="fft")
    p.add_argument("--json", default="fft.json")
    p.add_argument("--max-points", type=int, default=200000)
    args = p.parse_args()
    try:
        rows = parse_dataflash(args.log, include=["GYR", "ACC", "IMU", "IMU_FAST", "RAW_IMU", "VIBE", "ISBH", "ISBD"])
        if rows.get("ISBH") or rows.get("ISBD"):
            result = fft_from_isb_rows(rows, max_points=args.max_points)
            if result.get("available"):
                result = write_isb_plot(result, args.out)
            write_json(args.json, result)
            print("FFT generated from ISBH/ISBD batch sampler" if result.get("available") else result.get("reason"))
            return 0
        typ, df = pick_imu_table(rows)
        result = {"available": False, "message": typ, "reason": None, "plots": [], "peaks": []}
        if typ is None or df is None or len(df) < 100:
            result["reason"] = "No suitable high-rate IMU/raw gyro/accelerometer table found. VIBE-only logs are insufficient for frequency-domain FFT."
            write_json(args.json, result)
            print(result["reason"])
            return 0
        import numpy as np
        import plotly.graph_objects as go
        out = ensure_dir(args.out)
        if "TimeS" not in df.columns or len(df["TimeS"].dropna()) < 100:
            result["reason"] = "IMU table lacks a usable time base."
            write_json(args.json, result)
            return 0
        # Pick gyro or accel fields dynamically.
        candidates = [c for c in ["GyrX", "GyrY", "GyrZ", "AccX", "AccY", "AccZ", "GX", "GY", "GZ", "AX", "AY", "AZ"] if c in df.columns]
        if not candidates:
            candidates = [c for c in df.columns if any(k in c.lower() for k in ["gyr", "gyro", "acc"]) and c != "TimeS"][:6]
        if not candidates:
            result["reason"] = f"{typ} present, but no gyro/accel fields were recognized."
            write_json(args.json, result)
            return 0
        df = df.dropna(subset=["TimeS"]).sort_values("TimeS")
        if len(df) > args.max_points:
            df = df.iloc[-args.max_points:]
        t = df["TimeS"].to_numpy(dtype=float)
        dt = np.median(np.diff(t))
        if not np.isfinite(dt) or dt <= 0:
            result["reason"] = "Could not determine valid sample interval."
            write_json(args.json, result)
            return 0
        fs = 1.0 / dt
        fig = go.Figure()
        peaks = []
        for col in candidates:
            y = df[col].to_numpy(dtype=float)
            y = y - np.nanmean(y)
            y = np.nan_to_num(y)
            if len(y) < 128:
                continue
            window = np.hanning(len(y))
            spec = np.abs(np.fft.rfft(y * window))
            freq = np.fft.rfftfreq(len(y), d=dt)
            fig.add_trace(go.Scatter(x=freq, y=spec, mode="lines", name=col))
            if len(spec) > 5:
                # Exclude near-DC.
                valid = freq > 5
                if valid.any():
                    idx = np.argsort(spec[valid])[-5:]
                    vf = freq[valid][idx]
                    va = spec[valid][idx]
                    for f, a in sorted(zip(vf, va), key=lambda x: x[1], reverse=True):
                        peaks.append({"field": col, "frequency_hz": float(f), "amplitude": float(a)})
        fig.update_layout(title=f"FFT spectrum from {typ} ({fs:.1f} Hz estimated sample rate)", template="plotly_white", xaxis_title="Frequency (Hz)", yaxis_title="Amplitude")
        plot_path = out / "11_fft_noise_spectrum.html"
        fig.write_html(str(plot_path), include_plotlyjs="cdn")
        result.update({"available": True, "message": typ, "sample_rate_hz_estimate": float(fs), "fields": candidates, "plots": [str(plot_path)], "peaks": peaks[:30]})
        write_json(args.json, result)
        print(f"FFT generated from {typ}; sample rate estimate {fs:.1f} Hz")
        return 0
    except AnalysisError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
