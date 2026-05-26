#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from statistics import mean
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ap_common import AnalysisError, clip_columns, collect_dataflash, ensure_dir, numeric_series, safe_float, safe_int, write_json
from ap_log_fft import fft_from_isb_rows, fft_from_tables

METHODIC_8_1_URL = "https://ardupilot.github.io/MethodicConfigurator/TUNING_GUIDE_ArduCopter#81-notch-filter-calibration"
ARDUPILOT_NOTCH_URL = "https://ardupilot.org/copter/docs/common-imu-notch-filtering.html"
ARDUPILOT_RAW_IMU_URL = "https://ardupilot.org/copter/docs/common-raw-imu-logging.html"

MESSAGES = [
    "VIBE",
    "IMU",
    "GYR",
    "ACC",
    "IMU_FAST",
    "RAW_IMU",
    "ISBH",
    "ISBD",
    "RATE",
    "PIDR",
    "PIDP",
    "PIDY",
    "PARM",
    "ESC",
    "ESCX",
    "EDT2",
    "RPM",
    "BAT",
    "POWR",
    "RCOU",
    "RCO2",
    "RCO3",
    "DSF",
    "DRO",
    "DROP",
    "ARM",
]

PARAMETERS = [
    "INS_HNTCH_ENABLE",
    "INS_HNTCH_MODE",
    "INS_HNTCH_FREQ",
    "INS_HNTCH_BW",
    "INS_HNTCH_ATT",
    "INS_HNTCH_HMNCS",
    "INS_HNTCH_REF",
    "INS_HNTCH_OPTS",
    "INS_HNTC2_ENABLE",
    "INS_HNTC2_MODE",
    "INS_HNTC2_FREQ",
    "INS_HNTC2_BW",
    "INS_HNTC2_ATT",
    "INS_HNTC2_HMNCS",
    "INS_HNTC2_REF",
    "INS_HNTC2_OPTS",
    "INS_GYRO_FILTER",
    "INS_RAW_LOG_OPT",
    "INS_LOG_BAT_MASK",
    "INS_LOG_BAT_OPT",
    "LOG_BITMASK",
    "LOG_FILE_RATEMAX",
    "MOT_PWM_TYPE",
    "SERVO_BLH_AUTO",
    "SERVO_BLH_MASK",
    "SERVO_BLH_POLES",
    "RPM*_TYPE",
    "RPM*_SCALING",
    "RPM*_MIN",
    "RPM*_MAX",
]


def rows_to_tables(rows_by_message: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    try:
        import pandas as pd
    except Exception as exc:
        raise AnalysisError("pandas is required for Methodic notch review. Install dependencies with: pip install -r requirements.txt") from exc
    return {name: pd.DataFrame(rows) for name, rows in rows_by_message.items() if rows}


def analyze_notch_review(log_path: str | Path, *, plots_dir: str | Path | None = None) -> dict[str, Any]:
    rows, index, stats = collect_dataflash(log_path, include=MESSAGES, source=str(log_path))
    tables = rows_to_tables(rows)
    params = index.get("parameters", {}) or {}
    current = current_notch_configuration(params)
    vibe = analyze_vibration(tables)
    pid = analyze_pid_noise(tables)
    esc = analyze_esc_rpm(tables)
    logging = analyze_logging_health(index, params)
    raw = raw_imu_context(rows, tables)
    fft = fft_context(rows, tables)
    peaks = dominant_peaks(fft)
    source = notch_source_recommendation(esc, fft, current)
    risk = cpu_logging_risk(current, logging, params)

    result = empty_result(current)
    result["evidence_used"] = [
        {"type": "messages_present", "messages": sorted(tables.keys())},
        {"type": "raw_imu_context", "value": raw},
        {"type": "fft_context", "value": public_fft_context(fft)},
        {"type": "vibration", "value": vibe},
        {"type": "pid_noise", "value": pid},
        {"type": "esc_rpm", "value": esc},
        {"type": "logging_health", "value": logging},
        {"type": "cpu_logging_risk", "value": risk},
    ]
    result["filter_review_ready"] = bool(fft.get("fft_available") and not vibe.get("severe") and not logging.get("limits_diagnosis"))
    result["notch_source_recommendation"] = source
    result["dominant_peaks"] = peaks
    result["current_notch_configuration"] = current
    result["parameter_context"] = parameter_context(params)
    result["analysis_window"]["parser_stats"] = stats
    result["missing_evidence"] = missing_evidence(tables, fft, raw, esc, params)
    result["findings"] = classify_findings(current, vibe, pid, esc, logging, fft, risk)
    result["checked_but_not_supported"] = checked_but_not_supported(tables, fft, esc)
    result["result"], result["safety_gate"] = classify_result(result, current, vibe, fft, logging, risk, esc)
    result["next_methodic_step"] = "8.2" if result["result"] in {"pass", "conditional_pass"} else "repeat_8.1"
    result["recommended_next_steps"] = recommended_next_steps(result, current, fft, esc, vibe, logging, risk)
    result["what_not_to_do"] = [
        "Do not automatically set notch parameters from this script output.",
        "Do not use notch filters to hide mechanical vibration, clipping, loose hardware, or prop/motor imbalance.",
        "Do not use aggressive notch count, bandwidth, or attenuation without Filter Review evidence; excessive filtering adds phase lag.",
        "Do not leave high-volume raw IMU or batch logging enabled after the diagnostic capture.",
        "Do not proceed past unresolved output oscillation or severe vibration safety gates.",
    ]
    result["confidence_limits"] = confidence_limits(fft, raw, esc, logging)
    if plots_dir:
        result["plots"] = make_plots(tables, Path(plots_dir), fft, current)
    return result


def empty_result(current: dict[str, Any]) -> dict[str, Any]:
    return {
        "methodic_step": "8.1",
        "title": "Harmonic notch / filter review",
        "official_reference": {
            "url": METHODIC_8_1_URL,
            "supporting_urls": [ARDUPILOT_NOTCH_URL, ARDUPILOT_RAW_IMU_URL],
        },
        "result": "inconclusive",
        "safety_gate": "repeat_step",
        "filter_review_ready": False,
        "notch_source_recommendation": "inconclusive",
        "dominant_peaks": [],
        "current_notch_configuration": current,
        "evidence_used": [],
        "missing_evidence": [],
        "manual_observations_required": [
            "No visible vibration",
            "No abnormal sound",
            "No hot motors or ESCs after the filter-review flight",
        ],
        "analysis_window": {
            "selection": "whole_log",
            "preferred_window": "First-flight hover or targeted Filter Review capture with representative motor RPM/throttle.",
            "start_s": None,
            "end_s": None,
        },
        "findings": [],
        "checked_but_not_supported": [],
        "parameter_context": {},
        "plots": [],
        "recommended_next_steps": [],
        "what_not_to_do": [],
        "next_methodic_step": None,
        "confidence_limits": [],
    }


def parameter_context(params: dict[str, Any]) -> dict[str, Any]:
    present: dict[str, Any] = {}
    missing = []
    for name in PARAMETERS:
        if "*" in name:
            prefix, suffix = name.split("*", 1)
            matches = {k: v for k, v in params.items() if k.startswith(prefix) and k.endswith(suffix)}
            if matches:
                present.update(matches)
            else:
                missing.append(name)
        elif name in params:
            present[name] = params[name]
        else:
            missing.append(name)
    return {"relevant_parameters": PARAMETERS, "present": present, "missing_or_not_logged": missing, "source": "log PARM messages" if params else "no PARM messages found"}


def current_notch_configuration(params: dict[str, Any]) -> dict[str, Any]:
    first = notch_group(params, "INS_HNTCH")
    second = notch_group(params, "INS_HNTC2")
    enabled = bool(first.get("enabled") or second.get("enabled"))
    return {
        "enabled": enabled,
        "primary": first,
        "secondary": second,
        "gyro_filter_hz": safe_float(params.get("INS_GYRO_FILTER")),
        "raw_logging": {
            "INS_RAW_LOG_OPT": safe_int(params.get("INS_RAW_LOG_OPT"), 0),
            "INS_LOG_BAT_MASK": safe_int(params.get("INS_LOG_BAT_MASK"), 0),
            "INS_LOG_BAT_OPT": safe_int(params.get("INS_LOG_BAT_OPT"), 0),
            "LOG_BITMASK": safe_int(params.get("LOG_BITMASK")),
            "LOG_FILE_RATEMAX": safe_int(params.get("LOG_FILE_RATEMAX")),
        },
    }


def notch_group(params: dict[str, Any], prefix: str) -> dict[str, Any]:
    enable_name = f"{prefix}_ENABLE"
    enabled = safe_int(params.get(enable_name), 0) == 1 if enable_name in params else safe_int(params.get(f"{prefix}_ENABLE"), 0) == 1
    hmncs = safe_int(params.get(f"{prefix}_HMNCS"), 0)
    return {
        "enabled": enabled,
        "mode": safe_int(params.get(f"{prefix}_MODE")),
        "freq_hz": safe_float(params.get(f"{prefix}_FREQ")),
        "bandwidth_hz": safe_float(params.get(f"{prefix}_BW")),
        "attenuation_db": safe_float(params.get(f"{prefix}_ATT")),
        "harmonics_mask": hmncs,
        "harmonics_count": count_bits(hmncs or 0),
        "reference": safe_float(params.get(f"{prefix}_REF")),
        "options": safe_int(params.get(f"{prefix}_OPTS")),
    }


def count_bits(value: int) -> int:
    return bin(max(0, int(value))).count("1")


def series_values(df: Any, col: str | None) -> list[float]:
    if df is None or col is None:
        return []
    s = numeric_series(df, [col])
    if s is None:
        return []
    return [float(v) for v in s.dropna().tolist()]


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * pct / 100.0))))
    return float(ordered[idx])


def summarize(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"available": False, "samples": 0}
    return {
        "available": True,
        "samples": len(values),
        "min": min(values),
        "max": max(values),
        "mean": mean(values),
        "p95_abs": percentile([abs(v) for v in values], 95),
        "max_abs": max(abs(v) for v in values),
    }


def analyze_vibration(tables: dict[str, Any]) -> dict[str, Any]:
    vibe = tables.get("VIBE")
    if vibe is None or len(vibe) == 0:
        return {"available": False, "warning": "VIBE missing; vibration and clipping cannot be assessed.", "severe": False, "grey_zone": False}
    axes = {}
    for col in ("VibeX", "VibeY", "VibeZ"):
        vals = series_values(vibe, col)
        if vals:
            axes[col] = {"p95": percentile([abs(v) for v in vals], 95), "max": max(abs(v) for v in vals), "unit": "m/s/s"}
    clip_delta = {}
    for col in clip_columns(vibe):
        vals = series_values(vibe, col)
        if len(vals) > 1:
            clip_delta[col] = max(vals) - min(vals)
    max_axis = max((item["max"] for item in axes.values()), default=None)
    p95_axis = max((item["p95"] for item in axes.values() if item.get("p95") is not None), default=None)
    severe = (max_axis or 0.0) > 60.0 or any((delta or 0.0) > 0 for delta in clip_delta.values())
    grey = not severe and ((max_axis or 0.0) > 15.0 or (p95_axis or 0.0) > 15.0)
    return {"available": True, "axes": axes, "p95_axis": p95_axis, "max_axis": max_axis, "clip_delta": clip_delta, "severe": severe, "grey_zone": grey}


def analyze_pid_noise(tables: dict[str, Any]) -> dict[str, Any]:
    messages = {}
    for name in ("PIDR", "PIDP", "PIDY"):
        df = tables.get(name)
        if df is None or len(df) == 0:
            continue
        terms = {}
        for col in ("D", "Dmod", "Flags", "SRate", "P", "I"):
            if col in df.columns:
                vals = series_values(df, col)
                if vals:
                    terms[col] = summarize(vals)
        messages[name] = {"samples": len(df), "terms": terms}
    dmod_low = []
    flags_nonzero = []
    for name, data in messages.items():
        dmod = data.get("terms", {}).get("Dmod")
        flags = data.get("terms", {}).get("Flags")
        if dmod and dmod.get("min") is not None and dmod["min"] < 0.8:
            dmod_low.append(name)
        if flags and (flags.get("max_abs") or 0.0) > 0:
            flags_nonzero.append(name)
    return {"available": bool(messages), "messages": messages, "dmod_low_messages": dmod_low, "flags_nonzero_messages": flags_nonzero}


def analyze_esc_rpm(tables: dict[str, Any]) -> dict[str, Any]:
    messages = {}
    for name in ("ESC", "ESCX", "EDT2", "RPM"):
        df = tables.get(name)
        if df is None or len(df) == 0:
            continue
        fields = {}
        for col in df.columns:
            lower = str(col).lower()
            if "rpm" in lower or "frq" in lower or "freq" in lower:
                vals = series_values(df, col)
                if vals:
                    fields[col] = summarize(vals)
        messages[name] = {"samples": len(df), "rpm_fields": fields}
    return {"available": bool(messages), "rpm_available": any(msg.get("rpm_fields") for msg in messages.values()), "messages": messages}


def analyze_logging_health(index: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    health = dict(index.get("logging_health") or {})
    high_volume = high_volume_logging_enabled(params)
    health["high_volume_logging_enabled"] = high_volume
    return health


def high_volume_logging_enabled(params: dict[str, Any]) -> bool:
    raw = safe_int(params.get("INS_RAW_LOG_OPT"), 0) or 0
    bat_mask = safe_int(params.get("INS_LOG_BAT_MASK"), 0) or 0
    bat_opt = safe_int(params.get("INS_LOG_BAT_OPT"), 0) or 0
    return raw != 0 or bat_mask != 0 or bat_opt != 0


def raw_imu_context(rows: dict[str, list[dict[str, Any]]], tables: dict[str, Any]) -> dict[str, Any]:
    present = [name for name in ("IMU", "GYR", "ACC", "IMU_FAST", "RAW_IMU", "ISBH", "ISBD") if rows.get(name) or name in tables]
    return {
        "raw_or_high_rate_messages_present": present,
        "batch_sampler_available": bool(rows.get("ISBH") and rows.get("ISBD")),
        "raw_imu_available": any(name in present for name in ("IMU", "GYR", "ACC", "IMU_FAST", "RAW_IMU")),
    }


def fft_context(rows: dict[str, list[dict[str, Any]]], tables: dict[str, Any]) -> dict[str, Any]:
    if rows.get("ISBH") or rows.get("ISBD"):
        return fft_from_isb_rows(rows)
    return fft_from_tables(tables)


def public_fft_context(fft: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in fft.items() if k != "_series"}


def dominant_peaks(fft: dict[str, Any]) -> list[dict[str, Any]]:
    peaks = list(fft.get("peaks") or [])
    peaks.sort(key=lambda item: safe_float(item.get("amplitude"), 0.0) or 0.0, reverse=True)
    return peaks[:12]


def notch_source_recommendation(esc: dict[str, Any], fft: dict[str, Any], current: dict[str, Any]) -> str:
    if esc.get("rpm_available"):
        return "esc_rpm"
    primary_mode = (current.get("primary") or {}).get("mode")
    if primary_mode == 1:
        return "throttle"
    if fft.get("fft_available"):
        return "fft"
    if current.get("enabled") and (current.get("primary") or {}).get("freq_hz"):
        return "static"
    return "inconclusive"


def cpu_logging_risk(current: dict[str, Any], logging: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    harmonics = (current.get("primary", {}).get("harmonics_count") or 0) + (current.get("secondary", {}).get("harmonics_count") or 0)
    filters_enabled = int(bool(current.get("primary", {}).get("enabled"))) + int(bool(current.get("secondary", {}).get("enabled")))
    raw_high = high_volume_logging_enabled(params)
    broad_notch = False
    for group in (current.get("primary") or {}, current.get("secondary") or {}):
        freq = group.get("freq_hz") or 0.0
        bw = group.get("bandwidth_hz") or 0.0
        if freq > 0 and bw / freq > 0.7:
            broad_notch = True
    high = bool(logging.get("limits_diagnosis") and raw_high) or harmonics > 6 or (filters_enabled == 2 and harmonics > 4) or broad_notch
    return {
        "high": high,
        "filters_enabled": filters_enabled,
        "harmonics_count_total": harmonics,
        "high_volume_logging_enabled": raw_high,
        "broad_notch_detected": broad_notch,
    }


def missing_evidence(tables: dict[str, Any], fft: dict[str, Any], raw: dict[str, Any], esc: dict[str, Any], params: dict[str, Any]) -> list[str]:
    missing = []
    if "VIBE" not in tables:
        missing.append("Missing VIBE; vibration/clipping cannot be assessed.")
    if not raw.get("raw_or_high_rate_messages_present"):
        missing.append("Missing raw/high-rate IMU or ISBH/ISBD batch sampler evidence for Filter Review.")
    if not fft.get("fft_available"):
        missing.append(f"FFT/filter evidence unavailable: {fft.get('reason') or 'unknown reason'}.")
    for name in ("RATE", "PIDR", "PIDP", "PIDY", "PARM"):
        if name not in tables:
            missing.append(f"Missing strongly recommended message: {name}.")
    if not esc.get("rpm_available"):
        missing.append("ESC/RPM telemetry is absent; RPM-tracked notch evidence is unavailable from this log.")
    if not params:
        missing.append("PARM messages are absent; current notch configuration cannot be confirmed.")
    return missing


def classify_findings(current: dict[str, Any], vibe: dict[str, Any], pid: dict[str, Any], esc: dict[str, Any], logging: dict[str, Any], fft: dict[str, Any], risk: dict[str, Any]) -> list[dict[str, Any]]:
    findings = []
    if vibe.get("severe"):
        findings.append({"severity": "fail", "safety_gate": "bench_check_required", "finding": "Severe vibration or clipping was detected; notch filters must not be used to hide this.", "evidence": vibe})
    elif vibe.get("grey_zone"):
        findings.append({"severity": "conditional", "finding": "Vibration is in the Methodic grey zone; check clipping and hardware before treating filter review as clean.", "evidence": vibe})
    elif vibe.get("available"):
        findings.append({"severity": "info", "finding": "VIBE evidence is below severe/grey-zone thresholds.", "evidence": vibe})
    else:
        findings.append({"severity": "conditional", "finding": "VIBE evidence is missing.", "evidence": vibe})

    if logging.get("limits_diagnosis"):
        findings.append({"severity": "fail", "safety_gate": "repeat_step", "finding": "Logging health limits notch/filter evidence; dropouts or sparse timing can invalidate FFT review.", "evidence": logging})
    if risk.get("high"):
        findings.append({"severity": "fail", "safety_gate": "repeat_step", "finding": "CPU/logging/filter-load risk is high for this notch review.", "evidence": risk})
    if not fft.get("fft_available"):
        findings.append({"severity": "conditional", "finding": "FFT/filter evidence is missing or unusable.", "evidence": public_fft_context(fft)})
    if not current.get("enabled"):
        findings.append({"severity": "conditional", "finding": "Harmonic notch is not currently enabled; this log can guide a controlled Filter Review capture but should not auto-set parameters.", "evidence": current})
    if pid.get("dmod_low_messages") or pid.get("flags_nonzero_messages"):
        findings.append({"severity": "conditional", "finding": "PID Dmod/Flags evidence suggests possible noise limiting or controller protection activity.", "evidence": pid})
    if not esc.get("rpm_available"):
        findings.append({"severity": "conditional", "finding": "ESC/RPM telemetry is unavailable; use throttle-based or FFT evidence only if supported by the capture.", "evidence": esc})
    return findings


def checked_but_not_supported(tables: dict[str, Any], fft: dict[str, Any], esc: dict[str, Any]) -> list[str]:
    checked = []
    if fft.get("fft_available"):
        checked.append("FFT/filter review input availability checked and usable")
    if esc.get("available") and not esc.get("rpm_available"):
        checked.append("ESC telemetry present but RPM fields were not found")
    if "BAT" not in tables and "POWR" not in tables:
        checked.append("Power context not available for this filter review")
    return checked


def classify_result(result: dict[str, Any], current: dict[str, Any], vibe: dict[str, Any], fft: dict[str, Any], logging: dict[str, Any], risk: dict[str, Any], esc: dict[str, Any]) -> tuple[str, str]:
    if vibe.get("severe"):
        return "fail", "bench_check_required"
    if logging.get("limits_diagnosis") or risk.get("high"):
        return "fail", "repeat_step"
    if not fft.get("fft_available"):
        if vibe.get("available") and not vibe.get("severe"):
            return "conditional_pass", "proceed_with_caution"
        return "inconclusive", "repeat_step"
    if not esc.get("rpm_available"):
        return "conditional_pass", "proceed_with_caution"
    if current.get("enabled") and not vibe.get("grey_zone"):
        return "pass", "proceed"
    return "conditional_pass", "proceed_with_caution"


def recommended_next_steps(result: dict[str, Any], current: dict[str, Any], fft: dict[str, Any], esc: dict[str, Any], vibe: dict[str, Any], logging: dict[str, Any], risk: dict[str, Any]) -> list[str]:
    if result["result"] == "pass":
        return [
            "Agent should inspect FFT peaks, VIBE/clipping, PID Dmod/flags, and current notch parameters before accepting Methodic 8.1.",
            "If the evidence agrees, proceed to Methodic 8.2 throttle-controller review.",
        ]
    steps = []
    if vibe.get("severe"):
        steps.append("Stop filter tuning and inspect mechanical vibration sources, prop/motor balance, mounts, frame stiffness, and clipping before another filter review.")
    if logging.get("limits_diagnosis"):
        steps.append("Repeat the diagnostic capture only after resolving logging dropouts, timestamp gaps, or sparse data; do not trust missing FFT peaks from a damaged capture.")
    if risk.get("high"):
        steps.append("Reduce diagnostic logging/filter-load risk before repeating the capture; high-rate logging should be short and targeted.")
    if not fft.get("fft_available"):
        steps.append("Collect a controlled Filter Review log with raw IMU logging or ISBH/ISBD batch sampling, then disable high-volume logging after the capture.")
    if not esc.get("rpm_available"):
        steps.append("If ESC/RPM telemetry is not available, use throttle-based or FFT-derived evidence only after checking voltage compensation and representative hover/throttle coverage.")
    if not current.get("enabled"):
        steps.append("Use the Filter Review tool to evaluate candidate notch settings; do not upload automatic notch changes from this script.")
    if vibe.get("grey_zone"):
        steps.append("Treat vibration grey-zone evidence as a hardware check first; proceed to notch review only with caution and zero clipping.")
    return steps or ["Resolve the listed evidence limits before treating Methodic 8.1 as complete."]


def confidence_limits(fft: dict[str, Any], raw: dict[str, Any], esc: dict[str, Any], logging: dict[str, Any]) -> list[str]:
    limits = []
    if not fft.get("fft_available"):
        limits.append("No usable FFT/filter evidence was available; dominant frequencies and filter effectiveness cannot be confirmed.")
    if not raw.get("batch_sampler_available") and not raw.get("raw_imu_available"):
        limits.append("Neither raw IMU nor batch-sampler logging was present.")
    if not esc.get("rpm_available"):
        limits.append("RPM-tracked notch suitability cannot be confirmed without ESC/RPM telemetry.")
    if logging.get("high_volume_logging_enabled"):
        limits.append("High-volume raw/batch logging appears enabled; it should be disabled after diagnostic capture.")
    return limits


def make_plots(tables: dict[str, Any], plots_dir: Path, fft: dict[str, Any], current: dict[str, Any]) -> list[str]:
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except Exception:
        return []
    out = ensure_dir(plots_dir)
    plots = []

    peaks = dominant_peaks(fft)
    if peaks:
        fig = go.Figure()
        fig.add_trace(go.Bar(x=[p.get("frequency_hz") for p in peaks], y=[p.get("amplitude") for p in peaks], text=[p.get("field") for p in peaks], name="Dominant peaks"))
        fig.update_layout(title="Methodic 8.1 FFT dominant peaks", template="plotly_white", xaxis_title="Frequency (Hz)", yaxis_title="Amplitude")
        plots.append(write_plot(fig, out / "methodic_8_1_fft_spectrum.html"))

    vibe = tables.get("VIBE")
    if vibe is not None and "TimeS" in getattr(vibe, "columns", []):
        fig = go.Figure()
        for col in ("VibeX", "VibeY", "VibeZ", *clip_columns(vibe)):
            if col in vibe.columns:
                fig.add_trace(go.Scatter(x=vibe["TimeS"], y=vibe[col], mode="lines", name=f"VIBE.{col}"))
        fig.update_layout(title="Methodic 8.1 VIBE and clipping", template="plotly_white", hovermode="x unified")
        plots.append(write_plot(fig, out / "methodic_8_1_vibe_clipping.html"))

    pid_fig = make_subplots(rows=3, cols=1, shared_xaxes=True, subplot_titles=("PIDR", "PIDP", "PIDY"))
    has_pid = False
    for row, name in enumerate(("PIDR", "PIDP", "PIDY"), start=1):
        df = tables.get(name)
        if df is None or "TimeS" not in getattr(df, "columns", []):
            continue
        for col in ("D", "Dmod", "Flags", "SRate"):
            if col in df.columns:
                pid_fig.add_trace(go.Scatter(x=df["TimeS"], y=df[col], mode="lines", name=f"{name}.{col}"), row=row, col=1)
                has_pid = True
    if has_pid:
        pid_fig.update_layout(title="Methodic 8.1 PID Dmod/Flags", template="plotly_white", hovermode="x unified")
        plots.append(write_plot(pid_fig, out / "methodic_8_1_rate_pid_dmod_flags.html"))

    rpm_fig = go.Figure()
    for name in ("ESC", "ESCX", "EDT2", "RPM"):
        df = tables.get(name)
        if df is None or "TimeS" not in getattr(df, "columns", []):
            continue
        for col in df.columns:
            if "rpm" in str(col).lower():
                rpm_fig.add_trace(go.Scatter(x=df["TimeS"], y=df[col], mode="lines", name=f"{name}.{col}"))
    if rpm_fig.data:
        rpm_fig.update_layout(title="Methodic 8.1 ESC/RPM telemetry", template="plotly_white", hovermode="x unified")
        plots.append(write_plot(rpm_fig, out / "methodic_8_1_esc_rpm.html"))

    fig = go.Figure(data=[go.Table(
        header={"values": ["Parameter", "Value"]},
        cells={"values": config_table_values(current)},
    )])
    fig.update_layout(title="Methodic 8.1 notch parameter summary")
    plots.append(write_plot(fig, out / "methodic_8_1_notch_configuration.html"))
    return plots


def config_table_values(current: dict[str, Any]) -> list[list[Any]]:
    rows = []
    for prefix, group in [("primary", current.get("primary") or {}), ("secondary", current.get("secondary") or {})]:
        for key in ("enabled", "mode", "freq_hz", "bandwidth_hz", "attenuation_db", "harmonics_mask", "reference", "options"):
            rows.append([f"{prefix}.{key}", group.get(key)])
    rows.append(["gyro_filter_hz", current.get("gyro_filter_hz")])
    for key, value in (current.get("raw_logging") or {}).items():
        rows.append([key, value])
    return [[r[0] for r in rows], [r[1] for r in rows]]


def write_plot(fig: Any, path: Path) -> str:
    fig.write_html(str(path), include_plotlyjs="cdn")
    return str(path)


def write_summary(path: Path, result: dict[str, Any]) -> None:
    lines = [
        "# Methodic 8.1 Harmonic Notch / Filter Review",
        "",
        f"- Result: `{result['result']}`",
        f"- Safety gate: `{result['safety_gate']}`",
        f"- Filter Review ready: `{result['filter_review_ready']}`",
        f"- Notch source recommendation: `{result['notch_source_recommendation']}`",
        f"- Next Methodic step: `{result['next_methodic_step']}`",
        f"- Official reference: {METHODIC_8_1_URL}",
        "",
        "## Findings",
    ]
    lines.extend(f"- {item.get('severity', 'info')}: {item.get('finding')}" for item in result["findings"]) if result["findings"] else lines.append("- No deterministic finding was produced.")
    lines.extend(["", "## Missing Evidence"])
    lines.extend(f"- {item}" for item in result["missing_evidence"]) if result["missing_evidence"] else lines.append("- None reported by deterministic checks.")
    lines.extend(["", "## Recommended Next Steps"])
    lines.extend(f"- {item}" for item in result["recommended_next_steps"])
    lines.extend(["", "## What Not To Do"])
    lines.extend(f"- {item}" for item in result["what_not_to_do"])
    ensure_dir(path.parent)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Gather deterministic Methodic 8.1 harmonic notch/filter evidence.")
    parser.add_argument("log", help="ArduPilot DataFlash .BIN/.log file")
    parser.add_argument("--out", default="out/methodic_8_1.json")
    parser.add_argument("--summary", default=None)
    parser.add_argument("--plots", default=None)
    args = parser.parse_args()
    try:
        result = analyze_notch_review(args.log, plots_dir=args.plots)
        write_json(args.out, result)
        if args.summary:
            write_summary(Path(args.summary), result)
        print(json.dumps(result, indent=2))
        return 0
    except AnalysisError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
