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

from ap_common import AnalysisError, clip_columns, collect_dataflash, ensure_dir, numeric_series, rows_to_dataframe, safe_float, safe_int, write_json
from ap_log_fft import fft_from_isb_rows, fft_from_tables
from ap_methodic_oscillation import classify_oscillation

METHODIC_8_3_URL = "https://ardupilot.github.io/MethodicConfigurator/TUNING_GUIDE_ArduCopter#83-suppress-frame-resonance-with-pid-notch-filters-advancedoptional"
ARDUPILOT_NOTCH_URL = "https://ardupilot.org/copter/docs/common-imu-notch-filtering.html"
RESULT_VALUES = {"not_needed", "candidate", "unsafe_to_attempt", "inconclusive"}
MESSAGES = [
    "RATE",
    "PIDR",
    "PIDP",
    "PIDY",
    "VIBE",
    "IMU",
    "GYR",
    "ACC",
    "IMU_FAST",
    "RAW_IMU",
    "ISBH",
    "ISBD",
    "RCOU",
    "RCO2",
    "RCO3",
    "PARM",
    "DSF",
    "DRO",
    "DROP",
    "ARM",
]
PARAMETERS = [
    "FILT1_*",
    "FILT2_*",
    "FILT3_*",
    "FILT4_*",
    "FILT5_*",
    "FILT6_*",
    "FILT7_*",
    "FILT8_*",
    "ATC_RAT_RLL_FLTT",
    "ATC_RAT_RLL_FLTE",
    "ATC_RAT_RLL_FLTD",
    "ATC_RAT_RLL_NTF",
    "ATC_RAT_RLL_NEF",
    "ATC_RAT_PIT_FLTT",
    "ATC_RAT_PIT_FLTE",
    "ATC_RAT_PIT_FLTD",
    "ATC_RAT_PIT_NTF",
    "ATC_RAT_PIT_NEF",
    "ATC_RAT_YAW_FLTT",
    "ATC_RAT_YAW_FLTE",
    "ATC_RAT_YAW_FLTD",
    "ATC_RAT_YAW_NTF",
    "ATC_RAT_YAW_NEF",
    "PSC_ACCZ_NTF",
    "PSC_ACCZ_NEF",
    "INS_HNTCH_*",
    "INS_HNTC2_*",
    "INS_GYRO_FILTER",
    "LOG_BITMASK",
]
AXES = {
    "roll": {"rate": "ROut", "actual": "R", "target": "RDes", "pid": "PIDR"},
    "pitch": {"rate": "POut", "actual": "P", "target": "PDes", "pid": "PIDP"},
    "yaw": {"rate": "YOut", "actual": "Y", "target": "YDes", "pid": "PIDY"},
}


def rows_to_tables(rows_by_message: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    return {name: rows_to_dataframe(rows) for name, rows in rows_by_message.items() if rows}


def analyze_pid_notch_review(log_path: str | Path, *, plots_dir: str | Path | None = None) -> dict[str, Any]:
    rows, index, stats = collect_dataflash(log_path, include=MESSAGES, source=str(log_path))
    tables = rows_to_tables(rows)
    params = index.get("parameters", {}) or {}
    vibration = analyze_vibration(tables)
    logging = analyze_logging(index)
    notch = notch_precondition(params)
    output_osc = output_oscillation_context(tables)
    fft = fft_context(rows, tables)
    rate_pid_frequency = analyze_rate_pid_frequency(tables)
    motor_frequency = analyze_motor_frequency(tables)
    pid_state = analyze_pid_state(tables)
    preconditions = build_preconditions(notch, vibration, output_osc, logging)
    candidates = select_resonance_candidates(rate_pid_frequency, motor_frequency, fft)

    result = empty_result(params)
    result["analysis_window"]["parser_stats"] = stats
    result["preconditions"] = preconditions
    result["evidence_quality"] = evidence_quality(tables, fft, rate_pid_frequency, preconditions)
    result["resonance_frequency_hz"] = candidates[0]["frequency_hz"] if candidates else None
    result["affected_axis"] = candidates[0]["axis"] if candidates else None
    result["resonance_candidates"] = candidates
    result["evidence_used"] = [
        {"type": "messages_present", "messages": sorted(tables.keys())},
        {"type": "preconditions", "value": preconditions},
        {"type": "vibration", "value": vibration},
        {"type": "output_oscillation_context", "value": output_osc},
        {"type": "logging_health", "value": logging},
        {"type": "fft_context", "value": public_fft_context(fft)},
        {"type": "rate_pid_frequency", "value": rate_pid_frequency},
        {"type": "motor_output_frequency", "value": motor_frequency},
        {"type": "pid_state", "value": pid_state},
    ]
    result["missing_evidence"] = missing_evidence(tables, fft)
    result["findings"] = classify_findings(preconditions, candidates, rate_pid_frequency, pid_state, vibration)
    result["checked_but_not_supported"] = checked_but_not_supported(tables, candidates, fft)
    result["result"], result["safety_gate"] = classify_result(preconditions, candidates, result["evidence_quality"], vibration, output_osc)
    result["next_methodic_step"] = "8.4" if result["result"] in {"not_needed", "candidate"} else "repeat_8.3_or_return_to_8.1"
    result["recommended_next_steps"] = recommended_next_steps(result, candidates, preconditions)
    result["what_not_to_do"] = [
        "Do not automatically add or upload FILTn_* or ATC_RAT_*_NTF/NEF parameters from this script output.",
        "Do not use PID notch filters to mask mechanical vibration, clipping, loose hardware, or prop/motor imbalance.",
        "Do not attempt PID notch work if harmonic notch setup, output oscillation, vibration, or CPU/logging health is unresolved.",
        "Do not add FILTn_* parameters manually if the firmware does not expose them; Methodic notes these are ArduCopter 4.5+ parameters.",
        "Do not treat a single weak spectrum peak as a final filter recommendation; the agent must inspect the evidence and follow-up logs.",
    ]
    if plots_dir:
        result["plots"] = make_plots(tables, Path(plots_dir), rate_pid_frequency, candidates)
    return result


def empty_result(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "methodic_step": "8.3",
        "title": "PID notch / frame resonance review",
        "official_reference": {"url": METHODIC_8_3_URL, "supporting_urls": [ARDUPILOT_NOTCH_URL]},
        "result": "inconclusive",
        "safety_gate": "repeat_step",
        "resonance_frequency_hz": None,
        "affected_axis": None,
        "evidence_quality": "low",
        "preconditions": {},
        "resonance_candidates": [],
        "evidence_used": [],
        "missing_evidence": [],
        "manual_observations_required": ["No visible shaking", "No abnormal sound", "Vehicle remains controllable during isolated inputs"],
        "analysis_window": {"selection": "whole_log", "preferred_window": "Short isolated roll, pitch, and yaw input logs after harmonic notch setup is correct.", "start_s": None, "end_s": None},
        "findings": [],
        "checked_but_not_supported": [],
        "parameter_context": parameter_context(params),
        "plots": [],
        "recommended_next_steps": [],
        "what_not_to_do": [],
        "next_methodic_step": None,
        "confidence_limits": [],
    }


def parameter_context(params: dict[str, Any]) -> dict[str, Any]:
    present = {}
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
    filt_present = any(k.startswith("FILT") for k in params)
    return {
        "relevant_parameters": PARAMETERS,
        "present": present,
        "missing_or_not_logged": missing,
        "source": "log PARM messages" if params else "no PARM messages found",
        "firmware_support_warning": None if filt_present else "FILTn_* parameters were not logged; firmware may not expose PID notch filters. Do not add them manually if not visible.",
    }


def series_values(df: Any, col: str | None) -> list[float]:
    if df is None or not col:
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
    return {"available": True, "samples": len(values), "mean": mean(values), "p95_abs": percentile([abs(v) for v in values], 95), "max_abs": max(abs(v) for v in values)}


def analyze_vibration(tables: dict[str, Any]) -> dict[str, Any]:
    vibe = tables.get("VIBE")
    if vibe is None or len(vibe) == 0:
        return {"available": False, "severe": False, "grey_zone": False, "warning": "VIBE missing; vibration/clipping precondition cannot be confirmed."}
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
    max_axis = max((a["max"] for a in axes.values()), default=None)
    p95_axis = max((a["p95"] for a in axes.values() if a.get("p95") is not None), default=None)
    severe = (max_axis or 0.0) > 60.0 or any((v or 0.0) > 0 for v in clip_delta.values())
    grey = not severe and ((max_axis or 0.0) > 15.0 or (p95_axis or 0.0) > 15.0)
    return {"available": True, "axes": axes, "clip_delta": clip_delta, "max_axis": max_axis, "p95_axis": p95_axis, "severe": severe, "grey_zone": grey}


def analyze_logging(index: dict[str, Any]) -> dict[str, Any]:
    health = dict(index.get("logging_health") or {})
    return health


def notch_precondition(params: dict[str, Any]) -> dict[str, Any]:
    enabled = safe_int(params.get("INS_HNTCH_ENABLE"), 0) == 1 or safe_int(params.get("INS_HNTC2_ENABLE"), 0) == 1
    return {
        "harmonic_notch_configured": enabled,
        "primary_mode": safe_int(params.get("INS_HNTCH_MODE")),
        "primary_freq_hz": safe_float(params.get("INS_HNTCH_FREQ")),
        "secondary_enabled": safe_int(params.get("INS_HNTC2_ENABLE"), 0) == 1,
    }


def output_oscillation_context(tables: dict[str, Any]) -> dict[str, Any]:
    rate = tables.get("RATE")
    if rate is None or len(rate) == 0:
        return {"available": False, "unresolved": False, "axes": {}, "warning": "RATE missing; output oscillation precondition cannot be confirmed."}
    times = series_values(rate, "TimeS")
    axes = {}
    unresolved = False
    for axis, spec in AXES.items():
        values = series_values(rate, spec["rate"])
        if not values:
            continue
        cls = classify_oscillation(values, times[: len(values)], threshold=0.15, min_samples=20, min_duration_s=1.0)
        metrics = cls.get("metrics") or {}
        high = cls.get("classification") in {"oscillatory", "mixed"} and (
            (metrics.get("p95_abs") or 0.0) > 0.15
            or (metrics.get("percent_above_threshold") or 0.0) > 5.0
        )
        unresolved = unresolved or high
        axes[axis] = {"classification": cls.get("classification"), "metrics": metrics, "unresolved_output_oscillation": high}
    return {"available": True, "unresolved": unresolved, "axes": axes}


def fft_context(rows: dict[str, list[dict[str, Any]]], tables: dict[str, Any]) -> dict[str, Any]:
    if rows.get("ISBH") or rows.get("ISBD"):
        return fft_from_isb_rows(rows)
    return fft_from_tables(tables)


def public_fft_context(fft: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in fft.items() if k != "_series"}


def build_preconditions(notch: dict[str, Any], vibration: dict[str, Any], output_osc: dict[str, Any], logging: dict[str, Any]) -> dict[str, Any]:
    return {
        "harmonic_notch_review_passed_or_conditional": bool(notch.get("harmonic_notch_configured")),
        "vibration_not_severe": not vibration.get("severe", False),
        "output_oscillation_not_unresolved": not output_osc.get("unresolved", False),
        "cpu_logging_health_acceptable": not logging.get("limits_diagnosis", False),
        "details": {"notch": notch, "vibration": vibration, "output_oscillation": output_osc, "logging_health": logging},
    }


def frequency_spectrum(df: Any, fields: list[str], axis: str, source: str) -> list[dict[str, Any]]:
    if df is None or len(df) < 64 or "TimeS" not in getattr(df, "columns", []):
        return []
    try:
        import numpy as np
    except Exception:
        return []
    times = numeric_series(df, ["TimeS"])
    if times is None:
        return []
    t = times.to_numpy(dtype=float)
    if len(t) < 64:
        return []
    dt = np.diff(t)
    dt = dt[np.isfinite(dt) & (dt > 0)]
    if len(dt) < 10:
        return []
    sample_dt = float(np.median(dt))
    if not math.isfinite(sample_dt) or sample_dt <= 0 or sample_dt > 0.1:
        return []
    out = []
    for field in fields:
        if field not in df.columns:
            continue
        y_series = numeric_series(df, [field])
        if y_series is None or len(y_series.dropna()) < 64:
            continue
        y = y_series.to_numpy(dtype=float)
        length = min(len(y), len(t))
        y = y[:length]
        y = y - np.nanmean(y)
        y = np.nan_to_num(y)
        spec = np.abs(np.fft.rfft(y * np.hanning(length)))
        freq = np.fft.rfftfreq(length, d=sample_dt)
        valid = (freq >= 10.0) & (freq <= min(120.0, 0.45 / sample_dt))
        if not valid.any():
            continue
        vf = freq[valid]
        vs = spec[valid]
        if len(vs) < 5:
            continue
        top_idx = int(np.argmax(vs))
        peak_amp = float(vs[top_idx])
        floor = float(np.median(vs) + 1e-9)
        ratio = peak_amp / floor if floor > 0 else None
        out.append({"axis": axis, "source": source, "field": field, "frequency_hz": float(vf[top_idx]), "amplitude": peak_amp, "noise_floor": floor, "peak_to_floor_ratio": ratio, "samples": int(length), "sample_rate_hz": float(1.0 / sample_dt)})
    return out


def analyze_rate_pid_frequency(tables: dict[str, Any]) -> dict[str, Any]:
    peaks = []
    for axis, spec in AXES.items():
        rate = tables.get("RATE")
        fields = [f for f in [spec["rate"], spec["actual"], spec["target"]] if f]
        peaks.extend(frequency_spectrum(rate, fields, axis, "RATE"))
        pid = tables.get(spec["pid"])
        peaks.extend(frequency_spectrum(pid, ["D", "P", "I"], axis, spec["pid"]))
    strong = [p for p in peaks if significant_peak(p)]
    return {"available": bool(peaks), "peaks": sorted(peaks, key=lambda p: p.get("peak_to_floor_ratio") or 0.0, reverse=True)[:24], "strong_peaks": sorted(strong, key=lambda p: p.get("peak_to_floor_ratio") or 0.0, reverse=True)[:12]}


def analyze_motor_frequency(tables: dict[str, Any]) -> dict[str, Any]:
    peaks = []
    for name in ("RCOU", "RCO2", "RCO3"):
        df = tables.get(name)
        if df is None:
            continue
        fields = [c for c in df.columns if str(c).startswith("C") and str(c)[1:].isdigit()]
        peaks.extend(frequency_spectrum(df, fields[:12], "motor_outputs", name))
    strong = [p for p in peaks if significant_peak(p)]
    return {"available": bool(peaks), "peaks": sorted(peaks, key=lambda p: p.get("peak_to_floor_ratio") or 0.0, reverse=True)[:24], "strong_peaks": sorted(strong, key=lambda p: p.get("peak_to_floor_ratio") or 0.0, reverse=True)[:12]}


def significant_peak(peak: dict[str, Any]) -> bool:
    ratio = peak.get("peak_to_floor_ratio") or 0.0
    amplitude = peak.get("amplitude") or 0.0
    source = str(peak.get("source") or "")
    minimum = 5.0 if source.startswith("RCO") else 0.05
    return ratio >= 8.0 and amplitude >= minimum


def analyze_pid_state(tables: dict[str, Any]) -> dict[str, Any]:
    messages = {}
    dmod_low = []
    flags_nonzero = []
    for name in ("PIDR", "PIDP", "PIDY"):
        df = tables.get(name)
        if df is None:
            continue
        terms = {}
        for col in ("Dmod", "Flags", "D", "P", "I", "SRate"):
            vals = series_values(df, col)
            if vals:
                terms[col] = summarize(vals)
        if "Dmod" in terms and terms["Dmod"].get("available") and min(series_values(df, "Dmod")) < 0.8:
            dmod_low.append(name)
        if "Flags" in terms and (terms["Flags"].get("max_abs") or 0.0) > 0:
            flags_nonzero.append(name)
        messages[name] = {"samples": len(df), "terms": terms}
    return {"available": bool(messages), "messages": messages, "dmod_low_messages": dmod_low, "flags_nonzero_messages": flags_nonzero}


def select_resonance_candidates(rate_pid: dict[str, Any], motor: dict[str, Any], fft: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = []
    fft_peaks = sorted(fft.get("peaks") or [], key=lambda p: safe_float(p.get("amplitude"), 0.0) or 0.0, reverse=True)[:20]
    for peak in rate_pid.get("strong_peaks") or []:
        freq = peak.get("frequency_hz")
        if freq is None:
            continue
        motor_near = nearest_frequency(freq, motor.get("strong_peaks") or [], tolerance_hz=3.0)
        imu_near = nearest_frequency(freq, fft_peaks, tolerance_hz=3.0)
        if motor_near or imu_near or (peak.get("peak_to_floor_ratio") or 0.0) >= 15.0:
            candidates.append({
                "axis": peak.get("axis"),
                "frequency_hz": freq,
                "rate_pid_peak": peak,
                "supporting_motor_peak": motor_near,
                "supporting_imu_peak": imu_near,
                "confidence": "high" if motor_near or imu_near else "medium",
                "interpretation": "Candidate frame resonance appears in rate/PID frequency content and supporting motor/IMU evidence or a very strong controller peak.",
            })
    candidates.sort(key=lambda c: (c.get("confidence") == "high", c["rate_pid_peak"].get("peak_to_floor_ratio") or 0.0), reverse=True)
    return candidates[:6]


def nearest_frequency(freq: float, peaks: list[dict[str, Any]], tolerance_hz: float) -> dict[str, Any] | None:
    best = None
    best_delta = tolerance_hz
    for peak in peaks:
        pf = safe_float(peak.get("frequency_hz"))
        if pf is None:
            continue
        delta = abs(pf - freq)
        if delta <= best_delta:
            best = peak
            best_delta = delta
    return best


def evidence_quality(tables: dict[str, Any], fft: dict[str, Any], rate_pid: dict[str, Any], preconditions: dict[str, Any]) -> str:
    if not preconditions.get("cpu_logging_health_acceptable") or not preconditions.get("vibration_not_severe"):
        return "low"
    has_required = "RATE" in tables and any(name in tables for name in ("PIDR", "PIDP", "PIDY")) and "PARM" in tables
    if has_required and rate_pid.get("available") and fft.get("fft_available"):
        return "high"
    if has_required and rate_pid.get("available"):
        return "medium"
    return "low"


def missing_evidence(tables: dict[str, Any], fft: dict[str, Any]) -> list[str]:
    missing = []
    for name in ("RATE", "VIBE", "PARM"):
        if name not in tables:
            missing.append(f"Missing required/strong evidence: {name}")
    if not any(name in tables for name in ("PIDR", "PIDP", "PIDY")):
        missing.append("Missing PIDR/PIDP/PIDY; PID notch review cannot assess controller-side resonance.")
    if not fft.get("fft_available"):
        missing.append(f"Frequency-domain IMU evidence unavailable: {fft.get('reason') or 'no usable FFT evidence'}.")
    return missing


def classify_findings(preconditions: dict[str, Any], candidates: list[dict[str, Any]], rate_pid: dict[str, Any], pid_state: dict[str, Any], vibration: dict[str, Any]) -> list[dict[str, Any]]:
    findings = []
    if not preconditions.get("harmonic_notch_review_passed_or_conditional"):
        findings.append({"severity": "blocker", "finding": "Harmonic notch setup is not confirmed; PID notch review should wait.", "evidence": preconditions["details"]["notch"]})
    if not preconditions.get("vibration_not_severe"):
        findings.append({"severity": "blocker", "finding": "Severe vibration or clipping makes PID notch work unsafe to attempt.", "evidence": vibration})
    if not preconditions.get("output_oscillation_not_unresolved"):
        findings.append({"severity": "blocker", "finding": "Output oscillation is unresolved; PID notch review must not proceed.", "evidence": preconditions["details"]["output_oscillation"]})
    if not preconditions.get("cpu_logging_health_acceptable"):
        findings.append({"severity": "blocker", "finding": "CPU/logging health is not acceptable for PID notch evidence.", "evidence": preconditions["details"]["logging_health"]})
    if candidates:
        findings.append({"severity": "candidate", "finding": "A candidate axis-specific frame resonance was detected in rate/PID evidence.", "evidence": candidates[0]})
    elif rate_pid.get("available"):
        findings.append({"severity": "info", "finding": "No strong rate/PID resonance candidate was detected.", "evidence": rate_pid})
    if pid_state.get("dmod_low_messages") or pid_state.get("flags_nonzero_messages"):
        findings.append({"severity": "caution", "finding": "PID Dmod/Flags indicate possible noise limiting or controller protection activity.", "evidence": pid_state})
    return findings


def checked_but_not_supported(tables: dict[str, Any], candidates: list[dict[str, Any]], fft: dict[str, Any]) -> list[str]:
    out = []
    if not candidates and "RATE" in tables:
        out.append("RATE/PID frequency review did not support a PID notch candidate")
    if not fft.get("fft_available"):
        out.append("IMU FFT evidence was checked but unavailable or unusable")
    if "RCOU" not in tables and "RCO2" not in tables and "RCO3" not in tables:
        out.append("Motor output frequency support was unavailable")
    return out


def classify_result(preconditions: dict[str, Any], candidates: list[dict[str, Any]], quality: str, vibration: dict[str, Any], output_osc: dict[str, Any]) -> tuple[str, str]:
    if vibration.get("severe") or output_osc.get("unresolved") or not preconditions.get("cpu_logging_health_acceptable"):
        return "unsafe_to_attempt", "bench_check_required" if vibration.get("severe") else "do_not_proceed"
    if not preconditions.get("harmonic_notch_review_passed_or_conditional"):
        return "inconclusive", "repeat_step"
    if quality == "low":
        return "inconclusive", "repeat_step"
    if candidates and quality in {"medium", "high"}:
        return "candidate", "proceed_with_caution"
    return "not_needed", "proceed"


def recommended_next_steps(result: dict[str, Any], candidates: list[dict[str, Any]], preconditions: dict[str, Any]) -> list[str]:
    if result["result"] == "not_needed":
        return [
            "Do not add PID notch filters from this evidence; continue the Methodic workflow if the agent agrees no resonance candidate exists.",
            "Keep the harmonic notch, vibration, and output-oscillation gates documented before proceeding to Methodic 8.4.",
        ]
    if result["result"] == "candidate":
        top = candidates[0]
        return [
            f"Treat {top.get('axis')} near {top.get('frequency_hz'):.1f} Hz as a PID notch review candidate only; the agent must inspect plots and firmware parameter availability.",
            "Use the ArduPilot PID Review and Filter Review tools before proposing any FILTn/ATC_RAT_*_NEF/NTF review candidate.",
            "Plan a follow-up short isolated-axis log to confirm improvement; disable the PID notch if follow-up evidence does not improve.",
        ]
    if result["result"] == "unsafe_to_attempt":
        return [
            "Do not attempt PID notch filtering until vibration/clipping, output oscillation, and CPU/logging health blockers are resolved.",
            "Return to Methodic 7.1.1 or 8.1 as indicated by the blocking evidence.",
        ]
    return [
        "Collect a short isolated-axis log with RATE, PIDR/PIDP/PIDY, VIBE, PARM, and usable frequency-domain evidence before classifying Methodic 8.3.",
        "Confirm harmonic notch review is passed or conditional before using this optional PID notch step.",
    ]


def make_plots(tables: dict[str, Any], plots_dir: Path, rate_pid: dict[str, Any], candidates: list[dict[str, Any]]) -> list[str]:
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except Exception:
        return []
    out = ensure_dir(plots_dir)
    plots = []
    peak_fig = go.Figure()
    peaks = rate_pid.get("peaks") or []
    if peaks:
        peak_fig.add_trace(go.Bar(x=[p["frequency_hz"] for p in peaks], y=[p.get("peak_to_floor_ratio") for p in peaks], text=[f"{p['axis']} {p['source']}.{p['field']}" for p in peaks], name="RATE/PID peaks"))
        for candidate in candidates:
            peak_fig.add_vline(x=candidate["frequency_hz"], line_color="#dc2626", line_dash="dash")
        peak_fig.update_layout(title="Methodic 8.3 RATE/PID frequency evidence", template="plotly_white", xaxis_title="Frequency (Hz)", yaxis_title="Peak/noise floor ratio")
        plots.append(write_plot(peak_fig, out / "methodic_8_3_rate_pid_frequency.html"))

    rate = tables.get("RATE")
    if rate is not None and "TimeS" in getattr(rate, "columns", []):
        fig = make_subplots(rows=3, cols=1, shared_xaxes=True, subplot_titles=("Roll", "Pitch", "Yaw"))
        for row, field in enumerate(("ROut", "POut", "YOut"), start=1):
            if field in rate.columns:
                fig.add_trace(go.Scatter(x=rate["TimeS"], y=rate[field], mode="lines", name=f"RATE.{field}"), row=row, col=1)
        fig.update_layout(title="Methodic 8.3 RATE outputs", template="plotly_white", hovermode="x unified")
        plots.append(write_plot(fig, out / "methodic_8_3_rate_outputs.html"))

    pid_fig = make_subplots(rows=3, cols=1, shared_xaxes=True, subplot_titles=("PIDR", "PIDP", "PIDY"))
    has_pid = False
    for row, name in enumerate(("PIDR", "PIDP", "PIDY"), start=1):
        df = tables.get(name)
        if df is None or "TimeS" not in getattr(df, "columns", []):
            continue
        for col in ("P", "I", "D", "Dmod", "Flags"):
            if col in df.columns:
                pid_fig.add_trace(go.Scatter(x=df["TimeS"], y=df[col], mode="lines", name=f"{name}.{col}"), row=row, col=1)
                has_pid = True
    if has_pid:
        pid_fig.update_layout(title="Methodic 8.3 PID terms, Dmod, and Flags", template="plotly_white", hovermode="x unified")
        plots.append(write_plot(pid_fig, out / "methodic_8_3_pid_terms_dmod_flags.html"))

    vibe = tables.get("VIBE")
    if vibe is not None and "TimeS" in getattr(vibe, "columns", []):
        fig = go.Figure()
        for col in ("VibeX", "VibeY", "VibeZ", *clip_columns(vibe)):
            if col in vibe.columns:
                fig.add_trace(go.Scatter(x=vibe["TimeS"], y=vibe[col], mode="lines", name=f"VIBE.{col}"))
        fig.update_layout(title="Methodic 8.3 VIBE and clipping", template="plotly_white", hovermode="x unified")
        plots.append(write_plot(fig, out / "methodic_8_3_vibe_clipping.html"))
    return plots


def write_plot(fig: Any, path: Path) -> str:
    fig.write_html(str(path), include_plotlyjs="cdn")
    return str(path)


def write_summary(path: Path, result: dict[str, Any]) -> None:
    lines = [
        "# Methodic 8.3 PID Notch / Frame Resonance Review",
        "",
        f"- Result: `{result['result']}`",
        f"- Safety gate: `{result['safety_gate']}`",
        f"- Affected axis: `{result['affected_axis']}`",
        f"- Resonance frequency: `{result['resonance_frequency_hz']}`",
        f"- Evidence quality: `{result['evidence_quality']}`",
        f"- Official reference: {METHODIC_8_3_URL}",
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
    parser = argparse.ArgumentParser(description="Gather deterministic Methodic 8.3 PID notch/frame-resonance evidence.")
    parser.add_argument("log", help="ArduPilot DataFlash .BIN/.log file")
    parser.add_argument("--out", default="out/methodic_8_3.json")
    parser.add_argument("--summary", default=None)
    parser.add_argument("--plots", default=None)
    args = parser.parse_args()
    try:
        result = analyze_pid_notch_review(args.log, plots_dir=args.plots)
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
