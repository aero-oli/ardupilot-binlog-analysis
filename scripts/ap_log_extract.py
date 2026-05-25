#!/usr/bin/env python3
from __future__ import annotations
import argparse
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ap_common import DEFAULT_MESSAGES, AnalysisError, ensure_dir, parse_dataflash, parse_time_window, time_column, write_json, write_table

def filter_rows_by_time(rows_by_type, start_s=None, end_s=None):
    if start_s is None and end_s is None:
        return rows_by_type
    out = {}
    for typ, rows in rows_by_type.items():
        kept = []
        for row in rows:
            _, t = time_column(row)
            if t is None:
                kept.append(row)
                continue
            if start_s is not None and t < start_s:
                continue
            if end_s is not None and t > end_s:
                continue
            kept.append(row)
        out[typ] = kept
    return out

def main() -> int:
    p = argparse.ArgumentParser(description="Extract ArduPilot DataFlash messages to CSV or Parquet tables.")
    p.add_argument("log")
    p.add_argument("--messages", default=",".join(DEFAULT_MESSAGES), help="Comma-separated message names, or ALL")
    p.add_argument("--out", default="tables")
    p.add_argument("--format", choices=["csv", "parquet"], default="csv")
    p.add_argument("--manifest", default=None)
    p.add_argument("--window", default=None, help="Optional TimeS window as START:END or around:CENTER:RADIUS")
    p.add_argument("--start-time", type=float, default=None, help="Optional start TimeS")
    p.add_argument("--end-time", type=float, default=None, help="Optional end TimeS")
    p.add_argument("--max-messages", type=int, default=None, help="Optional parse limit")
    p.add_argument("--armed-only", action="store_true", help="Extract rows only while ARM messages indicate armed state when available")
    args = p.parse_args()
    include = None if args.messages.strip().upper() == "ALL" else [m.strip().upper() for m in args.messages.split(",") if m.strip()]
    out = ensure_dir(args.out)
    window = parse_time_window(args.window)
    if args.start_time is not None:
        window["start_s"] = args.start_time
    if args.end_time is not None:
        window["end_s"] = args.end_time
    if window["start_s"] is not None and window["end_s"] is not None and window["end_s"] < window["start_s"]:
        raise AnalysisError("--end-time must be greater than or equal to --start-time")
    manifest = {"log": args.log, "format_requested": args.format, "analysis_window": window, "tables": {}, "warnings": []}
    try:
        rows = parse_dataflash(
            args.log,
            include=include,
            max_messages=args.max_messages,
            start_s=window["start_s"],
            end_s=window["end_s"],
            armed_only=args.armed_only,
        )
        if args.max_messages:
            manifest["warnings"].append("Extraction used --max-messages; output tables may be partial.")
        if args.armed_only:
            manifest["warnings"].append("Extraction used --armed-only; rows before ARM state could be excluded if ARM messages were available.")
        for typ, data in sorted(rows.items()):
            if not data or typ in {"FMT", "FMTU"}:
                continue
            fmt = args.format
            dest = out / f"{typ}.{fmt}"
            try:
                write_table(data, dest, fmt=fmt)
            except Exception as exc:
                if fmt == "parquet":
                    manifest["warnings"].append(f"Could not write parquet for {typ}: {exc}; wrote CSV fallback")
                    dest = out / f"{typ}.csv"
                    write_table(data, dest, fmt="csv")
                    fmt = "csv"
                else:
                    raise
            manifest["tables"][typ] = {"path": str(dest), "rows": len(data), "format": fmt}
        manifest_path = args.manifest or str(out / "manifest.json")
        write_json(manifest_path, manifest)
        print(f"Extracted {len(manifest['tables'])} tables to {out}")
        return 0
    except AnalysisError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
