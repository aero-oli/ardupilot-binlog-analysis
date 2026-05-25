---
name: ardupilot-binlog-analysis
description: Analyze ArduPilot DataFlash .bin/.log files for ArduCopter tuning, fault diagnosis, symptom-led investigation, yaw/roll/pitch/altitude issues, vibration, EKF/GPS, battery, motor/ESC behaviour, AutoTune, System ID, plot generation, and before/after comparison. Use when the user asks to inspect, diagnose, compare, graph, tune, summarize, or review ArduPilot logs. Do not use for PX4 .ulg logs unless they have been converted to ArduPilot-style tables.
---

# ArduPilot Bin Log Analysis Skill

Use this skill for ArduPilot onboard DataFlash `.bin` or `.log` analysis. Default to ArduCopter unless the log clearly identifies another vehicle type or the user asks for Plane/Rover/Sub. Generic parsing, extraction, plotting, and segmenting work across ArduPilot DataFlash logs; tuning and motor-mix diagnosis are strongest for Copter/multirotor logs.

The goal is to produce evidence-backed conclusions and plots. Use bundled scripts for deterministic parsing, metrics, plotting, FFT, comparison, and diagnosis evidence. Codex must choose the relevant investigations, inspect the generated evidence, and write the final conclusions itself.

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

For the investigation sequence, use `references/how-to-investigate.md`: validate and inventory first, run the manifest before symptom diagnosis, select a relevant time window, plot desired-vs-actual signals, then treat script findings as hypotheses to verify against timing and missing evidence.

## Analysis modes

Choose the mode from the user's request. If the user reports a symptom, symptom-led diagnosis has priority over a general review.

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

First create an investigation manifest. This is a planning artifact only; it identifies available evidence, missing evidence, suggested next commands/plots, confidence limits, and questions to answer. Do not treat it as a diagnosis or final conclusion.

```bash
python scripts/ap_log_investigation_manifest.py LOG.BIN --symptom "USER SYMPTOM" --out out/investigation.json
```

Inspect `out/investigation.json`, then run deterministic evidence gathering:

```bash
python scripts/ap_log_diagnose.py LOG.BIN --symptom "USER SYMPTOM" --out out/diagnosis.json --plots out/plots/diagnosis
```

`out/diagnosis.json` separates abnormal evidence from ordinary telemetry summaries:

- `findings`: thresholded or event-backed issues such as PID limiting, ESC errors, output saturation, EKF/GPS test-ratio failures, vibration clipping, failsafes, or altitude/rate tracking errors.
- `context`: useful ranges and summaries that exist but are not fault evidence by themselves, such as normal BAT voltage/current ranges, ESC RPM/current/temperature ranges, CTUN/BARO ranges, or ESCX duty/power ranges.
- `checked_but_not_supported`: checks that ran but did not cross the diagnostic threshold.
- `missing_required`, `missing_strongly_recommended`, and `missing_optional`: unavailable messages separated by diagnostic importance. For yaw, only `ATT` and `RATE` are required; `PIDY`, `RCOU`, and `MODE` strengthen confidence, while timeline/context messages such as `MSG`, `EV`, and `ERR` are optional evidence.

Then inspect `out/diagnosis.json`, generated plots, validation/index summaries, and any relevant extracted tables before writing conclusions. The final answer must include:

- user-reported symptom;
- likely causes ranked by confidence;
- evidence for each cause;
- causes checked but not supported;
- missing data;
- safety-critical checks before further flight;
- generated plots;
- what cannot be concluded.

### Mode 2: full health and tuning review

Use when the user asks generally to analyse, review, or summarize a log.

```bash
python scripts/ap_log_extract.py LOG.BIN --out out/tables --format csv
python scripts/ap_log_segments.py --tables out/tables --json out/segments.json --summary out/segments.md
python scripts/ap_log_metrics.py --tables out/tables --json out/metrics.json --summary out/metrics.md
python scripts/ap_log_plots.py --tables out/tables --metrics out/metrics.json --out out/plots --events
python scripts/ap_log_tuning.py --tables out/tables --out out/tuning.json --plots out/plots/tuning
python scripts/ap_log_fft.py LOG.BIN --out out/fft --json out/fft.json
```

Then inspect the JSON outputs, generated plots, extracted message tables, and relevant reference notes. Write the final health/tuning conclusions directly.

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

Use `MESSAGE.FIELD` names from the extracted CSV headers. For open-ended requests such as “plot anything” or when the requested message may not be in the default extraction set, rerun extraction with `--messages ALL` before plotting:

```bash
python scripts/ap_log_extract.py LOG.BIN --messages ALL --out out/tables --format csv
```

Use repeated `--series` arguments for multiple traces, `--secondary MESSAGE.FIELD` when units differ, and `--mode subplots` when separate stacked plots are clearer than overlaying. Simple arithmetic expressions are supported for derived traces. Use `--align-tolerance SECONDS` to prevent sparse messages from being matched across a large timestamp gap:

```bash
python scripts/ap_log_custom_plot.py --tables out/tables --series 'GPS.Alt-BARO.Alt=GPS minus baro' --align-tolerance 0.25 --events --out out/plots/gps_minus_baro.html
```

Use `--window START:END` or `--window around:CENTER:RADIUS` on metrics, plots, tuning, custom plots, extraction, and diagnosis when the user asks about a specific event or mode segment.

For symptom-led diagnosis and custom plots, prefer selector options when exact timestamps are not known:

```bash
python scripts/ap_log_diagnose.py LOG.BIN --symptom "yaw issue" --mode LOITER --out out/diagnosis.json
python scripts/ap_log_diagnose.py LOG.BIN --symptom "yaw issue" --around-msg "yaw" --around-radius 15 --out out/diagnosis.json
python scripts/ap_log_diagnose.py LOG.BIN --symptom "motor issue" --high-throttle-only --out out/diagnosis.json
```

Selectors include `--mode`, `--around-msg`, `--around-event`, `--around-error`, `--takeoff-only`, `--hover-candidates`, and `--high-throttle-only`. If a requested selector cannot be resolved from available log messages, the script should fail with a clear error rather than silently using whole-log averages.

## Symptom diagnosis fault tree

For symptom-led diagnosis, `references/symptom-diagnosis-map.yaml` is authoritative for symptom classification, diagnostic message tiers, relevant parameters, recommended plot groups, diagnostic questions, and likely fault branches. If no YAML alias confidently matches, use `general_investigation` rather than guessing. Then follow the relevant reference file:

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
- mapped output channels from `RCOU`, `RCO2`, and `RCO3`, interpreted with `SERVOx_FUNCTION` output mapping when `PARM` is present
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
- Use `RCOU`, `RCO2`, and `RCO3` for servo/motor channel output saturation or asymmetry; output-channel conclusions are higher confidence when `SERVOx_FUNCTION` mapping is available from `PARM`.
- For Copter motor-output conclusions, treat `SERVOx_FUNCTION` `33-40` as Motor1-Motor8 and `82-85` as Motor9-Motor12. Do not treat tilt functions such as `41`, `45-47`, or `75-76` as normal motor outputs.
- Use `ESC`/`ESCX`/`EDT2` only if present; do not infer ESC telemetry if missing.
- Use `VIBE` and clipping for vibration health; use raw IMU or `ISBH`/`ISBD` batch-sample data for FFT if available.
- Use `GPS` and `XKF*` for estimator/navigation issues.
- Use `BAT` and `POWR` for battery and board power issues.
- Use `RCIN` with `RCMAP_ROLL`, `RCMAP_PITCH`, `RCMAP_THROTTLE`, and `RCMAP_YAW` when present to distinguish pilot-commanded motion from autopilot/mode, estimator, mechanical, or uncommanded behaviour. If RC mapping parameters are missing, state that default channel order was assumed.
- Use `parameter_context` from manifest/diagnosis as investigation context only. Treat missing relevant parameters separately from values that are logged as zero or matching defaults, and do not turn parameter context into automatic tuning advice.
- Preserve explicit units from script JSON. If a value reports unit `unknown`, do not infer one unless the log message documentation or field context confirms it.
- Use `MODE`, `MSG`, `EV`, `ERR`, `ARM` to build the timeline.
- When data conflicts, present competing hypotheses and explain what would confirm/refute them.

## Output standard

The final answer to the user should be concise but technical. It must be written by Codex from the evidence gathered. Include links to generated artifacts if files are created. For serious issues, use this order:

1. Most likely issue.
2. Why.
3. Evidence from the log.
4. What to check first.
5. Parameters/hardware settings that matter.
6. Safety-critical status before flight.
7. What cannot be concluded.

Never bury a safety-critical finding behind tuning optimisation.
