# ERR Subsys/ECode Reference

DataFlash `ERR` rows record autopilot error and failsafe timeline entries. `ERR.Subsys` identifies the subsystem that reported the error, and `ERR.ECode` is the subsystem-specific error code.

Use decoded ERR entries as context, not proof by themselves. Meanings can vary by ArduPilot vehicle, firmware version, and source branch. Always correlate the decoded row with timing, `MODE`, `MSG`, `EV`, `ARM`, RC input, power, GPS/EKF/compass, and the user-reported symptom.

For local decoding, run:

```bash
python scripts/ap_err_decode.py --index out/index.json --json out/err_decode.json
python scripts/ap_err_decode.py --tables out/timeline --json out/err_decode.json
```

The decoder intentionally uses a conservative mapping for common documented pairs. Unknown pairs remain `unknown`; do not guess a meaning from the number alone.

Common examples included locally:

- `Subsys=5 ECode=1`: radio failsafe triggered.
- `Subsys=6 ECode=1`: battery failsafe triggered.
- `Subsys=11 ECode=2`: GPS glitch occurred.
- `Subsys=12 ECode=1`: crash into ground detected.
- `Subsys=12 ECode=2`: loss of control detected.
- `Subsys=16 ECode=2`: EKF bad variance.
- `Subsys=17 ECode=1`: EKF failsafe triggered.
- `Subsys=25 ECode=1`: thrust loss detected.
- `Subsys=29 ECode=1`: excessive vibration compensation activated.

If the decoder returns `confidence: unknown`, state that the local mapping does not identify the code and avoid converting it into a specific root cause without firmware-specific source/docs.
