---
name: ardupilot-binlog-analysis
description: Investigate ArduPilot DataFlash .bin/.log flight logs. Use for Copter log diagnosis, tuning review, symptom-led fault analysis, plots, vibration/FFT, EKF/GPS, power, motor/ESC, AutoTune, System ID, and before/after comparison. Not for PX4 .ulg logs unless converted.
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

If dependency or import failures occur, run the skill doctor before starting or retrying a long workflow:

```bash
python scripts/ap_skill_doctor.py
```

Create an output directory, usually `out/` or `log-analysis-out/`, and run validation/indexing before interpretation.

```bash
python scripts/ap_log_validate.py LOG.BIN --json out/validate.json --summary out/validate.md
python scripts/ap_log_index.py LOG.BIN --json out/index.json --summary out/index.md
```

If the file is a telemetry `.tlog`, not a DataFlash `.bin/.log`, state that this skill is optimized for DataFlash logs and that telemetry logs may not include the same onboard messages.

For the investigation sequence, use `references/how-to-investigate.md`: validate and inventory first, run the manifest before symptom diagnosis, select a relevant time window, plot desired-vs-actual signals, then treat script findings as hypotheses to verify against timing and missing evidence. If validation, indexing, or the manifest shows missing required/strongly recommended messages, use `references/logging-configuration-for-investigation.md` to explain what should be logged and `references/evidence-gathering-flights.md` to decide whether the next evidence should be a parameter dump, bench inspection, ground test, restrained test, or controlled flight.

If validation or indexing reports parse errors, bad-byte skips, logging dropouts, missing timebase, missing core messages after arm, no `FMT`, no `PARM`, or likely truncated/partial data, consult `references/corrupt-or-incomplete-log.md` before writing conclusions. Treat missing evidence in damaged logs as a confidence limit, not proof that a fault was absent.

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
- “would not arm”
- “radio failsafe”
- “crashed”
- “loss of control”

For multi-log cases or complex multi-symptom reports, prefer the case-level investigation runner first. It orchestrates validation, indexing, manifest generation, primary and secondary diagnoses, mode comparison, parameter lookup, FFT where relevant, and next-step planning into a structured evidence pack for the agent. It does not write the final user-facing diagnosis.

```bash
python scripts/ap_case_investigate.py --logs LOG1.BIN LOG2.BIN --symptom "USER SYMPTOM" --out out/case
```

Inspect `out/case/case_manifest.json`, `out/case/case_summary.md`, `out/case/recommended_agent_reading_order.md`, and the per-log artifacts under `out/case/logs/`. Treat failures recorded in the case manifest as confidence limits, then write the final answer yourself from the evidence.

First create an investigation manifest. This is a planning artifact only; it identifies available evidence, missing evidence, suggested next commands/plots, confidence limits, and questions to answer. Do not treat it as a diagnosis or final conclusion.

```bash
python scripts/ap_log_investigation_manifest.py LOG.BIN --symptom "USER SYMPTOM" --out out/investigation.json
```

Inspect `primary_symptom_class`, `secondary_symptom_classes`, `multi_symptom_reasoning`, and `recommended_secondary_commands`. If secondary classes are present, run the relevant secondary diagnosis, mode-comparison, or plot workflows before forming conclusions; do not draw conclusions from the primary branch alone.

Inspect `out/investigation.json`, then run deterministic evidence gathering:

```bash
python scripts/ap_log_diagnose.py LOG.BIN --symptom "USER SYMPTOM" --out out/diagnosis.json --plots out/plots/diagnosis
```

`out/diagnosis.json` separates abnormal evidence from ordinary telemetry summaries:

- `findings`: thresholded or event-backed issues such as PID limiting, ESC errors, output saturation, EKF/GPS test-ratio failures, vibration clipping, failsafes, or altitude/rate tracking errors.
- `context`: useful ranges and summaries that exist but are not fault evidence by themselves, such as normal BAT voltage/current ranges, ESC RPM/current/temperature ranges, CTUN/BARO ranges, or ESCX duty/power ranges.
- `checked_but_not_supported`: checks that ran but did not cross the diagnostic threshold.
- `missing_required`, `missing_strongly_recommended`, and `missing_optional`: unavailable messages separated by diagnostic importance. For yaw, only `ATT` and `RATE` are required; `PIDY`, `RCOU`, and `MODE` strengthen confidence, while timeline/context messages such as `MSG`, `EV`, and `ERR` are optional evidence.
- `next_evidence_gathering`: structured planning guidance for what to collect next when evidence is missing. Read this before recommending a parameter review, bench check, ground test, restrained test, controlled flight, or no-fly-until-checked path. It is a safety planning aid, not a diagnosis.
- `flight_status` and `recommended_next_steps`: structured planning aids for the final user answer. Read them, verify they match the findings and missing evidence, and surface the relevant ordered next steps in your own words. Do not treat them as an automatically generated final answer.

For safety-relevant cases, after diagnosis and any mode comparison, create an explicit planning aid from the available JSON outputs:

```bash
python scripts/ap_next_steps.py --diagnosis out/diagnosis.json --mode-compare out/mode_compare.json --param-lookup out/param_lookup.json --fft out/fft.json --manifest out/investigation.json --json out/next_steps.json --summary out/next_steps.md
```

All inputs are optional; pass whichever outputs exist. Inspect `out/next_steps.json` and `out/next_steps.md`, then write the final answer yourself from the evidence. Do not treat the planner summary as a final diagnosis.

When required or strongly recommended evidence is missing, load `references/logging-configuration-for-investigation.md` and `references/evidence-gathering-flights.md` before writing the missing-data section. Explain the confidence limit and, when appropriate, give conservative guidance for a future diagnostic capture. Do not turn missing evidence into automatic parameter changes or a recommendation to repeat unsafe flight.

Then inspect `out/diagnosis.json`, generated plots, validation/index summaries, and any relevant extracted tables before writing conclusions. The final answer must include:

- user-reported symptom;
- likely causes ranked by confidence;
- evidence for each cause;
- causes checked but not supported;
- missing data;
- safety-critical checks before further flight;
- generated plots;
- what cannot be concluded.

## When evidence is missing

Inspect `missing_required`, `missing_strongly_recommended`, `missing_optional`, `what_cannot_be_concluded`, and `next_evidence_gathering` in the investigation manifest before advising next steps.

```bash
python scripts/ap_log_investigation_manifest.py LOG.BIN --symptom "USER SYMPTOM" --out out/investigation.json
```

Use `references/evidence-gathering-flights.md` and `references/logging-configuration-for-investigation.md` to recommend the safest next evidence source. Do not automatically recommend another flight:

- suspected motor/ESC/power failure: bench inspection and ground checks first;
- crash/loss-of-control: no repeat flight until hardware and setup checks are complete;
- pre-arm/boot issue: recommend `LOG_DISARMED`/boot logging where appropriate, not flight;
- vibration/filter evidence missing: controlled short capture only when the aircraft is otherwise stable; warn about large logs and dropouts;
- EKF/GPS issue: compare modes only if manual control is stable.

High-volume logging for raw IMU, batch sampling, FFT, or disarmed boot evidence should be targeted, checked for logging dropouts after capture, and normally disabled again after the diagnostic test.

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

For mission-vs-manual complaints, especially yaw or wobble that appears in AUTO/mission but not in manual or positioning modes, run the mode comparison aid before drawing conclusions:

```bash
python scripts/ap_log_mode_compare.py LOG.BIN --symptom yaw_misbehaviour --compare-modes AUTO,POSHOLD,ALTHOLD,STABILIZE --active-flight-only --json out/mode_compare.json --plots out/plots/mode_compare
```

Treat mode comparison as scoping evidence. Inspect the intervals, active-flight criteria, missing evidence, and confidence limits before deciding whether AUTO behaviour reflects mission yaw demand, estimator/navigation context, or a control/authority issue.

Selectors include `--mode`, `--around-msg`, `--around-event`, `--around-error`, `--takeoff-only`, `--hover-candidates`, and `--high-throttle-only`. For diagnosis and custom plots, `--mode` uses all matching mode intervals and excludes intervening non-matching gaps; inspect `analysis_window.intervals_found`, `analysis_window.intervals_used`, and `analysis_window.non_matching_gaps_excluded` when reporting scope. If a requested selector cannot be resolved from available log messages, the script should fail with a clear error rather than silently using whole-log averages.

`--hover-candidates` uses a conservative duration-based CTUN hover heuristic. Tune it with `--hover-min-duration`, `--hover-alt-span-max`, `--hover-throttle-min`, and `--hover-throttle-max`; report the selected `analysis_window.criteria` and candidate intervals when using it.

## Symptom diagnosis fault tree

For symptom-led diagnosis, `references/symptom-diagnosis-map.yaml` is authoritative for symptom classification, diagnostic message tiers, relevant parameters, recommended plot groups, diagnostic questions, and likely fault branches. If no YAML alias confidently matches, use `general_investigation` rather than guessing. Then follow the relevant reference file:

- yaw or heading issue: `references/yaw-diagnosis.md`
- roll/pitch wobble or oscillation: `references/attitude-rate-diagnosis.md`
- motor/ESC issue: `references/motor-esc-diagnosis.md`
- GPS/EKF/Loiter issue: `references/ekf-gps-diagnosis.md`
- compass/yaw-source issue: `references/compass-yaw-source-diagnosis.md`
- barometer/rangefinder altitude issue: `references/baro-rangefinder-altitude-diagnosis.md`
- vibration/noise issue: `references/vibration-diagnosis.md`
- battery/power issue: `references/battery-power-diagnosis.md`
- RC/failsafe/pre-arm/arming issue: `references/rc-failsafe-prearm-diagnosis.md`
- crash/loss of control: `references/crash-or-loss-of-control-diagnosis.md`
- missing log evidence or future capture setup: `references/logging-configuration-for-investigation.md`
- safe next evidence-gathering activity: `references/evidence-gathering-flights.md`

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
- If the log lacks `PARM`, ask for a Mission Planner/QGC/MAVProxy `.param` file or parameter dump and pass it with `--params vehicle.param` to manifest, diagnosis, lookup, or metrics tools. Treat external parameters as configuration context, not proof of in-flight values unless the export timestamp/version is known to match the flight. If external parameters conflict with logged `PARM`, logged `PARM` remains primary and the conflict must be reported.
- Use compact parameter metadata only to explain logged parameter context, ranges, units, enums, bitmasks, and logging settings. Metadata may not match the exact firmware in the log and latest-source metadata may include unreleased parameters. For ad hoc explanation run `python scripts/ap_param_lookup.py --index out/index.json --symptom yaw_misbehaviour --json out/param_lookup.json` or `python scripts/ap_param_lookup.py --index out/index.json --names WP_YAW_BEHAVIOR,ATC_RATE_Y_MAX`. To refresh the local compact cache from ArduPilot's machine-readable parameter metadata, run `python scripts/update_parameter_metadata.py --fetch --vehicle ArduCopter` or add `--refresh-metadata` to `ap_param_lookup.py`. Do not recommend parameter changes automatically from metadata.
- Treat `logging_health.confirmed_dropouts` as confidence-limiting logging evidence. Treat `logging_health.possible_dropouts` as context to inspect unless other logging-health fields also limit confidence.
- If core evidence is absent, use `references/logging-configuration-for-investigation.md` to explain future logging setup and `references/evidence-gathering-flights.md` to choose the safest next capture activity. Keep the advice flight-safe: high-volume raw IMU, batch sampling, FFT, or disarmed logging should be targeted, checked for dropouts after capture, and normally disabled again afterward.
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

For safety-relevant findings, the final answer must include a clear `Recommended next steps` section. Keep it ordered:

1. `Immediate safety gate`: choose the conservative gate that fits the evidence, such as normal analysis only, no AUTO/mission flying, controlled hover only, ground test only, bench only, or do not fly until checked.
2. `Bench/hardware checks`.
3. `Configuration/logging checks`.
4. `Controlled evidence-gathering activity, only if safe`.
5. `Reanalysis step`.
6. `What not to do`.

Do not stop at missing evidence limits confidence. If evidence is missing, state what specific evidence should be collected next, how to collect it safely, and what must be checked first.

For reusable wording patterns and a mission/yaw/wobble example, read `references/final-answer-patterns.md`. Keep SKILL.md as investigation guidance: gather evidence, inspect outputs, and write the final answer yourself; do not add automatic report generation.

Never bury a safety-critical finding behind tuning optimisation.
