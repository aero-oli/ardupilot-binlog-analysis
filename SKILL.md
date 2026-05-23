---
name: ardupilot-binlog-analysis
description: Analyze ArduPilot DataFlash .bin/.log files for ArduCopter tuning, fault diagnosis, symptom-led investigation, yaw/roll/pitch/altitude issues, vibration, EKF/GPS, battery, motor/ESC behaviour, AutoTune, System ID, plot generation, and before/after comparison. Use when the user asks to inspect, diagnose, compare, graph, tune, summarize, or review ArduPilot logs. Do not use for PX4 .ulg logs unless they have been converted to ArduPilot-style tables.
---

# ArduPilot Bin Log Analysis Skill

Use this skill for ArduPilot onboard DataFlash `.bin` or `.log` analysis. Default to ArduCopter unless the log clearly identifies another vehicle type or the user asks for Plane/Rover/Sub. Generic parsing, extraction, plotting, segmenting, and reports work across ArduPilot DataFlash logs; tuning and motor-mix diagnosis are strongest for Copter/multirotor logs.

The goal is to produce evidence-backed diagnostic reports and plots. Use bundled scripts for deterministic parsing, metrics, plotting, FFT, comparison, and report assembly. Use reasoning to interpret those results conservatively.

## Safety rules

- Never declare an aircraft safe to fly from a log alone.
- Put safety-critical findings first: loss of control, yaw/attitude authority loss, motor/ESC asymmetry, vibration/clipping, GPS/EKF errors, battery/power faults, failsafes, compass/yaw estimator problems, or motor output saturation.
- Separate observation, evidence, interpretation, confidence, and recommended checks.
- Do not recommend disabling arming checks, EKF checks, GPS checks, battery failsafes, compass checks, logging, or other protections as a routine fix.
- Do not recommend blind mass parameter changes.
- If recommending parameter review, specify the exact parameter names, why they matter, and bench/ground verification required before flight.
- Treat all tuning advice as aircraft-, firmware-, frame-, battery-, payload-, and prop-specific.
- If required log messages are missing, explicitly state what cannot be concluded.
- Mark every diagnosis confidence as `high`, `medium`, or `low`.
- Prefer “check/verify X” over “set X to Y” unless the evidence is strong and the change is a conservative correction.

## First action

Create an output directory, usually `out/` or `log-analysis-out/`, and run validation/indexing before interpretation.

```bash
python scripts/ap_log_validate.py LOG.BIN --json out/validate.json --summary out/validate.md
python scripts/ap_log_index.py LOG.BIN --json out/index.json --summary out/index.md
```

If the file is a telemetry `.tlog`, not a DataFlash `.bin/.log`, state that this skill is optimized for DataFlash logs and that telemetry logs may not include the same onboard messages.

## Analysis modes

Choose the mode from the user's request. If the user reports a symptom, symptom-led diagnosis has priority over a generic report.

### Mode 1: symptom-led diagnosis

Use when the user says something like:

- “yaw seems to be misbehaving”
- “it wobbles in pitch”
- “it oscillates”
- “toilet bowling”
- “motors pulsed”
- “altitude hold shot up”
- “it drifted in Loiter”
- “GPS glitch”
- “EKF error”
- “battery sag”
- “crashed”
- “loss of control”

Run:

```bash
python scripts/ap_log_diagnose.py LOG.BIN --symptom "USER SYMPTOM" --out out/diagnosis.json --plots out/plots/diagnosis
python scripts/ap_report_pack.py --index out/index.json --diagnosis out/diagnosis.json --out out/report.md
```

The report must include:

- user-reported symptom;
- likely causes ranked by confidence;
- evidence for each cause;
- causes checked but not supported;
- missing data;
- safety-critical checks before further flight;
- generated plots;
- what cannot be concluded.

### Mode 2: full health and tuning report

Use when the user asks generally to analyse, review, or summarize a log.

```bash
python scripts/ap_log_extract.py LOG.BIN --out out/tables --format csv
python scripts/ap_log_segments.py --tables out/tables --json out/segments.json --summary out/segments.md
python scripts/ap_log_metrics.py --tables out/tables --json out/metrics.json --summary out/metrics.md
python scripts/ap_log_plots.py --tables out/tables --metrics out/metrics.json --out out/plots --events
python scripts/ap_log_tuning.py --tables out/tables --out out/tuning.json --plots out/plots/tuning
python scripts/ap_log_fft.py LOG.BIN --out out/fft --json out/fft.json
python scripts/ap_report_pack.py --index out/index.json --metrics out/metrics.json --tuning out/tuning.json --fft out/fft.json --out out/report.md
```

### Mode 3: before/after comparison

Use when the user provides two or more logs or asks whether changes improved things.

```bash
python scripts/ap_log_compare.py BEFORE.BIN AFTER.BIN --out out/compare
```

For segment-specific comparison:

```bash
python scripts/ap_log_compare.py BEFORE.BIN AFTER.BIN --before-window 100:180 --after-window 95:175 --out out/compare
```

Then summarize parameter differences, metric changes, windows used, and whether the comparison is valid. Do not claim improvement if the flight segments are not comparable.

### Mode 4: graph pack only

Use when the user asks for plots/graphs.

```bash
python scripts/ap_log_extract.py LOG.BIN --out out/tables --format csv
python scripts/ap_log_plots.py --tables out/tables --out out/plots --events
```

For a specific requested graph, plot named log fields directly from extracted tables:

```bash
python scripts/ap_log_custom_plot.py --tables out/tables --series GPS.Alt --series BARO.Press --secondary BARO.Press --title "GPS altitude and barometric pressure" --out out/plots/gps_altitude_pressure.html
```

Use `MESSAGE.FIELD` names from the extracted CSV headers. Use repeated `--series` arguments for multiple traces, `--secondary MESSAGE.FIELD` when units differ, and `--mode subplots` when separate stacked plots are clearer than overlaying. Simple arithmetic expressions are supported for derived traces:

```bash
python scripts/ap_log_custom_plot.py --tables out/tables --series 'GPS.Alt-BARO.Alt=GPS minus baro' --events --out out/plots/gps_minus_baro.html
```

Use `--window START:END` or `--window around:CENTER:RADIUS` on metrics, plots, tuning, custom plots, extraction, and diagnosis when the user asks about a specific event or mode segment.

## Symptom diagnosis fault tree

For symptom-led diagnosis, classify the symptom using `references/symptom-diagnosis-map.yaml`. Then follow the relevant reference file:

- yaw or heading issue: `references/yaw-diagnosis.md`
- roll/pitch wobble or oscillation: `references/attitude-rate-diagnosis.md`
- motor/ESC issue: `references/motor-esc-diagnosis.md`
- GPS/EKF/Loiter issue: `references/ekf-gps-diagnosis.md`
- vibration/noise issue: `references/vibration-diagnosis.md`
- battery/power issue: `references/battery-power-diagnosis.md`
- crash/loss of control: `references/crash-or-loss-of-control-diagnosis.md`

For yaw complaints, specifically distinguish:

1. pilot/autopilot commanded yaw;
2. yaw controller tracking failure;
3. motor/ESC/prop/frame yaw authority limitation;
4. yaw tune oscillation;
5. compass/EKF/yaw-source issue;
6. vibration/noise corrupting control or estimation;
7. battery/throttle saturation reducing yaw authority.

Required yaw evidence sources, if present:

- `ATT.DesYaw` vs `ATT.Yaw`
- `RATE.YDes` vs `RATE.Y` and `RATE.YOut`
- `PIDY.Tar`, `PIDY.Act`, `PIDY.Err`, `PIDY.P`, `PIDY.I`, `PIDY.D`, `PIDY.FF`, `PIDY.Dmod`, `PIDY.SRate`, `PIDY.Flags`
- `RCOU.C*`, interpreted with `SERVOx_FUNCTION` output mapping when `PARM` is present
- `ESC` / `ESCX` / `EDT2` telemetry if present
- `MAG`, `XKF3`, `XKF4` if present
- `VIBE`, `IMU`, raw IMU, or `ISBH`/`ISBD` batch sampling if present
- `BAT`, `POWR`
- `RCIN` if present
- `MODE`, `MSG`, `EV`, `ERR`

## Interpretation rules

- Use `ATT` for desired-vs-achieved vehicle attitude.
- Use `RATE` for desired-vs-achieved angular rate and normalized controller outputs.
- Use `PIDR`, `PIDP`, `PIDY` for controller target, actual, error, terms, slew limiting, and flags.
- Use `RCOU` for servo/motor channel output saturation or asymmetry; output-channel conclusions are higher confidence when `SERVOx_FUNCTION` mapping is available from `PARM`.
- Use `ESC`/`ESCX`/`EDT2` only if present; do not infer ESC telemetry if missing.
- Use `VIBE` and clipping for vibration health; use raw IMU or `ISBH`/`ISBD` batch-sample data for FFT if available.
- Use `GPS` and `XKF*` for estimator/navigation issues.
- Use `BAT` and `POWR` for battery and board power issues.
- Use `MODE`, `MSG`, `EV`, `ERR`, `ARM` to build the timeline.
- When data conflicts, present competing hypotheses and explain what would confirm/refute them.

## Output standard

The final answer to the user should be concise but technical. Include links to generated artifacts if files are created. For serious issues, use this order:

1. Most likely issue.
2. Why.
3. Evidence from the log.
4. What to check first.
5. Parameters/hardware settings that matter.
6. Safety-critical status before flight.
7. What cannot be concluded.

Never bury a safety-critical finding behind tuning optimisation.
