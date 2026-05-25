from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path


DEFAULT_MAP_PATH = Path(__file__).resolve().parents[1] / "references" / "symptom-diagnosis-map.yaml"
REQUIRED_CLASS_FIELDS = [
    "name",
    "aliases",
    "required_messages",
    "strongly_recommended_messages",
    "optional_context_messages",
    "relevant_parameters",
    "recommended_plot_groups",
    "diagnostic_questions",
    "likely_fault_branches",
]
LIST_FIELDS = [field for field in REQUIRED_CLASS_FIELDS if field != "name"]
MESSAGE_FIELDS = ["required_messages", "strongly_recommended_messages", "optional_context_messages"]
ESC_TELEMETRY_GROUP = {"ESC", "ESCX", "EDT2"}


def _analysis_error(message):
    from ap_common import AnalysisError

    return AnalysisError(message)


def _normalise_text(value):
    return re.sub(r"\s+", " ", str(value).lower()).strip()


def _message_name(value):
    name = str(value).strip().upper()
    if not name:
        raise _analysis_error("symptom map contains an empty message name")
    return name


def _validate_list(item, field, class_name):
    value = item.get(field)
    if not isinstance(value, list):
        raise _analysis_error(f"symptom class '{class_name}' field '{field}' must be a list")
    return value


def _validate_class(item, index):
    if not isinstance(item, dict):
        raise _analysis_error(f"symptom class entry #{index} must be a mapping")
    class_name = item.get("name", f"#{index}")
    for field in REQUIRED_CLASS_FIELDS:
        if field not in item:
            raise _analysis_error(f"symptom class '{class_name}' missing required field '{field}'")
    if not isinstance(item["name"], str) or not item["name"].strip():
        raise _analysis_error(f"symptom class entry #{index} has invalid field 'name'")

    normalised = {"name": item["name"].strip()}
    for field in LIST_FIELDS:
        values = _validate_list(item, field, normalised["name"])
        if field in MESSAGE_FIELDS:
            normalised[field] = [_message_name(value) for value in values]
        else:
            normalised[field] = [str(value).strip() for value in values if str(value).strip()]
    normalised["aliases"] = [_normalise_text(alias) for alias in normalised["aliases"]]
    return normalised


@lru_cache(maxsize=8)
def _load_symptom_map_cached(path_string):
    path = Path(path_string)
    try:
        import yaml
    except Exception as exc:
        raise _analysis_error("PyYAML is required to load references/symptom-diagnosis-map.yaml. Install dependencies with: pip install -r requirements.txt") from exc
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise _analysis_error(f"Could not read symptom diagnosis map '{path}': {exc}") from exc
    if not isinstance(data, dict):
        raise _analysis_error("symptom diagnosis map must be a YAML mapping")
    if data.get("version") != 1:
        raise _analysis_error("symptom diagnosis map field 'version' must be 1")
    default_class = data.get("default_class")
    if not isinstance(default_class, str) or not default_class.strip():
        raise _analysis_error("symptom diagnosis map missing required string field 'default_class'")
    classes = data.get("symptom_classes")
    if not isinstance(classes, list) or not classes:
        raise _analysis_error("symptom diagnosis map field 'symptom_classes' must be a non-empty list")

    by_name = {}
    ordered = []
    aliases = {}
    for index, item in enumerate(classes, start=1):
        entry = _validate_class(item, index)
        name = entry["name"]
        if name in by_name:
            raise _analysis_error(f"duplicate symptom class '{name}' in symptom diagnosis map")
        by_name[name] = entry
        ordered.append(name)
        for alias in entry["aliases"]:
            if not alias:
                continue
            aliases.setdefault(alias, []).append(name)
    if default_class not in by_name:
        raise _analysis_error(f"default symptom class '{default_class}' is not defined in symptom_classes")
    ambiguous = sorted(alias for alias, names in aliases.items() if len(set(names)) > 1)
    if ambiguous:
        raise _analysis_error("symptom diagnosis map has aliases assigned to multiple classes: " + ", ".join(ambiguous[:10]))
    return {"version": 1, "default_class": default_class.strip(), "classes": by_name, "ordered_class_names": ordered}


def load_symptom_map(path=None):
    path = Path(path or DEFAULT_MAP_PATH).resolve()
    return _load_symptom_map_cached(str(path))


def requirement_spec(symptom_class, path=None):
    symptom_map = load_symptom_map(path)
    classes = symptom_map["classes"]
    return classes.get(symptom_class, classes[symptom_map["default_class"]])


def diagnosis_requirements(path=None):
    return load_symptom_map(path)["classes"]


def missing_by_tier(index, symptom_class, missing_messages, path=None):
    spec = requirement_spec(symptom_class, path)
    missing_required = missing_messages(index, spec["required_messages"])
    missing_strongly = missing_messages(index, spec["strongly_recommended_messages"])
    missing_optional = missing_messages(index, spec["optional_context_messages"])
    if any(msg in index.get("messages", {}) for msg in ESC_TELEMETRY_GROUP):
        missing_strongly = [msg for msg in missing_strongly if msg not in ESC_TELEMETRY_GROUP]
        missing_optional = [msg for msg in missing_optional if msg not in ESC_TELEMETRY_GROUP]
    return missing_required, missing_strongly, missing_optional


def classify_symptom_from_map(text, path=None):
    symptom_map = load_symptom_map(path)
    query = _normalise_text(text)
    if not query:
        return symptom_map["default_class"]

    matches = []
    for class_name in symptom_map["ordered_class_names"]:
        entry = symptom_map["classes"][class_name]
        for alias in entry["aliases"]:
            if not alias:
                continue
            pattern = r"(?<!\w)" + re.escape(alias) + r"(?!\w)"
            if re.search(pattern, query):
                matches.append((len(alias.split()), len(alias), class_name, alias))
    if not matches:
        return symptom_map["default_class"]
    matches.sort(reverse=True)
    best_word_count, best_length, best_class, _best_alias = matches[0]
    tied = {class_name for word_count, length, class_name, _alias in matches if word_count == best_word_count and length == best_length}
    if len(tied) > 1:
        return symptom_map["default_class"]
    return best_class
