#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from ap_log_investigation_manifest import PLOT_COMMANDS, SPECIAL_PLOT_GROUPS
from ap_methodic_registry import MethodicRegistryError, get_step, load_registry
from ap_next_step_helpers import build_diagnosis_action_plan


SYMPTOM_MAP = ROOT / "references" / "symptom-diagnosis-map.yaml"
REQUIRED_SYMPTOM_FIELDS = {
    "name",
    "aliases",
    "required_messages",
    "strongly_recommended_messages",
    "optional_context_messages",
    "diagnostic_questions",
    "likely_fault_branches",
    "recommended_plot_groups",
}
REQUIRED_METHODIC_FIELDS = {
    "step_id",
    "title",
    "phase",
    "official_anchor",
    "official_url",
    "required_messages",
    "strongly_recommended_messages",
    "optional_messages",
    "relevant_parameters",
    "preferred_window",
    "manual_observations_required",
    "pass_fail_summary",
    "next_step_if_pass",
    "next_step_if_conditional",
    "next_step_if_fail",
    "safety_gate_notes",
}
REQUIRED_METHODIC_STEPS = [
    "7.1",
    "7.1.1",
    "8.1",
    "8.2",
    "8.3",
    "8.4",
    "8.5",
    "9.1",
    "9.2",
    "9.3",
    "9.4",
    "9.5",
    "9.6",
    "9.7",
    "10.1",
    "10.2",
    "11.1",
    "11.2",
    "12.1",
    "12.2",
    "12.3",
    "13",
]
KEY_LOGGING_PARAMETERS = [
    "LOG_BITMASK",
    "LOG_BACKEND_TYPE",
    "LOG_DISARMED",
    "LOG_FILE_RATEMAX",
    "LOG_DARM_RATEMAX",
    "LOG_BLK_RATEMAX",
    "INS_RAW_LOG_OPT",
    "INS_LOG_BAT_MASK",
    "INS_LOG_BAT_OPT",
    "EK3_LOG_LEVEL",
]
SAMPLE_METADATA_PARAMS = [
    "LOG_BITMASK",
    "WP_YAW_BEHAVIOR",
    "ATC_RATE_Y_MAX",
    "MOT_YAW_HEADROOM",
    "SERVO*_FUNCTION",
]
UNSAFE_PATTERNS = [
    "disable arming checks",
    "disable ekf",
    "disable gps failsafe",
    "disable battery failsafe",
    "safe to fly",
]
CAUTION_TERMS = [
    "do not",
    "don't",
    "never",
    "not recommend",
    "cannot",
    "must not",
    "not proof",
    "not declare",
]
SAFETY_PATTERN_HEADINGS = [
    "Mission Yaw And Wobble",
    "Motor/ESC Issue",
    "Vibration/Filter Issue",
    "EKF/GPS/Loiter Issue",
    "RC/Failsafe/Pre-Arm Issue",
    "Crash/Loss-Of-Control",
]


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


def load_symptom_map():
    return yaml.safe_load(SYMPTOM_MAP.read_text(encoding="utf-8"))


def symptom_classes():
    data = load_symptom_map()
    classes = data.get("symptom_classes", [])
    assert_true(classes, "symptom_classes must not be empty")
    return classes


def markdown_refs(text):
    refs = set(re.findall(r"`(references/[^`]+?\.(?:md|yaml|json))`", text))
    refs.update(re.findall(r"\]\((references/[^)]+?\.(?:md|yaml|json))\)", text))
    refs.update(re.findall(r"(?<![\w/])(references/[A-Za-z0-9_./-]+\.(?:md|yaml|json))", text))
    return refs


def markdown_section(text, heading):
    match = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", text, flags=re.M | re.S)
    assert_true(match is not None, f"final-answer patterns missing section: {heading}")
    return match.group(1)


def test_symptom_map_shape_and_plot_groups():
    available_plot_groups = set(PLOT_COMMANDS) | set(SPECIAL_PLOT_GROUPS)
    for spec in symptom_classes():
        name = spec.get("name")
        missing_fields = REQUIRED_SYMPTOM_FIELDS - set(spec)
        assert_true(not missing_fields, f"{name} missing fields: {sorted(missing_fields)}")
        assert_true(isinstance(spec.get("required_messages"), list), f"{name} required_messages must be a list")
        assert_true(isinstance(spec.get("strongly_recommended_messages"), list), f"{name} strongly_recommended_messages must be a list")
        assert_true(isinstance(spec.get("optional_context_messages"), list), f"{name} optional_context_messages must be a list")
        assert_true(spec.get("diagnostic_questions"), f"{name} must have diagnostic_questions")
        assert_true(spec.get("likely_fault_branches"), f"{name} must have likely_fault_branches")
        assert_true(spec.get("recommended_plot_groups"), f"{name} must have recommended_plot_groups")
        unknown_groups = [group for group in spec["recommended_plot_groups"] if group not in available_plot_groups]
        assert_true(not unknown_groups, f"{name} uses unknown plot groups: {unknown_groups}")


def test_skill_references_exist():
    skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    refs = markdown_refs(skill)
    assert_true(refs, "SKILL.md should link reference files")
    for ref in sorted(refs):
        assert_true((ROOT / ref).exists(), f"SKILL.md reference does not exist: {ref}")
    for required in [
        "references/yaw-diagnosis.md",
        "references/attitude-rate-diagnosis.md",
        "references/motor-esc-diagnosis.md",
        "references/ekf-gps-diagnosis.md",
        "references/compass-yaw-source-diagnosis.md",
        "references/baro-rangefinder-altitude-diagnosis.md",
        "references/vibration-diagnosis.md",
        "references/battery-power-diagnosis.md",
        "references/rc-failsafe-prearm-diagnosis.md",
        "references/crash-or-loss-of-control-diagnosis.md",
        "references/final-answer-patterns.md",
        "references/evidence-gathering-flights.md",
        "references/logging-configuration-for-investigation.md",
        "references/corrupt-or-incomplete-log.md",
        "references/timeline-interpretation.md",
        "references/methodic-configurator-workflows.md",
        "references/methodic-output-patterns.md",
        "references/methodic-step-registry.yaml",
    ]:
        assert_true((ROOT / required).exists(), f"symptom guide missing: {required}")


def test_methodic_registry_loads_and_required_steps_exist():
    registry = load_registry()
    steps = {step["step_id"]: step for step in registry["steps"]}
    for step_id in REQUIRED_METHODIC_STEPS:
        assert_true(step_id in steps, f"methodic registry missing step {step_id}")
    assert_true(get_step("motor output oscillation", registry)["step_id"] == "7.1.1", "methodic alias lookup failed")


def test_methodic_registry_entries_have_required_fields():
    registry = load_registry()
    for step in registry["steps"]:
        missing = REQUIRED_METHODIC_FIELDS - set(step)
        assert_true(not missing, f"methodic step {step.get('step_id')} missing fields: {sorted(missing)}")
        for field in ["required_messages", "strongly_recommended_messages", "optional_messages", "relevant_parameters", "manual_observations_required"]:
            assert_true(isinstance(step[field], list), f"methodic step {step['step_id']} {field} must be list")
        assert_true("TUNING_GUIDE_ArduCopter" in step["official_url"], f"methodic step {step['step_id']} must link official guide")


def test_methodic_unknown_step_clean_error():
    try:
        get_step("not-a-methodic-step", load_registry())
    except MethodicRegistryError as exc:
        assert_true("Unknown Methodic step" in str(exc), "unknown Methodic step error should be clean")
    else:
        raise AssertionError("unknown Methodic step should fail")


def test_methodic_step_7_1_1_structured_result():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        fake_log = tmp_path / "empty.BIN"
        fake_log.write_bytes(b"")
        out = tmp_path / "methodic.json"
        summary = tmp_path / "methodic.md"
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "ap_methodic_step.py"),
                str(fake_log),
                "--step",
                "7.1.1",
                "--out",
                str(out),
                "--summary",
                str(summary),
            ],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        assert_true(proc.returncode == 0, f"methodic step command failed: {proc.stderr}")
        data = json.loads(out.read_text(encoding="utf-8"))
        for field in [
            "methodic_step",
            "title",
            "official_reference",
            "result",
            "safety_gate",
            "evidence_used",
            "missing_evidence",
            "manual_observations_required",
            "analysis_window",
            "findings",
            "checked_but_not_supported",
            "parameter_context",
            "plots",
            "recommended_next_steps",
            "what_not_to_do",
            "next_methodic_step",
            "confidence_limits",
        ]:
            assert_true(field in data, f"methodic result missing {field}")
        assert_true(data["methodic_step"] == "7.1.1", "wrong Methodic step returned")
        assert_true(data["result"] in {"pass", "conditional_pass", "fail", "inconclusive", "not_applicable"}, "invalid Methodic result")
        assert_true(summary.exists(), "methodic summary should be written")


def test_skill_links_methodic_reference():
    skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    for required in [
        "Mode 0: Methodic Configurator tuning step review",
        "references/methodic-configurator-workflows.md",
        "references/methodic-step-registry.yaml",
        "references/methodic-output-patterns.md",
        "https://ardupilot.github.io/MethodicConfigurator/TUNING_GUIDE_ArduCopter",
        "python scripts/ap_methodic_step.py",
        "Treat the step result as structured evidence, not final truth",
        "Can proceed?",
        "Next Methodic step/file",
    ]:
        assert_true(required in skill, f"SKILL.md missing Methodic reference: {required}")


def test_readme_mentions_methodic_mode():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for required in [
        "Methodic Configurator Support",
        "not an automatic tuner",
        "7.1.1 Motor output oscillation check",
        "9.5 AutoTune sequence",
        "13 Productive configuration",
        "python scripts/ap_methodic_step.py",
    ]:
        assert_true(required in readme, f"README.md missing Methodic mode content: {required}")


def test_final_answer_patterns_linked_and_complete():
    skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    path = ROOT / "references" / "final-answer-patterns.md"
    assert_true("references/final-answer-patterns.md" in skill, "SKILL.md should link final-answer patterns")
    text = path.read_text(encoding="utf-8")
    for phrase in [
        "Most likely issue",
        "Mission Yaw And Wobble",
        "Motor/ESC Issue",
        "Vibration/Filter Issue",
        "EKF/GPS/Loiter Issue",
        "RC/Failsafe/Pre-Arm Issue",
        "Crash/Loss-Of-Control",
        "Recommended next steps",
        "What not to do",
        "Do not overstate confidence",
        "Methodic Configurator Step Review",
        "Methodic 7.1.1 Motor Output Oscillation",
    ]:
        assert_true(phrase in text, f"final-answer patterns missing phrase: {phrase}")


def test_final_answer_patterns_include_methodic_requirements():
    text = (ROOT / "references" / "final-answer-patterns.md").read_text(encoding="utf-8")
    section = markdown_section(text, "Methodic Configurator Step Review")
    for phrase in [
        "Methodic step result",
        "Can proceed?",
        "Next Methodic step/file",
        "What not to do",
    ]:
        assert_true(phrase in section, f"Methodic final-answer pattern missing phrase: {phrase}")


def test_safety_relevant_final_answer_patterns_have_action_plan_terms():
    text = (ROOT / "references" / "final-answer-patterns.md").read_text(encoding="utf-8")
    for heading in SAFETY_PATTERN_HEADINGS:
        section = markdown_section(text, heading).lower()
        for phrase in ["safety gate", "missing evidence", "recommended next steps", "what not to do"]:
            assert_true(phrase in section, f"{heading} pattern missing required action-plan phrase: {phrase}")


def test_skill_requires_safety_next_steps():
    skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    for phrase in [
        "Recommended next steps",
        "Immediate safety gate",
        "What not to do",
        "Do not stop at missing evidence",
    ]:
        assert_true(phrase in skill, f"SKILL.md missing required safety next-step phrase: {phrase}")


def test_reference_coverage_and_key_files():
    evidence = (ROOT / "references" / "evidence-gathering-flights.md").read_text(encoding="utf-8")
    for spec in symptom_classes():
        assert_true(f"### {spec['name']}" in evidence, f"evidence-gathering-flights.md missing section for {spec['name']}")

    logging = ROOT / "references" / "logging-configuration-for-investigation.md"
    assert_true(logging.exists(), "logging-configuration-for-investigation.md should exist")
    logging_text = logging.read_text(encoding="utf-8")
    for param in KEY_LOGGING_PARAMETERS:
        assert_true(param in logging_text, f"logging reference missing {param}")

    assert_true((ROOT / "references" / "corrupt-or-incomplete-log.md").exists(), "corrupt/incomplete reference should exist")
    assert_true((ROOT / "references" / "timeline-interpretation.md").exists(), "timeline interpretation reference should exist")
    assert_true((ROOT / "references" / "parameter-metadata.md").exists(), "parameter metadata reference should exist")


def test_reference_safety_wording():
    checked_paths = [ROOT / "SKILL.md", ROOT / "README.md"]
    checked_paths.extend(sorted((ROOT / "references").glob("*.md")))
    for path in checked_paths:
        text = path.read_text(encoding="utf-8").lower()
        for match in re.finditer("|".join(re.escape(p) for p in UNSAFE_PATTERNS), text):
            context = text[max(0, match.start() - 120): match.end() + 120]
            assert_true(any(term in context for term in CAUTION_TERMS), f"{path} contains uncautioned unsafe wording near: {match.group(0)}")


def test_next_step_helper_output_shape():
    plan = build_diagnosis_action_plan(
        symptom_class="yaw_misbehaviour",
        symptom_text="yaw feels off especially during missions, generally wobbly and unstable",
        findings=[
            {
                "possible_cause": "AUTO yaw tracking worse than non-AUTO",
                "severity": "likely-issue",
                "evidence": ["mode comparison", "RATE yaw tracking error"],
            }
        ],
        missing_strongly_recommended=["PIDY", "PIDR", "PIDP"],
        missing_optional=["ESC"],
        mode_comparison={"ranking": [{"decoded_mode": "AUTO", "ranking_score": 8.0}]},
        fft_availability={"fft_available": False, "reason": "raw/high-rate IMU missing"},
    )
    assert_true(isinstance(plan.get("flight_status"), dict), "next-step helper output missing flight_status object")
    assert_true(plan["flight_status"].get("classification"), "flight_status missing classification")
    assert_true(isinstance(plan.get("recommended_next_steps"), list), "next-step helper output missing recommended_next_steps list")
    assert_true(plan["recommended_next_steps"], "recommended_next_steps should not be empty")
    step_types = [step.get("type") for step in plan["recommended_next_steps"]]
    assert_true(step_types[0] == "immediate_safety_gate", "first next step should be the immediate safety gate")
    assert_true("what_not_to_do" in step_types, "recommended_next_steps should include what_not_to_do")
    for step in plan["recommended_next_steps"]:
        for key in ["priority", "type", "action", "reason", "applies_to", "source_evidence"]:
            assert_true(key in step, f"recommended_next_steps entry missing {key}")


def test_parameter_metadata_schema_and_samples():
    schema = ROOT / "references" / "parameter-metadata" / "schema.md"
    metadata_file = ROOT / "references" / "parameter-metadata" / "ArduCopter-latest.min.json"
    assert_true(schema.exists(), "parameter metadata schema file should exist")
    assert_true(metadata_file.exists(), "compact parameter metadata JSON should exist")
    data = json.loads(metadata_file.read_text(encoding="utf-8"))
    assert_true(data.get("caveat") and "may not exactly match" in data["caveat"], "metadata caveat should be present")
    by_name = {entry["name"]: entry for entry in data.get("parameters", [])}
    for name in SAMPLE_METADATA_PARAMS:
        assert_true(name in by_name, f"sample metadata parameter missing: {name}")
        entry = by_name[name]
        for key in ["name", "display_name", "description", "source_vehicle", "metadata_version"]:
            assert_true(key in entry, f"{name} metadata missing {key}")


def test_repo_housekeeping_files():
    assert_true((ROOT / ".gitignore").exists(), ".gitignore should exist")
    package_script = ROOT / "scripts" / "package_skill.sh"
    package_check = ROOT / "scripts" / "check_package.sh"
    if package_script.exists() or package_check.exists():
        assert_true(package_script.exists(), "package script should exist when package checking is implemented")
        assert_true(package_check.exists(), "package check script should exist when packaging is implemented")


def main():
    test_symptom_map_shape_and_plot_groups()
    test_skill_references_exist()
    test_methodic_registry_loads_and_required_steps_exist()
    test_methodic_registry_entries_have_required_fields()
    test_methodic_unknown_step_clean_error()
    test_methodic_step_7_1_1_structured_result()
    test_skill_links_methodic_reference()
    test_final_answer_patterns_linked_and_complete()
    test_safety_relevant_final_answer_patterns_have_action_plan_terms()
    test_skill_requires_safety_next_steps()
    test_reference_coverage_and_key_files()
    test_reference_safety_wording()
    test_next_step_helper_output_shape()
    test_parameter_metadata_schema_and_samples()
    test_repo_housekeeping_files()
    print("reference consistency tests passed")


if __name__ == "__main__":
    main()
