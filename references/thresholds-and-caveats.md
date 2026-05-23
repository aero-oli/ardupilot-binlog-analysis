# Thresholds and caveats

These are heuristics for triage, not universal pass/fail rules.

## GPS

- HDOP below 1.5 is generally very good.
- HDOP over 2.0 can indicate poor position quality.
- Satellite count below 12 is worth investigating for Copter position-control issues.
- Always check fix type, HAcc/VAcc if present, mode, environment and whether the issue happens only in position-control modes.

## EKF

- XKF4/NKF4 test-ratio values over 1 indicate rejection by the relevant innovation gate.
- A single spike is different from sustained rejection.
- Correlate with mode changes, GPS/compass messages and actual symptom timing.

## Motor output saturation

- Outputs near conventional PWM limits such as <=1100 us or >=1900 us are heuristically suspicious.
- Actual limits depend on output protocol, ESC calibration, SERVO/MOT parameters and logging representation.
- Output-channel conclusions are high confidence only when `SERVOx_FUNCTION` mapping is available; otherwise RCOU interpretation is generic.
- Treat saturation as evidence of limited headroom only when correlated with controller error or requested output.

## Windows and segments

- Whole-log metrics are useful for triage but weaker for transient faults.
- Prefer `--window START:END`, `--window around:CENTER:RADIUS`, or a mode segment from `ap_log_segments.py` when the symptom timing is known.

## Vibration

- VIBE values above roughly 30 m/s/s or any clipping increase are worth investigating.
- Logs may expose clipping as `Clip` or as per-IMU `Clip0`, `Clip1`, `Clip2` fields; inspect whichever fields are present.
- VIBE alone does not identify frequency; use raw IMU/batch sampling/FFT for resonance and notch-filter diagnosis.

## PID flags

- PID*.Flags LIMIT indicates output saturation / I-term anti-windup active.
- Dmod reduction suggests protective dynamic D-term scaling; investigate noise/filtering before increasing D.

## Battery/power

- Low voltage or sag must be interpreted relative to cell count, battery chemistry, current, capacity, age and calibration.
- Board VCC ripple/brownout evidence needs POWR plus flight context.
