# Firmware version notes

The scripts are designed to work with actual message fields present in each log instead of assuming a fixed firmware schema.

- Some logs may use NKF* instead of XKF* on older firmware.
- Some field names differ by firmware and vehicle type.
- RATE is not logged in some fixed-wing Plane modes.
- ESC telemetry appears only if supported and configured.
- Raw/high-rate IMU data appears only if relevant logging was enabled.
- IMU batch-sampler FFT data appears as ISBH/ISBD only when batch logging is configured.
- Extended DShot telemetry status appears as EDT2 only when the firmware/hardware path logs it.
- Parameter names can change between versions; report the firmware string and parameters found in the log.
