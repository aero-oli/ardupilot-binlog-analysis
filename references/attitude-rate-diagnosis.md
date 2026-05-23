# Roll/pitch attitude-rate diagnosis

Use ATT desired vs achieved to confirm the symptom axis. Use RATE desired vs achieved to identify controller tracking. Use PIDR/PIDP terms and flags to identify limiting or noise. Use mapped output channels from RCOU/RCO2/RCO3 to verify actuator saturation and VIBE/FFT for vibration/filtering. Do not tune roll/pitch gains until motor output headroom and vibration are acceptable.

## Key checks

- ATT.DesRoll/Roll and ATT.DesPitch/Pitch.
- RATE.RDes/R/ROut and RATE.PDes/P/POut.
- PIDR/PIDP Err, P, I, D, FF, Dmod, SRate, Flags.
- Mapped output channels for saturation/asymmetry.
- VIBE clipping and FFT if high-rate IMU data exists.
- BAT voltage/current during high error.

## Common conclusions

- High error + high output + mapped output-channel saturation: authority/headroom issue.
- Oscillation around target + no saturation: tune/filtering issue.
- I-term buildup: persistent trim/CG/torque/airframe imbalance or external disturbance.
- Noise in D term / Dmod reduction: gyro noise/filtering/resonance issue.
