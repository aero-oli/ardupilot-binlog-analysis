# Plot catalog

## Standard plots

1. `00_flight_overview.html` — altitude/throttle, battery, vibration, motor outputs.
2. `02_attitude_tracking_roll_pitch_yaw.html` — ATT desired vs achieved attitude.
3. `03_rate_tracking_roll_pitch_yaw.html` — RATE desired vs achieved rates and controller outputs.
4. `04_pidr_terms.html`, `04_pidp_terms.html`, `04_pidy_terms.html` — PID terms and flags.
5. `05_motor_outputs_rcout.html` — mapped motor output channels from RCOU/RCO2/RCO3.
6. `06_esc_telemetry.html` — ESC RPM/current/voltage/error if present.
7. `06b_escx_extended_telemetry.html` — ESCX input duty, output duty, power percentage, and flags if present.
8. `07_battery_voltage_current_sag.html` — battery voltage/current/capacity.
9. `08_vibration_vibe_imu.html` — VIBE axes and clipping.
10. `09_ekf_gps_health.html` — GPS quality and EKF test ratios.
11. `10_autotune_atun.html` — AutoTune progress if present.
12. `11_fft_noise_spectrum.html` — FFT spectrum if raw/high-rate IMU or ISBH/ISBD batch-sampler data exists.

## Custom plots

Use `scripts/ap_log_custom_plot.py` when the user asks for a specific graph that is not in the standard catalog. It reads extracted tables and accepts repeated `--series MESSAGE.FIELD` arguments. For open-ended graph requests, first extract with `--messages ALL` so uncommon messages are available. Use `--secondary MESSAGE.FIELD` for a right y-axis when units differ. Use `--events` to overlay MODE/ERR/EV/MSG markers and `--window START:END` to plot a specific segment.

Example:

```bash
python scripts/ap_log_custom_plot.py --tables out/tables --series GPS.Alt --series BARO.Press --secondary BARO.Press --title "GPS altitude and barometric pressure" --out out/plots/gps_altitude_pressure.html
```

Derived expression example:

```bash
python scripts/ap_log_custom_plot.py --tables out/tables --series 'GPS.Alt-BARO.Alt=GPS minus baro' --align-tolerance 0.25 --events --out out/plots/gps_minus_baro.html
```

Use `--align-tolerance SECONDS` for derived expressions when messages are logged at different rates; the manifest reports rows dropped because no operand was close enough in time.

Mode segment discovery:

```bash
python scripts/ap_log_segments.py --tables out/tables --json out/segments.json --summary out/segments.md
```

## Symptom-led yaw plots

1. `yaw_attitude_desired_vs_actual.html`
2. `yaw_rate_desired_vs_actual.html`
3. `yaw_pid_terms.html`
4. `motor_outputs_during_yaw_error.html`
5. `rcin_yaw_rate_command_response.html` when `RCIN` is available.

## RCIN command-response plots

- Yaw: `RCIN` yaw channel with `RATE.YDes` and `RATE.Y`.
- Roll: `RCIN` roll channel with `ATT.DesRoll` and `ATT.Roll`.
- Pitch: `RCIN` pitch channel with `ATT.DesPitch` and `ATT.Pitch`.
- Throttle/power: `RCIN` throttle channel with `CTUN.ThO`, `BAT.Curr`, and `BAT.Volt`.

Use `RCMAP_*` parameters when available. If they are absent, label the plot/diagnosis as using the default ArduPilot channel order assumption.

Generate only plots supported by data present in the log.
