# EKF/GPS diagnosis

Use this for Loiter drift, toilet bowling, GPS glitches, EKF errors, position jumps, yaw-source issues and mode-change failures.

## Checks

- GPS NSats, HDop/HAcc/VAcc, status/fix type.
- XKF3 innovations: velocity, position, height, magnetic/yaw innovations where available.
- XKF4 test ratios: values over 1 indicate rejection by the relevant innovation gate.
- MSG/ERR/EV for GPS glitch, EKF failsafe, mode-change failures.
- Compare modes: if bad only in Loiter/Auto/RTL, position/estimator evidence matters more.
- If yaw or heading problems occur mainly in AUTO/mission, compare `RATE.YDes`
  vs `RATE.Y` and inspect `WP_YAW_BEHAVIOR`, waypoint geometry, `WPNAV_SPEED`,
  `WPNAV_ACCEL`, and `WPNAV_ACCEL_C` as navigation-demand context before treating
  the symptom as only a yaw tune problem.
- Check VIBE and power because estimator issues may be secondary.

## Interpretation

- Sudden GPS quality drop plus aggressive correction in autonomous modes points toward GPS glitch.
- Magnetic/yaw innovation problems plus heading symptoms point toward compass/yaw-source issues.
- EKF test-ratio spikes should be correlated with the symptom time, not interpreted in isolation.
- Mission-only yaw demand should be separated from estimator drift: high
  commanded yaw rate in AUTO can be expected from mission/nav behaviour, while
  poor tracking or yaw-source innovation evidence needs separate confirmation.
