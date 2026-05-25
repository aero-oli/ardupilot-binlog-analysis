# Corrupt Or Incomplete Logs

Use this reference when validation or indexing reports parser errors, bad-byte skips, missing timebase, missing core messages, dropouts, truncated data, or otherwise incomplete evidence. Do not attempt to repair logs automatically.

## Agent Behaviour

- State log quality limitations clearly before drawing conclusions.
- Do not treat absence of a message as absence of a fault.
- Lower confidence where timing, missing rows, dropped messages, or abrupt log endings matter.
- Prefer evidence that exists over speculation from missing evidence.
- Request another log, parameter dump, bench check, ground test, or targeted capture only when safe.
- Keep safety-critical conclusions conservative. A damaged log cannot clear an aircraft to fly.

## Common Cases

### Truncated `.BIN`

Symptoms: parser stops early, duration is shorter than expected, mode/event sequence is incomplete, or the log ends during an event. Treat the end of the file as a data boundary, not proof that the fault stopped. If the log ends before or at impact, state that post-event evidence is unavailable.

### Parser Bad-Byte Skips

Bad bytes or parser warnings indicate corrupted regions. Correlations across those regions are lower confidence, and missing messages near the skipped data cannot be interpreted as absent faults.

### No `FMT`

DataFlash `FMT` messages describe schemas. If absent, some parsers can still decode known messages, but schema confidence is reduced, especially for unusual firmware, custom builds, or converted logs.

### No Usable Timebase

Without `TimeUS`, `TimeMS`, `Time`, or equivalent timing, plots and sequence analysis may be limited to row order. Avoid exact timing claims, rate estimates, window selection, and correlation claims unless another reliable timebase exists.

### No `PARM`

Without logged `PARM`, configuration context, motor output mapping, RC mapping, failsafe settings, and filter/logging settings may be incomplete. Ask for a Mission Planner/QGC/MAVProxy `.param` file or parameter dump when useful. Treat external parameters as configuration context, not proof of in-flight values unless timestamp/version matches the flight.

### Missing Core Messages After Arm

If `ATT`, `RATE`, or actuator output messages are absent after arming, do not infer the attitude controller, rate controller, or actuator outputs were healthy. State what cannot be evaluated and use available messages only.

### Telemetry `.tlog`

Telemetry logs can be useful context but are not equivalent to onboard DataFlash. They often lack high-rate controller, estimator, logging-health, and raw sensor messages. Prefer onboard `.BIN`/`.LOG` for this skill.

### Partial SD-Card Or Logging Failure

DSF/DMS/drop-count messages, large timestamp gaps, sparse high-rate messages, or abrupt stops can indicate storage or logging problems. Treat timing-sensitive conclusions as reduced confidence and inspect logging health before concluding a fault did not occur.

### Very Large Raw IMU Logs

Raw/high-rate IMU and batch sampler logs can become large enough to create dropouts or partial captures. Use them only for short targeted captures, check DSF/DMS/logging health afterward, and disable high-volume logging after the test.

### Logging Dropouts

Confirmed dropouts reduce confidence for missing evidence and short transient events. Possible drop-like fields are context to inspect, not proof by themselves.

### Log Ends Before Or At Impact

If the log ends before, at, or immediately after a suspected impact, do not claim the final causal sequence is complete. Use pre-impact evidence and state that impact/post-impact evidence is unavailable.

### Abrupt End During Suspected Brownout

If the log stops abruptly during voltage sag, power warnings, failsafes, or uncontrolled motion, treat brownout/power interruption as plausible context only when supported by BAT/POWR/PM/MSG/ERR evidence. Absence of later messages is not proof of recovery.
