# Timeline interpretation

Use this when `MSG`, `EV`, `ERR`, `ARM`, or `MODE` entries appear near a
selected analysis window.

## Relative timing

- `inside_window`: event time falls inside any selected analysis interval. Treat
  this as candidate causal or supporting evidence, then confirm against the
  relevant signal timing.
- `before_window`: event time is before the first selected interval. Treat this
  as setup, precondition, arming, or context evidence unless the issue clearly
  continues into the symptom window.
- `after_window`: event time is after the last selected interval. Treat this as
  safety context for the next flight, not direct proof of the in-window symptom
  unless timing, mode history, or continuing symptoms link it back.

For split mode windows, an event is in-window if it falls inside any selected
interval. Events in excluded gaps between intervals are context, not direct
evidence for the selected mode segment.

## Causal weight

In-window `ERR`, `EV`, `MSG`, `ARM`, or `MODE` changes carry more diagnostic
weight because they overlap the symptom window. They still need correlation with
the control, estimator, power, vibration, or actuator signals.

Post-flight or disarmed warnings such as compass unhealthy, GPS yaw unavailable,
battery failsafe, radio failsafe, or pre-arm failures may be safety-critical for
the next flight. They are not automatically the cause of an earlier in-mission
yaw, wobble, or navigation issue.

State both facts when needed: the warning is safety-relevant, and it occurred
outside the symptom window.
