# RC failsafe, arming, and pre-arm diagnosis

Use this guide for "would not arm", pre-arm errors, radio/RC/throttle/GCS failsafe, lost RC input, hardware safety switch, and arming-check failures. The aim is to reconstruct the evidence timeline and identify what to inspect next; it is not a shortcut for bypassing protections.

Official ArduPilot grounding:

- Pre-arm checks intentionally prevent arming when calibration, configuration, sensor, power, or other safety issues are detected: <https://ardupilot.org/copter/docs/common-prearm-safety-checks.html>
- The hardware safety switch can hold outputs in a safety state and produce a pre-arm condition until cleared: <https://ardupilot.org/copter/docs/common-safety-switch-pixhawk.html>
- DataFlash logging must contain the relevant messages; disarmed logging may be needed for boot and pre-arm failures: <https://ardupilot.org/copter/docs/common-logs.html>

## Evidence order

1. Build the `MSG` / `ERR` / `EV` / `ARM` / `MODE` timeline first.
   - Look for the exact pre-arm, arming-denied, failsafe, safety-switch, battery, GPS, EKF, compass, or radio message.
   - Decide whether the event happened before arming, during arming, or after flight had already started.
   - Treat missing `MSG` and `ERR` as a major confidence limit. Do not infer that the issue did not happen just because the log lacks the message.

2. Check RC link and command evidence.
   - Use `RCIN` to see whether roll, pitch, throttle, and yaw inputs are present, stable, and mapped as expected.
   - Review `RCMAP_ROLL`, `RCMAP_PITCH`, `RCMAP_THROTTLE`, and `RCMAP_YAW`; if `PARM` is missing, state that channel mapping is assumed.
   - Review `RC_OPTIONS` and `RC_PROTOCOLS` when no RC input, unexpected receiver behaviour, or protocol changes are suspected.
   - For throttle/radio failsafe, correlate `RCIN` with `MSG`/`ERR`/`EV`, mode changes, and any `ARM` state change.

3. Check failsafe and safety parameters as context.
   - `FS_THR_ENABLE`, `FS_OPTIONS`, and `FS_GCS_ENABLE` describe configured failsafe behaviour; they do not prove the failsafe occurred unless the timeline supports it.
   - `ARMING_CHECK` and `BRD_SAFETYENABLE` help explain arming and safety-switch behaviour. Do not recommend skipping checks as a routine fix.

4. Check battery and board power.
   - Use `BAT` for battery voltage/current and arming thresholds such as `BATT_ARM_VOLT` and `BATT_ARM_MAH`.
   - Use `POWR` for board Vcc and power flags.
   - A battery or board-power arming failure should be investigated as a power-health issue before flight.

5. Check GPS, EKF, and compass pre-arm evidence.
   - Use `GPS`, `XKF4`, and `MAG` with `MSG`/`ERR` text to support or reject GPS, EKF, or compass pre-arm causes.
   - Review `GPS_TYPE`, `COMPASS_USE`, `EK3_SRC1_POSXY`, and `EK3_SRC1_YAW` as configuration context.
   - Do not treat a navigation-mode issue as an RC/failsafe issue unless the timeline or RC evidence supports it.

6. Decide whether `LOG_DISARMED` was needed.
   - Boot, startup, pre-arm, and arming-denied evidence often occurs before normal armed logging starts.
   - If the current log begins too late or lacks `MSG`/`ERR`/`ARM`, ask for a short ground capture with `LOG_DISARMED` enabled and the exact GCS message recorded.
   - Warn that disarmed logging can create large logs and should normally be disabled again after the diagnostic capture.

## Do not do this

- Do not bypass arming checks, GPS/EKF/compass checks, battery checks, receiver failsafes, or the safety switch as a routine fix.
- Do not arm or fly just to capture a log when a pre-arm failure, power issue, missing RC input, or safety-switch problem is unresolved.
- Do not recommend a repeat flight after a failsafe or lost-control event until bench, wiring, receiver, power, parameter, and failsafe checks are complete.
- Do not treat absent `MSG`, `ERR`, `EV`, `ARM`, or `RCIN` evidence as proof that arming/failsafe behaviour was normal.
