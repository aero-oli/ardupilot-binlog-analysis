# Vibration and filtering diagnosis

Use VIBE for processed vibration and clipping. Use raw/high-rate IMU messages for FFT; VIBE alone is not enough for frequency-domain notch selection.

## Checks

- VIBE.VibeX/Y/Z max, p95 and time correlation with symptoms.
- VIBE clipping delta from `Clip` or per-IMU `Clip0`, `Clip1`, `Clip2` fields, whichever are present.
- RATE/PID noise and Dmod reduction.
- Raw IMU/GYR/ACC/IMU_FAST data or ISBH/ISBD batch-sampler data for FFT if present.
- Battery/current and throttle correlation.

## Interpretation

- High vibration or clipping invalidates aggressive tuning conclusions.
- Dominant FFT peaks can guide filter review, but parameter changes require aircraft-specific filter setup and verification.
