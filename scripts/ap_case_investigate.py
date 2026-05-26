#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from ap_artifact_recommendations import merge_recommended_artifacts
from ap_common import read_json, write_json


MODE_COMPARE_TOKENS = ("mission", "auto", "manual", "loiter", "poshold", "waypoint")
FFT_TOKENS = ("vibration", "vibes", "noisy", "resonance", "filter", "fft", "wobble", "unstable")


def _default_runner(cmd, cwd=None):
    proc = subprocess.run(cmd, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return proc.returncode, proc.stdout, proc.stderr


def _slug(value):
    stem = Path(value).stem
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("_")
    return text or "log"


def _symptom_slug(symptom_class):
    mapping = {
        "yaw_misbehaviour": "yaw",
        "attitude_rate_issue": "attitude",
        "ekf_gps_issue": "ekf_gps",
        "compass_yaw_source_issue": "compass_yaw",
    }
    if symptom_class in mapping:
        return mapping[symptom_class]
    text = re.sub(r"_(issue|misbehaviour)$", "", str(symptom_class))
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")
    return text or "secondary"


def _read_json_if_exists(path):
    path = Path(path)
    if not path.exists():
        return {}
    try:
        return read_json(path)
    except Exception as exc:
        return {"_read_error": str(exc)}


def _run_step(runner, cmd, failures, *, cwd=None, artifact=None, label=None, required=False):
    rc, stdout, stderr = runner(cmd, cwd=cwd)
    record = {
        "label": label or (Path(cmd[1]).name if len(cmd) > 1 else "command"),
        "command": cmd,
        "returncode": rc,
        "artifact": str(artifact) if artifact else None,
    }
    if stdout:
        record["stdout_tail"] = stdout[-1000:]
    if stderr:
        record["stderr_tail"] = stderr[-1000:]
    if rc != 0:
        record["required"] = bool(required)
        failures.append(record)
    return record


def _symptom_classes(manifest):
    primary = manifest.get("primary_symptom_class") or manifest.get("symptom_class") or "general_investigation"
    secondary = manifest.get("secondary_symptom_classes") or []
    return primary, list(secondary)


def _needs_mode_compare(symptom, manifest):
    text = str(symptom or "").lower()
    if any(token in text for token in MODE_COMPARE_TOKENS):
        return True
    commands = "\n".join(manifest.get("recommended_next_commands", []) + manifest.get("recommended_secondary_commands", []))
    return "ap_log_mode_compare.py" in commands


def _needs_fft(symptom, manifest):
    text = str(symptom or "").lower()
    classes = [manifest.get("symptom_class"), manifest.get("primary_symptom_class")] + list(manifest.get("secondary_symptom_classes") or [])
    if "vibration_issue" in classes:
        return True
    return any(token in text for token in FFT_TOKENS)


def _diagnosis_command(log_path, symptom, out_json, plots_dir):
    return [sys.executable, str(SCRIPT_DIR / "ap_log_diagnose.py"), log_path, "--symptom", symptom, "--out", str(out_json), "--plots", str(plots_dir), "--events"]


def _mode_compare_command(log_path, symptom_class, out_json, plots_dir):
    return [
        sys.executable,
        str(SCRIPT_DIR / "ap_log_mode_compare.py"),
        log_path,
        "--symptom",
        symptom_class,
        "--compare-modes",
        "AUTO,POSHOLD,ALTHOLD,STABILIZE",
        "--active-flight-only",
        "--json",
        str(out_json),
        "--plots",
        str(plots_dir),
    ]


def _artifact_map(log_dir):
    return {
        "validate": log_dir / "validate.json",
        "validate_summary": log_dir / "validate.md",
        "index": log_dir / "index.json",
        "index_summary": log_dir / "index.md",
        "manifest": log_dir / "investigation.json",
        "diagnosis": log_dir / "diagnosis.json",
        "diagnosis_plots": log_dir / "plots" / "diagnosis",
        "param_lookup": log_dir / "param_lookup.json",
        "fft": log_dir / "fft.json",
        "fft_dir": log_dir / "fft",
        "next_steps": log_dir / "next_steps.json",
        "next_steps_summary": log_dir / "next_steps.md",
    }


def _write_case_summary(out_dir, case_manifest):
    lines = [
        "# Case Evidence Pack Summary",
        "",
        "This is an evidence-pack summary for the agent. It is not a final diagnosis.",
        "",
        f"- Logs processed: {case_manifest['logs_processed']}",
        f"- Failures recorded: {len(case_manifest['failures'])}",
        "",
        "## Logs",
    ]
    for log in case_manifest["logs"]:
        lines.extend([
            f"- `{log['log']}`",
            f"  - vehicle/firmware: {log.get('vehicle') or 'unknown'} / {log.get('firmware') or 'unknown'}",
            f"  - primary: {log.get('primary_symptom_class')}",
            f"  - secondary: {', '.join(log.get('secondary_symptom_classes') or []) or 'none'}",
            f"  - warnings: {len(log.get('warnings') or [])}",
        ])
    if case_manifest["confidence_limits"]:
        lines.append("")
        lines.append("## Confidence Limits")
        lines.extend(f"- {item}" for item in case_manifest["confidence_limits"])
    Path(out_dir / "case_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_reading_order(out_dir, logs):
    lines = [
        "# Recommended Agent Reading Order",
        "",
        "1. Validation warnings: read each `logs/<log>/validate.json` and `validate.md`.",
        "2. Mode comparison: read `comparisons/mode_compare_summary.json` and per-log `mode_compare*.json` files.",
        "3. Primary diagnosis: read each per-log `diagnosis.json`.",
        "4. Secondary diagnosis: read any `diagnosis_*.json` files for secondary symptom classes.",
        "5. Param lookup: read each `param_lookup.json`.",
        "6. FFT: read each `fft.json` when present.",
        "7. Next steps: read each `next_steps.json` and `next_steps.md`.",
        "",
        "Use this as an evidence navigation aid. Codex still writes the final conclusions.",
    ]
    Path(out_dir / "recommended_agent_reading_order.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _summarise_mode_comparisons(out_dir, log_records):
    comparisons = []
    for log in log_records:
        for key, path in log.get("key_artifacts", {}).items():
            if key.startswith("mode_compare") and path:
                data = _read_json_if_exists(path)
                comparisons.append({
                    "log": log["log"],
                    "artifact": path,
                    "decoded_modes": data.get("decoded_modes"),
                    "ranking": data.get("ranking"),
                    "confidence_limits": data.get("confidence_limits", []),
                })
    write_json(out_dir / "comparisons" / "mode_compare_summary.json", {"mode_comparisons": comparisons})


def _summarise_cross_log(out_dir, log_records):
    write_json(out_dir / "comparisons" / "cross_log_summary.json", {
        "logs": [
            {
                "log": log["log"],
                "vehicle": log.get("vehicle"),
                "firmware": log.get("firmware"),
                "primary_symptom_class": log.get("primary_symptom_class"),
                "secondary_symptom_classes": log.get("secondary_symptom_classes", []),
                "warnings": log.get("warnings", []),
                "missing_evidence": log.get("missing_evidence", {}),
            }
            for log in log_records
        ],
        "note": "Cross-log summary is artifact inventory and comparison context only, not a final diagnosis.",
    })


def build_case_investigation(logs, symptom, out_dir, runner=None):
    runner = runner or _default_runner
    out_dir = Path(out_dir)
    (out_dir / "logs").mkdir(parents=True, exist_ok=True)
    (out_dir / "comparisons").mkdir(parents=True, exist_ok=True)
    (out_dir / "plots").mkdir(parents=True, exist_ok=True)
    failures = []
    log_records = []

    _run_step(
        runner,
        [sys.executable, str(SCRIPT_DIR / "ap_skill_doctor.py"), "--json", str(out_dir / "skill_doctor.json"), "--out-dir", str(out_dir / "skill_doctor")],
        failures,
        artifact=out_dir / "skill_doctor.json",
        label="skill_doctor",
    )

    for log_path in logs:
        slug = _slug(log_path)
        log_dir = out_dir / "logs" / slug
        log_dir.mkdir(parents=True, exist_ok=True)
        artifacts = _artifact_map(log_dir)

        _run_step(runner, [sys.executable, str(SCRIPT_DIR / "ap_log_validate.py"), log_path, "--json", str(artifacts["validate"]), "--summary", str(artifacts["validate_summary"])], failures, artifact=artifacts["validate"], label="validate")
        _run_step(runner, [sys.executable, str(SCRIPT_DIR / "ap_log_index.py"), log_path, "--json", str(artifacts["index"]), "--summary", str(artifacts["index_summary"])], failures, artifact=artifacts["index"], label="index")
        _run_step(runner, [sys.executable, str(SCRIPT_DIR / "ap_log_investigation_manifest.py"), log_path, "--symptom", symptom, "--out", str(artifacts["manifest"])], failures, artifact=artifacts["manifest"], label="manifest")

        manifest = _read_json_if_exists(artifacts["manifest"])
        index = _read_json_if_exists(artifacts["index"])
        primary, secondary = _symptom_classes(manifest)

        _run_step(runner, _diagnosis_command(log_path, symptom, artifacts["diagnosis"], artifacts["diagnosis_plots"]), failures, artifact=artifacts["diagnosis"], label="primary_diagnosis")

        key_artifacts = {key: str(value) for key, value in artifacts.items() if key not in {"diagnosis_plots", "fft_dir"}}
        for symptom_class in secondary:
            sec_slug = _symptom_slug(symptom_class)
            sec_path = log_dir / f"diagnosis_{sec_slug}.json"
            sec_plots = log_dir / "plots" / sec_slug
            _run_step(runner, _diagnosis_command(log_path, symptom_class, sec_path, sec_plots), failures, artifact=sec_path, label=f"secondary_diagnosis:{symptom_class}")
            key_artifacts[f"diagnosis_{sec_slug}"] = str(sec_path)

        mode_compare_paths = []
        if _needs_mode_compare(symptom, manifest):
            mode_path = log_dir / "mode_compare.json"
            _run_step(runner, _mode_compare_command(log_path, primary, mode_path, log_dir / "plots" / "mode_compare"), failures, artifact=mode_path, label="mode_compare")
            mode_compare_paths.append(mode_path)
            key_artifacts["mode_compare"] = str(mode_path)
            for symptom_class in secondary:
                if symptom_class in {"yaw_misbehaviour", "attitude_rate_issue", "ekf_gps_issue", "compass_yaw_source_issue"}:
                    sec_slug = _symptom_slug(symptom_class)
                    sec_mode_path = log_dir / f"mode_compare_{sec_slug}.json"
                    _run_step(runner, _mode_compare_command(log_path, symptom_class, sec_mode_path, log_dir / "plots" / f"mode_compare_{sec_slug}"), failures, artifact=sec_mode_path, label=f"mode_compare:{symptom_class}")
                    mode_compare_paths.append(sec_mode_path)
                    key_artifacts[f"mode_compare_{sec_slug}"] = str(sec_mode_path)

        param_path = artifacts["param_lookup"]
        _run_step(runner, [sys.executable, str(SCRIPT_DIR / "ap_param_lookup.py"), "--index", str(artifacts["index"]), "--symptom", primary, "--json", str(param_path)], failures, artifact=param_path, label="param_lookup")

        if _needs_fft(symptom, manifest):
            _run_step(runner, [sys.executable, str(SCRIPT_DIR / "ap_log_fft.py"), log_path, "--out", str(artifacts["fft_dir"]), "--json", str(artifacts["fft"])], failures, artifact=artifacts["fft"], label="fft")

        next_steps_cmd = [
            sys.executable,
            str(SCRIPT_DIR / "ap_next_steps.py"),
            "--diagnosis", str(artifacts["diagnosis"]),
            "--manifest", str(artifacts["manifest"]),
            "--param-lookup", str(param_path),
            "--json", str(artifacts["next_steps"]),
            "--summary", str(artifacts["next_steps_summary"]),
        ]
        if mode_compare_paths:
            next_steps_cmd.extend(["--mode-compare", str(mode_compare_paths[0])])
        if artifacts["fft"].exists():
            next_steps_cmd.extend(["--fft", str(artifacts["fft"])])
        _run_step(runner, next_steps_cmd, failures, artifact=artifacts["next_steps"], label="next_steps")

        recommended_user_artifacts = merge_recommended_artifacts(
            _read_json_if_exists(mode_compare_paths[0]) if mode_compare_paths else {},
            _read_json_if_exists(artifacts["diagnosis"]),
            _read_json_if_exists(artifacts["next_steps"]),
            manifest,
        )

        warnings = []
        warnings.extend(_read_json_if_exists(artifacts["validate"]).get("warnings", []))
        warnings.extend(manifest.get("warnings", []))
        confidence_limits = []
        confidence_limits.extend((_read_json_if_exists(artifacts["validate"]).get("quality") or {}).get("confidence_limits", []))
        confidence_limits.extend(manifest.get("confidence_limits", []))
        log_records.append({
            "log": log_path,
            "log_dir": str(log_dir),
            "vehicle": index.get("vehicle"),
            "firmware": index.get("firmware"),
            "warnings": warnings,
            "primary_symptom_class": primary,
            "secondary_symptom_classes": secondary,
            "key_artifacts": key_artifacts,
            "recommended_user_artifacts": recommended_user_artifacts,
            "missing_evidence": manifest.get("missing_evidence", {}),
            "confidence_limits": confidence_limits,
        })

    _summarise_cross_log(out_dir, log_records)
    _summarise_mode_comparisons(out_dir, log_records)

    confidence_limits = []
    for log in log_records:
        confidence_limits.extend(log.get("confidence_limits", []))
    for failure in failures:
        label = str(failure.get("label") or "command").replace("_", " ")
        confidence_limits.append(f"Sub-command failed ({label}): {' '.join(failure.get('command', []))}")

    case_manifest = {
        "symptom": symptom,
        "logs_processed": len(log_records),
        "logs": log_records,
        "failures": failures,
        "confidence_limits": list(dict.fromkeys(confidence_limits)),
        "key_artifacts": {
            "case_summary": str(out_dir / "case_summary.md"),
            "recommended_agent_reading_order": str(out_dir / "recommended_agent_reading_order.md"),
            "cross_log_summary": str(out_dir / "comparisons" / "cross_log_summary.json"),
            "mode_compare_summary": str(out_dir / "comparisons" / "mode_compare_summary.json"),
        },
        "recommended_user_artifacts": merge_recommended_artifacts(*log_records),
        "planning_note": "Case investigation produces an evidence pack for the agent. It does not write the final diagnosis.",
    }
    write_json(out_dir / "case_manifest.json", case_manifest)
    _write_case_summary(out_dir, case_manifest)
    _write_reading_order(out_dir, log_records)
    return case_manifest


def main():
    parser = argparse.ArgumentParser(description="Build a case-level ArduPilot evidence pack across one or more DataFlash logs.")
    parser.add_argument("--logs", nargs="+", required=True, help="One or more DataFlash .bin/.log files")
    parser.add_argument("--symptom", required=True, help="User-reported symptom text")
    parser.add_argument("--out", default="out/case", help="Case output directory")
    args = parser.parse_args()
    result = build_case_investigation(args.logs, args.symptom, Path(args.out))
    print(f"Case evidence pack written: {args.out} ({result['logs_processed']} logs, {len(result['failures'])} failures)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
