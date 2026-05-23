#!/usr/bin/env python3
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ap_common import classify_symptom

def main() -> int:
    p = argparse.ArgumentParser(description="Classify a user symptom into an ArduPilot diagnostic workflow.")
    p.add_argument("symptom")
    args = p.parse_args()
    print(json.dumps({"symptom": args.symptom, "class": classify_symptom(args.symptom)}, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
