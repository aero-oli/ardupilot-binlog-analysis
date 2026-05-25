# Final Answer Patterns

Use this reference when a safety-relevant diagnosis needs a stronger action plan. These are patterns for the agent-written final answer, not templates for automatic report generation.

## Safety-Relevant Next Steps

When the finding affects loss of control, yaw/attitude authority, motor/ESC behaviour, vibration, GPS/EKF, compass/yaw source, power, failsafes, or other flight safety, include a `Recommended next steps` section with this order:

1. Immediate safety gate.
   State the conservative limit first. Use the narrowest safe activity supported by the evidence: normal analysis only, no AUTO/mission flying, controlled hover only, ground test only, bench only, or do not fly until checked.
2. Bench/hardware checks.
   Name the specific physical checks that could confirm or clear the likely fault.
3. Configuration/logging checks.
   Name the parameters, log messages, or telemetry streams that are needed for confidence.
4. Controlled evidence-gathering activity, only if safe.
   Describe the shortest low-risk activity that could collect the missing evidence after the earlier checks pass.
5. Reanalysis step.
   Tell the user to reanalyse the new log or parameter dump before tuning or returning to higher-risk operation.
6. What not to do.
   Call out unsafe shortcuts and blind changes that the evidence does not justify.

Do not stop at missing evidence limits confidence. If evidence is missing, state what to collect, how to collect it safely, and what must be checked first.

Never declare the aircraft safe to fly from logs alone. Never recommend disabling arming, EKF, GPS, battery, compass, radio, fence, or other failsafe protections as a routine fix. Do not recommend blind parameter changes; tie every suggested configuration review to observed evidence or a clearly stated missing-evidence question.

## Mission/Yaw/Wobble Example

For a mission-flight yaw wobble, yaw authority concern, or unstable AUTO behaviour:

1. Immediate safety gate.
   Pause AUTO/mission flying. Do not resume mission work from this log alone. If the issue involved strong wobble, yaw divergence, saturation, failsafe warnings, or unclear control authority, keep the aircraft to bench or ground checks until the physical and configuration checks below are complete. If checks pass and no hard safety fault remains, the next flight evidence should be a short controlled hover only, not another mission.
2. Bench/hardware checks.
   Inspect props, motor order/direction, motor bearings, ESC connections, frame stiffness, arm twist, flight-controller mounting, vibration isolation, and wiring near compass/GPS and power paths.
3. Configuration/logging checks.
   Investigate compass/GPS yaw source behaviour, battery failsafe warnings, radio failsafe warnings, EKF/GPS messages, mode changes, and yaw-related parameters only as context. Improve logging so the next capture includes PIDY, PIDR, PIDP, RATE/ATT, RCOU, RCIN, BAT, POWR, GPS/GPA, compass/yaw-source messages, vibration, and ESC telemetry if the hardware supports it.
4. Controlled evidence-gathering activity, only if safe.
   After bench and configuration checks pass, capture a short controlled hover in a clear area with a ready abort plan. Keep it brief and do not use AUTO/mission mode for the diagnostic capture.
5. Reanalysis step.
   Reanalyse the new hover log and parameter context before tuning. Confirm whether yaw demand, estimator/yaw source, motor/ESC authority, vibration, or failsafe timing is actually supported by the evidence.
6. What not to do.
   Do not repeat the mission to see if it happens again. Do not tune yaw/attitude gains blindly. Do not disable arming checks, EKF/GPS/compass checks, battery failsafe, or radio failsafe as a routine fix.
