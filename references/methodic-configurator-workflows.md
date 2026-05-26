# Methodic Configurator Workflows

## Scope

This reference covers ArduCopter Methodic Configurator tuning workflow support for this skill. It is scoped to step-aware analysis of DataFlash logs and parameter context for the Methodic Configurator ArduCopter tuning guide.

This skill analyses logs and parameters, gathers evidence, classifies Methodic step status, produces plots where useful, and guides the agent toward conservative next actions. It does not auto-tune the vehicle, upload parameters, generate final tuning conclusions, or replace the Methodic Configurator workflow.

The agent must inspect the generated evidence and write the final user-facing conclusion. Treat Methodic script output as structured evidence, not final truth.

## Safety Rules

- Never skip Methodic safety gates.
- Never recommend blind gain changes.
- Never declare the aircraft safe to fly.
- Never treat missing evidence as proof that a step passed.
- Never recommend disabling arming, EKF, GPS, battery, logging, compass, or other protection checks as a routine fix.
- Do not continue tuning when evidence indicates output oscillation, loss of control, excessive vibration, unhealthy power, estimator failures, motor/ESC faults, or unverified configuration.
- When a step depends on observations outside the log, say so explicitly.

## Step Result Classification

- `pass`: Required evidence is present, no safety-critical issue was found for the step, and any required manual observations are explicitly reported as normal. Do not treat this as a declaration that the aircraft is safe to fly.
- `conditional_pass`: Evidence mostly supports proceeding, but one or more limitations require caution, reduced scope, extra checks, or repeated review after the next step.
- `fail`: Evidence indicates the step objective was not met or a safety-relevant issue must be resolved before continuing.
- `inconclusive`: Required log data, parameter context, analysis window, or manual observations are missing or contradictory.
- `not_applicable`: The step does not apply to the vehicle, firmware, hardware, or user objective, and the reason is explicit.

## Safety Gate Classification

- `proceed`: No step-specific blocker was found. The agent must still avoid declaring the aircraft safe to fly; do not describe the aircraft as safe.
- `proceed_with_caution`: Continue only with conservative limits, clear monitoring, and the missing-evidence caveats listed by the analysis.
- `repeat_step`: Repeat the current Methodic step or collect a better log/parameter/observation set before moving on.
- `do_not_proceed`: Do not move to the next tuning step until the identified safety-relevant issue is investigated and resolved.
- `bench_check_required`: Stop flight progression and perform bench, hardware, wiring, propeller, motor, ESC, frame, or configuration checks before considering another controlled test.

## Manual Observations

Some Methodic steps require evidence that may not exist in a DataFlash log. The agent must ask for, record, and weigh observations such as:

- hot motors immediately after landing;
- hot ESCs immediately after landing;
- audible vibration or oscillation during flight;
- visible shaking;
- hard-to-control, sluggish, or unstable behaviour;
- whether the test was performed in the expected mode and height range;
- whether the pilot had enough control margin to safely stop the test.

If these observations are required and unavailable, classify the step as `inconclusive` or `conditional_pass` at most, depending on the log evidence. Do not promote a log-only result to `pass` when the official workflow requires physical or pilot observations.

## Agent Workflow

1. Identify the Methodic step from the user request.
2. Run `python scripts/ap_methodic_step.py LOG.BIN --step STEP_ID --out out/methodic_STEP.json --summary out/methodic_STEP.md --plots out/plots/methodic_STEP`.
3. Inspect the JSON, Markdown summary, and plots.
4. Cross-check missing evidence and manual observations.
5. Write the final response yourself with a clear result, safety gate, evidence, confidence limits, recommended next steps, and what not to do.

## Official Source

The registry is based on the official Methodic Configurator ArduCopter tuning guide:

https://ardupilot.github.io/MethodicConfigurator/TUNING_GUIDE_ArduCopter
