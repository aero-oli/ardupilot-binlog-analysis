# Barometer / Rangefinder Altitude Diagnosis

Use this guide when the user reports barometer drift, rangefinder issues, lidar
altitude jumps, terrain altitude problems, or altitude-estimate jumps. This class
reuses altitude/throttle, EKF, vibration, power, and actuator checks.

1. Start with altitude estimate timing.
   - Use `CTUN` for desired altitude, actual altitude, throttle output, and climb
     context.
   - Use `BARO` and `RNGF` when present to compare barometer/rangefinder changes
     against the altitude-control response.

2. Separate control error from sensor/estimator error.
   - If `CTUN.DAlt` and `CTUN.Alt` diverge with high throttle or output
     saturation, inspect thrust, power, and controller behaviour first.
   - If `BARO`, `RNGF`, GPS altitude, or `XKF4` height evidence jumps before the
     control response, prioritize sensor and EKF height-source evidence.

3. Check related contributors.
   - Use `VIBE` for vibration/clipping that can affect height estimates.
   - Use `BAT`, `POWR`, and `RCOU`/`RCO2`/`RCO3` for power or thrust limitation.
   - Use `MODE`, `MSG`, `ERR`, and `EV` for terrain, rangefinder, EKF, or sensor
     warnings.

4. Review relevant parameters as context.
   - `RNGFND1_TYPE`, `RNGFND1_ORIENT`, `RNGFND1_MIN_CM`, `RNGFND1_MAX_CM`
   - `EK3_SRC1_POSZ`, `EK3_OGN_HGT_MASK`
   - `PSC_POSZ_P`, `PSC_VELZ_P`, `PSC_ACCZ_P`

5. State confidence limits.
   - Missing `CTUN` prevents strong altitude-control interpretation.
   - Missing `BARO` prevents a strong barometer conclusion.
   - Missing `RNGF` limits rangefinder/terrain claims; missing `XKF4`, `GPS`,
     `VIBE`, `BAT`, `POWR`, or actuator outputs limits supporting context.

Do not suggest aggressive altitude testing. If more evidence is needed, prefer
bench inspection and ground checks first, then only a controlled hover or small
AltHold altitude change when propulsion, power, sensors, and mechanical setup are
already healthy.
