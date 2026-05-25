#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from ap_log_investigation_manifest import PLOT_COMMANDS, SPECIAL_PLOT_GROUPS


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
    ]:
        assert_true((ROOT / required).exists(), f"symptom guide missing: {required}")


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
    assert_true((ROOT / "references" / "parameter-metadata.md").exists(), "parameter metadata reference should exist")


def test_reference_safety_wording():
    for path in sorted((ROOT / "references").glob("*.md")):
        text = path.read_text(encoding="utf-8").lower()
        for match in re.finditer("|".join(re.escape(p) for p in UNSAFE_PATTERNS), text):
            context = text[max(0, match.start() - 120): match.end() + 120]
            assert_true(any(term in context for term in CAUTION_TERMS), f"{path} contains uncautioned unsafe wording near: {match.group(0)}")


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
    test_reference_coverage_and_key_files()
    test_reference_safety_wording()
    test_parameter_metadata_schema_and_samples()
    test_repo_housekeeping_files()
    print("reference consistency tests passed")


if __name__ == "__main__":
    main()
