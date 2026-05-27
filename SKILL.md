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

When logs contain `ERR` rows, use the local ERR decoder and `references/err-subsys-ecode.md` before web searching for common `ERR.Subsys`/`ERR.ECode` context:

```bash
python scripts/ap_err_decode.py --index out/index.json --json out/err_decode.json
```

If the decoder confidence is `unknown`, state that the local mapping does not identify the code and avoid assigning a specific cause without firmware-specific evidence.

## Analysis modes

Choose the mode from the user's request. If the user reports a symptom, symptom-led diagnosis has priority over a general review.

### Mode 0: Methodic Configurator tuning step review

Use when the user names an ArduPilot Methodic Configurator step, asks whether a Methodic step passed, asks about Methodic Configurator tuning, or asks for step-aware tuning workflow guidance.

Trigger examples:

- "I am on Methodic step 7.1.1"
- "Check motor output oscillation"
- "Can I proceed to the notch step?"
- "Review this AutoTune log"
- "Review this QuikTune log"
- "Check if this System ID flight is usable"

First read `references/methodic-configurator-workflows.md`, `references/methodic-step-registry.yaml`, and, when writing the final answer, `references/methodic-output-patterns.md`. The official guide source for the registry is `https://ardupilot.github.io/MethodicConfigurator/TUNING_GUIDE_ArduCopter`.

Workflow:

1. Run validation and indexing first:

   ```bash
   python scripts/ap_log_validate.py LOG.BIN --json out/validate.json --summary out/validate.md
   python scripts/ap_log_index.py LOG.BIN --json out/index.json --summary out/index.md
   ```

2. Run the Methodic step entrypoint:

   ```bash
   python scripts/ap_methodic_step.py LOG.BIN --step STEP --out out/methodic_STEP.json --summary out/methodic_STEP.md --plots out/plots/methodic_STEP
   ```

3. Inspect the JSON result, safety gate, evidence used, missing evidence, plots, recommended next steps, and `what_not_to_do`.
4. Treat conditional, failed, and inconclusive safety gates as blockers or caveated gates. Do not skip them without user confirmation and a documented safety rationale.
5. Write the final conclusion yourself. The scripts gather evidence; they do not produce final tuning conclusions or change parameters.

Example:

```bash
python scripts/ap_methodic_step.py LOG.BIN --step 7.1.1 --out out/methodic_7_1_1.json --summary out/methodic_7_1_1.md --plots out/plots/methodic_7_1_1
```

For Methodic answers, the final response must include:

1. Methodic step result.
2. Can proceed?
3. Why.
4. Evidence.
5. Missing evidence.
6. Before proceeding.
7. Next Methodic step/file.
8. What not to do.

For Methodic 7.1.1 specifically, the dispatcher uses the dedicated motor-output oscillation evidence tool. You may also run it directly when iterating on evidence collection:

```bash
python scripts/ap_methodic_711_motor_oscillation.py LOG.BIN --out out/methodic_711.json --summary out/methodic_711.md --plots out/plots/methodic_711
```

This tool gathers hover-window, RC-centered RATE output, PID term, mapped motor output, vibration, and ESC telemetry evidence. It classifies the step conservatively and never changes gains.

For Methodic 8.1 harmonic notch / filter review, the dispatcher uses the dedicated notch evidence tool. You may also run it directly:

```bash
python scripts/ap_methodic_notch_review.py LOG.BIN --out out/methodic_8_1.json --summary out/methodic_8_1.md --plots out/plots/methodic_8_1
```

This tool gathers VIBE/clipping, raw/high-rate IMU or ISBH/ISBD FFT readiness, dominant peaks, PID Dmod/Flags, ESC/RPM evidence, logging health, and current notch parameter context. It gives safe next actions and must not be treated as an automatic notch-parameter setter.

For Methodic 8.2 throttle-controller review, the dispatcher uses the dedicated throttle-controller evidence tool. You may also run it directly:

```bash
python scripts/ap_methodic_throttle_controller.py LOG.BIN --out out/methodic_8_2.json --summary out/methodic_8_2.md --plots out/plots/methodic_8_2
```

This tool gathers hover-window quality, `CTUN.ThO`/`CTUN.ThH`, `MOT_THST_HOVER`, altitude target/actual evidence, motor headroom, battery/board power, and vibration evidence. It frames MOT/PSC parameter changes only as review candidates; it must not write parameters or tune from a poor hover.

For Methodic 8.3 PID notch / frame resonance review, the dispatcher uses the dedicated optional advanced evidence tool. You may also run it directly:

```bash
python scripts/ap_methodic_pid_notch_review.py LOG.BIN --out out/methodic_8_3.json --summary out/methodic_8_3.md --plots out/plots/methodic_8_3
```

This tool checks whether PID notch review is `not_needed`, a `candidate`, `unsafe_to_attempt`, or `inconclusive`. It requires the agent to inspect the evidence before any conclusion and must not generate final FILTn/ATC_RAT notch parameter changes.

For Methodic 8.4 EKF altitude-source review, the dispatcher uses the dedicated altitude-source evidence tool. You may also run it directly:

```bash
python scripts/ap_methodic_ekf_altitude_source.py LOG.BIN --out out/methodic_8_4.json --summary out/methodic_8_4.md --plots out/plots/methodic_8_4
```

This tool reviews `CTUN.DAlt`/`Alt`, `BARO`, GPS/GPA altitude context, optional `RNGF`, EKF height test ratios/innovations, vibration, power, and mode/event context. It must not change EKF height-source parameters automatically.

For Methodic 8.5 QuikTune/manual PID review and Methodic 9.2 QuikTune standard setup/results, the dispatcher uses the dedicated QuikTune/manual tuning evidence tool. You may also run it directly:

```bash
python scripts/ap_methodic_quicktune_review.py LOG.BIN --before-params before.param --after-params after.param --out out/methodic_8_5.json --summary out/methodic_8_5.md --plots out/plots/methodic_8_5
```

This tool reviews QuikTune/script messages, before/after or log PARM tuning changes, ATT/RATE tracking, PID terms, RATE output oscillation, RC contamination, vibration, motor outputs, and battery/power evidence. It must not write gains, auto-accept QuikTune output, or recommend gain increases when vibration, noise, saturation, or unstable tracking is present.

For Methodic 9.1 MagFit review, the dispatcher uses the dedicated MagFit evidence tool. You may also run it directly:

```bash
python scripts/ap_methodic_magfit_review.py LOG.BIN --out out/methodic_9_1.json --summary out/methodic_9_1.md --plots out/plots/methodic_9_1
```

This tool reviews MAG field evidence, heading diversity, MagFit flight-profile suitability, MAG timing correlation with current/throttle, EKF yaw/mag test ratios, GPS/yaw-source context, and compass/EKF warnings. It must not write compass offsets, compass orientation, motor-compensation, or EKF yaw-source parameters automatically.

For Methodic 9.3 tune evaluation with feed-forward disabled, 9.4 tune evaluation with feed-forward enabled, and 9.6 performance evaluation, the dispatcher uses the dedicated post-tune evidence tool. You may also run it directly:

```bash
python scripts/ap_methodic_tune_eval.py LOG.BIN --step 9.3 --out out/methodic_9_3.json --summary out/methodic_9_3.md --plots out/plots/methodic_9_3
python scripts/ap_methodic_tune_eval.py BEFORE.BIN AFTER.BIN --compare --out out/tune_compare.json
```

This tool reviews isolated-axis input quality, ATT/RATE tracking, RATE output demand, PID terms/flags/Dmod, RC contamination/coupling, mapped motor output saturation, vibration/clipping, battery/board power, and before/after comparability. It must not write gains, claim improvement from non-comparable logs, or recommend gain increases when vibration, clipping, saturation, or power issues are present.

For Methodic 9.5 AutoTune review, the dispatcher uses the dedicated AutoTune evidence tool. You may also run it directly:

```bash
python scripts/ap_methodic_autotune_review.py LOG.BIN --out out/methodic_9_5.json --summary out/methodic_9_5.md --plots out/plots/methodic_9_5
```

This tool reviews `ATUN`/`ATDE`, tuned axes, completion/save status, AutoTune-relevant parameter changes, prerequisite evidence, post-AutoTune tracking, poor-solution indicators, motor output headroom, vibration/clipping, battery state, and mode/message context. It must not auto-apply gains or recommend AutoTune unless initial tune, filters, vibration, actuator headroom, and control stability are acceptable.

For Methodic 9.7 derivative feed-forward calculation review, the dispatcher uses the dedicated D_FF evidence tool. You may also run it directly:

```bash
python scripts/ap_methodic_dff_calc.py LOG.BIN --axis roll,pitch,yaw --out out/methodic_9_7.json --summary out/methodic_9_7.md --plots out/plots/methodic_9_7
```

This tool checks clean isolated manoeuvres, angular acceleration, RC axis isolation, RATE output response, actuator saturation, vibration/clipping, RATE sample rate, and current `ATC_RAT_*_D_FF` values. It may produce candidate values only when evidence is strong enough; it must not write D_FF parameters, and any externally applied candidate requires validation with a fresh log.

For Methodic 10.1 wind estimation / drag coefficients review, the dispatcher can classify log evidence, but candidate coefficients require running the dedicated tool directly with vehicle metadata:

```bash
python scripts/ap_methodic_wind_drag_review.py LOG.BIN --mass-kg 12.5 --frontal-area-m2 0.35 --side-area-m2 0.32 --out out/methodic_10_1.json --summary out/methodic_10_1.md --plots out/plots/methodic_10_1
```

This tool reviews GPS speed, IMU/ACC acceleration, attitude/rate context, EKF/NKF wind or consistency evidence, wind variability, required mass/area metadata, and candidate `EK3_DRAG_BCOEF_X`, `EK3_DRAG_BCOEF_Y`, and `EK3_DRAG_MCOEF` context. It must not auto-set EKF drag parameters or claim coefficient accuracy in variable wind; any externally applied parameter change requires validation.

For Methodic 10.2 barometer compensation review, the dispatcher uses the dedicated baro compensation evidence tool. You may also run it directly:

```bash
python scripts/ap_methodic_baro_comp_review.py LOG.BIN --out out/methodic_10_2.json --summary out/methodic_10_2.md --plots out/plots/methodic_10_2
```

This tool reviews `BARO` altitude/pressure, `CTUN.DAlt`/`Alt`, GPS altitude/speed, EKF height test ratios/innovations, forward-flight/wind-exposure segment quality, vibration/power correlation, and optional rangefinder effects. It must not infer compensation from hover-only data or auto-change baro compensation parameters.

For Methodic 11.1 System ID flight review, the dispatcher uses the dedicated System ID data-quality evidence tool. You may also run it directly:

```bash
python scripts/ap_methodic_sysid_review.py LOG.BIN --out out/methodic_11_1.json --summary out/methodic_11_1.md --plots out/plots/methodic_11_1
```

This tool reviews `SID`/`SIDD` excitation, `SIDS`/`SID_AXIS` axis context, RATE response, actuator saturation, vibration/noise, battery/power, mode context, and logging health. It may classify data as ready for model review, repeat-needed, unusable, or inconclusive; it must not generate final PID values, claim no-wind conditions from logs alone, or auto-apply analytical model results.

For Methodic 11.2 analytical PID optimisation review, run the dedicated artifact-review tool directly because it needs a System ID review JSON and a proposed parameter file:

```bash
python scripts/ap_methodic_analytical_pid_review.py --sysid out/methodic_11_1.json --proposed-params proposed.param --before-log BEFORE.BIN --after-log AFTER.BIN --out out/methodic_11_2.json --summary out/methodic_11_2.md
```

This tool checks whether the System ID input was model-ready, whether proposed `ATC_RAT_*`, `ATC_ANG_*`, and `ATC_ACCEL_*` changes are bounded, whether rollback values are available, and whether an optional validation log shows oscillation, saturation, vibration, or clipping. It must not apply parameters, generate final PID values, or accept analytical outputs without a controlled validation plan.

For Methodic 12.1 position-controller tuning review, the dispatcher uses the dedicated outer-loop evidence tool. You may also run it directly:

```bash
python scripts/ap_methodic_position_controller_review.py LOG.BIN --out out/methodic_12_1.json --summary out/methodic_12_1.md --plots out/plots/methodic_12_1
```

This tool reviews Loiter/PosHold or other position-control evidence only after checking GPS/EKF confidence and inner attitude/rate loop prerequisites. It inspects `GPS`/`GPA`, `XKF*`/`NKF*`, `ATT`/`RATE`, `CTUN`, `RCIN`, `MODE`, `VIBE`, `BAT`/`POWR`, `PARM`, and optional `POS`/`NTUN`/`PSC`/`RNGF` messages. It must not tune outer loops when inner loops or GPS/EKF are poor, and it must not auto-change `PSC_*`, `LOIT_*`, or `WPNAV_*` parameters.

For Methodic 12.2 Guided-operation review, the dispatcher uses the dedicated Guided evidence tool. You may also run it directly:

```bash
python scripts/ap_methodic_guided_operation_review.py LOG.BIN --out out/methodic_12_2.json --summary out/methodic_12_2.md --plots out/plots/methodic_12_2
```

This tool reviews whether Guided mode was present, position/velocity/altitude tracking, failsafe/error context, RC override or pilot intervention, companion/GCS command context when logged, GPS/EKF quality, and power/vibration confounders. It may classify evidence as ready for further Guided checks, not ready, inconclusive, or not applicable, but it must not certify Guided operation as safe or operationally ready.

For Methodic 12.3 precision-landing review, the dispatcher uses the dedicated precision-land evidence tool. You may also run it directly:

```bash
python scripts/ap_methodic_precision_land_review.py LOG.BIN --out out/methodic_12_3.json --summary out/methodic_12_3.md --plots out/plots/methodic_12_3
```

This tool reviews precision-landing target messages where present, rangefinder health, descent profile, landing/mode timeline, failsafe/error context, RC intervention, GPS/EKF quality, and power/vibration confounders. It may classify evidence as ready for further controlled precision-land tests, sensor review needed, fail, inconclusive, or not applicable; it must not certify precision landing as operationally safe from one log.

For Methodic 13 productive configuration, run the dedicated final audit tool directly because it needs an index, final parameter file, and Methodic progress record:

```bash
python scripts/ap_methodic_productive_config_check.py --index out/index.json --params vehicle.param --methodic-progress out/methodic_progress.json --out out/methodic_13.json --summary out/methodic_13.md
```

This tool checks diagnostic logging cleanup, normal logging adequacy, `ARMING_CHECK`, battery/RC/GCS/EKF/geofence failsafes, battery monitor setup, notch/filter validation, prior Methodic progress, mode/RC mapping context, compass/GPS/yaw-source caveats, and conflicts between logged/index parameters and the external final `.param` file. It may say `ready_for_operational_checks`, `not_ready`, or `inconclusive`; it must never provide a flight-safety signoff.

For Methodic multi-step tuning work, use the progress helper to combine step JSON outputs into a single blocker/next-step view:

```bash
python scripts/ap_methodic_progress.py out/methodic_7_1.json out/methodic_7_1_1.json out/methodic_8_1.json --out out/methodic_progress.json --summary out/methodic_progress.md
```

For before/after Methodic step comparison, use the compare helper:

```bash
python scripts/ap_methodic_compare.py BEFORE.BIN AFTER.BIN --step 8.1 --out out/methodic_compare_8_1.json --summary out/methodic_compare_8_1.md --plots out/plots/methodic_compare_8_1
```

These helpers organize evidence and comparability limits only. They must not generate final reports automatically, and failed or inconclusive Methodic steps must not be skipped without user confirmation and a documented safety rationale.

If the user gives required observations such as motor/ESC heat, audible oscillation, visible shaking, or hard-to-control behaviour, pass them as repeated `--manual-observation` values. If those observations are not available, preserve that as missing evidence; do not promote the step to a clean pass from log evidence alone.

`ap_methodic_step.py` returns a standard schema with `result`, `safety_gate`, `evidence_used`, `missing_evidence`, `manual_observations_required`, `findings`, `parameter_context`, `plots`, `recommended_next_steps`, `what_not_to_do`, `next_methodic_step`, and `confidence_limits`. Treat the step result as structured evidence, not final truth. Inspect the JSON, summary, and plots before writing conclusions yourself.

Every Methodic final answer must include clear next steps:

1. Methodic step and result.
2. Can proceed? Use the safety gate to answer proceed, proceed with caution, repeat, bench check, or do not proceed.
3. Why.
4. Evidence used.
5. Missing evidence and manual observations still required.
6. Before proceeding.
7. Next Methodic step/file only if the evidence supports it.
8. What not to do.

This mode does not auto-tune, upload parameters, or recommend blind gain changes. Never skip Methodic safety gates and never declare the aircraft safe to fly.

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
- `control_evidence_completeness`: standard completeness status for tracking, PID, actuator, ESC, RC input, vibration, FFT, GPS/EKF, and parameter context. Inspect this before ranking causes or deciding whether tuning/controller conclusions are supportable.
- `events_relative_to_window`: `MSG`, `EV`, `ERR`, `ARM`, and `MODE` entries grouped as before-window, inside-window, and after-window. Use this to separate symptom-window evidence from post-flight or disarmed safety context.
- `recommended_user_artifacts`: concise shortlist of plots/files worth linking in the final answer, with reasons. Use it as a starting point, not a requirement to link every generated file.
- `next_evidence_gathering`: structured planning guidance for what to collect next when evidence is missing. Read this before recommending a parameter review, bench check, ground test, restrained test, controlled flight, or no-fly-until-checked path. It is a safety planning aid, not a diagnosis.
- `flight_status` and `recommended_next_steps`: structured planning aids for the final user answer. Read them, verify they match the findings and missing evidence, and surface the relevant ordered next steps in your own words. Do not treat them as an automatically generated final answer.

For safety-relevant cases, after diagnosis and any mode comparison, create an explicit planning aid from the available JSON outputs:

```bash
python scripts/ap_next_steps.py --diagnosis out/diagnosis.json --mode-compare out/mode_compare.json --param-lookup out/param_lookup.json --fft out/fft.json --manifest out/investigation.json --json out/next_steps.json --summary out/next_steps.md
```

All inputs are optional; pass whichever outputs exist. Inspect `out/next_steps.json` and `out/next_steps.md`, then write the final answer yourself from the evidence. Do not treat the planner summary as a final diagnosis.

For complex, multi-symptom, or multi-log cases with several JSON outputs, build a concise evidence digest before writing the final answer:

```bash
python scripts/ap_evidence_digest.py --diagnosis out/diagnosis.json --mode-compare out/mode_compare.json --param-lookup out/param_lookup.json --fft out/fft.json --manifest out/investigation.json --next-steps out/next_steps.json --json out/evidence_digest.json --summary out/evidence_digest.md
```

Inspect the digest for strongest supported observations, unsupported checks, missing evidence, confidence limits, timeline/failsafe context, and recommended next steps. Treat it as an agent reading aid, not a final answer or report generator.

When required or strongly recommended evidence is missing, load `references/logging-configuration-for-investigation.md` and `references/evidence-gathering-flights.md` before writing the missing-data section. Explain the confidence limit and, when appropriate, give conservative guidance for a future diagnostic capture. Do not turn missing evidence into automatic parameter changes or a recommendation to repeat unsafe flight.

Then inspect `out/diagnosis.json`, generated plots, validation/index summaries, and any relevant extracted tables before writing conclusions. The final answer must include:

- user-reported symptom;
- likely causes ranked by confidence;
- evidence for each cause;
- control evidence completeness and confidence limits when logs are partial;
- causes checked but not supported;
- missing data;
- safety-critical checks before further flight;
- top relevant generated plots/files, usually from `recommended_user_artifacts`, without cluttering the answer with every generated file;
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

When the user reports mode-specific behaviour and you need full diagnosis outputs per mode, run the mode-scoped diagnosis helper after or alongside mode comparison:

```bash
python scripts/ap_log_diagnose_modes.py LOG.BIN --symptom "yaw feels off especially during missions" --modes AUTO,POSHOLD --active-flight-only --out out/mode_diagnosis
```

Treat mode comparison and mode-scoped diagnosis as supporting evidence. Inspect the intervals, active-flight criteria, `modes_found`, `requested_modes_missing`, `manual_control_confidence`, `manual_control_limitations`, missing evidence, warnings, key differences, and confidence limits before deciding whether AUTO behaviour reflects mission yaw demand, estimator/navigation context, or a control/authority issue. POSHOLD and LOITER are useful comparison context but are not pure manual attitude modes; do not describe POSHOLD as pure manual control. Do not treat `mode_diagnosis_summary.json` as a final answer.

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
- timeline timing and causal weight: `references/timeline-interpretation.md`

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
- Separate `events_relative_to_window.inside_window` from `before_window` and `after_window` before using warnings as causal evidence. Post-flight or disarmed warnings can be safety-critical for the next flight without proving the in-window symptom; use `references/timeline-interpretation.md` when this distinction matters.
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
