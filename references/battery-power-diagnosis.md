# Battery and power diagnosis

Use BAT for flight battery and POWR for board power. Interpret voltage relative to cell count and battery type; do not assume cell count from voltage without evidence.

## Checks

- BAT.Volt / VoltR minimum and sag under current.
- BAT.Curr max and correlation with throttle and attitude errors.
- BAT.CurrTot capacity consumed.
- POWR.VCC ripple/drop and log ending while airborne.
- ESC per-instance voltage/current if present.

## Interpretation

- Battery sag can reduce thrust/yaw authority.
- Board VCC ripple/drop can indicate power module/regulator/peripheral loading issues.
- A log ending abruptly while altitude is still high can indicate brownout or logging/power failure.
