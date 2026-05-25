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

7. Check battery/power and vibration as time-correlated contributors.

   Battery sag, high current, board power flags, high vibration, and clipping are relevant when they occur in the symptom window or correlate with the affected signal. Whole-log maxima are context unless timing supports relevance.

8. Treat script findings as hypotheses.

   A finding means a threshold or rule fired. Confirm it against plots, timing, available messages, and missing evidence before ranking it as a likely cause.

9. Always state missing data and confidence limits.

   Separate missing required data from missing strongly recommended or optional context. Explain when log dropouts, timestamp gaps, message sparsity, absent RCIN, absent ESC telemetry, or missing parameters limit confidence.
   If the current log is insufficient, use `logging-configuration-for-investigation.md` to describe the missing logging/messages and `evidence-gathering-flights.md` to choose the safest next evidence-gathering activity. The next step may be a parameter dump, bench inspection, ground test, restrained test, or controlled flight.

10. Do not recommend unsafe flight or disabling checks.

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

3. Plot yaw desired vs actual signals.

   ```bash
   python scripts/ap_log_extract.py LOG.BIN --messages ATT,RATE,PIDY,RCOU,RCO2,RCO3,MAG,XKF3,XKF4,BAT,POWR,VIBE,MODE,MSG,EV,ERR,RCIN,PARM --out out/tables --format csv
   python scripts/ap_log_custom_plot.py --tables out/tables --series ATT.DesYaw --series ATT.Yaw --out out/plots/yaw_att.html
   python scripts/ap_log_custom_plot.py --tables out/tables --series RATE.YDes --series RATE.Y --out out/plots/yaw_rate.html
   ```

4. Inspect `PIDY` and actuator evidence.

   Check `PIDY.Err`, PID terms, `PIDY.Dmod`, `PIDY.Flags`, `RATE.YOut`, mapped motor outputs, and ESC/ESCX/EDT2 if present. If yaw error rises while yaw output is high and motor outputs saturate, yaw authority is supported. If desired yaw changes first, check whether it was pilot-commanded (`RCIN`) or mode/autopilot-commanded.

5. If controller evidence does not explain the issue, inspect yaw estimator and compass evidence.

   Compare heading/yaw jumps with `MAG`, `XKF3`, `XKF4`, GPS/yaw-source messages if logged, flight mode, and yaw rate. Estimator or compass hypotheses need timing or innovation/test-ratio support, not MAG data alone.

6. Inspect battery, power, and vibration as supporting context.

   Check whether voltage sag, high current, power flags, high vibration, or clipping occurred in the same window as yaw error or output saturation. If they occurred elsewhere, list them as context rather than cause.

7. Form a ranked conclusion only after the evidence is checked.

   Rank likely causes by the strongest time-aligned evidence. Include checked-but-not-supported hypotheses, missing evidence, confidence limits, and safety-critical checks before further flight.
