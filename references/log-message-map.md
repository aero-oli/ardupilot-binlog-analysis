# ArduPilot log message map

| Area | Primary messages | Practical use |
|---|---|---|
| Attitude | ATT | Desired vs achieved vehicle roll, pitch and yaw. |
| Rates | RATE | Desired vs achieved angular rates and normalized controller outputs. |
| PID | PIDR, PIDP, PIDY, PIDA, PIDN, PIDE | Target, actual, error, P/I/D/FF terms, Dmod, slew rate and flags. |
| Motors/servos | RCOU, RCO2, RCO3, PARM | Servo/motor channel outputs. Use `SERVOx_FUNCTION` from PARM to identify motor channels before assigning motor meaning. |
| ESC telemetry | ESC, ESCX, EDT2 | RPM, voltage, current, temperature, motor temperature, ESC error rate and extended DShot status where provided. |
| Battery | BAT, BCL | Voltage, current, used capacity, resistance, remaining percentage where available. |
| Board power | POWR | VCC and board power health. |
| Vibration | VIBE, IMU, ACC, GYR, ISBH, ISBD | Processed vibration, clipping, raw/high-rate IMU sources and batch-sampler sources for FFT. |
| GPS | GPS, GPA, GPS2 | Fix/status, satellite count, HDOP/HAcc/VAcc and GPS health. |
| EKF | XKF1, XKF2, XKF3, XKF4, XKFS, NKF* | EKF state, innovations, test ratios, core health. |
| Events | MSG, EV, ERR, ARM, MODE | Flight timeline, arming, mode changes, failsafes, errors. |
| AutoTune | ATUN | Axis, tuning step, target/min/max and generated gains. |
| System ID | SID, SIDD, SIDS | Frequency response / system identification analysis. |

Critical field meanings used by this skill:

- ATT.DesYaw vs ATT.Yaw: desired vs achieved heading.
- RATE.YDes vs RATE.Y and RATE.YOut: yaw rate target, achieved yaw rate and normalized yaw output.
- PIDY.Flags bit 1: output saturated / I-term anti-windup active.
- RCOU/RCO2/RCO3 `C*`: output PWM-like values; use `SERVOx_FUNCTION` output mapping before assigning motors.
- Copter motor functions are Motor1-Motor8 at `33-40` and Motor9-Motor12 at `82-85`; tilt functions such as `41`, `45-47`, and `75-76` are not normal motor outputs.
- XKF4 SV/SP/SH/SM: squared innovation test ratios; values >1 indicate rejection under the relevant innovation gate.
