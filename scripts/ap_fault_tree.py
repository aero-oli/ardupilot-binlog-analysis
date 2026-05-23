#!/usr/bin/env python3
from __future__ import annotations
import argparse
import json
from pathlib import Path

FAULT_TREES = {
  "yaw_misbehaviour": [
    "Confirm commanded vs uncommanded yaw using ATT.DesYaw/ATT.Yaw and RCIN yaw.",
    "Check RATE.YDes/RATE.Y and RATE.YOut for controller demand vs achieved response.",
    "Check PIDY Err/P/I/D/FF/Dmod/SRate/Flags for limiting, windup or noise protection.",
    "Check mapped RCOU/RCO2/RCO3 outputs for saturation/asymmetry and ESC telemetry for RPM/current/errors.",
    "Check XKF3/XKF4/MAG for yaw-source or magnetic innovation issues.",
    "Check VIBE/IMU/FFT and BAT/POWR for contributing vibration or power limitations."
  ],
  "attitude_rate_issue": [
    "Check ATT desired vs achieved axis.",
    "Check RATE desired vs achieved axis.",
    "Check PIDR/PIDP terms and flags.",
    "Check mapped output-channel saturation, vibration, filtering and battery sag before gain changes."
  ],
  "ekf_gps_issue": [
    "Check GPS NSats/HDop/HAcc/VAcc and dropouts.",
    "Check XKF3 innovations and XKF4 test ratios.",
    "Check mode changes/failsafes and whether symptoms occur only in position-control modes."
  ]
}

def main() -> int:
    p = argparse.ArgumentParser(description="Print the deterministic fault tree for a symptom class.")
    p.add_argument("symptom_class")
    args = p.parse_args()
    print(json.dumps({"symptom_class": args.symptom_class, "steps": FAULT_TREES.get(args.symptom_class, [])}, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
