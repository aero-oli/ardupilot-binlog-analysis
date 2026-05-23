# Crash or loss-of-control diagnosis

Prioritise timeline and safety-critical evidence over tuning.

## Order of operations

1. Build timeline from MSG, EV, ERR, ARM and MODE.
2. Determine if loss of control was preceded by failsafe, EKF/GPS/compass error, battery/power event, motor output saturation, vibration/clipping or RC loss.
3. Compare ATT desired vs achieved and RATE desired vs achieved.
4. Check mapped RCOU/RCO2/RCO3 outputs and ESC telemetry for actuator failure/saturation.
5. Check BAT/POWR and VIBE.
6. State what cannot be concluded.

## Rules

- Do not blame tune unless actuator, estimator, power and mechanical issues have been checked.
- If the log ends in-air, treat power/logging failure as safety-critical until disproven.
- If motor output or ESC telemetry indicates asymmetry, treat as safety-critical.
