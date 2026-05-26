#!/usr/bin/env python3
from __future__ import annotations

from typing import Any, Dict, Iterable, Optional


ATTITUDE_RATE_CLASSES = {"yaw_misbehaviour", "attitude_rate_issue", "crash_or_loss_of_control", "general_investigation"}
ACTUATOR_MESSAGES = {"RCOU", "RCO2", "RCO3"}
ESC_MESSAGES = {"ESC", "ESCX", "EDT2"}
RAW_IMU_MESSAGES = {"IMU", "IMU_FAST", "GYR", "ACC", "ISBH", "ISBD", "RAW_IMU"}


def _table_has_rows(table: Any) -> bool:
    try:
        return table is not None and len(table) > 0
    except Exception:
        return table is not None


def _present_messages(index: Optional[Dict[str, Any]] = None, tables: Optional[Dict[str, Any]] = None) -> set[str]:
    present = set()
    for name in ((index or {}).get("messages") or {}).keys():
        present.add(str(name).upper())
    for name, table in (tables or {}).items():
        if _table_has_rows(table):
            present.add(str(name).upper())
    return present


def _status_any(present: set[str], messages: Iterable[str]) -> str:
    return "available" if present & {m.upper() for m in messages} else "missing"


def _status_all_some(present: set[str], messages: Iterable[str]) -> str:
    required = {m.upper() for m in messages}
    found = present & required
    if found == required:
        return "available"
    if found:
        return "partial"
    return "missing"


def _pid_messages_for(symptom_class: str) -> list[str]:
    if symptom_class == "yaw_misbehaviour":
        return ["PIDY"]
    if symptom_class == "attitude_rate_issue":
        return ["PIDR", "PIDP"]
    return ["PIDR", "PIDP", "PIDY"]


def _fft_status(present: set[str], fft_result: Optional[Dict[str, Any]], limits: list[str]) -> str:
    if fft_result:
        if fft_result.get("fft_available") is True:
            return "available"
        reason = fft_result.get("reason") or fft_result.get("status") or "FFT output did not contain usable frequency evidence"
        limits.append(f"FFT unusable: {reason}; vibration/filter conclusions are limited.")
        return "unusable"
    if present & RAW_IMU_MESSAGES:
        limits.append("FFT was not provided even though raw/high-rate IMU evidence may be present; filter/noise conclusions are limited until FFT is run.")
    else:
        limits.append("FFT evidence is missing; vibration/filter-frequency conclusions are limited.")
    return "missing"


def _parameter_status(index: Optional[Dict[str, Any]], present: set[str], external_parameter_context: Optional[Dict[str, Any]], limits: list[str]) -> str:
    params = (index or {}).get("parameters") or {}
    external_params = (external_parameter_context or {}).get("parameters") or {}
    if "PARM" in present or (params and not external_params):
        return "available"
    if params or external_params:
        limits.append("Parameter context comes from an external parameter file, not logged PARM evidence for this flight.")
        return "partial"
    limits.append("Parameter context is missing; configuration-dependent conclusions are limited.")
    return "missing"


def _gps_ekf_status(present: set[str]) -> str:
    gps = bool(present & {"GPS", "GPS2", "GPA"})
    estimator = bool(present & {"XKF1", "XKF2", "XKF3", "XKF4", "NKF1", "NKF2", "NKF3", "NKF4"})
    mag = "MAG" in present
    if gps and estimator and mag:
        return "available"
    if gps or estimator or mag:
        return "partial"
    return "missing"


def _overall_status(symptom_class: str, statuses: Dict[str, str], limits: list[str]) -> str:
    if symptom_class in ATTITUDE_RATE_CLASSES:
        if statuses["attitude_tracking"] == "missing" or statuses["rate_tracking"] == "missing":
            limits.append("ATT/RATE tracking evidence is missing; control-cause ranking should remain low confidence.")
            return "low"
    if symptom_class == "yaw_misbehaviour":
        if statuses["pid_terms"] != "available":
            limits.append("PIDY is missing; yaw controller/tuning confidence is capped at medium.")
        if statuses["actuator_outputs"] == "missing":
            limits.append("Actuator outputs are missing; yaw authority and saturation confidence is reduced.")
        if statuses["esc_telemetry"] == "missing":
            limits.append("ESC-level confirmation is unavailable without ESC/ESCX/EDT2 telemetry.")
    if symptom_class == "attitude_rate_issue" and statuses["pid_terms"] != "available":
        limits.append("PIDR/PIDP evidence is incomplete; roll/pitch tuning confidence is reduced.")

    if statuses["attitude_tracking"] == "missing" or statuses["rate_tracking"] == "missing":
        return "low"
    important_missing = [
        statuses["pid_terms"] != "available",
        statuses["actuator_outputs"] == "missing",
        statuses["esc_telemetry"] == "missing",
        statuses["fft"] in {"missing", "unusable"},
        statuses["parameter_context"] == "missing",
    ]
    return "medium" if any(important_missing) else "high"


def build_control_evidence_completeness(
    symptom_class: str = "general_investigation",
    *,
    index: Optional[Dict[str, Any]] = None,
    tables: Optional[Dict[str, Any]] = None,
    fft_result: Optional[Dict[str, Any]] = None,
    external_parameter_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Summarize whether control-diagnosis evidence is complete enough to rank causes.

    This is a planning and confidence aid. It does not diagnose a fault.
    """
    present = _present_messages(index=index, tables=tables)
    limits: list[str] = []
    statuses: Dict[str, str] = {
        "attitude_tracking": _status_any(present, {"ATT"}),
        "rate_tracking": _status_any(present, {"RATE"}),
        "pid_terms": _status_all_some(present, _pid_messages_for(symptom_class)),
        "actuator_outputs": _status_any(present, ACTUATOR_MESSAGES),
        "esc_telemetry": _status_any(present, ESC_MESSAGES),
        "rc_input": _status_any(present, {"RCIN"}),
        "vibration": "available" if "VIBE" in present else ("partial" if present & RAW_IMU_MESSAGES else "missing"),
        "fft": _fft_status(present, fft_result, limits),
        "gps_ekf": _gps_ekf_status(present),
        "parameter_context": _parameter_status(index, present, external_parameter_context, limits),
    }
    statuses["overall"] = _overall_status(symptom_class, statuses, limits)
    statuses["confidence_limits"] = list(dict.fromkeys(limits))
    return statuses
