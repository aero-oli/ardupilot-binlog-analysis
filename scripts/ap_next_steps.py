#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from ap_common import write_json
from ap_next_step_helpers import build_diagnosis_action_plan


INPUT_NAMES = {
    "diagnosis": "diagnosis input missing",
    "mode_compare": "mode comparison input missing",
    "param_lookup": "parameter lookup input missing",
    "fft": "FFT input missing",
    "manifest": "manifest input missing",
}


def _read_json(path):
    if not path:
        return None, None
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, f"{path} not found"
    except json.JSONDecodeError as exc:
        return None, f"{path} is not valid JSON: {exc}"


def _dedupe(items):
    out = []
    seen = set()
    for item in items or []:
        if item is None:
            continue
        text = str(item).strip()
        if text and text not in seen:
            out.append(text)
            seen.add(text)
    return out


def _as_list(value):
    if not value:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _action_by_type(steps, step_type):
    return [step["action"] for step in steps if step.get("type") == step_type]


def _messages_from_plan(diagnosis, manifest, mode_compare=None):
    messages = []
    for source in [diagnosis or {}, manifest or {}]:
        for key in ["missing_required", "missing_strongly_recommended", "missing_optional"]:
            messages.extend(_as_list(source.get(key)))
        if "missing_evidence" in source:
            missing = source.get("missing_evidence") or {}
            messages.extend(_as_list(missing.get("required")))
            messages.extend(_as_list(missing.get("strongly_recommended")))
            messages.extend(_as_list(missing.get("optional")))
        next_evidence = source.get("next_evidence_gathering") or {}
        messages.extend(_as_list(next_evidence.get("messages_to_capture")))
    messages.extend(_mode_compare_missing(mode_compare))
    return _dedupe(messages)


def _merge_next_evidence(diagnosis, manifest):
    merged = {}
    for source in [manifest or {}, diagnosis or {}]:
        plan = source.get("next_evidence_gathering") or {}
        for key, value in plan.items():
            if isinstance(value, list):
                merged.setdefault(key, [])
                merged[key].extend(value)
            elif key not in merged:
                merged[key] = value
    for key, value in list(merged.items()):
        if isinstance(value, list):
            merged[key] = _dedupe(value)
    return merged


def _param_lookup_logging_notes(param_lookup):
    notes = []
    if not param_lookup:
        return notes
    for entry in param_lookup.get("parameters", []) or []:
        name = str(entry.get("name", ""))
        if name.startswith("LOG_") or name.startswith("INS_LOG") or name.startswith("INS_RAW"):
            bits = entry.get("decoded_bits")
            caveat = entry.get("bitmask_caveat") or entry.get("metadata_caveat")
            detail = f"Review {name}"
            if bits:
                detail += ": decoded context is " + ", ".join(str(bit) for bit in bits[:8])
            if caveat:
                detail += f" ({caveat})"
            notes.append(detail)
        for item in entry.get("possibly_missing_for_symptom", []) or []:
            message = item.get("message")
            if message:
                notes.append(f"Review logging configuration for missing {message}.")
    context = param_lookup.get("symptom_context") or {}
    for item in context.get("selected", []) or []:
        name = item.get("name")
        if name and str(name).startswith(("LOG_", "INS_LOG", "INS_RAW")):
            notes.append(f"Review {name} as logging context only; do not infer missing evidence from metadata alone.")
    return _dedupe(notes)


def _mode_compare_missing(mode_compare):
    if not mode_compare:
        return []
    missing = mode_compare.get("missing_evidence") or {}
    out = []
    if isinstance(missing, dict):
        for value in missing.values():
            out.extend(_as_list(value))
    else:
        out.extend(_as_list(missing))
    return _dedupe(out)


def _fft_notes(fft):
    if not fft:
        return [], []
    logging = []
    capture = []
    if fft.get("fft_available") is False:
        reason = fft.get("reason") or fft.get("unavailable_reason") or "FFT unavailable"
        logging.append(f"FFT unavailable: {reason}. Review raw/high-rate IMU or batch-sampler evidence before filter conclusions.")
        guidance = fft.get("next_capture_guidance")
        if guidance:
            capture.extend(_as_list(guidance))
        else:
            capture.append("Use raw/high-rate IMU or batch-sampler capture only if safe, after mechanical checks, and keep it short.")
    return _dedupe(logging), _dedupe(capture)


def _confidence_limits(diagnosis, manifest, mode_compare, fft, limitations):
    out = list(limitations)
    out.extend(_as_list((diagnosis or {}).get("what_cannot_be_concluded")))
    out.extend(_as_list((diagnosis or {}).get("vibration_confidence_limits")))
    out.extend(_as_list((manifest or {}).get("confidence_limits")))
    out.extend(_as_list((mode_compare or {}).get("confidence_limits")))
    if fft and fft.get("fft_available") is False:
        out.append("FFT/filter confidence is limited because usable FFT evidence is unavailable.")
    return _dedupe(out)


def build_next_steps_plan(*, diagnosis=None, mode_compare=None, param_lookup=None, fft=None, manifest=None):
    raw_inputs = {
        "diagnosis": diagnosis,
        "mode_compare": mode_compare,
        "param_lookup": param_lookup,
        "fft": fft,
        "manifest": manifest,
    }
    diagnosis = diagnosis or {}
    manifest = manifest or {}
    limitations = [message for key, message in INPUT_NAMES.items() if raw_inputs.get(key) is None]
    symptom_class = diagnosis.get("symptom_class") or manifest.get("symptom_class") or "general_investigation"
    symptom_text = diagnosis.get("symptom_text") or manifest.get("symptom_text") or symptom_class
    findings = list(diagnosis.get("findings") or [])
    missing_required = _dedupe(_as_list(diagnosis.get("missing_required")) + _as_list((manifest.get("missing_evidence") or {}).get("required")))
    missing_strong = _dedupe(_as_list(diagnosis.get("missing_strongly_recommended")) + _as_list((manifest.get("missing_evidence") or {}).get("strongly_recommended")))
    missing_optional = _dedupe(_as_list(diagnosis.get("missing_optional")) + _as_list((manifest.get("missing_evidence") or {}).get("optional")) + _mode_compare_missing(mode_compare))
    next_evidence = _merge_next_evidence(diagnosis, manifest)
    fft_logging, fft_capture = _fft_notes(fft)
    if fft_capture:
        next_evidence.setdefault("suggested_safe_capture", [])
        next_evidence["suggested_safe_capture"] = _dedupe(next_evidence["suggested_safe_capture"] + fft_capture)
    if fft_logging:
        next_evidence.setdefault("logging_profile_hints", [])
        next_evidence["logging_profile_hints"] = _dedupe(next_evidence["logging_profile_hints"] + fft_logging)

    action_plan = build_diagnosis_action_plan(
        symptom_class=symptom_class,
        symptom_text=symptom_text,
        findings=findings,
        missing_required=missing_required,
        missing_strongly_recommended=missing_strong,
        missing_optional=missing_optional,
        next_evidence_gathering=next_evidence,
        logging_health=diagnosis.get("logging_health") or manifest.get("logging_health") or {},
        mode_comparison=mode_compare,
        fft_availability=fft,
    )
    steps = action_plan["recommended_next_steps"]
    what_not_to_do = _action_by_type(steps, "what_not_to_do")
    logging_changes = _dedupe(_action_by_type(steps, "logging_configuration_checks") + _param_lookup_logging_notes(param_lookup) + fft_logging)
    controlled_capture = _dedupe(_action_by_type(steps, "controlled_evidence_capture") + fft_capture)
    bench_checks = _dedupe(_action_by_type(steps, "bench_mechanical_checks") + _as_list(next_evidence.get("bench_checks_first")))
    reanalysis = _dedupe(_action_by_type(steps, "reanalysis"))

    return {
        "flight_status": action_plan["flight_status"],
        "recommended_next_steps": steps,
        "what_not_to_do": _dedupe(what_not_to_do),
        "missing_evidence_to_capture": _messages_from_plan(diagnosis, manifest, mode_compare=mode_compare),
        "bench_checks_first": bench_checks,
        "logging_changes_to_review": logging_changes,
        "controlled_capture_plan": controlled_capture,
        "reanalysis_plan": reanalysis,
        "confidence_limits": _confidence_limits(diagnosis, manifest, mode_compare, fft, limitations),
        "inputs_used": {
            "diagnosis": bool(diagnosis),
            "mode_compare": bool(mode_compare),
            "param_lookup": bool(param_lookup),
            "fft": bool(fft),
            "manifest": bool(manifest),
        },
        "limitations": limitations,
        "planning_note": "This is a structured planning aid for the agent to inspect; it is not a final diagnosis.",
    }


def _section(lines, heading, items):
    lines.append(f"## {heading}")
    if not items:
        lines.append("- No specific item generated from the supplied evidence.")
    else:
        for item in items:
            lines.append(f"- {item}")
    lines.append("")


def markdown_summary(plan):
    lines = ["# Next-Step Plan", ""]
    status = plan.get("flight_status") or {}
    _section(lines, "Immediate safety gate", [f"{status.get('classification', 'unknown')}: {status.get('reason', 'No reason provided.')}"])
    _section(lines, "Bench checks first", plan.get("bench_checks_first") or [])
    _section(lines, "Logging/configuration checks", plan.get("logging_changes_to_review") or [])
    _section(lines, "Controlled evidence capture", plan.get("controlled_capture_plan") or [])
    _section(lines, "Reanalyse", plan.get("reanalysis_plan") or [])
    _section(lines, "What not to do", plan.get("what_not_to_do") or [])
    _section(lines, "Confidence limits", plan.get("confidence_limits") or [])
    lines.append("_Planning aid only; Codex must inspect the evidence and write the final answer._")
    lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Build a structured next-step plan from existing ArduPilot evidence JSON outputs.")
    parser.add_argument("--diagnosis")
    parser.add_argument("--mode-compare")
    parser.add_argument("--param-lookup")
    parser.add_argument("--fft")
    parser.add_argument("--manifest")
    parser.add_argument("--json", default="next_steps.json")
    parser.add_argument("--summary")
    args = parser.parse_args()

    loaded = {}
    limitations = []
    for key, path in [
        ("diagnosis", args.diagnosis),
        ("mode_compare", args.mode_compare),
        ("param_lookup", args.param_lookup),
        ("fft", args.fft),
        ("manifest", args.manifest),
    ]:
        data, error = _read_json(path)
        loaded[key] = data
        if error:
            limitations.append(error)
    plan = build_next_steps_plan(**loaded)
    plan["confidence_limits"] = _dedupe(plan.get("confidence_limits", []) + limitations)
    plan["limitations"] = _dedupe(plan.get("limitations", []) + limitations)
    write_json(args.json, plan)
    if args.summary:
        Path(args.summary).parent.mkdir(parents=True, exist_ok=True)
        Path(args.summary).write_text(markdown_summary(plan), encoding="utf-8")
    print(f"Next-step plan written: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
