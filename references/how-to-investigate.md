# How To Investigate Logs

Use this as an operating sequence before forming conclusions. The scripts provide evidence and hypotheses; Codex is responsible for checking whether the evidence actually supports the reported symptom.

## Procedure

1. Validate the log and inventory messages before interpretation.

   ```bash
   python scripts/ap_log_validate.py LOG.BIN --json out/validate.json --summary out/validate.md
   python scripts/ap_log_index.py LOG.BIN --json out/index.json --summary out/index.md
   ```

   Check vehicle type, firmware hints, duration, message availability, logging-health warnings, and whether core messages exist after arming.

2. If the user reports a symptom, run the investigation manifest first.

   ```bash
   python scripts/ap_log_investigation_manifest.py LOG.BIN --symptom "USER SYMPTOM" --out out/investigation.json
   ```

   Treat the manifest as a plan: available evidence, missing evidence, questions to answer, suggested plots, and confidence limits. It is not a diagnosis.

3. Choose a relevant time window before interpreting metrics.

   Prefer `--window`, `--mode`, `--around-msg`, `--around-event`, `--around-error`, `--takeoff-only`, `--hover-candidates`, or `--high-throttle-only` over whole-log averages when the symptom occurred in a specific phase. If a requested selector cannot be resolved, state that and avoid silently falling back to whole-log conclusions.

4. Plot desired vs actual signals.

   For attitude/rate symptoms, compare desired and achieved values before looking for causes. Use custom plots when the standard diagnosis plots are not enough.

   ```bash
   python scripts/ap_log_custom_plot.py --tables out/tables --series ATT.DesYaw --series ATT.Yaw --out out/plots/yaw_att.html
   python scripts/ap_log_custom_plot.py --tables out/tables --series RATE.YDes --series RATE.Y --out out/plots/yaw_rate.html
   ```

5. Check actuator authority and saturation.

   Inspect `RATE.*Out`, `RCOU`/`RCO2`/`RCO3`, motor mapping from `SERVOx_FUNCTION`, and ESC telemetry if available. Saturation, output clipping, one motor/ESC behaving differently, or PID limit flags can support an authority hypothesis.

6. Check estimator, GPS, and compass evidence.

   Use `GPS`, `MAG`, `XKF3`, `XKF4`, mode/timeline messages, and yaw-source helpers where available. Do not infer compass interference from MAG ranges alone; require timing, correlation, or estimator evidence.

7. For arming, pre-arm, RC, or failsafe symptoms, follow `rc-failsafe-prearm-diagnosis.md`.

   Start with the `MSG`/`ERR`/`EV`/`ARM`/`MODE` timeline, then inspect `RCIN`, `RCMAP_*`, failsafe parameters, battery/board power, and GPS/EKF/compass pre-arm evidence. If the issue happened before arming and the log lacks timeline evidence, ask for a ground-only `LOG_DISARMED` capture rather than a flight.

8. Check battery/power and vibration as time-correlated contributors.

   Battery sag, high current, board power flags, high vibration, and clipping are relevant when they occur in the symptom window or correlate with the affected signal. Whole-log maxima are context unless timing supports relevance.

9. Treat script findings as hypotheses.

   A finding means a threshold or rule fired. Confirm it against plots, timing, available messages, and missing evidence before ranking it as a likely cause.

10. Always state missing data and confidence limits.

   Separate missing required data from missing strongly recommended or optional context. Explain when log dropouts, timestamp gaps, message sparsity, absent RCIN, absent ESC telemetry, or missing parameters limit confidence.
   If the current log is insufficient, use `logging-configuration-for-investigation.md` to describe the missing logging/messages and `evidence-gathering-flights.md` to choose the safest next evidence-gathering activity. The next step may be a parameter dump, bench inspection, ground test, restrained test, or controlled flight.

11. For safety-relevant cases, build a next-step planning aid after diagnosis and any mode comparison outputs exist.

   ```bash
   python scripts/ap_next_steps.py --diagnosis out/diagnosis.json --mode-compare out/mode_compare.json --param-lookup out/param_lookup.json --fft out/fft.json --manifest out/investigation.json --json out/next_steps.json --summary out/next_steps.md
   ```

   Pass only the outputs that exist. Inspect the generated plan for the immediate safety gate, bench checks, logging/configuration checks, controlled evidence capture, reanalysis, and what not to do. Treat it as planning guidance, not an automatic final diagnosis.

12. Do not recommend unsafe flight or disabling checks.

    Never declare the aircraft safe from a log alone. Do not recommend disabling arming, EKF, GPS, compass, battery, logging, or failsafe checks as a routine fix. Prefer targeted inspection, bench verification, and conservative ground checks.

## Worked Yaw Example

User report: "yaw misbehaves".

1. Run validation and index, then create the investigation manifest.

   ```bash
   python scripts/ap_log_validate.py LOG.BIN --json out/validate.json --summary out/validate.md
   python scripts/ap_log_index.py LOG.BIN --json out/index.json --summary out/index.md
   python scripts/ap_log_investigation_manifest.py LOG.BIN --symptom "yaw misbehaves" --out out/investigation.json
   ```

2. Use the manifest to identify the best window. If the symptom happened in Loiter or near a message/event, run diagnosis and plots in that window.

   ```bash
   python scripts/ap_log_diagnose.py LOG.BIN --symptom "yaw misbehaves" --mode LOITER --out out/diagnosis.json --plots out/plots/diagnosis
   ```

3. If the complaint is mission/AUTO versus manual or positioning modes, compare modes directly before treating the issue as a generic yaw tune problem.

   ```bash
   python scripts/ap_log_mode_compare.py LOG.BIN --symptom yaw_misbehaviour --compare-modes AUTO,POSHOLD,ALTHOLD,STABILIZE --active-flight-only --json out/mode_compare.json --plots out/plots/mode_compare
   ```

   Treat this as a diagnostic aid: inspect `intervals_found`, `intervals_used`,
   active-flight criteria, missing evidence, and confidence limits before
   ranking causes.

4. Plot yaw desired vs actual signals.

   ```bash
   python scripts/ap_log_extract.py LOG.BIN --messages ATT,RATE,PIDY,RCOU,RCO2,RCO3,MAG,XKF3,XKF4,BAT,POWR,VIBE,MODE,MSG,EV,ERR,RCIN,PARM --out out/tables --format csv
   python scripts/ap_log_custom_plot.py --tables out/tables --series ATT.DesYaw --series ATT.Yaw --out out/plots/yaw_att.html
   python scripts/ap_log_custom_plot.py --tables out/tables --series RATE.YDes --series RATE.Y --out out/plots/yaw_rate.html
   ```

5. Inspect `PIDY` and actuator evidence.

   Check `PIDY.Err`, PID terms, `PIDY.Dmod`, `PIDY.Flags`, `RATE.YOut`, mapped motor outputs, and ESC/ESCX/EDT2 if present. If yaw error rises while yaw output is high and motor outputs saturate, yaw authority is supported. If desired yaw changes first, check whether it was pilot-commanded (`RCIN`) or mode/autopilot-commanded.

6. If controller evidence does not explain the issue, inspect yaw estimator and compass evidence.

   Compare heading/yaw jumps with `MAG`, `XKF3`, `XKF4`, GPS/yaw-source messages if logged, flight mode, and yaw rate. Estimator or compass hypotheses need timing or innovation/test-ratio support, not MAG data alone.

7. Inspect battery, power, and vibration as supporting context.

   Check whether voltage sag, high current, power flags, high vibration, or clipping occurred in the same window as yaw error or output saturation. If they occurred elsewhere, list them as context rather than cause.

8. Build the next-step plan if the case is safety-relevant.

   ```bash
   python scripts/ap_next_steps.py --diagnosis out/diagnosis.json --mode-compare out/mode_compare.json --manifest out/investigation.json --json out/next_steps.json --summary out/next_steps.md
   ```

   Use it to make sure the final answer includes the ordered safety gate, bench checks, logging improvements, controlled capture if safe, reanalysis, and what not to do.

9. Form a ranked conclusion only after the evidence is checked.

   Rank likely causes by the strongest time-aligned evidence. Include checked-but-not-supported hypotheses, missing evidence, confidence limits, and safety-critical checks before further flight.

## Worked Loiter / GPS-EKF Example

User report: "it drifted in Loiter" or "GPS glitch / EKF error".

1. Run validation, index, and the manifest.

   ```bash
   python scripts/ap_log_validate.py LOG.BIN --json out/validate.json --summary out/validate.md
   python scripts/ap_log_index.py LOG.BIN --json out/index.json --summary out/index.md
   python scripts/ap_log_investigation_manifest.py LOG.BIN --symptom "Loiter drift GPS EKF issue" --out out/investigation.json
   ```

2. Choose the window. Prefer `--mode LOITER`, `--around-msg "GPS"`, `--around-event "EKF"`, or an exact `--window`. If possible, run a comparison diagnosis for a stable non-navigation mode.

   ```bash
   python scripts/ap_log_diagnose.py LOG.BIN --symptom "Loiter drift GPS EKF issue" --mode LOITER --out out/diagnosis-loiter.json --plots out/plots/loiter --events
   python scripts/ap_log_diagnose.py LOG.BIN --symptom "Loiter drift GPS EKF issue" --mode ALTHOLD --out out/diagnosis-althold.json --plots out/plots/althold --events
   ```

3. Extract and plot GPS/EKF timeline evidence.

   ```bash
   python scripts/ap_log_extract.py LOG.BIN --messages GPS,GPS2,GPA,XKF1,XKF3,XKF4,NKF4,MAG,ATT,RATE,VIBE,BAT,POWR,MODE,MSG,EV,ERR,PARM --out out/tables --format csv
   python scripts/ap_log_custom_plot.py --tables out/tables --series GPS.Status --series GPS.NSats --series GPS.HDop --secondary GPS.HDop --events --title "GPS quality with mode timeline" --out out/plots/gps_quality.html
   python scripts/ap_log_custom_plot.py --tables out/tables --series XKF4.SV --series XKF4.SP --series XKF4.SH --series XKF4.SM --events --title "EKF test ratios with mode timeline" --out out/plots/ekf_test_ratios.html
   ```

4. Inspect evidence.

   Check whether GPS status, satellite count, HDOP/HAcc/VAcc, EKF test ratios, innovations, mode changes, `MSG`/`ERR`/`EV`, compass/yaw-source evidence, vibration, or power changed before the drift. If Loiter is bad but AltHold/Stabilize is stable, navigation/estimator evidence gets more weight; if all modes are unstable, inspect attitude, power, vibration, or mechanical causes first.

5. State missing evidence and confidence limits.

   If `GPS`, `XKF1`, `XKF3`, `XKF4`, `MODE`, `MSG`, `ERR`, or `EV` are missing, say which EKF/GPS claims cannot be made. Use `logging-configuration-for-investigation.md` and `evidence-gathering-flights.md` before suggesting another capture.

6. Decide whether more evidence is needed.

   Do not recommend Loiter/Auto testing if manual or AltHold control is not already reliable, if GPS/EKF/compass warnings persist, or if the vehicle drifted toward hazards. The next activity may be parameter review, bench/ground GPS-compass checks, or a controlled AltHold-vs-Loiter comparison only when safe.

## Worked Vibration / Filter / FFT Example

User report: "bad vibration", "noisy gyro", "filter issue", or "need FFT".

1. Run validation, index, and the manifest.

   ```bash
   python scripts/ap_log_validate.py LOG.BIN --json out/validate.json --summary out/validate.md
   python scripts/ap_log_index.py LOG.BIN --json out/index.json --summary out/index.md
   python scripts/ap_log_investigation_manifest.py LOG.BIN --symptom "vibration filter FFT issue" --out out/investigation.json
   ```

2. Choose the window. Use hover, high-throttle, or an event-centered window if the vibration appears only during a specific phase.

   ```bash
   python scripts/ap_log_diagnose.py LOG.BIN --symptom "vibration filter FFT issue" --hover-candidates --out out/diagnosis-vibration.json --plots out/plots/vibration --events
   python scripts/ap_log_diagnose.py LOG.BIN --symptom "vibration filter FFT issue" --high-throttle-only --out out/diagnosis-high-throttle.json --plots out/plots/high-throttle --events
   ```

3. Extract and plot vibration and control-response evidence.

   ```bash
   python scripts/ap_log_extract.py LOG.BIN --messages VIBE,IMU,GYR,ACC,ISBH,ISBD,RATE,PIDR,PIDP,PIDY,BAT,POWR,ESC,ESCX,EDT2,MODE,MSG,EV,ERR,PARM --out out/tables --format csv
   python scripts/ap_log_custom_plot.py --tables out/tables --series VIBE.VibeX --series VIBE.VibeY --series VIBE.VibeZ --events --title "Vibration levels" --out out/plots/vibe_levels.html
   python scripts/ap_log_custom_plot.py --tables out/tables --series VIBE.Clip0 --series VIBE.Clip1 --series VIBE.Clip2 --events --title "IMU clipping" --out out/plots/vibe_clipping.html
   ```

4. Run FFT only when raw/high-rate IMU or batch-sampler evidence exists.

   ```bash
   python scripts/ap_log_fft.py LOG.BIN --out out/fft --json out/fft.json
   ```

   If `IMU`, `GYR`, `ACC`, `ISBH`, or `ISBD` is absent, state that FFT/filter confidence is limited and use the logging/evidence-gathering references for a short, controlled future capture. Warn about large logs, logging dropouts, and resetting high-volume logging afterward.

5. Inspect evidence.

   Check `VIBE` levels, clipping counters, raw/batch FFT peaks, `RATE` tracking, PID `Dmod`/flags, motor RPM if present, battery/power, and whether the symptom aligns with throttle or a mode segment. Treat FFT peaks as filter evidence only when the logged sensor data and timing support them.

6. State missing evidence and confidence limits.

   Missing `VIBE` prevents basic vibration assessment. Missing `IMU`/`GYR`/`ACC`/`ISBH`/`ISBD` prevents strong FFT/filter conclusions. Missing `RATE`/PID evidence limits claims about vibration affecting control; missing motor RPM/ESC telemetry limits notch-source confirmation.

7. Decide whether more evidence is needed.

   Safety-critical vibration or clipping comes before tuning. Do not recommend further flight if hardware is loose/damaged, clipping rises rapidly, or prior vibration coincided with loss of control. Recommend bench inspection first; recommend short raw/high-rate capture only if the vehicle is otherwise controllable.

## Worked Motor / ESC / Thrust-Loss Example

User report: "motor stopped", "ESC desync", "motor pulsing", or "lost thrust".

1. Run validation, index, and the manifest.

   ```bash
   python scripts/ap_log_validate.py LOG.BIN --json out/validate.json --summary out/validate.md
   python scripts/ap_log_index.py LOG.BIN --json out/index.json --summary out/index.md
   python scripts/ap_log_investigation_manifest.py LOG.BIN --symptom "motor ESC thrust loss" --out out/investigation.json
   ```

2. Choose the window. Use an exact event window, first error, high-throttle window, or final seconds before log end.

   ```bash
   python scripts/ap_log_diagnose.py LOG.BIN --symptom "motor ESC thrust loss" --high-throttle-only --out out/diagnosis-motor.json --plots out/plots/motor --events
   python scripts/ap_log_diagnose.py LOG.BIN --symptom "motor ESC thrust loss" --around-error --around-radius 20 --out out/diagnosis-error-window.json --plots out/plots/motor-error --events
   ```

3. Extract and plot actuator, ESC, power, and rate evidence.

   ```bash
   python scripts/ap_log_extract.py LOG.BIN --messages RCOU,RCO2,RCO3,ESC,ESCX,EDT2,RATE,ATT,PIDR,PIDP,PIDY,BAT,POWR,VIBE,MODE,MSG,EV,ERR,RCIN,PARM --out out/tables --format csv
   python scripts/ap_log_custom_plot.py --tables out/tables --series RCOU.C1 --series RCOU.C2 --series RCOU.C3 --series RCOU.C4 --events --title "Mapped actuator outputs" --out out/plots/motor_outputs.html
   python scripts/ap_log_custom_plot.py --tables out/tables --series RATE.RDes --series RATE.R --series RATE.PDes --series RATE.P --series RATE.YDes --series RATE.Y --events --title "Rate tracking during thrust issue" --out out/plots/rate_tracking_motor.html
   python scripts/ap_log_custom_plot.py --tables out/tables --series BAT.Volt --series BAT.Curr --secondary BAT.Curr --events --title "Battery during thrust issue" --out out/plots/power_motor.html
   ```

   If `ESC`, `ESCX`, or `EDT2` exists, add ESC-specific plots for RPM, current, voltage, temperature, status, errors, duty, power, and stress. If not, state that ESC telemetry is missing and use `RCOU`/`RCO2`/`RCO3`, `BAT`, `POWR`, and `RATE` as proxy evidence.

4. Inspect evidence.

   Confirm `SERVOx_FUNCTION` mapping from `PARM` before naming motors. Look for output saturation/asymmetry, ESC status/error/stress, RPM mismatch, current/voltage sag, board-power flags, rate error, vibration, and whether the log ended abruptly. A motor/ESC hypothesis needs time alignment, not just a whole-log maximum.

5. State missing evidence and confidence limits.

   Missing `RCOU`/`RCO2`/`RCO3` prevents actuator-output confirmation. Missing `PARM` weakens channel-to-motor mapping. Missing `ESC`/`ESCX`/`EDT2` prevents ESC-level confirmation. Missing `BAT`/`POWR` limits power/thrust-loss interpretation.

6. Decide whether more evidence is needed.

   Do not recommend another flight after suspected motor, ESC, prop, wiring, connector, or power failure until bench inspection and ground checks are complete. The next evidence should normally be parameter review, hardware inspection, props-off motor tests where appropriate, or restrained ground checks, not a repeat flight.

## Worked Altitude / Throttle Example

User report: "Altitude Hold climbed", "dropped in AltHold", or "throttle problem".

1. Run validation, index, and the manifest.

   ```bash
   python scripts/ap_log_validate.py LOG.BIN --json out/validate.json --summary out/validate.md
   python scripts/ap_log_index.py LOG.BIN --json out/index.json --summary out/index.md
   python scripts/ap_log_investigation_manifest.py LOG.BIN --symptom "AltHold climb drop throttle issue" --out out/investigation.json
   ```

2. Choose the window. Prefer `--mode ALTHOLD`, `--mode LOITER`, an altitude-event window, or a hover/high-throttle selector depending on the report.

   ```bash
   python scripts/ap_log_diagnose.py LOG.BIN --symptom "AltHold climb drop throttle issue" --mode ALTHOLD --out out/diagnosis-altitude.json --plots out/plots/altitude --events
   python scripts/ap_log_diagnose.py LOG.BIN --symptom "AltHold climb drop throttle issue" --hover-candidates --out out/diagnosis-hover.json --plots out/plots/hover --events
   ```

3. Extract and plot altitude, throttle, power, estimator, and actuator evidence.

   ```bash
   python scripts/ap_log_extract.py LOG.BIN --messages CTUN,BARO,RNGF,GPS,XKF4,NKF4,ATT,RATE,RCOU,RCO2,RCO3,BAT,POWR,VIBE,ESC,ESCX,EDT2,MODE,MSG,EV,ERR,RCIN,PARM --out out/tables --format csv
   python scripts/ap_log_custom_plot.py --tables out/tables --series CTUN.DAlt --series CTUN.Alt --series CTUN.ThO --series CTUN.ThH --secondary CTUN.ThO --events --title "Altitude target, actual, and throttle" --out out/plots/altitude_throttle.html
   python scripts/ap_log_custom_plot.py --tables out/tables --series BARO.Alt --series GPS.Alt --series XKF4.SH --secondary XKF4.SH --events --title "Height sensors and EKF height test ratio" --out out/plots/height_estimator.html
   python scripts/ap_log_custom_plot.py --tables out/tables --series BAT.Volt --series BAT.Curr --series POWR.Vcc --secondary BAT.Curr --events --title "Power during altitude issue" --out out/plots/altitude_power.html
   python scripts/ap_log_custom_plot.py --tables out/tables --series RCOU.C1 --series RCOU.C2 --series RCOU.C3 --series RCOU.C4 --events --title "Motor outputs during altitude issue" --out out/plots/altitude_outputs.html
   ```

4. Inspect evidence.

   Compare `CTUN.DAlt` vs `CTUN.Alt`, `CTUN.ThO`/`ThH`, climb/descent rate if present, `BARO`, `RNGF`, GPS altitude, EKF height test ratios, battery sag, board power, vibration/clipping, and mapped outputs. Separate likely control tracking, estimator/barometer/rangefinder, vibration, and power/thrust limitation branches.

5. State missing evidence and confidence limits.

   Missing `CTUN` prevents strong altitude-control interpretation. Missing `BARO`/`RNGF`/`GPS`/`XKF4` limits sensor/estimator claims. Missing `BAT`/`POWR`/`RCOU` limits thrust and power conclusions. Missing `VIBE` limits vibration/baro-disturbance interpretation.

6. Decide whether more evidence is needed.

   Safety-critical climb/drop, thrust loss, power sag, severe vibration, or sensor faults come before tuning. Do not suggest aggressive altitude tests. If further capture is appropriate, it should be a conservative hover or small AltHold altitude change only after propulsion, power, sensor, and mechanical checks.
