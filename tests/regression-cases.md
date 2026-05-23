# Regression cases

Run these against representative real logs before relying on the skill operationally. Use `tests/real_log_fixture_check.sh /path/to/log.bin` for a non-committed local fixture run; it validates, indexes, extracts, derives segments, generates whole-log and windowed metrics, standard plots with events, and a GPS-altitude/barometric-pressure custom plot when the log contains `GPS` and `BARO`.

| Case | Expected behaviour |
|---|---|
| Good hover | No safety-critical issue from deterministic heuristics; standard plots generated. |
| Missing RATE | Tuning confidence low; final conclusions should say rate tracking cannot be assessed. |
| Missing ESC/ESCX/EDT2 | Report says ESC-level confirmation is impossible. |
| ESCX present | Metrics summarize `inpct`, `outpct`, `flags`, and `Pwr`; diagnosis/plots do not treat ESC telemetry as missing. |
| High vibration | VIBE finding raised; tuning confidence reduced. |
| GPS dropout | GPS/EKF finding raised with GPS quality evidence. |
| Yaw issue | Symptom class yaw_misbehaviour; yaw plots generated; causes ranked. |
| Motor saturation | Mapped RCOU/RCO2/RCO3 output-channel saturation finding raised. |
| Output mapping present | `SERVOx_FUNCTION` mapping limits motor-specific interpretation to mapped motor channels, including Copter Motor9-Motor12 at functions `82-85`. |
| Known event window | `--window` outputs only use the requested TimeS range. |
| Derived plot | Custom plot expression such as `GPS.Alt-BARO.Alt` is generated without arbitrary Python evaluation and can use `--align-tolerance` to avoid stale timestamp matches. |
| AutoTune | ATUN summary produced. |
| Raw IMU or ISBH/ISBD present | FFT plot produced. |
| Tlog supplied | Validation warns that skill is optimized for DataFlash. |
| Bench-only log | Final conclusions should say flight conclusions are not supported. |
| Corrupt/truncated log | Parser exits non-zero with partial/parse warning rather than inventing results. |
