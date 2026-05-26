#!/usr/bin/env python3
from __future__ import annotations

from typing import Iterable, Mapping, Sequence

RESULT_VALUES = {"pass", "conditional_pass", "fail", "inconclusive", "not_applicable"}
SAFETY_GATE_VALUES = {"proceed", "proceed_with_caution", "repeat_step", "do_not_proceed", "bench_check_required"}

SAFETY_SEVERITIES = {"safety-critical", "critical", "blocker", "fail"}
BENCH_TERMS = ("motor", "esc", "prop", "wiring", "frame", "hot", "heat", "burn", "smoke")


def normalize_manual_observations(observations: Sequence[str] | None) -> list[str]:
    return [str(item).strip() for item in (observations or []) if str(item).strip()]


def missing_manual_observations(required: Sequence[str] | None, provided: Sequence[str] | None) -> list[str]:
    required_items = list(required or [])
    provided_text = " | ".join(normalize_manual_observations(provided)).lower()
    if not provided_text:
        return required_items
    missing = []
    for item in required_items:
        tokens = [token for token in str(item).lower().replace("/", " ").split() if len(token) >= 4]
        if tokens and any(token in provided_text for token in tokens):
            continue
        missing.append(item)
    return missing


def finding_is_safety_blocker(finding: Mapping) -> bool:
    severity = str(finding.get("severity", "")).lower()
    if severity in SAFETY_SEVERITIES:
        return True
    text = " ".join(str(finding.get(key, "")) for key in ("finding", "interpretation", "summary")).lower()
    return any(term in text for term in ("unsafe", "loss of control", "saturation", "oscillation", "hard to control"))


def classify_from_findings(
    *,
    findings: Iterable[Mapping],
    missing_required: Sequence[str] | None = None,
    missing_manual: Sequence[str] | None = None,
    has_bench_relevant_blocker: bool = False,
    not_applicable: bool = False,
) -> tuple[str, str]:
    if not_applicable:
        return "not_applicable", "proceed_with_caution"

    findings_list = list(findings or [])
    blockers = [f for f in findings_list if finding_is_safety_blocker(f)]
    if blockers:
        if has_bench_relevant_blocker or any(_bench_related(f) for f in blockers):
            return "fail", "bench_check_required"
        return "fail", "do_not_proceed"

    if missing_required:
        return "inconclusive", "repeat_step"

    if missing_manual:
        return "conditional_pass", "proceed_with_caution"

    caution_findings = [
        f for f in findings_list
        if str(f.get("severity", "")).lower() in {"worth-checking", "warning", "caution", "medium"}
    ]
    if caution_findings:
        return "conditional_pass", "proceed_with_caution"

    return "pass", "proceed"


def _bench_related(finding: Mapping) -> bool:
    text = " ".join(str(finding.get(key, "")) for key in ("finding", "interpretation", "recommended_checks")).lower()
    return any(term in text for term in BENCH_TERMS)


def conservative_gate(result: str, *, has_manual_missing: bool = False, has_safety_blocker: bool = False) -> str:
    if has_safety_blocker:
        return "bench_check_required"
    if result == "pass":
        return "proceed_with_caution" if has_manual_missing else "proceed"
    if result == "conditional_pass":
        return "proceed_with_caution"
    if result == "fail":
        return "do_not_proceed"
    if result == "not_applicable":
        return "proceed_with_caution"
    return "repeat_step"
