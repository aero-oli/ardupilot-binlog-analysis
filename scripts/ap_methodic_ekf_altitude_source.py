#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from statistics import mean
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ap_common import AnalysisError, clip_columns, collect_dataflash, ensure_dir, numeric_series, rows_to_dataframe, safe_float, safe_int, write_json

METHODIC_8_4_URL = "https://ardupilot.github.io/MethodicConfigurator/TUNING_GUIDE_ArduCopter#84-configure-the-ekf-altitude-source-weights"
LOG_MESSAGES_URL = "https://ardupilot.org/copter/docs/logmessages.html"
MESSAGES = [
    "CTUN",
    "BARO",
    "GPS",
    "GPS2",
    "GPA",
    "XKF1",
    "XKF2",
    "XKF3",
    "XKF4",
    "NKF1",
    "NKF2",
    "NKF3",
    "NKF4",
    "VIBE",
    "BAT",
    "POWR",
    "PARM",
    "RNGF",
    "ATT",
    "RATE",
    "MODE",
    "MSG",
    "ERR",
    "EV",
    "ARM",
]
PARAMETERS = [
    "EK3_SRC1_POSZ",
    "EK3_OGN_HGT_MASK",
    "EK3_PRIMARY",
    "RNGFND*_TYPE",
    "RNGFND*_ORIENT",
    "RNGFND*_MIN_CM",
    "RNGFND*_MAX_CM",
    "PSC_POSZ_P",
    "PSC_VELZ_P",
    "PSC_VELZ_I",
    "PSC_ACCZ_P",
    "PSC_ACCZ_I",
    "PSC_ACCZ_D",
]


def rows_to_tables(rows_by_message: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    return {name: rows_to_dataframe(rows) for name, rows in rows_by_message.items() if rows}


def analyze_ekf_altitude_source(log_path: str | Path, *, plots_dir: str | Path | None = None) -> dict[str, Any]:
    rows, index, stats = collect_dataflash(log_path, include=MESSAGES, source=str(log_path))
    tables = rows_to_tables(rows)
    params = index.get("parameters", {}) or {}

    ctun = analyze_ctun_altitude(tables)
    baro = analyze_baro(tables)
    gps = analyze_gps_altitude(tables)
    rngf = analyze_rangefinder(tables, params)
    ekf = analyze_ekf_height(tables)
    vibe = analyze_vibration(tables)
    power = analyze_power(tables)
    modes = analyze_modes(tables)
    events = analyze_events(tables)

    result = empty_result(params)
    result["analysis_window"]["parser_stats"] = stats
    result["height_source_assessment"] = {
        "baro": baro["status"],
        "rangefinder": rngf["status"],
        "gps_altitude": gps["status"],
        "ekf_height_test_ratio": ekf.get("height_test_ratio"),
        "details": {
            "ctun": ctun,
            "baro": baro,
            "rangefinder": rngf,
            "gps_altitude": gps,
            "ekf": ekf,
            "vibration": vibe,
            "power": power,
            "modes": modes,
            "events": events,
        },
    }
    result["evidence_used"] = [
        {"type": "messages_present", "messages": sorted(tables.keys())},
        {"type": "ctun_altitude", "value": ctun},
        {"type": "baro", "value": baro},
        {"type": "gps_altitude", "value": gps},
        {"type": "rangefinder", "value": rngf},
        {"type": "ekf_height", "value": ekf},
        {"type": "vibration", "value": vibe},
        {"type": "power", "value": power},
        {"type": "mode_specific_behaviour", "value": modes},
        {"type": "events", "value": events},
    ]
    result["missing_evidence"] = missing_evidence(tables, ctun, baro, ekf, power, vibe)
    result["findings"] = classify_findings(ctun, baro, gps, rngf, ekf, vibe, power, events)
    result["checked_but_not_supported"] = checked_but_not_supported(tables)
    result["result"], result["safety_gate"] = classify_result(result["findings"], result["missing_evidence"], ctun, baro, ekf, vibe)
    result["next_methodic_step"] = "8.5" if result["result"] in {"pass", "review_required"} else "repeat_8.4"
    result["recommended_next_steps"] = recommended_next_steps(result, ctun, baro, gps, rngf, ekf, vibe, power)
    result["what_not_to_do"] = [
        "Do not change EKF height source weighting unless source-specific evidence supports it.",
        "Do not treat GPS altitude as a primary height-source fix without EKF/baro/rangefinder evidence.",
        "Do not mask severe vibration, barometer disturbance, rangefinder dropout, or power problems by changing EKF parameters.",
        "Do not guess EKF source settings from missing logs; collect CTUN, BARO, GPS/GPA, XKF*/NKF*, VIBE, BAT/POWR, and PARM evidence.",
    ]
    result["confidence_limits"] = confidence_limits(result["missing_evidence"], ekf, rngf, gps)
    if plots_dir:
        result["plots"] = make_plots(tables, Path(plots_dir))
    return result


def empty_result(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "methodic_step": "8.4",
        "title": "EKF altitude source weights / review",
        "official_reference": {"url": METHODIC_8_4_URL, "supporting_urls": [LOG_MESSAGES_URL]},
        "result": "inconclusive",
        "safety_gate": "repeat_step",
        "height_source_assessment": {},
        "evidence_used": [],
        "missing_evidence": [],
        "manual_observations_required": ["No unexplained altitude jumps", "No surface-effect or airflow condition that invalidates the capture"],
        "analysis_window": {"selection": "whole_log", "preferred_window": "Representative hover and altitude changes using the intended altitude sensors.", "start_s": None, "end_s": None},
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
    return {"relevant_parameters": PARAMETERS, "present": present, "missing_or_not_logged": missing, "source": "log PARM messages" if params else "no PARM messages found"}


def series_values(df: Any, col: str | None) -> list[float]:
    if df is None or not col:
        return []
    s = numeric_series(df, [col])
    if s is None:
        return []
    return [float(v) for v in s.dropna().tolist()]


def first_col(df: Any, names: list[str]) -> str | None:
    if df is None:
        return None
    lower = {str(c).lower(): c for c in getattr(df, "columns", [])}
    for name in names:
        if name in getattr(df, "columns", []):
            return name
        if name.lower() in lower:
            return lower[name.lower()]
    return None


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * pct / 100.0))))
    return float(ordered[idx])


def summarize(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"available": False, "samples": 0}
    return {"available": True, "samples": len(values), "min": min(values), "max": max(values), "mean": mean(values), "p95_abs": percentile([abs(v) for v in values], 95), "range": max(values) - min(values)}


def analyze_ctun_altitude(tables: dict[str, Any]) -> dict[str, Any]:
    ctun = tables.get("CTUN")
    if ctun is None or len(ctun) == 0:
        return {"available": False, "status": "missing", "reason": "CTUN missing; desired/actual altitude tracking cannot be assessed."}
    alt_col = first_col(ctun, ["Alt", "BAlt"])
    dalt_col = first_col(ctun, ["DAlt", "TAlt"])
    alt = series_values(ctun, alt_col)
    dalt = series_values(ctun, dalt_col)
    err = [a - d for a, d in zip(alt, dalt)] if alt and dalt else []
    err_p95 = percentile([abs(v) for v in err], 95) if err else None
    span = max(alt) - min(alt) if alt else None
    status = "usable"
    if not alt:
        status = "missing"
    elif err_p95 is not None and err_p95 > 1.0:
        status = "suspect"
    elif span is not None and span > 3.0:
        status = "suspect"
    return {
        "available": bool(alt),
        "status": status,
        "altitude": summarize(alt),
        "desired_altitude": summarize(dalt),
        "alt_minus_dalt": summarize(err),
        "altitude_error_p95_m": err_p95,
        "altitude_span_m": span,
        "fields": {"altitude": alt_col, "desired_altitude": dalt_col},
    }


def analyze_baro(tables: dict[str, Any]) -> dict[str, Any]:
    baro = tables.get("BARO")
    if baro is None or len(baro) == 0:
        return {"available": False, "status": "missing", "reason": "BARO missing; barometer source quality cannot be assessed."}
    alt = series_values(baro, first_col(baro, ["Alt"]))
    press = series_values(baro, first_col(baro, ["Press", "PressAbs", "P"] ))
    status = "usable"
    if not alt:
        status = "missing"
    elif len(alt) >= 2 and max(alt) - min(alt) > 5.0:
        status = "suspect"
    return {"available": bool(alt or press), "status": status, "altitude": summarize(alt), "pressure": summarize(press)}


def analyze_gps_altitude(tables: dict[str, Any]) -> dict[str, Any]:
    gps = tables.get("GPS")
    if gps is None or len(gps) == 0:
        gps = tables.get("GPS2")
    gpa = tables.get("GPA")
    if gps is None or len(gps) == 0:
        return {"available": False, "status": "missing", "reason": "GPS/GPS2 missing; GPS altitude context unavailable."}
    alt = series_values(gps, first_col(gps, ["Alt", "RelAlt", "RAlt"]))
    status_vals = series_values(gps, first_col(gps, ["Status", "Fix", "FixType"]))
    vacc = series_values(gps, first_col(gps, ["VAcc", "VAccY", "Verr"]))
    if gpa is not None and len(gpa):
        vacc = vacc or series_values(gpa, first_col(gpa, ["VAcc", "VV", "VDop"]))
    status = "context_only"
    if not alt:
        status = "missing"
    elif status_vals and max(status_vals) < 3:
        status = "suspect"
    elif vacc and percentile(vacc, 95) is not None and (percentile(vacc, 95) or 0.0) > 3.0:
        status = "suspect"
    return {"available": bool(alt), "status": status, "altitude": summarize(alt), "fix_status": summarize(status_vals), "vertical_accuracy": summarize(vacc)}


def analyze_rangefinder(tables: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    rngf = tables.get("RNGF")
    configured = any(k.startswith("RNGFND") and k.endswith("_TYPE") and safe_int(v, 0) not in (None, 0) for k, v in params.items())
    if rngf is None or len(rngf) == 0:
        return {"available": False, "configured": configured, "status": "missing", "reason": "RNGF missing; rangefinder source cannot be assessed."}
    dist_col = first_col(rngf, ["Dist", "Range", "Rng", "D"])
    stat_col = first_col(rngf, ["Stat", "Status", "Qual", "Health"])
    values = series_values(rngf, dist_col)
    stat = series_values(rngf, stat_col)
    zero_pct = 100.0 * sum(1 for v in values if v <= 0.05) / len(values) if values else None
    dropout_pct = zero_pct or 0.0
    if stat:
        dropout_pct = max(dropout_pct, 100.0 * sum(1 for v in stat if v <= 0) / len(stat))
    status = "usable"
    if not values:
        status = "missing"
    elif dropout_pct > 10.0:
        status = "suspect"
    return {"available": bool(values), "configured": configured, "status": status, "range": summarize(values), "status_values": summarize(stat), "dropout_percent": dropout_pct}


def analyze_ekf_height(tables: dict[str, Any]) -> dict[str, Any]:
    ratios = []
    innovations = []
    sources = []
    for name in ("XKF4", "NKF4"):
        df = tables.get(name)
        if df is None:
            continue
        for col in ("SH", "HAGL", "TH", "SV"):
            vals = series_values(df, col)
            if vals and col == "SH":
                ratios.extend(vals)
                sources.append(f"{name}.{col}")
            elif vals and col != "SH":
                innovations.extend(vals)
    for name in ("XKF3", "NKF3"):
        df = tables.get(name)
        if df is None:
            continue
        for col in ("IH", "H", "VD", "IVD"):
            vals = series_values(df, col)
            if vals:
                innovations.extend(vals)
                sources.append(f"{name}.{col}")
    max_ratio = max(ratios) if ratios else None
    p95_ratio = percentile(ratios, 95) if ratios else None
    status = "usable"
    if not ratios and not innovations:
        status = "missing"
    elif max_ratio is not None and max_ratio > 1.5:
        status = "fail"
    elif max_ratio is not None and max_ratio > 1.0:
        status = "review_required"
    return {"available": bool(ratios or innovations), "status": status, "height_test_ratio": {"max": max_ratio, "p95": p95_ratio, "samples": len(ratios), "sources": sources}, "height_innovation_context": summarize(innovations)}


def analyze_vibration(tables: dict[str, Any]) -> dict[str, Any]:
    vibe = tables.get("VIBE")
    if vibe is None or len(vibe) == 0:
        return {"available": False, "status": "missing", "reason": "VIBE missing; vibration/baro correlation cannot be assessed."}
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
    severe = (max_axis or 0.0) > 60.0 or any((v or 0.0) > 0 for v in clip_delta.values())
    status = "fail" if severe else "usable"
    return {"available": True, "status": status, "axes": axes, "clip_delta": clip_delta, "max_axis": max_axis, "severe": severe}


def analyze_power(tables: dict[str, Any]) -> dict[str, Any]:
    bat = tables.get("BAT")
    powr = tables.get("POWR")
    out = {"available": bat is not None or powr is not None, "status": "usable"}
    if bat is not None and len(bat):
        volts = series_values(bat, first_col(bat, ["Volt", "VoltR", "V"]))
        out["voltage"] = summarize(volts)
        if volts and min(volts) < mean(volts) * 0.8:
            out["status"] = "suspect"
    if powr is not None and len(powr):
        vcc = series_values(powr, first_col(powr, ["Vcc", "VccMin"]))
        out["vcc"] = summarize(vcc)
        if vcc and min(vcc) < 4.7:
            out["status"] = "fail"
    if not out["available"]:
        out["status"] = "missing"
        out["warning"] = "BAT/POWR missing; power contribution cannot be assessed."
    return out


def analyze_modes(tables: dict[str, Any]) -> dict[str, Any]:
    mode = tables.get("MODE")
    if mode is None or len(mode) == 0:
        return {"available": False, "modes": []}
    label_col = first_col(mode, ["Mode", "Name", "ModeNum"])
    rows = []
    for row in mode.head(100).to_dict(orient="records"):
        rows.append({"time_s": safe_float(row.get("TimeS")), "mode": row.get(label_col) if label_col else None})
    return {"available": True, "modes": rows}


def analyze_events(tables: dict[str, Any]) -> dict[str, Any]:
    matches = []
    for name in ("MSG", "ERR", "EV"):
        df = tables.get(name)
        if df is None:
            continue
        for row in df.to_dict(orient="records"):
            text = " ".join(str(v).lower() for v in row.values())
            if any(term in text for term in ("ekf", "baro", "range", "rng", "terrain", "height", "alt")):
                matches.append({"message": name, **row})
    return {"available": bool(matches), "altitude_related_events": matches[:50]}


def missing_evidence(tables: dict[str, Any], ctun: dict[str, Any], baro: dict[str, Any], ekf: dict[str, Any], power: dict[str, Any], vibe: dict[str, Any]) -> list[str]:
    missing = []
    for name in ("CTUN", "BARO", "VIBE", "PARM"):
        if name not in tables:
            missing.append(f"Missing required/strong evidence: {name}")
    if not any(name in tables for name in ("GPS", "GPS2", "GPA")):
        missing.append("Missing GPS/GPA; GPS altitude context unavailable.")
    if not any(name in tables for name in ("XKF1", "XKF3", "XKF4", "NKF1", "NKF3", "NKF4")):
        missing.append("Missing XKF*/NKF*; EKF height innovation/test-ratio evidence unavailable.")
    if power.get("status") == "missing":
        missing.append("Missing BAT/POWR; power context unavailable.")
    if not ctun.get("available"):
        missing.append("CTUN altitude tracking is unavailable.")
    if not baro.get("available"):
        missing.append("BARO source evidence is unavailable.")
    if not ekf.get("available"):
        missing.append("EKF height test ratio or innovation evidence is unavailable.")
    if not vibe.get("available"):
        missing.append("VIBE evidence is unavailable.")
    return dedupe(missing)


def classify_findings(ctun: dict[str, Any], baro: dict[str, Any], gps: dict[str, Any], rngf: dict[str, Any], ekf: dict[str, Any], vibe: dict[str, Any], power: dict[str, Any], events: dict[str, Any]) -> list[dict[str, Any]]:
    findings = []
    if vibe.get("severe"):
        findings.append({"severity": "fail", "safety_gate": "bench_check_required", "finding": "Severe vibration or clipping blocks EKF altitude-source conclusions.", "evidence": vibe})
    if power.get("status") == "fail":
        findings.append({"severity": "fail", "safety_gate": "bench_check_required", "finding": "Board power issue can corrupt sensor/EKF evidence.", "evidence": power})
    if ctun.get("status") == "suspect":
        findings.append({"severity": "review", "finding": "CTUN desired/actual altitude tracking is suspect.", "evidence": ctun})
    if baro.get("status") == "suspect":
        findings.append({"severity": "review", "finding": "BARO altitude/pressure evidence is suspect.", "evidence": baro})
    if rngf.get("status") == "suspect":
        findings.append({"severity": "review", "finding": "Rangefinder evidence shows dropout or invalid range.", "evidence": rngf})
    if gps.get("status") == "suspect":
        findings.append({"severity": "review", "finding": "GPS altitude context is suspect; do not use GPS altitude as a blind height-source fix.", "evidence": gps})
    if ekf.get("status") == "fail":
        findings.append({"severity": "fail", "safety_gate": "do_not_proceed", "finding": "EKF height test ratio exceeds rejection threshold by a large margin.", "evidence": ekf})
    elif ekf.get("status") == "review_required":
        findings.append({"severity": "review", "finding": "EKF height test ratio exceeds 1.0 and requires source-specific review.", "evidence": ekf})
    if events.get("available"):
        findings.append({"severity": "review", "finding": "Altitude/EKF/baro/rangefinder related events were logged.", "evidence": events})
    if not findings and ctun.get("available") and baro.get("available") and ekf.get("available"):
        findings.append({"severity": "info", "finding": "Altitude source evidence is internally consistent in deterministic checks.", "evidence": {"ctun": ctun, "baro": baro, "ekf": ekf}})
    return findings


def classify_result(findings: list[dict[str, Any]], missing: list[str], ctun: dict[str, Any], baro: dict[str, Any], ekf: dict[str, Any], vibe: dict[str, Any]) -> tuple[str, str]:
    if not ctun.get("available") or not baro.get("available"):
        return "inconclusive", "repeat_step"
    if any(f.get("safety_gate") == "bench_check_required" for f in findings):
        return "fail", "bench_check_required"
    if any(f.get("severity") == "fail" for f in findings):
        return "fail", "do_not_proceed"
    if not ekf.get("available"):
        return "inconclusive", "repeat_step"
    if any(f.get("severity") == "review" for f in findings):
        return "review_required", "proceed_with_caution"
    if missing:
        return "review_required", "proceed_with_caution"
    return "pass", "proceed"


def checked_but_not_supported(tables: dict[str, Any]) -> list[str]:
    out = []
    if "RNGF" not in tables:
        out.append("Rangefinder evidence not logged; rangefinder source quality not assessed")
    if "ATT" not in tables or "RATE" not in tables:
        out.append("ATT/RATE context not logged; attitude coupling with altitude behaviour not assessed")
    if "MODE" not in tables:
        out.append("Mode-specific altitude behaviour was not assessed because MODE is missing")
    return out


def recommended_next_steps(result: dict[str, Any], ctun: dict[str, Any], baro: dict[str, Any], gps: dict[str, Any], rngf: dict[str, Any], ekf: dict[str, Any], vibe: dict[str, Any], power: dict[str, Any]) -> list[str]:
    if result["result"] == "pass":
        return ["Agent should inspect CTUN, BARO, GPS, EKF, vibration, and power evidence before accepting Methodic 8.4.", "If the evidence agrees, proceed to Methodic 8.5."]
    steps = []
    if result["result"] == "inconclusive":
        steps.append("Collect a better log with CTUN, BARO, GPS/GPA, XKF*/NKF*, VIBE, BAT/POWR, and PARM before changing EKF source parameters.")
    if vibe.get("severe"):
        steps.append("Fix vibration/clipping or mechanical setup before drawing EKF height-source conclusions.")
    if power.get("status") in {"suspect", "fail"}:
        steps.append("Resolve battery/board power evidence before blaming EKF altitude-source weighting.")
    if baro.get("status") == "suspect":
        steps.append("Inspect barometer airflow/foam/placement and pressure stability before changing EKF height-source weights.")
    if rngf.get("status") == "suspect":
        steps.append("Inspect rangefinder orientation, min/max range, surface validity, and dropout timing before using rangefinder height evidence.")
    if ekf.get("status") in {"review_required", "fail"}:
        steps.append("Correlate XKF4.SH or NKF height ratios with CTUN, BARO, RNGF, GPS altitude, vibration, power, and mode timeline before proposing any EKF source review candidate.")
    return steps or ["Resolve the listed source-specific evidence limits before treating Methodic 8.4 as complete."]


def confidence_limits(missing: list[str], ekf: dict[str, Any], rngf: dict[str, Any], gps: dict[str, Any]) -> list[str]:
    limits = list(missing)
    if rngf.get("status") == "missing":
        limits.append("Rangefinder source quality cannot be confirmed from this log.")
    if gps.get("status") == "missing":
        limits.append("GPS altitude is unavailable even as context.")
    if not ekf.get("height_test_ratio", {}).get("samples"):
        limits.append("EKF height test-ratio thresholds cannot be applied without XKF4.SH/NKF equivalent evidence.")
    return dedupe(limits)


def dedupe(items: list[str]) -> list[str]:
    out = []
    seen = set()
    for item in items:
        if item not in seen:
            out.append(item)
            seen.add(item)
    return out


def make_plots(tables: dict[str, Any], plots_dir: Path) -> list[str]:
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except Exception:
        return []
    out = ensure_dir(plots_dir)
    plots = []

    ctun = tables.get("CTUN")
    if ctun is not None and "TimeS" in getattr(ctun, "columns", []):
        fig = go.Figure()
        for col in ("DAlt", "Alt", "BAlt"):
            if col in ctun.columns:
                fig.add_trace(go.Scatter(x=ctun["TimeS"], y=ctun[col], mode="lines", name=f"CTUN.{col}"))
        fig.update_layout(title="Methodic 8.4 CTUN desired vs actual altitude", template="plotly_white", hovermode="x unified")
        plots.append(write_plot(fig, out / "methodic_8_4_ctun_altitude.html"))

    for message, cols, filename, title in [
        ("BARO", ("Alt", "Press", "PressAbs"), "methodic_8_4_baro.html", "BARO altitude and pressure"),
        ("RNGF", ("Dist", "Range", "Rng", "Status", "Stat"), "methodic_8_4_rngf.html", "Rangefinder"),
        ("GPS", ("Alt", "RelAlt", "RAlt", "VAcc", "Status", "NSats", "HDop"), "methodic_8_4_gps_altitude.html", "GPS altitude context"),
        ("XKF4", ("SH", "SV", "SP", "SM"), "methodic_8_4_ekf_height_ratio.html", "EKF test ratios"),
        ("VIBE", ("VibeX", "VibeY", "VibeZ"), "methodic_8_4_vibe.html", "VIBE and clipping"),
    ]:
        df = tables.get(message)
        if df is None or "TimeS" not in getattr(df, "columns", []):
            continue
        fig = go.Figure()
        plot_cols = list(cols)
        if message == "VIBE":
            plot_cols.extend(clip_columns(df))
        for col in plot_cols:
            if col in df.columns:
                fig.add_trace(go.Scatter(x=df["TimeS"], y=df[col], mode="lines", name=f"{message}.{col}"))
        if fig.data:
            fig.update_layout(title=f"Methodic 8.4 {title}", template="plotly_white", hovermode="x unified")
            plots.append(write_plot(fig, out / filename))

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, subplot_titles=("Battery", "Board power"))
    has_power = False
    bat = tables.get("BAT")
    if bat is not None and "TimeS" in getattr(bat, "columns", []):
        for col in ("Volt", "VoltR", "Curr"):
            if col in bat.columns:
                fig.add_trace(go.Scatter(x=bat["TimeS"], y=bat[col], mode="lines", name=f"BAT.{col}"), row=1, col=1)
                has_power = True
    powr = tables.get("POWR")
    if powr is not None and "TimeS" in getattr(powr, "columns", []):
        for col in ("Vcc", "VccMin"):
            if col in powr.columns:
                fig.add_trace(go.Scatter(x=powr["TimeS"], y=powr[col], mode="lines", name=f"POWR.{col}"), row=2, col=1)
                has_power = True
    if has_power:
        fig.update_layout(title="Methodic 8.4 BAT/POWR context", template="plotly_white", hovermode="x unified")
        plots.append(write_plot(fig, out / "methodic_8_4_bat_powr.html"))
    return plots


def write_plot(fig: Any, path: Path) -> str:
    fig.write_html(str(path), include_plotlyjs="cdn")
    return str(path)


def write_summary(path: Path, result: dict[str, Any]) -> None:
    lines = [
        "# Methodic 8.4 EKF Altitude Source Review",
        "",
        f"- Result: `{result['result']}`",
        f"- Safety gate: `{result['safety_gate']}`",
        f"- Next Methodic step: `{result['next_methodic_step']}`",
        f"- Official reference: {METHODIC_8_4_URL}",
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
    parser = argparse.ArgumentParser(description="Gather deterministic Methodic 8.4 EKF altitude-source evidence.")
    parser.add_argument("log", help="ArduPilot DataFlash .BIN/.log file")
    parser.add_argument("--out", default="out/methodic_8_4.json")
    parser.add_argument("--summary", default=None)
    parser.add_argument("--plots", default=None)
    args = parser.parse_args()
    try:
        result = analyze_ekf_altitude_source(args.log, plots_dir=args.plots)
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
