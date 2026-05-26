#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from ap_common import AnalysisError, collect_dataflash, mode_segments_from_tables, read_json, rows_to_dataframe, write_json
from ap_modes import decode_copter_mode, mode_label, mode_matches, mode_timeline_from_rows


SEVERITY_SCORE = {"critical": 5, "high": 4, "medium": 3, "low": 2, "info": 1}


def _default_runner(cmd, cwd=None):
    proc = subprocess.run(cmd, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return proc.returncode, proc.stdout, proc.stderr


def _split_modes(value):
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return [str(part).strip() for part in (value or []) if str(part).strip()]


def _mode_dir_name(mode):
    decoded = decode_copter_mode(mode) or mode_label(mode)
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", decoded).strip("_")
    return text or "mode"


def _mode_segments_from_log(log, max_messages=None):
    rows, index, _stats = collect_dataflash(log, include=["MODE"], max_messages=max_messages)
    tables = {typ: rows_to_dataframe(data) for typ, data in rows.items() if data}
    segments = mode_segments_from_tables(tables, log_end_s=index.get("end_time_s"))
    if not segments and index.get("modes"):
        segments = mode_timeline_from_rows(index.get("modes", []), log_end_s=index.get("end_time_s"))
    return segments


def _mode_available(query, segments):
    return any(
        mode_matches(segment.get("raw_mode"), query) or mode_matches(segment.get("decoded_mode"), query) or mode_matches(segment.get("mode"), query)
        for segment in segments
    )


def _read_json_if_exists(path):
    path = Path(path)
    if not path.exists():
        return {}
    try:
        return read_json(path)
    except Exception as exc:
        return {"_read_error": str(exc)}


def _diagnosis_command(log, symptom, mode, out_json, plots_dir, *, active_flight_only=False, exclude_ground_spool=False, max_messages=None):
    cmd = [
        sys.executable,
        str(SCRIPT_DIR / "ap_log_diagnose.py"),
        str(log),
        "--symptom",
        symptom,
        "--mode",
        str(mode),
        "--out",
        str(out_json),
        "--plots",
        str(plots_dir),
        "--events",
    ]
    if active_flight_only:
        cmd.append("--active-flight-only")
    if exclude_ground_spool:
        cmd.append("--exclude-ground-spool")
    if max_messages is not None:
        cmd.extend(["--max-messages", str(max_messages)])
    return cmd


def _severity_value(finding):
    return SEVERITY_SCORE.get(str(finding.get("severity") or "").lower(), 0)


def _numeric_evidence_score(finding):
    evidence = finding.get("evidence") or {}
    values = []
    if isinstance(evidence, dict):
        for value in evidence.values():
            if isinstance(value, (int, float)):
                values.append(abs(float(value)))
    return max(values) if values else 0.0


def _mode_score(diagnosis):
    findings = diagnosis.get("findings") or []
    if not findings:
        return {"finding_count": 0, "max_severity": 0, "evidence_score": 0.0}
    return {
        "finding_count": len(findings),
        "max_severity": max(_severity_value(item) for item in findings),
        "evidence_score": max(_numeric_evidence_score(item) for item in findings),
    }


def _summarize_differences(per_mode):
    scored = []
    for mode, item in per_mode.items():
        scored.append((mode, _mode_score(item.get("diagnosis", {}))))
    if len(scored) < 2:
        return []
    key_differences = []
    by_severity = sorted(scored, key=lambda item: (item[1]["max_severity"], item[1]["evidence_score"], item[1]["finding_count"]), reverse=True)
    worst, best = by_severity[0], by_severity[-1]
    if worst[1] != best[1]:
        key_differences.append(
            f"{worst[0]} has higher-severity findings or stronger numeric evidence than {best[0]}; inspect per-mode diagnosis before drawing conclusions."
        )
    missing_by_mode = {
        mode: sorted(set((item.get("diagnosis") or {}).get("missing_required", []) + (item.get("diagnosis") or {}).get("missing_strongly_recommended", [])))
        for mode, item in per_mode.items()
    }
    if len({tuple(v) for v in missing_by_mode.values()}) > 1:
        key_differences.append("Missing evidence differs by mode; compare each per-mode missing_required and missing_strongly_recommended list.")
    return key_differences


def diagnose_modes_for_log(
    log,
    *,
    symptom,
    modes,
    out_dir,
    active_flight_only=False,
    exclude_ground_spool=False,
    plots_root=None,
    max_messages=None,
    runner=None,
    mode_segments=None,
):
    runner = runner or _default_runner
    requested_modes = _split_modes(modes)
    if not requested_modes:
        raise AnalysisError("--modes must name at least one mode")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    segments = list(mode_segments) if mode_segments is not None else _mode_segments_from_log(log, max_messages=max_messages)

    per_mode = {}
    requested_missing = []
    confidence_limits = []
    failures = []

    for mode in requested_modes:
        if not _mode_available(mode, segments):
            requested_missing.append(mode)
            confidence_limits.append(f"Requested mode {mode} was not found in MODE intervals; no mode-scoped diagnosis was run for it.")
            continue
        mode_name = _mode_dir_name(mode)
        mode_dir = out_dir / mode_name
        plots_dir = (Path(plots_root) / mode_name / "plots") if plots_root else (mode_dir / "plots")
        diagnosis_path = mode_dir / "diagnosis.json"
        mode_dir.mkdir(parents=True, exist_ok=True)
        plots_dir.mkdir(parents=True, exist_ok=True)
        cmd = _diagnosis_command(
            log,
            symptom,
            mode,
            diagnosis_path,
            plots_dir,
            active_flight_only=active_flight_only,
            exclude_ground_spool=exclude_ground_spool,
            max_messages=max_messages,
        )
        rc, stdout, stderr = runner(cmd)
        failure = None
        if rc != 0:
            failure = {
                "mode": mode,
                "returncode": rc,
                "command": cmd,
                "stdout_tail": stdout[-1000:] if stdout else "",
                "stderr_tail": stderr[-1000:] if stderr else "",
            }
            failures.append(failure)
            confidence_limits.append(f"Diagnosis command failed for mode {mode}; inspect failures before using this mode comparison.")
        diagnosis = _read_json_if_exists(diagnosis_path)
        warnings = list(diagnosis.get("warnings") or [])
        missing_evidence = {
            "required": list(diagnosis.get("missing_required") or []),
            "strongly_recommended": list(diagnosis.get("missing_strongly_recommended") or []),
            "optional": list(diagnosis.get("missing_optional") or []),
        }
        per_mode[mode_name] = {
            "query": mode,
            "decoded_mode": decode_copter_mode(mode) or mode_label(mode),
            "diagnosis_json": str(diagnosis_path),
            "plots": str(plots_dir),
            "analysis_window": diagnosis.get("analysis_window", {}),
            "missing_evidence": missing_evidence,
            "warnings": warnings,
            "finding_count": len(diagnosis.get("findings") or []),
            "diagnosis": diagnosis,
            "failure": failure,
        }

    if not per_mode:
        confidence_limits.append("No requested modes were found; this mode-scoped diagnosis pack contains no per-mode diagnosis outputs.")

    summary = {
        "log": str(log),
        "symptom": symptom,
        "modes": requested_modes,
        "mode_intervals": segments,
        "requested_modes_missing": requested_missing,
        "per_mode": per_mode,
        "key_differences": _summarize_differences(per_mode),
        "confidence_limits": list(dict.fromkeys(confidence_limits)),
        "failures": failures,
        "diagnostic_aid_note": "Mode-scoped diagnosis is a supporting evidence workflow for the agent. It is not a final user-facing diagnosis.",
    }
    write_json(out_dir / "mode_diagnosis_summary.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description="Run symptom diagnosis separately for requested flight modes.")
    parser.add_argument("log")
    parser.add_argument("--symptom", required=True)
    parser.add_argument("--modes", required=True, help="Comma-separated mode names or numeric Copter mode ids, e.g. AUTO,POSHOLD or 3,16")
    parser.add_argument("--active-flight-only", action="store_true")
    parser.add_argument("--exclude-ground-spool", action="store_true")
    parser.add_argument("--plots", default=None, help="Optional root directory for per-mode plots; defaults to --out")
    parser.add_argument("--max-messages", type=int, default=None)
    parser.add_argument("--out", default="out/mode_diagnosis")
    args = parser.parse_args()
    result = diagnose_modes_for_log(
        args.log,
        symptom=args.symptom,
        modes=args.modes,
        out_dir=args.out,
        active_flight_only=args.active_flight_only,
        exclude_ground_spool=args.exclude_ground_spool,
        plots_root=args.plots,
        max_messages=args.max_messages,
    )
    print(
        f"Mode-scoped diagnosis written: {args.out} "
        f"({len(result['per_mode'])} modes, {len(result['requested_modes_missing'])} missing)"
    )
    return 2 if not result["per_mode"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
