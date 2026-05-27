#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ap_common import ensure_dir, write_json

PASS_RESULTS = {"pass", "ready_for_guided_checks", "ready_for_further_precision_land_tests", "ready_for_operational_checks", "not_needed", "ready_for_model", "ready_for_careful_test", "candidate", "ready_for_magfit"}
CONDITIONAL_RESULTS = {"conditional_pass", "candidate", "review_required", "compensation_needed"}
FAILED_RESULTS = {"fail", "not_ready", "do_not_proceed", "do_not_use", "unsafe_to_attempt", "fix_hardware_first", "fix_ekf_gps_first", "reduce_gains", "reduce_position_gains", "needs_sensor_review"}
INCONCLUSIVE_RESULTS = {"inconclusive", "repeat_step", "collect_better_log", "repeat_flight", "repeat_evaluation", "not_applicable"}
BLOCKING_GATES = {"do_not_proceed", "bench_check_required", "repeat_step"}


def build_progress(paths: list[str | Path]) -> dict[str, Any]:
    records = [load_step_result(path) for path in paths]
    records.sort(key=step_sort_key)
    steps = {record["step_id"]: record for record in records}
    completed = [r for r in records if classify_record(r) == "completed"]
    conditional = [r for r in records if classify_record(r) == "conditional"]
    failed = [r for r in records if classify_record(r) == "failed"]
    inconclusive = [r for r in records if classify_record(r) == "inconclusive"]
    blocker = current_blocker(records)
    next_step = recommended_next_step(records, blocker)
    status = progress_status(records, blocker, inconclusive, conditional)
    return {
        "progress_status": status,
        "steps": steps,
        "completed_steps": [summary_for_lists(r) for r in completed],
        "conditional_steps": [summary_for_lists(r) for r in conditional],
        "failed_steps": [summary_for_lists(r) for r in failed],
        "inconclusive_steps": [summary_for_lists(r) for r in inconclusive],
        "current_blocker": blocker,
        "recommended_next_methodic_step": next_step,
        "what_not_to_do": what_not_to_do(),
    }


def load_step_result(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    step_id = str(data.get("methodic_step") or data.get("step_id") or data.get("step") or "unknown")
    source_log = extract_log_file(data) or str(p)
    record = {
        "step_id": step_id,
        "methodic_step": step_id,
        "title": data.get("title") or "",
        "result": str(data.get("result") or data.get("productive_config_status") or "inconclusive"),
        "safety_gate": str(data.get("safety_gate") or "repeat_step"),
        "log_file": source_log,
        "source_json": str(p),
        "date_time": extract_datetime(data),
        "blocking_items": extract_blocking_items(data),
        "next_step": data.get("next_methodic_step"),
        "missing_evidence": list(data.get("missing_evidence") or []),
        "confidence_limits": list(data.get("confidence_limits") or []),
        "recommended_next_steps": list(data.get("recommended_next_steps") or []),
        "what_not_to_do": list(data.get("what_not_to_do") or []),
    }
    return record


def extract_log_file(data: dict[str, Any]) -> str | None:
    window = data.get("analysis_window") or {}
    for key in ("log_path", "log", "source", "before_log", "after_log", "index_path"):
        if window.get(key):
            return str(window[key])
    for item in data.get("evidence_used") or []:
        if isinstance(item, dict):
            value = item.get("value")
            if isinstance(value, dict):
                for key in ("log", "log_path", "source", "path"):
                    if value.get(key):
                        return str(value[key])
    return None


def extract_datetime(data: dict[str, Any]) -> str | None:
    for key in ("date_time", "datetime", "timestamp", "generated_at"):
        if data.get(key):
            return str(data[key])
    window = data.get("analysis_window") or {}
    for key in ("date_time", "datetime", "timestamp"):
        if window.get(key):
            return str(window[key])
    return None


def extract_blocking_items(data: dict[str, Any]) -> list[str]:
    items = list(data.get("blocking_items") or [])
    for finding in data.get("findings") or []:
        if not isinstance(finding, dict):
            continue
        severity = str(finding.get("severity") or "").lower()
        gate = str(finding.get("safety_gate") or "").lower()
        if severity in {"critical", "fail", "error"} or gate in BLOCKING_GATES:
            text = finding.get("finding") or finding.get("message") or str(finding)
            items.append(str(text))
    return sorted(set(items))


def classify_record(record: dict[str, Any]) -> str:
    result = record.get("result")
    gate = record.get("safety_gate")
    if result in FAILED_RESULTS or gate == "do_not_proceed" or record.get("blocking_items"):
        return "failed"
    if result in CONDITIONAL_RESULTS or gate == "proceed_with_caution":
        return "conditional"
    if result in PASS_RESULTS and gate not in BLOCKING_GATES:
        return "completed"
    return "inconclusive"


def current_blocker(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    for record in records:
        if classify_record(record) == "failed":
            return {
                "step_id": record["step_id"],
                "title": record["title"],
                "result": record["result"],
                "safety_gate": record["safety_gate"],
                "blocking_items": record["blocking_items"] or record["missing_evidence"],
                "source_json": record["source_json"],
            }
    for record in records:
        if classify_record(record) == "inconclusive":
            return {
                "step_id": record["step_id"],
                "title": record["title"],
                "result": record["result"],
                "safety_gate": record["safety_gate"],
                "blocking_items": record["missing_evidence"] or record["confidence_limits"],
                "source_json": record["source_json"],
            }
    return None


def recommended_next_step(records: list[dict[str, Any]], blocker: dict[str, Any] | None) -> str | None:
    if blocker:
        return blocker["step_id"]
    for record in reversed(records):
        next_step = record.get("next_step")
        if next_step:
            return str(next_step)
    return None


def progress_status(records: list[dict[str, Any]], blocker: dict[str, Any] | None, inconclusive: list[dict[str, Any]], conditional: list[dict[str, Any]]) -> str:
    if not records:
        return "inconclusive"
    if blocker and blocker.get("result") in FAILED_RESULTS:
        return "blocked"
    if blocker:
        return "inconclusive"
    if conditional:
        return "conditional"
    if inconclusive:
        return "inconclusive"
    return "ready_for_next_step"


def summary_for_lists(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "step_id": record["step_id"],
        "title": record["title"],
        "result": record["result"],
        "safety_gate": record["safety_gate"],
        "log_file": record["log_file"],
        "source_json": record["source_json"],
    }


def step_sort_key(record: dict[str, Any]) -> tuple[int, ...]:
    parts = []
    for part in str(record.get("step_id") or "").split("."):
        try:
            parts.append(int(part))
        except ValueError:
            parts.append(999)
    return tuple(parts or [999])


def what_not_to_do() -> list[str]:
    return [
        "Do not skip failed or inconclusive Methodic steps without user confirmation and a documented safety rationale.",
        "Do not treat this progress file as a final report; the agent must inspect the underlying evidence.",
        "Do not make blind gain or safety-parameter changes from progress status alone.",
        "Do not declare the aircraft safe to fly from Methodic progress tracking.",
    ]


def write_summary(path: str | Path, progress: dict[str, Any]) -> None:
    p = Path(path)
    ensure_dir(p.parent)
    lines = [
        "# Methodic Progress",
        "",
        f"- Progress status: `{progress['progress_status']}`",
        f"- Recommended next Methodic step: `{progress.get('recommended_next_methodic_step')}`",
        f"- Current blocker: `{(progress.get('current_blocker') or {}).get('step_id')}`",
        "",
        "## Steps",
    ]
    for record in progress.get("steps", {}).values():
        lines.append(f"- {record['step_id']} {record['title']}: `{record['result']}` / `{record['safety_gate']}`")
    lines.extend(["", "## What Not To Do"])
    lines.extend(f"- {item}" for item in progress["what_not_to_do"])
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Combine Methodic step JSON outputs into a progress tracker.")
    parser.add_argument("step_json", nargs="+", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args()
    progress = build_progress(args.step_json)
    write_json(args.out, progress)
    if args.summary:
        write_summary(args.summary, progress)
    else:
        print(json.dumps(progress, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
