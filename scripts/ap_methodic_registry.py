#!/usr/bin/env python3
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = ROOT / "references" / "methodic-step-registry.yaml"

REQUIRED_STEP_FIELDS = {
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


class MethodicRegistryError(RuntimeError):
    pass


def normalize_step_id(value: str) -> str:
    text = str(value).strip().lower()
    text = text.replace("_", ".")
    text = re.sub(r"\s+", " ", text)
    return text


def alias_keys(step: dict[str, Any]) -> set[str]:
    keys = {normalize_step_id(step["step_id"])}
    keys.add(normalize_step_id(step["step_id"]).replace(".", " "))
    for alias in step.get("aliases", []) or []:
        keys.add(normalize_step_id(str(alias)))
    return keys


def load_registry(path: str | Path | None = None, *, validate_paths: bool = True) -> dict[str, Any]:
    registry_path = Path(path) if path else DEFAULT_REGISTRY
    if not registry_path.exists():
        raise MethodicRegistryError(f"Methodic registry not found: {registry_path}")
    try:
        data = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise MethodicRegistryError(f"Could not parse Methodic registry YAML: {exc}") from exc
    validate_registry(data, registry_path=registry_path, validate_paths=validate_paths)
    return data


def validate_registry(data: Any, *, registry_path: Path = DEFAULT_REGISTRY, validate_paths: bool = True) -> None:
    if not isinstance(data, dict):
        raise MethodicRegistryError("Methodic registry must be a mapping")
    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        raise MethodicRegistryError("Methodic registry must contain a non-empty steps list")

    seen: set[str] = set()
    for idx, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            raise MethodicRegistryError(f"Step #{idx} must be a mapping")
        missing = REQUIRED_STEP_FIELDS - set(step)
        if missing:
            label = step.get("step_id", f"#{idx}")
            raise MethodicRegistryError(f"Methodic step {label} missing required fields: {sorted(missing)}")
        step_id = str(step["step_id"])
        if step_id in seen:
            raise MethodicRegistryError(f"Duplicate Methodic step_id: {step_id}")
        seen.add(step_id)
        for field in ["required_messages", "strongly_recommended_messages", "optional_messages", "relevant_parameters", "manual_observations_required"]:
            if not isinstance(step.get(field), list):
                raise MethodicRegistryError(f"Methodic step {step_id} field {field} must be a list")
        if not str(step.get("official_url", "")).startswith("https://ardupilot.github.io/MethodicConfigurator/TUNING_GUIDE_ArduCopter"):
            raise MethodicRegistryError(f"Methodic step {step_id} official_url must point to the official ArduCopter guide")
        _validate_local_refs(step, registry_path=registry_path, validate_paths=validate_paths)

    keys: dict[str, str] = {}
    for step in steps:
        for key in alias_keys(step):
            owner = keys.get(key)
            if owner and owner != step["step_id"]:
                raise MethodicRegistryError(f"Methodic alias {key!r} is ambiguous between {owner} and {step['step_id']}")
            keys[key] = step["step_id"]


def _validate_local_refs(step: dict[str, Any], *, registry_path: Path, validate_paths: bool) -> None:
    if not validate_paths:
        return
    for field, base in [("local_references", ROOT), ("analysis_scripts", ROOT)]:
        for rel in step.get(field, []) or []:
            path = base / str(rel)
            if not path.exists():
                raise MethodicRegistryError(f"Methodic step {step['step_id']} references missing {field} path: {rel}")


def get_step(step_id_or_alias: str, registry: dict[str, Any] | None = None) -> dict[str, Any]:
    data = registry or load_registry()
    needle = normalize_step_id(step_id_or_alias)
    for step in data.get("steps", []):
        if needle in alias_keys(step):
            return dict(step)
    known = ", ".join(str(step["step_id"]) for step in data.get("steps", []))
    raise MethodicRegistryError(f"Unknown Methodic step '{step_id_or_alias}'. Known steps: {known}")


if __name__ == "__main__":
    reg = load_registry()
    print(f"Loaded {len(reg['steps'])} Methodic steps from {DEFAULT_REGISTRY}")
