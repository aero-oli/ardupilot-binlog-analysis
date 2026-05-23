# Safety rules for ArduPilot log analysis

1. A log analysis must never be treated as flight clearance.
2. Safety-critical issues come before tuning optimisation.
3. Separate evidence from interpretation.
4. Use confidence levels: high, medium, low.
5. Do not recommend disabling safety, arming, EKF, GPS, compass, battery, radio, fence or failsafe protections as a routine fix.
6. Do not recommend aggressive PID changes from one log.
7. If a finding involves possible motor output saturation, ESC fault, prop/motor direction, GPS/EKF, compass/yaw-source, vibration/clipping, brownout, failsafe or loss of control, classify it as safety-critical until disproven.
8. If required messages are missing, state what cannot be concluded.
9. Distinguish bench-only actions, restrained ground tests and flight tests.
10. Treat every aircraft as mission-, frame-, battery-, prop-, payload-, firmware- and hardware-specific.

Official anchors:

- ArduPilot DataFlash logs are onboard logs, distinct from telemetry logs.
- DataFlash logs are self-describing through message/field definitions and should be interpreted by the actual fields present.
- ATT, RATE, PID*, RCOU/RCO2/RCO3, ESC, VIBE, GPS and XKF* messages have official meanings in ArduPilot log documentation.
- GPS/EKF/failsafe diagnosis should use the official diagnosing-logs guidance before community sources.
