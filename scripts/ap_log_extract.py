#!/usr/bin/env python3
from __future__ import annotations
import argparse
import gzip
import json
import sys
from collections import defaultdict
from contextlib import ExitStack
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ap_common import (
    DEFAULT_MESSAGES,
    AnalysisError,
    StreamingIndexBuilder,
    _armed_value,
    _message_iter,
    _row_in_time_window,
    ensure_dir,
    json_default,
    message_to_dict,
    message_type,
    parse_dataflash,
    parse_time_window,
    strip_private,
    time_column,
    write_json,
    write_table,
)

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

def jsonl_record(typ, row):
    _, timestamp_s = time_column(row)
    fields = strip_private(row)
    return {"message_type": typ, "timestamp_s": timestamp_s, "fields": fields}

def _open_jsonl(path, gzip_output):
    if gzip_output:
        return gzip.open(path, "wt", encoding="utf-8")
    return open(path, "w", encoding="utf-8")

def write_jsonl_stream(data_source, out, include=None, max_messages=None, start_s=None, end_s=None, armed_only=False, gzip_output=False, source=None):
    include_set = {m.strip().upper() for m in include} if include is not None else None
    out = ensure_dir(out)
    builder = StreamingIndexBuilder(source or (str(data_source) if isinstance(data_source, (str, Path)) else "stream"))
    handles = {}
    row_counts = defaultdict(int)
    total = 0
    collected = 0
    armed_state = not armed_only
    armed_filter_supported = not armed_only
    suffix = ".jsonl.gz" if gzip_output else ".jsonl"

    with ExitStack() as stack:
        for msg in _message_iter(data_source, max_messages=max_messages):
            total += 1
            typ = message_type(msg)
            row = message_to_dict(msg)
            row["_type"] = typ
            if typ == "ARM":
                arm = _armed_value(row)
                if arm is not None:
                    armed_state = arm
                    armed_filter_supported = True
            builder.add_row(typ, row)
            if include_set is not None and typ.upper() not in include_set:
                continue
            if typ in {"FMT", "FMTU"}:
                continue
            if not _row_in_time_window(row, start_s=start_s, end_s=end_s):
                continue
            if armed_only and not armed_state:
                continue
            if typ not in handles:
                handles[typ] = stack.enter_context(_open_jsonl(out / f"{typ}{suffix}", gzip_output))
            handles[typ].write(json.dumps(jsonl_record(typ, row), sort_keys=False, default=json_default) + "\n")
            row_counts[typ] += 1
            collected += 1

    stats = {
        "total_messages_read": total,
        "collected_rows": collected,
        "message_filter": sorted(include_set) if include_set is not None else None,
        "start_time_s": start_s,
        "end_time_s": end_s,
        "armed_only": armed_only,
        "armed_filter_supported": armed_filter_supported,
        "max_messages": max_messages,
        "max_messages_reached": bool(max_messages and total >= max_messages),
    }
    index = builder.to_index(stats=stats)
    return {
        typ: {"path": str(out / f"{typ}{suffix}"), "rows": row_counts[typ], "format": "jsonl", "compressed": bool(gzip_output)}
        for typ in sorted(row_counts)
    }, index, stats

def main() -> int:
    p = argparse.ArgumentParser(description="Extract ArduPilot DataFlash messages to CSV, Parquet, or JSONL tables.")
    p.add_argument("log")
    p.add_argument("--messages", default=",".join(DEFAULT_MESSAGES), help="Comma-separated message names, or ALL")
    p.add_argument("--out", default="tables")
    p.add_argument("--format", choices=["csv", "parquet", "jsonl"], default="csv")
    p.add_argument("--gzip", action="store_true", help="Compress JSONL output with gzip")
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
    if args.gzip and args.format != "jsonl":
        raise AnalysisError("--gzip is only supported with --format jsonl")
    manifest = {"log": args.log, "format_requested": args.format, "analysis_window": window, "tables": {}, "warnings": []}
    try:
        if args.format == "jsonl":
            tables, index, stats = write_jsonl_stream(
                args.log,
                out,
                include=include,
                max_messages=args.max_messages,
                start_s=window["start_s"],
                end_s=window["end_s"],
                armed_only=args.armed_only,
                gzip_output=args.gzip,
            )
            manifest["tables"] = tables
            manifest["parser_stats"] = stats
            manifest["message_index"] = index.get("messages", {})
            if args.max_messages:
                manifest["warnings"].append("Extraction used --max-messages; output tables may be partial.")
            if args.armed_only:
                manifest["warnings"].append("Extraction used --armed-only; rows before ARM state could be excluded if ARM messages were available.")
            manifest_path = args.manifest or str(out / "manifest.json")
            write_json(manifest_path, manifest)
            print(f"Extracted {len(manifest['tables'])} JSONL tables to {out}")
            return 0

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
