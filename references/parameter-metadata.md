# Parameter Metadata

The skill bundles compact ArduCopter parameter metadata for parameters commonly used during DataFlash log investigation. Use it to explain what logged values mean, decode common enums or bitmasks, and identify whether values are zero or match logged defaults.

Limitations:

- Metadata may not exactly match the firmware that produced a log.
- Latest-source metadata may include unreleased, renamed, or removed parameters.
- Some parameters vary by vehicle, board, frame class, firmware branch, or build options.
- Treat metadata as explanatory context, not proof of firmware-specific behaviour.
- Do not recommend parameter changes automatically from metadata alone. Any parameter review must be tied to observed log evidence, bench/ground verification, and the relevant ArduPilot documentation for the actual firmware.

Primary files:

- `references/parameter-metadata/ArduCopter-latest.min.json` - compact curated subset for current Copter investigation workflows.
- `references/parameter-metadata/schema.md` - JSON field definitions.

Refreshing metadata:

```bash
python scripts/update_parameter_metadata.py --fetch --vehicle ArduCopter
python scripts/ap_param_lookup.py --refresh-metadata --index out/index.json --symptom yaw_misbehaviour --json out/param_lookup.json
```

The updater fetches ArduPilot's machine-readable `apm.pdef.json` from `https://autotest.ardupilot.org/Parameters/ArduCopter/apm.pdef.json`, compacts only the investigation-focused subset, and writes the local cache. The lookup tool uses the local cache by default so investigations still work offline and are reproducible.

Agent workflow:

```bash
python scripts/ap_param_lookup.py --index out/index.json --symptom yaw_misbehaviour --json out/param_lookup.json
python scripts/ap_param_lookup.py --index out/index.json --names WP_YAW_BEHAVIOR,ATC_RATE_Y_MAX,MOT_YAW_HEADROOM
```

Use the output to explain logged parameter context and confidence limits. Do not convert it into automatic tuning, mission, navigation, failsafe, or logging-setting changes.
