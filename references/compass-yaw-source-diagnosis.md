# Compass / Yaw-Source Diagnosis

Use this guide when the user reports compass interference, bad heading, heading
jumps, GPS yaw, or moving-baseline yaw problems. This class reuses the existing
compass/yaw-source helpers; it is a routing and evidence guide, not a separate
automatic conclusion engine.

1. Start with timing.
   - Use `MODE`, `MSG`, `ERR`, and `EV` to locate compass, yaw-source, EKF, or
     navigation warnings.
   - Compare those times with the user-reported heading jump or navigation
     symptom.

2. Inspect attitude and yaw-source evidence.
   - Use `ATT` and `RATE` to decide whether the aircraft physically yawed or
     whether the estimate/heading changed without matching rate evidence.
   - Use `MAG`, `XKF3`, and `XKF4` for magnetic field, yaw innovation, and
     test-ratio context.
   - In AUTO/mission complaints, compare `RATE.YDes` with `RATE.Y`; continuous
     mission yaw demand can come from `WP_YAW_BEHAVIOR`, waypoint geometry, and
     yaw-rate/acceleration limits rather than compass error alone.

3. Check GPS yaw / moving-baseline context when relevant.
   - Review `EK3_SRC1_YAW`, `GPS_TYPE`, `GPS_TYPE2`, and `GPS_AUTO_SWITCH`.
   - Use `GPS`, `GPS2`, and `GPA` as supporting evidence for GPS health. Do not
     assume GPS yaw is present from parameter names alone.

4. Keep supporting causes visible.
   - Check `VIBE`, `BAT`, `POWR`, and motor outputs if the heading problem
     coincides with vibration, current draw, or output saturation.
   - RC input can distinguish commanded yaw from estimator/yaw-source behaviour.
   - Treat `WP_YAW_BEHAVIOR`, `ATC_RATE_Y_MAX`, `ATC_ACCEL_Y_MAX`, and
     `MOT_YAW_HEADROOM` as context only; do not infer a parameter fault without
     matching log evidence.

5. State confidence limits.
   - Missing `MAG`, `XKF3`, `XKF4`, `ATT`, `RATE`, or `MODE` prevents a strong
     compass/yaw-source conclusion.
   - Do not infer magnetic interference from compass data alone; require timing,
     correlation, estimator evidence, or clear warnings.

Do not recommend disabling compass, EKF, GPS, arming, or yaw-source checks as a
routine fix. If the current log is insufficient, use
`references/evidence-gathering-flights.md` and
`references/logging-configuration-for-investigation.md` to plan the lowest-risk
next evidence capture.
