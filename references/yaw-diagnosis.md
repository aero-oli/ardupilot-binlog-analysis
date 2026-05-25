# Yaw diagnosis workflow

Use this when the user reports yaw, heading, spinning, pirouetting, toilet-bowling or heading-hold problems.

## Fault tree

1. Is yaw commanded?
   - Check RCIN yaw if present.
   - Check ATT.DesYaw and RATE.YDes.
   - If desired yaw changes first, the yaw is commanded by pilot/autopilot.
   - If achieved yaw changes while desired yaw does not, suspect estimator, motor/ESC/mechanical issue or disturbance.

2. Can the yaw controller track the target?
   - Check RATE.YDes vs RATE.Y.
   - Check RATE.YOut.
   - High yaw error with high YOut means the controller is asking for correction but the aircraft is not responding enough.

3. Is the yaw PID limiting?
   - Check PIDY.Err, P, I, D, FF, DFF, Dmod, SRate, Flags.
   - PIDY.Flags bit 1 means output saturated / I-term anti-windup active.
   - Dmod reduction suggests dynamic D reduction due to limit-cycle/noise protection.

4. Is there actuator authority?
   - Check mapped output channels from RCOU/RCO2/RCO3 for saturation/asymmetry.
   - Check ESC RPM/current/temp/error if present.
   - Correlate yaw error with high throttle, battery sag and motor outputs near limits.

5. Is this really heading estimation, not physical yaw?
   - Check MAG, XKF3 and XKF4.
   - Use measured MAG field components such as MagX/MagY/MagZ, MX/MY/MZ or Mx/My/Mz for magnetic field magnitude. Do not use compass offset fields OfsX/OfsY/OfsZ as measured field strength.
   - Check whether the issue appears in Loiter/Auto but not Stabilize/AltHold.
   - XKF4 magnetic/velocity/position/height ratios above 1 indicate innovation rejection.

6. Are vibration or power contributing?
   - Check VIBE and clipping.
   - Check BAT and POWR.
   - Use FFT only when raw/high-rate IMU data exists.

## AUTO / Mission Yaw Context

If the yaw complaint is mainly in `AUTO` or during a mission, do not treat it as
purely manual yaw PID tuning.

- Compare `RATE.YDes` vs `RATE.Y` in the mission segment. Large, persistent, or
  continuous `RATE.YDes` in `AUTO` may be mission/navigation yaw demand rather
  than a spontaneous aircraft fault.
- Inspect `WP_YAW_BEHAVIOR`, waypoint geometry, `WPNAV_SPEED`, `WPNAV_ACCEL`,
  `WPNAV_ACCEL_C`, `ATC_RATE_Y_MAX`, `ATC_ACCEL_Y_MAX`, and
  `MOT_YAW_HEADROOM` as context for what the autopilot was allowed or expected
  to command.
- Compare the same aircraft in AltHold/PosHold/Loiter/AUTO only if it is safe
  and controllable. A problem that exists only in mission flight needs mission
  demand, navigation behaviour, estimator health, and yaw authority checked
  together.
- Do not change mission, yaw-rate, yaw-acceleration, navigation, or motor
  headroom parameters blindly. Use log evidence, mission geometry, and ground
  checks to decide whether a parameter review is justified.

## Ranked causes

- Yaw authority limited: RATE.YDes/RATE.Y diverge, RATE.YOut high, PIDY limit and/or mapped output-channel saturation.
- Motor/ESC/prop/frame issue: one output/ESC abnormal or persistent yaw bias, especially with ESC RPM/current errors.
- Yaw tune oscillation: RATE.Y oscillates around target without actuator saturation; PIDY terms oscillatory.
- EKF/compass/yaw-source issue: heading/yaw estimate jumps or XKF/measured-MAG evidence abnormal without matching motor outputs.
- Vibration/noise issue: high VIBE/clipping or FFT peaks correlate with yaw-rate noise.
- Battery/throttle issue: yaw degrades at high throttle or during voltage sag/current peaks.

## Safety-critical checks before further flight

- Verify motor order, prop direction, frame class/type, motor mapping and yaw torque direction.
- Check ESC/motor health and wiring.
- Check compass orientation/interference and yaw-source parameters if EKF/MAG evidence appears.
- Do not increase yaw gains until actuator saturation, vibration and power are ruled out.
