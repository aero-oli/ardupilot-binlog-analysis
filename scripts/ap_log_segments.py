#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ap_common import AnalysisError, load_tables, mode_segments_from_tables, write_json
from ap_modes import mode_decoding_note


def main() -> int:
    p = argparse.ArgumentParser(description="Derive flight mode segments from extracted ArduPilot log tables.")
    p.add_argument("--tables", required=True, help="Directory produced by ap_log_extract.py")
    p.add_argument("--json", default="segments.json")
    p.add_argument("--summary", default=None)
    args = p.parse_args()
    try:
        tables = load_tables(args.tables)
        log_end_s = None
        durations = []
        for df in tables.values():
            if df is not None and "TimeS" in df.columns and len(df["TimeS"].dropna()) > 0:
                durations.append(float(df["TimeS"].dropna().max()))
        if durations:
            log_end_s = max(durations)
        segments = mode_segments_from_tables(tables, log_end_s=log_end_s)
        result = {"tables": args.tables, "mode_decoding": mode_decoding_note(), "segments": segments}
        write_json(args.json, result)
        if args.summary:
            lines = ["# ArduPilot mode segments\n", f"- {mode_decoding_note()}\n", "| Raw mode | Decoded mode | Start s | End s | Duration s |", "|---|---|---:|---:|---:|"]
            for seg in segments:
                end = "" if seg.get("end_s") is None else f"{seg['end_s']:.3f}"
                dur = "" if seg.get("duration_s") is None else f"{seg['duration_s']:.3f}"
                lines.append(f"| {seg.get('raw_mode')} | {seg.get('decoded_mode', seg.get('mode'))} | {seg.get('start_s'):.3f} | {end} | {dur} |")
            Path(args.summary).parent.mkdir(parents=True, exist_ok=True)
            Path(args.summary).write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"Derived {len(segments)} mode segments")
        return 0
    except AnalysisError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
