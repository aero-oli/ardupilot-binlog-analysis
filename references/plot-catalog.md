# Plot catalog

## Standard plots

1. `00_flight_overview.html` — altitude/throttle, battery, vibration, motor outputs.
2. `02_attitude_tracking_roll_pitch_yaw.html` — ATT desired vs achieved attitude.
3. `03_rate_tracking_roll_pitch_yaw.html` — RATE desired vs achieved rates and controller outputs.
4. `04_pidr_terms.html`, `04_pidp_terms.html`, `04_pidy_terms.html` — PID terms and flags.
5. `05_motor_outputs_rcou.html` — RCOU output channels.
6. `06_esc_telemetry.html` — ESC RPM/current/voltage/error if present.
7. `06b_escx_extended_telemetry.html` — ESCX input duty, output duty, power percentage, and flags if present.
8. `07_battery_voltage_current_sag.html` — battery voltage/current/capacity.
9. `08_vibration_vibe_imu.html` — VIBE axes and clipping.
10. `09_ekf_gps_health.html` — GPS quality and EKF test ratios.
11. `10_autotune_atun.html` — AutoTune progress if present.
12. `11_fft_noise_spectrum.html` — FFT spectrum if raw/high-rate IMU or ISBH/ISBD batch-sampler data exists.

## Custom plots

Use `scripts/ap_log_custom_plot.py` when the user asks for a specific graph that is not in the standard catalog. It reads extracted tables and accepts repeated `--series MESSAGE.FIELD` arguments. Use `--secondary MESSAGE.FIELD` for a right y-axis when units differ. Use `--events` to overlay MODE/ERR/EV/MSG markers and `--window START:END` to plot a specific segment.

Example:

```bash
python scripts/ap_log_custom_plot.py --tables out/tables --series GPS.Alt --series BARO.Press --secondary BARO.Press --title "GPS altitude and barometric pressure" --out out/plots/gps_altitude_pressure.html
```

Derived expression example:

```bash
python scripts/ap_log_custom_plot.py --tables out/tables --series 'GPS.Alt-BARO.Alt=GPS minus baro' --events --out out/plots/gps_minus_baro.html
```

Mode segment discovery:

```bash
python scripts/ap_log_segments.py --tables out/tables --json out/segments.json --summary out/segments.md
```

## Symptom-led yaw plots

1. `yaw_attitude_desired_vs_actual.html`
2. `yaw_rate_desired_vs_actual.html`
3. `yaw_pid_terms.html`
4. `motor_outputs_during_yaw_error.html`

Generate only plots supported by data present in the log.
