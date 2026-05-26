# Methodic Output Patterns

Use these patterns when converting `ap_methodic_step.py` output into a final answer. The script output is evidence, not the final conclusion.

## Minimal Shape

1. State the Methodic step and result.
2. State the safety gate.
3. Summarize the strongest evidence.
4. List missing evidence and manual observations.
5. Give conservative next steps.
6. State what not to do.
7. State confidence limits.

## Recommended Wording

### Pass Or Conditional Pass

Use cautious language:

> The log evidence supports continuing to the next Methodic step, with the limits below. Do not treat this as a declaration that the aircraft is safe to fly.

Include the exact next Methodic step from `next_methodic_step`, but preserve any caveats in `recommended_next_steps`.

### Fail

Lead with the blocker:

> Do not continue to the next Methodic tuning step yet. The current evidence indicates a blocker that needs investigation first.

Then list the evidence and whether the next action is bench checks, repeating the step, or collecting better data.

### Inconclusive

Do not overstate:

> The step cannot be classified from this evidence set. The missing evidence below is required before treating the step as complete.

Explain whether another flight is appropriate. If motor/ESC heat, control difficulty, vibration, output saturation, or estimator/power faults are possible, prefer bench or configuration checks before another flight.

## What Not To Do

For every Methodic output, include a short `What not to do` section when the result is not a clear pass:

- Do not skip to later tuning steps because one log looks quiet.
- Do not make blind PID gain changes.
- Do not disable safety checks or failsafes to complete a step.
- Do not repeat a flight when bench evidence or log evidence suggests a hardware/control safety issue.
- Do not treat missing ESC telemetry, missing RATE/PID messages, or missing manual observations as normal evidence.
