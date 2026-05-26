#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


VIBE_WARN_THRESHOLD = 30.0


def _artifact(label: str, path: str, why: str, priority: int) -> Dict[str, Any]:
    return {"label": label, "path": path, "why": why, "priority": int(priority)}


def _find_plot(plots: Iterable[str], filename: str) -> Optional[str]:
    for plot in plots or []:
        if Path(str(plot)).name == filename:
            return str(plot)
    return None


def _ranking_modes(result: Dict[str, Any]) -> List[str]:
    modes = []
    for item in result.get("ranking") or []:
        mode = item.get("decoded_mode")
        if mode:
            modes.append(str(mode))
    return modes


def _auto_worse_reason(result: Dict[str, Any], fallback: str) -> str:
    modes = _ranking_modes(result)
    if modes and modes[0] == "AUTO" and "POSHOLD" in modes[1:]:
        return "shows AUTO worse than POSHOLD for the compared symptom"
    return fallback


def _vibration_elevated_modes(result: Dict[str, Any]) -> List[str]:
    elevated = []
    for item in result.get("mode_comparisons") or []:
        vibe = ((item.get("metrics") or {}).get("vibration")) or {}
        values = []
        for entry in vibe.values():
            if isinstance(entry, dict):
                value = entry.get("max", entry.get("p95"))
                if value is not None:
                    try:
                        values.append(float(value))
                    except (TypeError, ValueError):
                        pass
        if values and max(values) > VIBE_WARN_THRESHOLD:
            elevated.append(str(item.get("decoded_mode") or item.get("query") or "mode"))
    return elevated


def _motor_saturation_present(result: Dict[str, Any]) -> bool:
    for item in result.get("mode_comparisons") or []:
        channels = (((item.get("metrics") or {}).get("motor_outputs") or {}).get("channels")) or {}
        for entry in channels.values():
            if not isinstance(entry, dict):
                continue
            high = entry.get("pct_high_ge_1900") or 0
            low = entry.get("pct_low_le_1100") or 0
            try:
                if float(high) > 0 or float(low) > 0:
                    return True
            except (TypeError, ValueError):
                continue
    return False


def recommend_mode_compare_artifacts(result: Dict[str, Any], plots: Iterable[str]) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    plots = list(plots or [])
    if not plots:
        return [], "No plots were generated; recommended_user_artifacts is empty."
    artifacts: List[Dict[str, Any]] = []
    if result.get("symptom_class") == "yaw_misbehaviour":
        path = _find_plot(plots, "yaw_rate_comparison_by_mode.html")
        if path:
            artifacts.append(_artifact(
                "Yaw rate comparison by mode",
                path,
                _auto_worse_reason(result, "shows yaw rate tracking differences across the compared modes"),
                1,
            ))
    path = _find_plot(plots, "vibration_comparison_by_mode.html")
    if path:
        elevated = _vibration_elevated_modes(result)
        if elevated:
            mode_text = ", ".join(elevated)
            artifacts.append(_artifact("Vibration comparison by mode", path, f"shows vibration elevated in {mode_text}", 2))
    path = _find_plot(plots, "motor_outputs_comparison_by_mode.html")
    if path and _motor_saturation_present(result):
        artifacts.append(_artifact("Motor output comparison by mode", path, "shows motor output saturation/asymmetry in the compared windows", 3))
    return sorted(artifacts, key=lambda item: item["priority"]), None


def _finding_text(findings: Iterable[Dict[str, Any]], checked: Iterable[Dict[str, Any]]) -> str:
    parts = []
    for finding in findings or []:
        parts.append(str(finding.get("possible_cause", "")))
        parts.extend(str(item) for item in finding.get("evidence", []) or [])
    for item in checked or []:
        parts.append(str(item.get("check", "")))
        parts.append(str(item.get("result", "")))
    return "\n".join(parts).lower()


def recommend_diagnosis_artifacts(result: Dict[str, Any], plots: Iterable[str]) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    plots = list(plots or [])
    if not plots:
        return [], "No plots were generated; recommended_user_artifacts is empty."
    symptom_class = result.get("symptom_class")
    text = _finding_text(result.get("findings", []), result.get("checked_but_not_supported", []))
    artifacts: List[Dict[str, Any]] = []
    if symptom_class == "yaw_misbehaviour":
        path = _find_plot(plots, "yaw_rate_desired_vs_actual.html")
        if path:
            artifacts.append(_artifact("Yaw rate tracking", path, "shows RATE.YDes, RATE.Y, and RATE.YOut in the selected symptom window", 1))
        path = _find_plot(plots, "yaw_attitude_desired_vs_actual.html")
        if path:
            artifacts.append(_artifact("Yaw attitude tracking", path, "shows desired versus achieved yaw in the selected symptom window", 2))
    elif symptom_class == "attitude_rate_issue":
        path = _find_plot(plots, "rate_tracking_symptom.html")
        if path:
            artifacts.append(_artifact("Rate tracking", path, "shows desired versus achieved roll/pitch/yaw rates and controller outputs", 1))
        path = _find_plot(plots, "attitude_tracking_symptom.html")
        if path:
            artifacts.append(_artifact("Attitude tracking", path, "shows desired versus achieved attitude in the selected symptom window", 2))
    for filename, label, why, priority in [
        ("vibration_symptom.html", "Vibration and clipping", "shows vibration/clipping relevant to the selected symptom window", 3),
        ("motor_outputs_during_yaw_error.html", "Motor outputs during yaw error", "shows motor outputs during the yaw symptom window", 3),
        ("motor_outputs_symptom.html", "Motor outputs", "shows motor outputs not saturating in active flight" if "no output saturation" in text else "shows motor output saturation/asymmetry evidence", 3),
        ("esc_escx_edt2_symptom.html", "ESC telemetry", "shows ESC/ESCX/EDT2 telemetry for motor-level confirmation", 4),
        ("ekf_gps_symptom.html", "EKF/GPS timeline", "shows GPS quality and EKF test ratios in the selected symptom window", 4),
        ("battery_power_symptom.html", "Battery and board power", "shows battery and board-power context in the selected symptom window", 4),
    ]:
        path = _find_plot(plots, filename)
        if not path:
            continue
        if filename == "vibration_symptom.html" and not ("vibration" in text or symptom_class == "vibration_issue"):
            continue
        if filename in {"motor_outputs_during_yaw_error.html", "motor_outputs_symptom.html"} and not any(term in text for term in ["motor", "output", "saturation", "authority"]):
            continue
        artifacts.append(_artifact(label, path, why, priority))
    deduped = []
    seen = set()
    for artifact in sorted(artifacts, key=lambda item: item["priority"]):
        key = artifact["path"]
        if key not in seen:
            deduped.append(artifact)
            seen.add(key)
    return deduped[:5], None


def merge_recommended_artifacts(*sources: Dict[str, Any]) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    seen = set()
    for source in sources:
        if not source:
            continue
        for item in source.get("recommended_user_artifacts") or []:
            if isinstance(item, dict):
                path = str(item.get("path") or item.get("label") or "")
                if not path or path in seen:
                    continue
                merged.append({
                    "label": item.get("label") or Path(path).name,
                    "path": path,
                    "why": item.get("why") or "recommended by supporting evidence output",
                    "priority": int(item.get("priority") or 99),
                })
                seen.add(path)
            elif item:
                path = str(item)
                if path in seen:
                    continue
                merged.append({"label": Path(path).name, "path": path, "why": "recommended by supporting evidence output", "priority": 99})
                seen.add(path)
    return sorted(merged, key=lambda item: item["priority"])[:8]
