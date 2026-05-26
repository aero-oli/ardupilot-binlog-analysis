#!/usr/bin/env python3
from __future__ import annotations
import argparse
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ap_common import AnalysisError, collect_dataflash, message_inventory_markdown, write_json
from ap_err_decode import decode_err_entries

def main() -> int:
    p = argparse.ArgumentParser(description="Index an ArduPilot DataFlash log: messages, fields, parameters, events, errors, modes.")
    p.add_argument("log", help="ArduPilot DataFlash .bin/.log file")
    p.add_argument("--json", default="index.json", help="Output JSON path")
    p.add_argument("--summary", default=None, help="Optional markdown summary path")
    p.add_argument("--max-messages", type=int, default=None, help="Optional parse limit for quick inspection")
    p.add_argument("--messages", default=None, help="Optional comma-separated message names to retain as rows while still indexing all messages")
    args = p.parse_args()
    try:
        include = [m.strip().upper() for m in args.messages.split(",") if m.strip()] if args.messages else []
        _rows, index, _stats = collect_dataflash(args.log, include=include, max_messages=args.max_messages)
        index["decoded_errors"] = decode_err_entries(index.get("errors", []))
        write_json(args.json, index)
        if args.summary:
            Path(args.summary).parent.mkdir(parents=True, exist_ok=True)
            Path(args.summary).write_text(message_inventory_markdown(index), encoding="utf-8")
        print(f"Indexed {args.log}: {len(index['messages'])} message types, {index['parameter_count']} params")
        return 0
    except AnalysisError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
