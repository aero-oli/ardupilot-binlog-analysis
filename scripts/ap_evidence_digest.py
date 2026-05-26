#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from ap_artifact_recommendations import merge_recommended_artifacts
from ap_common import write_json


INPUT_LABELS = {
    "diagnosis": "diagnosis input missing",
    "mode_compare": "mode comparison input missing",
    "param_lookup": "parameter lookup input missing",
    "fft": "FFT input missing",
    "manifest": "manifest input missing",
    "next_steps": "next-step input missing",
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


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


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


def _finding_label(finding):
    cause = finding.get("possible_cause") or finding.get("check") or finding.get("name") or "Finding"
    confidence = finding.get("confidence")
    severity = finding.get("severity")
    prefix = cause
    if confidence or severity:
        prefix += f" ({', '.join(str(v) for v in [confidence, severity] if v)})"
    evidence = _as_list(finding.get("evidence"))
    if evidence:
        prefix += ": " + "; ".join(str(item) for item in evidence[:3])
    return prefix


def _findings_by_confidence(diagnosis):
    grouped = {"high": [], "medium": [], "low": [], "unknown": []}
    for finding in (diagnosis or {}).get("findings", []) or []:
        confidence = str(finding.get("confidence") or "unknown").lower()
        if confidence not in grouped:
            confidence = "unknown"
        grouped[confidence].append(_finding_label(finding))
    return grouped


def _checked_but_not_supported(diagnosis):
    out = []
    for item in (diagnosis or {}).get("checked_but_not_supported", []) or []:
        if isinstance(item, dict):
            out.append(f"{item.get('check', 'Check')}: {item.get('result', item)}")
        else:
            out.append(str(item))
    for item in (diagnosis or {}).get("checked", []) or []:
        if isinstance(item, dict) and "not" in str(item.get("result", "")).lower():
            out.append(f"{item.get('check', 'Check')}: {item.get('result')}")
    return _dedupe(out)


def _missing_from_source(source):
    out = []
    for key in ["missing_required", "missing_strongly_recommended", "missing_optional"]:
        values = _as_list((source or {}).get(key))
        if values:
            out.append(f"{key}: {', '.join(str(v) for v in values)}")
    for key in ["missing_evidence_to_capture", "bench_checks_first", "logging_changes_to_review"]:
        values = _as_list((source or {}).get(key))
        if values:
            out.append(f"{key}: {'; '.join(str(v) for v in values)}")
    missing = (source or {}).get("missing_evidence") or {}
    if isinstance(missing, dict):
        for key in ["required", "strongly_recommended", "optional", "optional_context"]:
            values = _as_list(missing.get(key))
            if values:
                out.append(f"missing_{key}: {', '.join(str(v) for v in values)}")
    return out


def _mode_highlights(mode_compare):
    if not mode_compare:
        return []
    out = []
    ranking = mode_compare.get("ranking") or []
    if len(ranking) >= 2:
        out.append(f"{ranking[0].get('decoded_mode') or ranking[0].get('query')} ranks worse than {ranking[-1].get('decoded_mode') or ranking[-1].get('query')} in mode comparison.")
    elif ranking:
        out.append(f"{ranking[0].get('decoded_mode') or ranking[0].get('query')} is the highest-ranked mode in the comparison.")
    if mode_compare.get("modes_found"):
        out.append("Modes found: " + ", ".join(str(v) for v in mode_compare.get("modes_found", [])))
    if mode_compare.get("requested_modes_missing"):
        out.append("Requested modes missing: " + ", ".join(str(v) for v in mode_compare.get("requested_modes_missing", [])))
    if mode_compare.get("manual_control_confidence"):
        out.append(f"Manual-control confidence: {mode_compare.get('manual_control_confidence')}")
    out.extend(_as_list(mode_compare.get("manual_control_limitations")))
    return _dedupe(out)


def _parameter_context(param_lookup):
    if not param_lookup:
        return []
    out = []
    for entry in param_lookup.get("parameters", []) or []:
        name = entry.get("name")
        if not name:
            continue
        value = entry.get("logged_value", entry.get("value"))
        units = entry.get("units")
        text = f"{name}={value}"
        if units:
            text += f" {units}"
        for key in ["meaning", "description", "metadata_note", "note"]:
            if entry.get(key):
                text += f" - {entry.get(key)}"
                break
        out.append(text)
    if param_lookup.get("note"):
        out.append(str(param_lookup["note"]))
    if param_lookup.get("parameter_source_precedence"):
        out.append(str(param_lookup["parameter_source_precedence"]))
    return _dedupe(out)


def _fft_status(fft):
    if not fft:
        return []
    if fft.get("fft_available") is False or fft.get("available") is False:
        reason = fft.get("reason") or fft.get("unavailable_reason") or "unusable FFT evidence"
        out = [f"FFT unavailable: {reason}"]
    elif fft.get("fft_available") is True or fft.get("available") is True:
        out = ["FFT available; inspect peaks and data quality before using filter/noise conclusions."]
    else:
        out = ["FFT status present but availability is unclear; inspect FFT JSON."]
    for item in _as_list(fft.get("reason_detail")) + _as_list(fft.get("next_capture_guidance")):
        out.append(str(item))
    return _dedupe(out)


def _timeline_context(diagnosis, manifest):
    out = []
    relative = (diagnosis or {}).get("events_relative_to_window") or (manifest or {}).get("events_relative_to_window") or {}
    for key, label in [
        ("inside_window", "inside-window"),
        ("before_window", "before-window"),
        ("after_window", "after-window"),
    ]:
        for item in relative.get(key, [])[:8]:
            out.append(f"{label} {item.get('source')} t={item.get('time_s')}: {item.get('label')}")
    for source in [diagnosis or {}, manifest or {}]:
        for key in ["decoded_errors", "timeline", "events", "warnings"]:
            value = source.get(key)
            if key == "decoded_errors":
                for item in value or []:
                    out.append(f"ERR t={item.get('time_s')} Subsys={item.get('subsys')} ECode={item.get('ecode')}: {item.get('meaning')} ({item.get('confidence')})")
            else:
                for item in _as_list(value):
                    out.append(str(item))
    return _dedupe(out)


def _in_window_evidence(diagnosis):
    if not diagnosis:
        return []
    out = []
    window = diagnosis.get("analysis_window") or {}
    if window:
        label = window.get("rule") or "selected window"
        start = window.get("start_s")
        end = window.get("end_s")
        out.append(f"{label}: start_s={start}, end_s={end}")
    for finding in diagnosis.get("findings", []) or []:
        out.append(_finding_label(finding))
    relative = diagnosis.get("events_relative_to_window") or {}
    for item in relative.get("inside_window", [])[:8]:
        out.append(f"inside-window {item.get('source')} t={item.get('time_s')}: {item.get('label')}")
    return _dedupe(out)


def _post_flight_prearm_context(diagnosis, manifest):
    out = []
    keywords = ("prearm", "pre-arm", "post-flight", "post flight", "disarm", "arming", "failsafe")
    for source in [diagnosis or {}, manifest or {}]:
        relative = source.get("events_relative_to_window") or {}
        for item in relative.get("after_window", [])[:12]:
            out.append(f"after-window {item.get('source')} t={item.get('time_s')}: {item.get('label')}")
    for item in _timeline_context(diagnosis, manifest):
        if any(keyword in item.lower() for keyword in keywords):
            out.append(item)
    for source in [diagnosis or {}, manifest or {}]:
        for item in _as_list(source.get("warnings")):
            if any(keyword in str(item).lower() for keyword in keywords):
                out.append(str(item))
    return _dedupe(out)


def _next_steps(next_steps, diagnosis):
    out = []
    source = next_steps or diagnosis or {}
    if source.get("flight_status"):
        status = source["flight_status"]
        out.append(f"flight_status={status.get('classification')}: {status.get('reason', '')}".strip())
    for step in source.get("recommended_next_steps", []) or []:
        if isinstance(step, dict):
            action = step.get("action")
            if action:
                out.append(f"{step.get('priority', '')}. {action}".strip())
        else:
            out.append(str(step))
    for item in source.get("what_not_to_do", []) or []:
        out.append("Do not: " + str(item))
    return _dedupe(out)


def _confidence_limits(diagnosis, mode_compare, manifest, fft, input_limitations):
    out = list(input_limitations)
    for source in [diagnosis or {}, mode_compare or {}, manifest or {}]:
        out.extend(_as_list(source.get("confidence_limits")))
        out.extend(_as_list(source.get("what_cannot_be_concluded")))
        out.extend(_as_list(source.get("manual_control_limitations")))
        logging_health = source.get("logging_health") or {}
        if logging_health.get("limits_diagnosis"):
            out.append(logging_health.get("confidence_impact") or "Logging health limits diagnosis confidence.")
    for source in [diagnosis or {}, manifest or {}, mode_compare or {}]:
        missing_text = "\n".join(_missing_from_source(source))
        if "PID" in missing_text or "PIDY" in missing_text or "PIDR" in missing_text or "PIDP" in missing_text:
            out.append("Missing PID evidence limits controller/tuning conclusions.")
        if "ESC" in missing_text:
            out.append("Missing ESC telemetry limits motor/ESC conclusions.")
    if fft and (fft.get("fft_available") is False or fft.get("available") is False):
        out.append("Unusable FFT evidence limits noise/filter conclusions.")
    return _dedupe(out)


def _recommended_artifacts(diagnosis, mode_compare, param_lookup, fft, manifest, next_steps):
    merged = merge_recommended_artifacts(mode_compare or {}, diagnosis or {}, next_steps or {}, manifest or {}, fft or {})
    if merged:
        return merged
    out = []
    for source in [manifest or {}]:
        artifacts = source.get("key_artifacts") or {}
        if isinstance(artifacts, dict):
            for label, path in artifacts.items():
                out.append({"label": str(label).replace("_", " "), "path": str(path), "why": "case-level evidence artifact for agent review", "priority": 99})
    return out[:8]


def _control_evidence_completeness(diagnosis, mode_compare, manifest):
    for source in [diagnosis or {}, mode_compare or {}, manifest or {}]:
        completeness = source.get("control_evidence_completeness")
        if completeness:
            return completeness
    return None


def build_evidence_digest(*, diagnosis=None, mode_compare=None, param_lookup=None, fft=None, manifest=None, next_steps=None, input_limitations=None):
    input_limitations = list(input_limitations or [])
    observations = []
    observations.extend(_finding_label(finding) for finding in (diagnosis or {}).get("findings", [])[:5])
    observations.extend(_mode_highlights(mode_compare)[:3])
    if diagnosis and mode_compare and param_lookup:
        observations.append("Diagnosis, mode comparison, and parameter context are all present; cross-check timing before promoting any hypothesis.")
    observations = _dedupe(observations)

    completeness = _control_evidence_completeness(diagnosis, mode_compare, manifest)
    digest = {
        "digest_note": "Evidence digest for the agent. This is not a final diagnosis and does not replace final reasoning.",
        "strongest_supported_observations": observations,
        "findings_by_confidence": _findings_by_confidence(diagnosis),
        "findings_checked_but_not_supported": _checked_but_not_supported(diagnosis),
        "mode_comparison_highlights": _mode_highlights(mode_compare),
        "in_window_evidence": _in_window_evidence(diagnosis),
        "post_flight_prearm_context": _post_flight_prearm_context(diagnosis, manifest),
        "missing_evidence": _dedupe(_missing_from_source(diagnosis) + _missing_from_source(manifest) + _missing_from_source(mode_compare) + _missing_from_source(next_steps)),
        "parameter_context": _parameter_context(param_lookup),
        "fft_noise_status": _fft_status(fft),
        "timeline_failsafe_context": _timeline_context(diagnosis, manifest),
        "control_evidence_completeness": completeness,
        "safety_gate_next_steps": _next_steps(next_steps, diagnosis),
        "recommended_user_artifacts": _recommended_artifacts(diagnosis, mode_compare, param_lookup, fft, manifest, next_steps),
        "confidence_limits": _confidence_limits(diagnosis, mode_compare, manifest, fft, input_limitations),
        "source_presence": {
            "diagnosis": diagnosis is not None,
            "mode_compare": mode_compare is not None,
            "param_lookup": param_lookup is not None,
            "fft": fft is not None,
            "manifest": manifest is not None,
            "next_steps": next_steps is not None,
        },
    }
    return digest


def markdown_summary(digest):
    sections = [
        ("Strongest supported observations", digest.get("strongest_supported_observations", [])),
        ("Findings by confidence", [
            f"{level}: " + ("; ".join(items) if items else "none")
            for level, items in (digest.get("findings_by_confidence") or {}).items()
        ]),
        ("Findings checked but not supported", digest.get("findings_checked_but_not_supported", [])),
        ("Mode comparison highlights", digest.get("mode_comparison_highlights", [])),
        ("In-window evidence", digest.get("in_window_evidence", [])),
        ("Post-flight/pre-arm context", digest.get("post_flight_prearm_context", [])),
        ("Missing evidence", digest.get("missing_evidence", [])),
        ("Parameter context", digest.get("parameter_context", [])),
        ("FFT/noise status", digest.get("fft_noise_status", [])),
        ("Timeline/failsafe context", digest.get("timeline_failsafe_context", [])),
        ("Control evidence completeness", _format_control_evidence_completeness(digest.get("control_evidence_completeness"))),
        ("Safety gate / next steps", digest.get("safety_gate_next_steps", [])),
        ("Recommended user artifacts", _format_user_artifacts(digest.get("recommended_user_artifacts", []))),
    ]
    lines = ["# Evidence Digest", "", digest.get("digest_note", "Evidence digest for the agent. This is not a final diagnosis."), ""]
    for title, items in sections:
        lines.append(f"## {title}")
        if items:
            lines.extend(f"- {item}" for item in items[:12])
        else:
            lines.append("- none")
        lines.append("")
    if digest.get("confidence_limits"):
        lines.append("## Confidence limits")
        lines.extend(f"- {item}" for item in digest["confidence_limits"])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _format_control_evidence_completeness(completeness):
    if not completeness:
        return []
    order = [
        "overall",
        "attitude_tracking",
        "rate_tracking",
        "pid_terms",
        "actuator_outputs",
        "esc_telemetry",
        "rc_input",
        "vibration",
        "fft",
        "gps_ekf",
        "parameter_context",
    ]
    items = [f"{key}: {completeness.get(key)}" for key in order if completeness.get(key)]
    items.extend(str(item) for item in completeness.get("confidence_limits", [])[:6])
    return items


def _format_user_artifacts(artifacts):
    out = []
    for item in artifacts or []:
        if isinstance(item, dict):
            label = item.get("label") or item.get("path")
            path = item.get("path")
            why = item.get("why")
            if path and why:
                out.append(f"{label}: {path} ({why})")
            elif path:
                out.append(f"{label}: {path}")
        elif item:
            out.append(str(item))
    return out


def build_from_paths(args):
    loaded = {}
    limitations = []
    for name in ["diagnosis", "mode_compare", "param_lookup", "fft", "manifest", "next_steps"]:
        value, error = _read_json(getattr(args, name))
        loaded[name] = value
        if error:
            limitations.append(error)
        elif getattr(args, name) is None:
            limitations.append(INPUT_LABELS[name])
    return build_evidence_digest(**loaded, input_limitations=limitations)


def main() -> int:
    parser = argparse.ArgumentParser(description="Merge ArduPilot investigation outputs into a concise evidence digest for the agent.")
    parser.add_argument("--diagnosis")
    parser.add_argument("--mode-compare", dest="mode_compare")
    parser.add_argument("--param-lookup", dest="param_lookup")
    parser.add_argument("--fft")
    parser.add_argument("--manifest")
    parser.add_argument("--next-steps", dest="next_steps")
    parser.add_argument("--json", help="Write digest JSON")
    parser.add_argument("--summary", help="Write digest Markdown summary")
    args = parser.parse_args()
    digest = build_from_paths(args)
    if args.json:
        write_json(args.json, digest)
    if args.summary:
        Path(args.summary).parent.mkdir(parents=True, exist_ok=True)
        Path(args.summary).write_text(markdown_summary(digest), encoding="utf-8")
    if not args.json and not args.summary:
        print(json.dumps(digest, indent=2, sort_keys=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
