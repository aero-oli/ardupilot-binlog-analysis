# Logging configuration for investigation

Use this reference when a log does not contain the messages needed to answer a
diagnostic question, or when the user asks what to capture on a future flight or
bench test. It is not a reason to fly an unsafe aircraft. Recommend only the
minimum extra logging needed for the question, and tell the user to restore
high-volume logging after the diagnostic capture.

## Baseline checks

- Confirm the log is an onboard DataFlash log, not only a telemetry log. ArduPilot
  records DataFlash logs on the autopilot, often to the SD card, and telemetry
  logs at the ground station.
- Review `LOG_BITMASK`. ArduPilot's Copter parameter docs describe the common
  baseline as enabling the basic log types with `65535`; relevant bits include
  attitude, GPS, control tuning, navigation tuning, RC input, IMU, battery,
  RC output, PID, compass, motors, fast IMU, raw IMU, and notch logging.
  `scripts/ap_param_lookup.py` decodes bitmask metadata when available and can
  explain likely missing message families, but bit definitions may vary by
  vehicle and firmware.
- Review `LOG_BACKEND_TYPE` so the intended file/block backend is enabled.
  For diagnostic .BIN capture, the file backend is normally the useful target.
- Review `LOG_FILE_RATEMAX`, `LOG_BLK_RATEMAX`, and `LOG_DARM_RATEMAX`.
  Rate limits can reduce log volume, but can also remove high-rate evidence
  needed for rate, vibration, FFT, or short transient analysis.
- Use `LOG_DISARMED` only when pre-arm, boot, arming-failure, startup EKF,
  sensor init, or bench-run evidence is needed. It can create very large logs.
- After capture, inspect logging health: `DSF` / `DMS` drop counts, timestamp
  gaps/resets, missing core messages after arming, and script
  `logging_health`. Missing evidence is not evidence that a fault did not occur.

## Investigation message sets

| Investigation | Messages to ask for in a future capture | Logging notes |
|---|---|---|
| General health / safety review | `MODE`, `MSG`, `EV`, `ERR`, `ARM`, `ATT`, `RATE`, `RCOU`/`RCO2`/`RCO3`, `RCIN`, `BAT`, `POWR`, `GPS`/`GPS2`/`GPA`, `XKF1`/`XKF3`/`XKF4` or `NKF*`, `MAG`, `VIBE`, `IMU`, `BARO`, `CTUN` | Use broad normal logging first. Add high-rate logging only for a specific symptom. |
| Yaw / attitude / rate tracking | `ATT`, `RATE`, `PIDY`, `PIDR`, `PIDP`, `RCOU`/`RCO2`/`RCO3`, `RCIN`, `MODE`, `MSG`, `EV`, `ERR`, plus `BAT`/`POWR`, `ESC`/`ESCX`/`EDT2`, `MAG`, `XKF3`, `XKF4`, `VIBE` | Need desired vs actual attitude/rate, PID flags, outputs, pilot inputs, estimator context, and power/motor limits. |
| Roll/pitch tuning | `ATT`, `RATE`, `PIDR`, `PIDP`, `RCOU`/`RCO2`/`RCO3`, `RCIN`, `VIBE`, `IMU`, `BAT`, `MODE` | Control tuning and PID logging must be present; correlate tracking error with output saturation and vibration. |
| Motor / ESC / actuator-output issues | `RCOU`, `RCO2`, `RCO3`, `PARM`, `ESC`, `ESCX`, `EDT2`, `BAT`, `POWR`, `RATE`, `ATT`, `MODE`, `MSG`, `EV`, `ERR` | `PARM`/`SERVOx_FUNCTION` is needed to map outputs to motors. ESC telemetry must be configured before it can appear in logs. |
| Battery / board-power issues | `BAT`, `POWR`, `RCOU`/`RCO2`/`RCO3`, `CTUN`, `RATE`, `ATT`, `MODE`, `MSG`, `EV`, `ERR`, optional `ESC`/`ESCX` current/voltage/temperature | Capture the high-load part of the event. Do not repeat a flight with suspected power failure until bench checks are complete. |
| GPS / EKF / Loiter / navigation issues | `GPS`, `GPS2`, `GPA`, `XKF1`, `XKF3`, `XKF4`, `XKFS` or `NKF*`, `MAG`, `BARO`, `CTUN`, `ATT`, `RATE`, `MODE`, `MSG`, `EV`, `ERR` | Keep `EK3_LOG_LEVEL` at a level that does not suppress needed EKF streaming logs; value `0` is the full logging mode in current Copter docs. |
| Compass / yaw-source issues | `MAG`, `XKF3`, `XKF4`, `XKF1` or `NKF*`, `ATT`, `RATE`, `PIDY`, `GPS`/`GPA`, `MODE`, `MSG`, `EV`, `ERR`, `PARM` | Need magnetic field, yaw innovations/test ratios, yaw-source messages, mode timeline, and relevant compass/EKF parameters. |
| Vibration / filter / FFT investigations | `VIBE`, `IMU`, `GYR`, `ACC`, `ISBH`, `ISBD`, `RATE`, `PIDR`, `PIDP`, `PIDY`, `ATT`, optional `ESC`/RPM evidence | Start with `VIBE` and `IMU`. Use raw IMU or batch sampling only for a planned filter/FFT capture, then disable it again. |
| Altitude / throttle / barometer / rangefinder issues | `CTUN`, `BARO`, `RNGF`, `ATT`, `RATE`, `RCOU`/`RCO2`/`RCO3`, `BAT`, `POWR`, `GPS`/`GPA`, `XKF1`/`XKF4`, `MODE`, `MSG`, `EV`, `ERR` | Need desired/actual altitude, throttle output, baro/rangefinder/GPS altitude context, estimator innovation context, and power limits. |
| AutoTune review | `ATUN`, `ATDE`, `ATT`, `RATE`, `PIDR`, `PIDP`, `PIDY`, `RCOU`/`RCO2`/`RCO3`, `VIBE`, `BAT`, `MODE`, `MSG`, `EV` | AutoTune messages explain tuning step/axis/gain evolution; still check saturation, vibration, battery state, and whether gains were saved. |
| System ID review | `SID`, `SIDD`, `SIDS`, `ATT`, `RATE`, `RCOU`/`RCO2`/`RCO3`, `VIBE`, `BAT`, `MODE` | System ID logs are useful only if the excitation was actually run and the relevant axes/messages are present. |
| Pre-arm / boot / arming-failure investigations | `MSG`, `ERR`, `EV`, `ARM`, `MODE`, `POWR`, `BAT`, `GPS`/`GPA`, `MAG`, `BARO`, `IMU`, `XKF*`/`NKF*`, `PARM` | Use `LOG_DISARMED` for this targeted capture. Disable disarmed logging again after the bench/startup evidence is collected. |

## High-rate logging and FFT evidence

- `INS_RAW_LOG_OPT` enables raw IMU logging when any bits are set. ArduPilot's
  raw IMU FFT guide warns that this can produce very large logs and logging
  dropouts, especially on slower autopilots. Prefer the narrowest option that
  answers the question, commonly primary gyro pre/post filter evidence for
  filter review.
- `INS_LOG_BAT_MASK` selects which IMUs are batch logged and may require reboot;
  `INS_LOG_BAT_OPT` controls batch sampler options such as sensor-rate,
  post-filter, or pre/post-filter sampling. These are for planned vibration /
  FFT captures, not routine logging.
- Raw IMU, batch sampling, fast IMU, fast harmonic-notch logging, and aggressive
  rate-limit changes can create huge logs or missed log entries. After capture,
  check `DSF`/`DMS`, timestamp gaps, and whether the required messages are
  continuous over the event window.
- High-volume settings should normally be returned to normal after the evidence
  flight or bench test: clear `INS_RAW_LOG_OPT`, clear or reduce batch sampling
  masks/options, and restore ordinary rate limits/logging profile.

## Parameter review checklist

- `LOG_BITMASK`: Does it include the message families needed for the symptom?
  If `PIDY`/`PIDR`/`PIDP` are missing and the PID logging bit appears absent,
  report that PID logging may not have been enabled. Do not claim this
  definitely explains missing messages without considering firmware, rate
  limits, log damage, and logging health.
- `LOG_BACKEND_TYPE`: Is the intended onboard backend enabled?
- `LOG_DISARMED`: Is disarmed/startup logging needed for this investigation, and
  has the user been warned about log size?
- `LOG_FILE_RATEMAX`, `LOG_DARM_RATEMAX`, `LOG_BLK_RATEMAX`: Could rate limiting
  hide the transient, or could unrestricted high-rate logging overload storage?
- `INS_RAW_LOG_OPT`: Only for raw IMU/filter evidence; disable again afterward.
- `INS_LOG_BAT_MASK`, `INS_LOG_BAT_OPT`: Only for batch-sampler FFT evidence;
  review reboot requirements and disable again afterward.
- `EK3_LOG_LEVEL`: For EKF/GPS/compass/yaw-source investigations, make sure EKF
  streaming logging has not been reduced so far that `XKF*` evidence is missing.

## Do not do this

- Do not disable arming checks, EKF checks, GPS checks, battery failsafes,
  compass checks, or other safety checks just to capture logs.
- Do not intentionally reproduce a dangerous loss-of-control event.
- Do not fly a vehicle suspected of serious motor, ESC, prop, frame, battery, or
  board-power failure until bench checks and controlled ground tests are complete.
- Do not treat missing messages, missing fields, or log dropouts as evidence that
  the reported issue did not happen.

## Official references

- ArduPilot Copter logging overview: <https://ardupilot.org/copter/docs/common-logs.html>
- ArduPilot Copter log message reference: <https://ardupilot.org/copter/docs/logmessages.html>
- ArduPilot Copter parameter reference: <https://ardupilot.org/copter/docs/parameters.html>
- Raw IMU logging for FFT analysis: <https://ardupilot.org/copter/docs/common-raw-imu-logging.html>
- Measuring vibration: <https://ardupilot.org/copter/docs/common-measuring-vibration.html>
