# ArduPilot Bin Log Analysis Skill

A Codex/agent skill for inspecting ArduPilot onboard DataFlash `.bin` and `.log` files. It gives an agent a structured workflow, reference material, and deterministic Python scripts for ArduCopter-focused log review, diagnosis, plotting, tuning triage, and before/after comparison.

The skill is strongest for Copter and multirotor logs. Generic parsing, extraction, plotting, and segmentation work across ArduPilot DataFlash logs, but Copter tuning and motor-output diagnosis are the main target. It is not intended for PX4 `.ulg` logs unless they have been converted to ArduPilot-style tables.

## What It Can Do

- Validate and index DataFlash logs.
- Extract log messages to CSV or Parquet tables.
- Generate health metrics, tuning summaries, FFT/noise plots, and interactive Plotly graph packs for the agent to interpret.
- Run symptom-led investigations for yaw, attitude, GPS/EKF, vibration, battery/power, motor/ESC, altitude/throttle, and crash/loss-of-control events.
- Review ArduCopter Methodic Configurator tuning steps with step-aware evidence, safety gates, plots, and next-step guidance.
- Compare before/after logs with optional segment-specific time windows.
- Create custom plots from any extracted `MESSAGE.FIELD`, including derived expressions such as `GPS.Alt-BARO.Alt`.
- Interpret Copter output channels using `SERVOx_FUNCTION`, including `RCOU`, `RCO2`, and `RCO3`.

## Safety Boundary

This skill cannot declare an aircraft safe to fly. It is designed to separate abnormal evidence from normal telemetry context, with diagnosis output shaped as `findings`, `context`, `checked_but_not_supported`, `missing_required`, `missing_strongly_recommended`, and `missing_optional`. Mechanical inspection, bench testing, configuration review, and controlled ground checks are still required after any serious log finding.

The Methodic Configurator tools are not an automatic tuner. They do not upload parameters, write gains, certify operational readiness, or replace the agent's evidence inspection.

## Install

Place this folder in your agent skills directory, for example:

```bash
~/.agents/skills/ardupilot-binlog-analysis/
```

Install script dependencies in the environment that will run the tools:

```bash
pip install -r requirements.txt
```

Check a fresh workspace before starting a long analysis:

```bash
python scripts/ap_skill_doctor.py
python scripts/ap_skill_doctor.py --json out/skill_doctor.json
```

If dependencies are missing and you want a local virtual environment, bootstrap one:

```bash
bash scripts/bootstrap_venv.sh
source .venv/bin/activate
python scripts/ap_skill_doctor.py
```

The main agent entrypoint is `SKILL.md`. The deterministic tools live in `scripts/`, with domain references in `references/`.

## How To Use

Install the folder as an agent skill, then ask your agent to analyse an ArduPilot DataFlash log. For example:

```text
Analyse this ArduPilot .bin log and tell me if anything looks unsafe.
```

```text
This flight had a yaw issue. Diagnose the likely causes and generate useful plots.
```

```text
Compare these two logs and tell me whether the tuning change improved things.
```

```text
I am on Methodic step 7.1.1. Check motor output oscillation and tell me whether I can proceed to the notch step.
```

```text
Review this AutoTune log against the Methodic workflow.
```

```text
Plot GPS altitude and barometric pressure, and include mode/error markers.
```

The agent reads `SKILL.md`, follows the safety rules, chooses the relevant investigations, runs the bundled scripts where useful, and writes its own concise evidence-backed conclusions with links to generated artifacts.

## Bundled Tools

The `scripts/` directory is mainly for the agent. It provides deterministic helpers for validation, indexing, extraction, metrics, plotting, tuning review, symptom diagnosis, FFT, comparison, and segment discovery.

You can run the scripts manually while developing or debugging the skill, but normal use is to ask the agent for the analysis you want and let it choose the workflow.

## Methodic Configurator Support

The skill supports ArduCopter Methodic Configurator tuning step review. The scripts gather deterministic evidence and classify a step result/safety gate; the agent still inspects the JSON, plots, missing evidence, and manual observations before writing conclusions.

Supported Methodic steps and groups:

- 7.1 First flight
- 7.1.1 Motor output oscillation check
- 8.1 Harmonic notch / filter review
- 8.2 Throttle controller
- 8.3 PID notch / frame resonance
- 8.4 EKF altitude source weights
- 8.5 QuikTune or manual PID tuning
- 9.1 MagFit
- 9.2 QuikTune standard setup/results
- 9.3 Tune evaluation with feed-forward disabled
- 9.4 Tune evaluation with feed-forward enabled
- 9.5 AutoTune sequence
- 9.6 Performance evaluation
- 9.7 Derivative feed-forward calculation
- 10.1 Wind estimation / drag coefficients
- 10.2 Barometer compensation
- 11.1 System ID flights
- 11.2 Analytical PID optimisation review
- 12.1 Position controller tuning
- 12.2 Guided operation
- 12.3 Precision landing
- 13 Productive configuration

Core entrypoints:

```bash
python scripts/ap_methodic_step.py LOG.BIN --step 7.1.1 --out out/methodic_7_1_1.json --summary out/methodic_7_1_1.md --plots out/plots/methodic_7_1_1
python scripts/ap_methodic_progress.py out/methodic_7_1.json out/methodic_7_1_1.json --out out/methodic_progress.json --summary out/methodic_progress.md
python scripts/ap_methodic_compare.py BEFORE.BIN AFTER.BIN --step 8.1 --out out/methodic_compare_8_1.json
```

Methodic outputs must be treated as evidence, not final truth. Failed, inconclusive, and conditional safety gates should not be skipped without user confirmation and a documented safety rationale.

## Tests

Run the lightweight checks:

```bash
uv run --with pymavlink --with pandas --with numpy --with plotly --with pyyaml bash tests/smoke_test.sh
```

Run regression tests:

```bash
uv run --with pymavlink --with pandas --with numpy --with plotly --with pyyaml python tests/regression_test.py
```

Run reference, metadata, and action-plan consistency checks:

```bash
uv run --with pyyaml python tests/reference_consistency_test.py
```

These checks do not require real `.BIN` logs. They verify the safety next-step
requirements in `SKILL.md`, linked references, final-answer pattern scaffolds,
next-step helper output shape, metadata samples, and conservative safety wording.

Run the real-log fixture check with a local, non-committed `.bin` file:

```bash
bash tests/real_log_fixture_check.sh /path/to/log.bin
```

## Repository Layout

- `SKILL.md` - agent instructions and workflow.
- `scripts/` - deterministic parsing, metrics, plotting, diagnosis, comparison, FFT, and segment discovery tools.
- `references/` - diagnosis guides, caveats, message map, and plot catalog.
- `assets/` - reusable example prompt material.
- `tests/` - smoke, regression, and real-log fixture checks.
- `agents/openai.yaml` - agent metadata.

## Notes

- Use `--window START:END` or `--window around:CENTER:RADIUS` when the symptom timing is known.
- Use `--messages ALL` before open-ended custom plotting.
- Output-channel conclusions are highest confidence when `PARM` includes `SERVOx_FUNCTION` values.
- Copter motor functions are treated as Motor1-Motor8 at `33-40` and Motor9-Motor12 at `82-85`; tilt outputs are not treated as normal motor outputs.
