#!/usr/bin/env python3
from __future__ import annotations
import argparse
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ap_common import build_index, ensure_dir, filter_tables_by_time, parse_dataflash, parse_time_window, write_json
from ap_log_metrics import compute_metrics
from ap_common import rows_to_dataframe


def metrics_for(log, window=None):
    rows = parse_dataflash(log)
    index = build_index(log, rows)
    tables = {typ: rows_to_dataframe(data) for typ, data in rows.items() if data and typ not in {"FMT", "FMTU"}}
    window = window or {"start_s": None, "end_s": None}
    tables = filter_tables_by_time(tables, **window)
    metrics = compute_metrics(tables, analysis_window=window)
    return index, metrics


def param_diff(before_params, after_params):
    out = []
    for k in sorted(set(before_params) | set(after_params)):
        b = before_params.get(k)
        a = after_params.get(k)
        if str(b) != str(a):
            out.append({"parameter": k, "before": b, "after": a})
    return out


def _flatten_numeric(prefix, value, out):
    if isinstance(value, dict):
        for key, child in value.items():
            _flatten_numeric(f"{prefix}.{key}" if prefix else str(key), child, out)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        out[prefix] = float(value)


def metric_differences(before_metrics, after_metrics, limit=100):
    before = {}
    after = {}
    _flatten_numeric("", before_metrics, before)
    _flatten_numeric("", after_metrics, after)
    rows = []
    for key in sorted(set(before) | set(after)):
        b = before.get(key)
        a = after.get(key)
        if b is None or a is None or b == a:
            continue
        delta = a - b
        pct = None if b == 0 else delta / abs(b) * 100
        rows.append({"metric": key, "before": b, "after": a, "delta": delta, "percent_delta": pct})
    rows.sort(key=lambda r: abs(r["delta"]), reverse=True)
    return rows[:limit]


def main() -> int:
    p = argparse.ArgumentParser(description="Compare two ArduPilot DataFlash logs: parameters and metrics.")
    p.add_argument("before")
    p.add_argument("after")
    p.add_argument("--out", default="compare")
    p.add_argument("--window", default=None, help="Same TimeS window for both logs")
    p.add_argument("--before-window", default=None)
    p.add_argument("--after-window", default=None)
    args = p.parse_args()
    out = ensure_dir(args.out)
    try:
        shared_window = parse_time_window(args.window)
        before_window = parse_time_window(args.before_window) if args.before_window else shared_window
        after_window = parse_time_window(args.after_window) if args.after_window else shared_window
        b_index, b_metrics = metrics_for(args.before, before_window)
        a_index, a_metrics = metrics_for(args.after, after_window)
        result = {
            "before": {"file": args.before, "index": b_index, "metrics": b_metrics},
            "after": {"file": args.after, "index": a_index, "metrics": a_metrics},
            "comparison_scope": {"before_window": before_window, "after_window": after_window},
            "parameter_differences": param_diff(b_index.get("parameters", {}), a_index.get("parameters", {})),
            "metric_differences": metric_differences(b_metrics, a_metrics),
            "comparison_notes": [
                "Only treat before/after metrics as meaningful if flight mode, payload, battery, wind, manoeuvres and segment duration are comparable.",
                "Do not infer tuning improvement solely from lower error if saturation, vibration or missing messages differ between logs."
            ],
        }
        write_json(out / "compare.json", result)
        # Markdown summary
        lines = ["# ArduPilot before/after comparison\n"]
        lines.append(f"- Before: {Path(args.before).name}")
        lines.append(f"- After: {Path(args.after).name}")
        lines.append(f"- Before window: {before_window}")
        lines.append(f"- After window: {after_window}")
        lines.append(f"- Parameter differences: {len(result['parameter_differences'])}\n")
        lines.append(f"- Numeric metric differences: {len(result['metric_differences'])}\n")
        lines.append("## Parameter differences")
        lines.append("| Parameter | Before | After |")
        lines.append("|---|---:|---:|")
        for d in result["parameter_differences"][:200]:
            lines.append(f"| `{d['parameter']}` | {d['before']} | {d['after']} |")
        lines.append("\n## Metric differences")
        lines.append("| Metric | Before | After | Delta | Delta % |")
        lines.append("|---|---:|---:|---:|---:|")
        for d in result["metric_differences"][:100]:
            pct = "" if d["percent_delta"] is None else f"{d['percent_delta']:.1f}%"
            lines.append(f"| `{d['metric']}` | {d['before']:.3f} | {d['after']:.3f} | {d['delta']:.3f} | {pct} |")
        lines.append("\n## Validity notes")
        for n in result["comparison_notes"]:
            lines.append(f"- {n}")
        (out / "compare.md").write_text("\n".join(lines)+"\n", encoding="utf-8")
        print(f"Comparison written to {out}")
        return 0
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
