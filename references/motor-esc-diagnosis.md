# Motor/ESC diagnosis

Use RCOU for commanded output and ESC/ESCX/EDT2 for feedback when present. Do not infer ESC health if ESC telemetry is absent.

## Checks

- RCOU channels near min/max.
- One channel persistently higher/lower than peers.
- ESC RPM mismatch for similar outputs.
- ESC current or temperature abnormal on one instance.
- ESC error rate above zero, ESCX nonzero flags, or EDT2 alert/warning/error status bits.
- ESCX input duty, output duty, or power percentage reaching unusual values for the flight phase.
- Battery sag during motor output demand.
- VIBE increases associated with a motor or throttle region.

## Safety-critical interpretation

Motor/ESC/prop problems can produce immediate loss of control. If suspected, recommend bench inspection and restrained motor tests only. Do not suggest further flight testing until hardware and direction checks are complete.
