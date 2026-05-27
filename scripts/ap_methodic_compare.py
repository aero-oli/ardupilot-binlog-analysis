#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

from ap_common import ensure_dir, write_json
from ap_methodic_progress import FAILED_RESULTS
from ap_methodic_registry import get_step, load_registry


def compare_methodic_step(before_log: str | Path, after_log: str | Path, *, step_id: str, plots_dir: str | Path | None = None) -> dict[str, Any]:
    before = run_step_analysis(before_log, step_id, plots_dir=Path(plots_dir) / "before" if plots_dir else None)
    after = run_step_analysis(after_log, step_id, plots_dir=Path(plots_dir) / "after" if plots_dir else None)
    comparable = assess_comparability(before, after)
    metrics = compare_metrics(flatten_numeric(before), flatten_numeric(after), comparable)
    confidence = confidence_limits(before, after, comparable)
    result = {
        "methodic_step": step_id,
        "title": after.get("title") or before.get("title") or "",
        "before": summarize_result(before_log, before),
        "after": summarize_result(after_log, after),
        "segments_comparable": comparable,
        "metric_differences": metrics,
        "comparison_result": comparison_result(before, after, comparable),
        "confidence_limits": confidence,
        "recommended_next_steps": recommended_next_steps(before, after, comparable),
        "what_not_to_do": what_not_to_do(),
    }
    return result


def run_step_analysis(log_path: str | Path, step_id: str, *, plots_dir: Path | None = None) -> dict[str, Any]:
    import ap_methodic_step

    registry = load_registry(None)
    step = get_step(step_id, registry)
    impl_name = ap_methodic_step.STEP_IMPLEMENTATIONS.get(step["step_id"])
    if not impl_name:
        result = ap_methodic_step.not_implemented_result(step)
    else:
        result = getattr(ap_methodic_step, impl_name)(Path(log_path), step, plots_dir, [])
    result.setdefault("analysis_window", {})
    result["analysis_window"].setdefault("log_path", str(log_path))
    return result


def assess_comparability(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    reasons = []
    before_window = before.get("analysis_window") or {}
    after_window = after.get("analysis_window") or {}
    before_selection = before_window.get("selection")
    after_selection = after_window.get("selection")
    if before_selection != after_selection:
        reasons.append(f"Analysis window selectors differ: before={before_selection}, after={after_selection}.")
    before_duration = duration(before_window)
    after_duration = duration(after_window)
    duration_ratio = None
    if before_duration and after_duration:
        duration_ratio = min(before_duration, after_duration) / max(before_duration, after_duration)
        if duration_ratio < 0.6:
            reasons.append("Analysis window durations differ by more than 40%.")
    if before.get("missing_evidence") != after.get("missing_evidence"):
        reasons.append("Missing-evidence sets differ between logs.")
    before_result = str(before.get("result"))
    after_result = str(after.get("result"))
    if before_result in FAILED_RESULTS or after_result in FAILED_RESULTS:
        reasons.append("One side has a blocking/failing result; compare as safety evidence, not improvement.")
    comparable = not reasons
    return {
        "comparable": comparable,
        "before_window": before_window,
        "after_window": after_window,
        "duration_ratio": duration_ratio,
        "reasons": reasons,
    }


def compare_metrics(before: dict[str, float], after: dict[str, float], comparable: dict[str, Any]) -> list[dict[str, Any]]:
    common = sorted(set(before) & set(after))
    rows = []
    for key in common:
        b = before[key]
        a = after[key]
        if not math.isfinite(b) or not math.isfinite(a):
            continue
        delta = a - b
        if abs(delta) < max(1e-9, abs(b) * 1e-9):
            continue
        rows.append({"metric": key, "before": b, "after": a, "delta": delta, "pct_delta": (100.0 * delta / abs(b)) if abs(b) > 1e-9 else None})
    rows.sort(key=lambda item: abs(item["delta"]), reverse=True)
    limited = rows[:80]
    for item in limited:
        item["interpretation_limit"] = None if comparable.get("comparable") else "Do not claim improvement because segments/evidence are not comparable."
    return limited


def flatten_numeric(data: Any, prefix: str = "") -> dict[str, float]:
    out: dict[str, float] = {}
    if isinstance(data, dict):
        for key, value in data.items():
            child = f"{prefix}.{key}" if prefix else str(key)
            out.update(flatten_numeric(value, child))
    elif isinstance(data, list):
        for idx, value in enumerate(data[:20]):
            child = f"{prefix}[{idx}]"
            out.update(flatten_numeric(value, child))
    elif isinstance(data, (int, float)) and not isinstance(data, bool) and math.isfinite(float(data)):
        out[prefix] = float(data)
    return out


def duration(window: dict[str, Any]) -> float | None:
    start = as_float(window.get("start_s"))
    end = as_float(window.get("end_s"))
    if start is None or end is None or end <= start:
        return None
    return end - start


def as_float(value: Any) -> float | None:
    try:
        out = float(value)
        return out if math.isfinite(out) else None
    except Exception:
        return None


def summarize_result(log_path: str | Path, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "log_file": str(log_path),
        "result": result.get("result"),
        "safety_gate": result.get("safety_gate"),
        "missing_evidence": result.get("missing_evidence") or [],
        "analysis_window": result.get("analysis_window") or {},
        "findings": result.get("findings") or [],
        "plots": result.get("plots") or [],
    }


def comparison_result(before: dict[str, Any], after: dict[str, Any], comparable: dict[str, Any]) -> str:
    if not comparable.get("comparable"):
        return "not_comparable"
    if str(after.get("result")) in FAILED_RESULTS:
        return "regression_or_blocker"
    if str(before.get("result")) in FAILED_RESULTS and str(after.get("result")) not in FAILED_RESULTS:
        return "blocker_cleared_evidence"
    if before.get("result") == after.get("result"):
        return "similar_classification"
    return "classification_changed_review_required"


def confidence_limits(before: dict[str, Any], after: dict[str, Any], comparable: dict[str, Any]) -> list[str]:
    limits = []
    limits.extend(f"Before: {item}" for item in before.get("confidence_limits") or [])
    limits.extend(f"After: {item}" for item in after.get("confidence_limits") or [])
    limits.extend(comparable.get("reasons") or [])
    if not comparable.get("comparable"):
        limits.append("Do not claim improvement unless the agent verifies comparable windows, inputs, and missing-evidence limits.")
    return sorted(set(limits))


def recommended_next_steps(before: dict[str, Any], after: dict[str, Any], comparable: dict[str, Any]) -> list[str]:
    if not comparable.get("comparable"):
        return [
            "Inspect both step outputs and plots before making any before/after claim.",
            "Collect comparable Methodic windows before claiming improvement or regression.",
            "Use the after-log result for safety gating only after evidence comparability is resolved.",
        ]
    if str(after.get("result")) in FAILED_RESULTS:
        return [
            "Treat the after log as blocked for this Methodic step.",
            "Do not proceed to later Methodic steps until the after-log blocker is resolved.",
        ]
    return [
        "Inspect metric differences and plots before writing conclusions.",
        "Use this comparison as evidence only; do not auto-tune or auto-apply parameters.",
    ]


def what_not_to_do() -> list[str]:
    return [
        "Do not claim improvement when segments, windows, or missing evidence differ materially.",
        "Do not skip failed or inconclusive Methodic steps because the after log has some better metrics.",
        "Do not generate a final report automatically; the agent must inspect the evidence.",
        "Do not auto-change gains or safety parameters from this comparison.",
    ]


def write_summary(path: str | Path, result: dict[str, Any]) -> None:
    p = Path(path)
    ensure_dir(p.parent)
    lines = [
        f"# Methodic Compare {result['methodic_step']}",
        "",
        f"- Comparison result: `{result['comparison_result']}`",
        f"- Segments comparable: `{result['segments_comparable']['comparable']}`",
        f"- Before result: `{result['before']['result']}` / `{result['before']['safety_gate']}`",
        f"- After result: `{result['after']['result']}` / `{result['after']['safety_gate']}`",
        "",
        "## Comparability Limits",
    ]
    if result["segments_comparable"]["reasons"]:
        lines.extend(f"- {item}" for item in result["segments_comparable"]["reasons"])
    else:
        lines.append("- No comparability blocker reported by the script.")
    lines.extend(["", "## What Not To Do"])
    lines.extend(f"- {item}" for item in result["what_not_to_do"])
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare before/after logs for one Methodic step.")
    parser.add_argument("before_log", type=Path)
    parser.add_argument("after_log", type=Path)
    parser.add_argument("--step", required=True)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--plots", type=Path)
    args = parser.parse_args()
    result = compare_methodic_step(args.before_log, args.after_log, step_id=args.step, plots_dir=args.plots)
    if args.out:
        write_json(args.out, result)
    if args.summary:
        write_summary(args.summary, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
