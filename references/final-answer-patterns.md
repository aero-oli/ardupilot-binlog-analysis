# Final answer patterns

Use this reference to structure Codex-written final answers from inspected
evidence. These are scaffolds, not automatic report templates. Do not copy a
pattern mechanically; fill it only with findings, caveats, plots, and next steps
supported by the generated outputs.

For safety-relevant findings, always include clear `Recommended next steps`.
Do not overstate confidence, do mention missing evidence, and never recommend
unsafe flight, repeating a risky event, bypassing checks, or blind parameter
changes.

## General Pattern

1. Most likely issue.
   State the leading hypothesis and confidence. If confidence is low or medium,
   say why.
2. Why.
   Explain the causal reasoning in one short paragraph, tied to timing and
   control/estimator/power evidence.
3. Evidence.
   List the strongest in-window observations first. Use
   `events_relative_to_window` to separate in-window events from pre/post-flight
   context.
4. Checked but not supported.
   Name plausible causes the scripts or plots checked but did not support.
5. Missing evidence.
   State exactly which messages, parameters, telemetry, or plots are missing and
   what claims they prevent.
6. Safety status.
   State the conservative operating gate: normal analysis only, no AUTO/mission
   flying, controlled hover only, ground test only, bench only, or do not fly
   until checked.
7. Recommended next steps.
   Give the ordered action plan: immediate safety gate, bench/hardware checks,
   configuration/logging checks, controlled evidence capture only if safe,
   reanalysis, then what not to do.
8. What not to do.
   Call out unsafe shortcuts and unsupported changes.

## Mission Yaw And Wobble

Use this when the user reports mission yaw, AUTO yaw, wobble, unstable manual
feel, or yaw/attitude authority concerns.

1. Is AUTO worse than non-AUTO?
   Use mode comparison, mode-scoped diagnosis, and `manual_control_limitations`.
   Do not call POSHOLD pure manual control.
2. Is yaw commanded or uncommanded?
   Compare `RATE.YDes`, `RATE.Y`, `ATT.DesYaw`, `ATT.Yaw`, `RCIN`, and
   `mission_yaw_demand`. Treat `WP_YAW_BEHAVIOR` as context, not proof.
3. Does controller output suggest authority limit?
   Check `RATE.YOut`, `PIDY`, PID limits, and tracking error.
4. Are motor outputs saturated during active flight?
   Use mapped `RCOU`/`RCO2`/`RCO3`, active-flight filtering, and ESC telemetry if
   available.
5. Is vibration/noise present in the same window?
   Use `VIBE`, clipping, FFT, and timing correlation. Whole-log high vibration
   is context unless it overlaps or correlates with the symptom.
6. Is EKF/GPS/compass supported or not?
   Distinguish in-window EKF/GPS/compass evidence from after-window pre-arm or
   disarmed warnings.
7. Missing evidence.
   Name missing `PIDY`, actuator outputs, ESC telemetry, RC input, raw/high-rate
   IMU, pure manual modes, or parameter context.
8. What are the ordered next steps?
   Safety gate: pause AUTO/mission flying when mission behaviour is suspect.
   Recommended next steps: bench/mechanical checks, configuration/logging checks,
   controlled hover only if safe, and reanalysis before tuning.
9. What not to do.
   Do not repeat the mission to see if it happens again. Do not tune yaw or
   attitude gains until vibration, actuator authority, power, and missing
   controller evidence are understood. Do not disable safety checks or failsafes.

## Motor/ESC Issue

1. State whether the evidence supports motor output saturation, actuator
   asymmetry, ESC status/error evidence, or only a possible motor/ESC hypothesis.
2. Tie the claim to active-flight timing, mapped output channels, ESC/ESCX/EDT2,
   battery/current, rate tracking, and any abrupt log ending.
3. Say what was checked but not supported, such as no active-flight output
   saturation or no ESC error rows.
4. Missing evidence: state missing `RCOU`/`RCO2`/`RCO3`, `PARM` mapping, ESC telemetry,
   battery/current, or rate evidence.
5. Safety gate: bench-only or do-not-fly gate when hardware/power is
   unresolved.
6. Recommended next steps: inspect props, motors, bearings, ESC wiring,
   connectors, frame arms, motor order/direction, and output mapping; collect
   ground/bench evidence before any controlled hover.
7. What not to do: do not repeat flight after suspected motor/ESC/power fault;
   do not increase gains to overcome a possible hardware issue; do not bypass
   arming, battery, or motor safety checks.

## Vibration/Filter Issue

1. State whether vibration or clipping is in-window, whole-log context, or
   unsupported.
2. Use `control_evidence_completeness`, `VIBE`, clipping deltas, raw/high-rate
   IMU, FFT availability, and correlations with rate/attitude errors.
3. Missing evidence: mention if FFT, raw/high-rate IMU, PID, or actuator evidence
   is missing or unusable and what that prevents.
4. Safety gate: do not fly through severe vibration or clipping; use bench or
   controlled-hover-only limits according to severity.
5. Recommended next steps: mechanical inspection first, then logging/config
   review for raw/high-rate IMU or batch sampler if needed, then only a short
   controlled capture if the aircraft is otherwise stable.
6. What not to do: do not tune filters or gains blindly; do not fly through
   severe vibration or clipping; do not leave high-volume diagnostic logging on
   after the test.

## EKF/GPS/Loiter Issue

1. State whether the issue is supported by in-window GPS/EKF evidence, compass or
   yaw-source evidence, mode-specific navigation behaviour, or only context.
2. Use `GPS`/`GPS2`/`GPA`, `XKF*`/`NKF*`, `MAG`, `MODE`, `MSG`/`ERR`/`EV`, power,
   vibration, and mode comparison.
3. Separate Loiter/POSHOLD/AUTO navigation evidence from Stabilize/AltHold
   attitude-control evidence.
4. Missing evidence: state missing GPS quality, EKF innovations/test ratios, MAG,
   timeline messages, pure manual modes, vibration/power context, or parameters.
5. Safety gate: no navigation/mission flight if navigation behaviour is suspect.
6. Recommended next steps: inspect GPS/compass placement, wiring, power, antenna
   view, vibration, and EKF/yaw-source configuration; collect controlled evidence
   only after manual/altitude control is stable.
7. What not to do: do not disable EKF/GPS/compass checks or failsafes as a
   routine fix; do not call post-flight pre-arm warnings the cause of an
   in-window event unless timing supports it.

## RC/Failsafe/Pre-Arm Issue

1. Lead with the timeline: `MSG`, `ERR`, `EV`, `ARM`, `MODE`, and
   `events_relative_to_window`.
2. State whether the evidence is pre-arm/ground-only, in-flight failsafe,
   post-flight/disarmed context, or missing.
3. Use `RCIN`, `RCMAP_*`, arming/failsafe parameters, battery/board power,
   GPS/EKF/compass pre-arm evidence, and decoded ERR context.
4. Missing evidence: state missing `MSG`, `ERR`, `ARM`, `RCIN`, `PARM`, power, GPS/EKF,
   or `LOG_DISARMED` for boot/pre-arm cases.
5. Safety gate: ground-test or bench-only gate until arming, RC, or failsafe
   evidence is understood.
6. Recommended next steps: record exact GCS messages; use `LOG_DISARMED` only
   when needed for boot/pre-arm evidence; check RC mapping, receiver health,
   safety switch, power, GPS/EKF/compass warnings, and failsafe settings.
7. What not to do: do not bypass arming checks, RC failsafe, battery failsafe,
   EKF/GPS/compass checks, or safety switch as a routine fix.

## Crash/Loss-Of-Control

1. Safety gate: put safety status first; do not fly until checked unless the
   evidence clearly shows a non-flight-only issue and the relevant checks are
   complete.
2. Build the timeline from in-window attitude/rate, motor outputs, power,
   vibration, GPS/EKF, RC input, mode changes, ERR/EV/MSG, and log ending.
3. Rank causes only by strongest time-aligned evidence. Separate safety context
   from causal proof.
4. Missing evidence: state checked-but-not-supported hypotheses and all missing
   evidence that prevents a stronger conclusion.
5. Recommended next steps: preserve logs and parameters, bench inspect airframe,
   props, motors, ESCs, wiring, FC mounting, power system, GPS/compass, and RC
   link; verify configuration and logging; reanalyse before any test.
6. What not to do: do not repeat the flight, resume mission work, tune around
   the crash, or disable failsafes/checks until hardware, configuration, and
   evidence gaps have been resolved.
