# Evidence-gathering activities

Use this when the current log cannot answer the diagnostic question. The next
activity may be a parameter dump, bench inspection, restrained ground test, or a
controlled flight. Do not imply that another flight is always appropriate.

Pair this guide with `references/logging-configuration-for-investigation.md` for
logging settings, high-volume logging caveats, and cleanup after capture.

## Safety-first decision logic

1. If motor, ESC, prop, frame, battery, connector, power-module, wiring, or board
   power failure is suspected, recommend bench inspection and ground checks
   before any flight.
2. If control instability, oscillation, or poor tracking is suspected, recommend
   mechanical/setup checks first. A flight capture is only appropriate in an open
   area, at low altitude, with gentle inputs and a ready abort plan.
3. If vibration or filter evidence is missing, use raw/high-rate IMU or batch
   sampling only for a short planned capture. Warn about large logs and dropouts,
   then tell the user to disable high-volume logging after the test.
4. If EKF/GPS/compass behaviour is suspected, compare Stabilize/AltHold with
   Loiter/Auto only if the aircraft is already controllable in non-navigation
   modes. Do not ask the user to continue a navigation-mode test after drift,
   flyaway tendency, or yaw-source instability appears.
5. If the event was a crash or loss of control, do not recommend repeat flight
   until mechanical, wiring, prop, motor, ESC, power, failsafe, and parameter
   checks are complete.

For every recommendation, state what the current log is missing, what the next
capture should prove or refute, and why the proposed activity is the lowest-risk
way to collect that evidence.

## Common setup for any next capture

- Ask for a full parameter dump when parameters are missing or output mapping,
  failsafe settings, logging settings, battery monitor setup, compass/GPS/EKF
  source configuration, or tuning values matter.
- Confirm the log profile includes the messages needed for the symptom class.
  Review `LOG_BITMASK`, `LOG_BACKEND_TYPE`, `LOG_FILE_RATEMAX`,
  `LOG_DARM_RATEMAX`, `LOG_BLK_RATEMAX`, and `LOG_DISARMED`.
- Enable `LOG_DISARMED` only for boot, pre-arm, arming-failure, startup sensor,
  or bench evidence. Warn that it can create large logs.
- For raw IMU/filter/FFT evidence, review `INS_RAW_LOG_OPT`,
  `INS_LOG_BAT_MASK`, and `INS_LOG_BAT_OPT`, then check `DSF`/`DMS` and timestamp
  continuity after capture.
- After the test, return high-volume settings to normal: clear raw IMU logging,
  clear or reduce batch logging, restore ordinary rate limits, and disable
  disarmed logging if it was only needed for the investigation.

## Symptom-class plans

### yaw_misbehaviour

- Minimum required evidence: `ATT`, `RATE`.
- Strongly recommended evidence: `PIDY`, `RCOU`/`RCO2`/`RCO3`, `MODE`, plus
  `PARM` for `SERVOx_FUNCTION` mapping.
- Useful optional context: `MSG`, `EV`, `ERR`, `RCIN`, `MAG`, `XKF3`, `XKF4`,
  `VIBE`, `BAT`, `POWR`, `ESC`, `ESCX`, `EDT2`.
- Logging parameters to review: `LOG_BITMASK`, `LOG_FILE_RATEMAX`,
  `LOG_DARM_RATEMAX`, `EK3_LOG_LEVEL`, `INS_RAW_LOG_OPT` only if vibration/noise
  may be contributing.
- Suggested plots/signals: `ATT.DesYaw` vs `ATT.Yaw`; `RATE.YDes` vs `RATE.Y`
  and `RATE.YOut`; `PIDY` terms/flags; mapped motor outputs; `RCIN` yaw;
  `MAG`/`XKF3`/`XKF4`; battery and vibration around the event.
- Safe test pattern: only after motor/prop/frame/setup checks, capture 30-60
  seconds of stable hover plus small yaw steps and small roll/pitch inputs in
  Stabilize or AltHold. If the symptom is navigation-only, compare AltHold and
  Loiter only if the vehicle is controllable.
- Bench checks before flight: prop condition/orientation, motor direction,
  frame twist, loose arms, ESC connections, output mapping, compass mounting,
  battery condition, and yaw-related parameters.
- Do not fly if: yaw authority was lost, a motor/ESC/power fault is suspected,
  the vehicle spun uncontrollably, compass/yaw-source faults persist on the
  ground, or the pilot cannot safely recover in manual attitude modes.
- Cleanup: disable raw/batch IMU logging if used; restore rate limits.

### attitude_rate_issue

- Minimum required evidence: `ATT`, `RATE`.
- Strongly recommended evidence: `PIDR`, `PIDP`, `RCOU`/`RCO2`/`RCO3`, `MODE`.
- Useful optional context: `PIDY`, `MSG`, `EV`, `ERR`, `RCIN`, `VIBE`, `IMU`,
  `BAT`, `POWR`, `ESC`, `ESCX`, `EDT2`, `XKF4`.
- Logging parameters to review: `LOG_BITMASK`, `LOG_FILE_RATEMAX`,
  `LOG_DARM_RATEMAX`, `INS_RAW_LOG_OPT`/`INS_LOG_BAT_MASK` only for suspected
  vibration/filter coupling.
- Suggested plots/signals: roll/pitch `ATT` desired vs actual; roll/pitch
  `RATE` desired vs actual and `*Out`; `PIDR`/`PIDP` terms, `Dmod`, flags;
  motor outputs; `RCIN` roll/pitch; vibration and battery in the same window.
- Safe test pattern: after mechanical and setup checks, use an open area,
  low-altitude stable hover, then small gentle roll/pitch inputs. Avoid aggressive
  stick steps until the vehicle is clearly stable.
- Bench checks before flight: loose frame/arms, props, motor bearings, ESC/motor
  sync, CG, payload security, flight-controller mounting, tune plausibility, and
  output mapping.
- Do not fly if: oscillation was severe, the aircraft diverged, outputs saturated
  without recovery, a motor/ESC/power issue is suspected, or mounting/mechanical
  faults remain.
- Cleanup: disable high-rate vibration logging if enabled for the capture.

### motor_esc_issue

- Minimum required evidence: no single message is mandatory, but meaningful log
  diagnosis usually needs actuator outputs and rate response.
- Strongly recommended evidence: `RCOU`/`RCO2`/`RCO3`, `RATE`, `PARM`.
- Useful optional context: `ESC`, `ESCX`, `EDT2`, `PIDR`, `PIDP`, `PIDY`, `BAT`,
  `POWR`, `VIBE`, `MSG`, `EV`, `ERR`, `ATT`, `RCIN`.
- Logging parameters to review: `LOG_BITMASK`, motor/RC-output logging,
  `LOG_FILE_RATEMAX`, ESC telemetry configuration, `LOG_DISARMED` for bench
  spool checks when appropriate.
- Suggested plots/signals: mapped motor outputs, ESC RPM/current/voltage/temp,
  EDT2 status/errors, `RATE` error, `BAT`/`POWR`, vibration, mode/events.
- Safe test pattern: bench inspection first. If safe and legal, use props-off
  motor tests or restrained ground checks to verify motor order, direction, ESC
  telemetry, and abnormal heat/noise. A flight capture is only appropriate after
  those checks pass.
- Bench checks before flight: props removed for motor tests, prop damage and
  orientation, motor bearings, bells, screws, solder joints, connectors, ESC
  calibration/config, motor order, frame damage, and battery/connector health.
- Do not fly if: any motor stopped, desynced, overheated, emitted smoke/smell,
  has bearing damage, has intermittent wiring, shows ESC errors, or power supply
  integrity is in doubt.
- Cleanup: disable `LOG_DISARMED` if it was only used for bench logging.

### vibration_issue

- Minimum required evidence: `VIBE`.
- Strongly recommended evidence: `RATE`.
- Useful optional context: `PIDR`, `PIDP`, `PIDY`, `IMU`, `GYR`, `ACC`, `ISBH`,
  `ISBD`, `BAT`, `POWR`, ESC/RPM evidence where available.
- Logging parameters to review: `LOG_BITMASK`, `LOG_FILE_RATEMAX`,
  `INS_RAW_LOG_OPT`, `INS_LOG_BAT_MASK`, `INS_LOG_BAT_OPT`, `INS_GYRO_FILTER`,
  harmonic-notch parameters, and `LOG_DARM_RATEMAX` for bench tests.
- Suggested plots/signals: `VIBE` axes and clipping; `IMU`/`GYR`/`ACC`;
  FFT from raw or batch IMU; `RATE` tracking; PID `Dmod`; motor RPM if present.
- Safe test pattern: first inspect mechanics. If safe, capture a short steady
  hover or normal low-risk flight segment with raw IMU/filter logging. Confirm
  logging bandwidth before the test and check for dropouts afterward.
- Bench checks before flight: prop balance/damage, motor bearings, loose screws,
  flight-controller mounting foam/tape, wiring contact with FC, frame resonance,
  payload movement, and fan/bench vibration sources.
- Do not fly if: clipping rises rapidly, vibration is severe enough to affect
  attitude/position hold, hardware is loose/damaged, or the previous event was
  loss of control.
- Cleanup: clear `INS_RAW_LOG_OPT`, clear/reduce `INS_LOG_BAT_MASK` and
  `INS_LOG_BAT_OPT`, and restore logging rate limits.

### ekf_gps_issue

- Minimum required evidence: `GPS`.
- Strongly recommended evidence: `XKF1`, `XKF3`, `XKF4`, `MODE`.
- Useful optional context: `MSG`, `EV`, `ERR`, `GPA`, `GPS2`, `MAG`, `ATT`,
  `RATE`, `VIBE`, `BAT`, `POWR`, `BARO`, `CTUN`.
- Logging parameters to review: `LOG_BITMASK`, `EK3_LOG_LEVEL`, GPS/GPA logging,
  compass logging, `LOG_FILE_RATEMAX`, and `LOG_DISARMED` only for boot/pre-arm
  EKF or sensor-init issues.
- Suggested plots/signals: GPS fix/status/sats/HDOP/HAcc/VAcc; EKF position,
  velocity, yaw and height innovations/test ratios; mode timeline; `MAG`;
  vibration and power around navigation-mode errors.
- Safe test pattern: if the aircraft is already stable in Stabilize/AltHold,
  capture AltHold vs Loiter comparison only if safe. Use open sky, open area,
  conservative altitude, and abort at the first navigation drift or yaw-source
  anomaly. Do not test Auto until Loiter is behaving safely.
- Bench/ground checks before flight: GPS placement, antenna view, compass
  orientation and calibration, magnetic interference, vibration, power to GPS,
  EKF source parameters, GPS2 setup, and arming/pre-arm messages.
- Do not fly if: GPS/compass/EKF pre-arm or failsafe warnings persist, yaw source
  is unstable, Loiter previously drifted toward hazards, or Stabilize/AltHold
  control is not already reliable.
- Cleanup: disable disarmed logging if used for startup evidence.

### battery_power_issue

- Minimum required evidence: `BAT`.
- Strongly recommended evidence: `POWR`, `RCOU`/`RCO2`/`RCO3`.
- Useful optional context: `CTUN`, `RATE`, `RCIN`, `ESC`, `ESCX`, `EDT2`, `ERR`,
  `EV`, `MSG`, `VIBE`, `ATT`.
- Logging parameters to review: `LOG_BITMASK`, battery and power logging,
  `LOG_FILE_RATEMAX`, ESC telemetry configuration, `LOG_DISARMED` for bench load
  checks only when useful.
- Suggested plots/signals: battery voltage/current/remaining capacity, board
  Vcc/flags, throttle demand, motor outputs, ESC current/voltage/temp, altitude
  response, log end timing.
- Safe test pattern: bench inspection and battery/load checks first. Only after
  power integrity is confirmed should a short low-altitude hover be considered;
  avoid high-current manoeuvres during evidence gathering.
- Bench checks before flight: battery health/internal resistance, connector fit,
  solder joints, power module, BEC/regulator, voltage calibration, current
  calibration, ESC power leads, and brownout/reset evidence.
- Do not fly if: brownout, severe voltage sag, connector heating, intermittent
  power, board Vcc faults, battery damage, or unexplained log termination is
  suspected.
- Cleanup: disable disarmed logging after bench capture if enabled.

### altitude_throttle_issue

- Minimum required evidence: no single message is mandatory, but `CTUN` is the
  key strongly recommended message for Copter altitude/throttle diagnosis.
- Strongly recommended evidence: `CTUN`.
- Useful optional context: `ATT`, `RATE`, `BAT`, `POWR`, `VIBE`, `BARO`, `RNGF`,
  `GPS`, `XKF4`, `ESC`, `ESCX`, `EDT2`, `RCOU`/`RCO2`/`RCO3`, `MODE`.
- Logging parameters to review: `LOG_BITMASK`, control/navigation tuning logging,
  barometer/rangefinder logging, `LOG_FILE_RATEMAX`, `EK3_LOG_LEVEL`.
- Suggested plots/signals: desired vs actual altitude, throttle output/demand,
  climb/descent rate, `BARO` altitude/pressure, `RNGF`, GPS altitude, EKF height
  innovation, battery sag, motor outputs, vibration.
- Safe test pattern: after power, propulsion, rangefinder/baro, and vibration
  checks, capture stable hover plus small gentle altitude changes in AltHold.
  Compare altitude modes only if basic thrust authority is healthy.
- Bench checks before flight: prop/motor thrust, battery condition, barometer
  foam/airflow, rangefinder mounting/health, vibration, payload/CG, throttle
  calibration, and altitude-controller parameter sanity.
- Do not fly if: thrust loss, power sag, severe vibration, barometer/rangefinder
  fault, or uncontrolled climb/descent is suspected.
- Cleanup: restore any high-rate logging used for vibration/height evidence.

### crash_or_loss_of_control

- Minimum required evidence: no single message is mandatory because any available
  timeline can be useful.
- Strongly recommended evidence: `ATT`, `RATE`, `RCOU`/`RCO2`/`RCO3`, `MODE`,
  plus `PARM`.
- Useful optional context: `EV`, `ERR`, `MSG`, `BAT`, `GPS`, `XKF4`, `VIBE`,
  `PIDR`, `PIDP`, `PIDY`, `ESC`, `ESCX`, `EDT2`, `RCIN`, `CTUN`, `POWR`, `MAG`.
- Logging parameters to review: full normal logging via `LOG_BITMASK`, actuator
  output logging, power/battery logging, EKF logging, and `LOG_DISARMED` only for
  post-repair bench/pre-arm evidence.
- Suggested plots/signals: mode/event/error timeline; RC input; attitude/rate;
  motor outputs; battery/board power; ESC telemetry; EKF/GPS; vibration/clipping;
  altitude/throttle.
- Safe test pattern: no repeat flight as a diagnostic shortcut. Start with
  parameter dump, hardware inspection, repair evidence, and ground tests. A
  controlled flight is a separate post-repair validation step, not the first
  evidence-gathering activity.
- Bench checks before flight: full airframe inspection, props, motors, ESCs,
  wiring, solder joints, connectors, battery, power module, flight controller
  mount, sensor orientation, compass/GPS, failsafe settings, arming checks, and
  parameter diff against known-good setup if available.
- Do not fly if: the cause of loss of control is not understood or mitigated,
  any structural/power/propulsion fault remains, failsafe/arming warnings exist,
  or the pilot cannot perform a controlled abort.
- Cleanup: if using `LOG_DISARMED` for repair validation, disable it after the
  bench/startup capture.

### general_investigation

- Minimum required evidence: none; start by inventorying what is present.
- Strongly recommended evidence: `ATT`, `RATE`.
- Useful optional context: `RCOU`/`RCO2`/`RCO3`, `MODE`, `MSG`, `EV`, `ERR`,
  `RCIN`, `PIDR`, `PIDP`, `PIDY`, `VIBE`, `BAT`, `POWR`, `GPS`, `XKF4`, `ESC`,
  `ESCX`, `EDT2`, `CTUN`, `BARO`, `MAG`, `PARM`.
- Logging parameters to review: `LOG_BITMASK`, `LOG_BACKEND_TYPE`,
  `LOG_FILE_RATEMAX`, `LOG_BLK_RATEMAX`, `LOG_DARM_RATEMAX`, `LOG_DISARMED`,
  and symptom-specific settings from the sections above.
- Suggested plots/signals: mode timeline, attitude/rate, motor outputs, power,
  vibration, GPS/EKF, RC input, altitude/throttle if relevant.
- Safe test pattern: first ask the user to identify the symptom, phase of flight,
  mode, and timestamp. If no safety-critical issue is suspected, a normal
  low-risk hover or short conservative flight may collect broad baseline data,
  but only after preflight and mechanical checks.
- Bench checks before flight: general preflight, prop/motor/frame inspection,
  battery and connector health, sensor mounting, parameter dump, and logging
  configuration.
- Do not fly if: any safety-critical symptom is present but not yet classified,
  the current log ended unexpectedly, failsafe/arming warnings are unresolved, or
  hardware condition is unknown after a hard landing.
- Cleanup: return any temporary logging changes to the normal profile.

## Example next-capture wording

- "The current log is missing `PIDY` and actuator outputs, so yaw confidence is
  limited. After motor/prop/output-mapping checks, capture 30-60 seconds of
  stable hover plus small roll/pitch/yaw inputs with `ATT`, `RATE`, `PIDY`,
  `RCOU`/`RCO2`/`RCO3`, `RCIN`, `BAT`, `POWR`, `VIBE`, `MODE`, `MSG`, `EV`, and
  `ERR` present."
- "Capture Loiter and AltHold comparison only if the aircraft is already stable
  and controllable in non-navigation modes; abort on drift, yaw-source warnings,
  or EKF/GPS failsafe indications."
- "Capture a short raw IMU/filter review flight only after confirming logging
  bandwidth. Check `DSF`/`DMS` dropouts afterward and disable raw/batch logging
  when the filter evidence has been collected."
- "Enable `LOG_DISARMED` only for boot, pre-arm, arming-failure, or bench
  startup evidence. Warn about large logs and turn it off again after capture."
