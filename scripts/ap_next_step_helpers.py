from __future__ import annotations


FLIGHT_STATUS_CLASSES = {
    "normal_analysis_only",
    "no_auto_missions",
    "controlled_hover_only",
    "ground_test_only",
    "bench_only",
    "do_not_fly_until_checked",
}


def _dedupe(items):
    out = []
    seen = set()
    for item in items or []:
        text = str(item).strip()
        if text and text not in seen:
            out.append(text)
            seen.add(text)
    return out


def _finding_text(findings):
    parts = []
    for finding in findings or []:
        parts.append(str(finding.get("possible_cause", "")))
        parts.append(str(finding.get("severity", "")))
        parts.append(str(finding.get("interpretation", "")))
        parts.extend(str(e) for e in finding.get("evidence", []) or [])
        parts.extend(str(c) for c in finding.get("recommended_checks", []) or [])
    return " ".join(parts).lower()


def _has_safety_critical_finding(findings):
    return any(str(finding.get("severity", "")).lower() == "safety-critical" for finding in findings or [])


def _confidence(findings, missing_required, logging_health, classification):
    if logging_health and logging_health.get("limits_diagnosis"):
        return "low"
    if missing_required:
        return "low"
    if classification in {"do_not_fly_until_checked", "bench_only", "ground_test_only"}:
        return "high" if _has_safety_critical_finding(findings) else "medium"
    if findings:
        return "medium"
    return "high"


def _mode_comparison_indicates_auto_issue(mode_comparison):
    if not mode_comparison:
        return False
    ranking = mode_comparison.get("ranking") or []
    if ranking:
        top = str(ranking[0].get("decoded_mode") or ranking[0].get("mode") or "").upper()
        if top == "AUTO":
            return True
    comparisons = mode_comparison.get("mode_comparisons") or []
    auto = [item for item in comparisons if str(item.get("decoded_mode") or item.get("mode") or "").upper() == "AUTO"]
    others = [item for item in comparisons if str(item.get("decoded_mode") or item.get("mode") or "").upper() not in {"AUTO", ""}]
    if auto and others:
        auto_score = auto[0].get("ranking_score")
        other_scores = [item.get("ranking_score") for item in others if item.get("ranking_score") is not None]
        if auto_score is not None and other_scores and auto_score > max(other_scores):
            return True
    return False


def _flight_status(symptom_class, symptom_text, findings, missing_required, missing_strongly, logging_health, mode_comparison=None):
    text = " ".join([str(symptom_class or ""), str(symptom_text or ""), _finding_text(findings)]).lower()
    has_safety = _has_safety_critical_finding(findings)

    if symptom_class == "crash_or_loss_of_control" or any(token in text for token in ["crash", "loss of control", "uncontrollable", "flyaway"]):
        classification = "do_not_fly_until_checked"
        reason = "Crash or loss-of-control evidence is safety-critical and cannot be cleared from logs alone."
    elif any(token in text for token in ["brownout", "power fault", "board-power", "battery failsafe"]) and (has_safety or symptom_class == "battery_power_issue"):
        classification = "do_not_fly_until_checked"
        reason = "Unresolved power or battery-failsafe uncertainty can remove control margin."
    elif symptom_class == "motor_esc_issue" or any(token in text for token in ["motor", "esc", "prop", "actuator", "output saturation"]):
        classification = "bench_only"
        reason = "Motor, ESC, prop, actuator, or output-saturation evidence needs hardware checks before flight activity."
    elif symptom_class == "rc_failsafe_prearm_issue" or any(token in text for token in ["prearm", "pre-arm", "radio failsafe", "rc failsafe", "arming"]):
        classification = "ground_test_only"
        reason = "RC, arming, pre-arm, or failsafe evidence should be resolved with ground/pre-arm checks before flight."
    elif (
        symptom_class in {"yaw_misbehaviour", "ekf_gps_issue", "compass_yaw_source_issue"}
        and (any(token in text for token in ["auto", "mission", "waypoint", "navigation"]) or _mode_comparison_indicates_auto_issue(mode_comparison))
    ):
        classification = "no_auto_missions"
        reason = "Mission/navigation-specific yaw or estimator behaviour is not trusted until mode-scoped evidence is checked."
    elif symptom_class in {"yaw_misbehaviour", "attitude_rate_issue", "vibration_issue"} or any(token in text for token in ["wobble", "vibration", "oscillation", "rate tracking"]):
        classification = "controlled_hover_only"
        reason = "Attitude, yaw, or vibration investigation should use only a short controlled hover capture after mechanical checks."
    elif missing_required or missing_strongly or has_safety:
        classification = "controlled_hover_only" if not has_safety else "do_not_fly_until_checked"
        reason = "Missing evidence limits confidence; use the lowest-risk activity that can collect the needed evidence."
    else:
        classification = "normal_analysis_only"
        reason = "No safety-relevant next flight activity is implied by the available planning inputs."

    return {
        "classification": classification,
        "reason": reason,
        "confidence": _confidence(findings, missing_required, logging_health, classification),
    }


def _step(priority, step_type, action, reason, applies_to, source_evidence):
    return {
        "priority": priority,
        "type": step_type,
        "action": action,
        "reason": reason,
        "applies_to": _dedupe(applies_to),
        "source_evidence": _dedupe(source_evidence),
    }


def _messages_to_capture(missing_required, missing_strongly, missing_optional):
    messages = _dedupe(list(missing_required or []) + list(missing_strongly or []))
    optional = set(missing_optional or [])
    if "ESC" in optional or "ESCX" in optional or "EDT2" in optional:
        messages.append("ESC telemetry if supported")
    return _dedupe(messages)


def build_diagnosis_action_plan(
    *,
    symptom_class,
    symptom_text="",
    findings=None,
    missing_required=None,
    missing_strongly_recommended=None,
    missing_optional=None,
    next_evidence_gathering=None,
    logging_health=None,
    mode_comparison=None,
    fft_availability=None,
):
    """Build planning aids for the agent's final answer.

    These fields are structured evidence guidance, not a generated final user answer.
    """
    findings = findings or []
    missing_required = list(missing_required or [])
    missing_strongly = list(missing_strongly_recommended or [])
    missing_optional = list(missing_optional or [])
    next_evidence_gathering = next_evidence_gathering or {}
    logging_health = logging_health or {}
    source = []
    if findings:
        source.append("diagnosis.findings")
    if missing_required:
        source.append("missing_required: " + ", ".join(missing_required))
    if missing_strongly:
        source.append("missing_strongly_recommended: " + ", ".join(missing_strongly))
    if missing_optional:
        source.append("missing_optional: " + ", ".join(missing_optional))
    if next_evidence_gathering:
        source.append("next_evidence_gathering")
    if logging_health.get("limits_diagnosis"):
        source.append("logging_health.limits_diagnosis")
    if mode_comparison:
        source.append("mode_comparison")
    if fft_availability:
        source.append("fft_availability")

    status = _flight_status(symptom_class, symptom_text, findings, missing_required, missing_strongly, logging_health, mode_comparison=mode_comparison)
    classification = status["classification"]
    applies_to = [str(symptom_class or "general_investigation")]
    text = " ".join([str(symptom_text or ""), _finding_text(findings)]).lower()
    steps = []

    immediate_actions = {
        "do_not_fly_until_checked": "Do not fly until checked; complete hardware, power, failsafe, and configuration checks before any further flight activity.",
        "bench_only": "Keep this to bench-only checks first; do not fly for evidence until the suspected hardware or power issue is resolved.",
        "ground_test_only": "Use ground/pre-arm testing only until the RC, arming, or failsafe issue is understood and resolved.",
        "no_auto_missions": "Pause AUTO/mission flying; do not resume mission operation from this diagnosis alone.",
        "controlled_hover_only": "After mechanical and configuration checks pass, limit any new capture to a short controlled hover.",
        "normal_analysis_only": "Continue normal log analysis; no safety-relevant flight activity is requested by this plan.",
    }
    steps.append(_step(1, "immediate_safety_gate", immediate_actions[classification], status["reason"], applies_to, source))

    if classification != "normal_analysis_only":
        if symptom_class in {"yaw_misbehaviour", "attitude_rate_issue", "vibration_issue"} or any(token in text for token in ["yaw", "wobble", "vibration", "oscillation", "mission"]):
            bench_action = "Inspect props, motor bearings, arm stiffness, frame damage, flight-controller mounting, wiring contact, compass/GPS wiring proximity, and payload movement."
        elif classification == "bench_only":
            bench_action = "Inspect props, motor order/direction, motor bearings, ESCs, solder joints, connectors, frame damage, output mapping, and battery/connector condition."
        else:
            bench_action = "Inspect the implicated hardware, wiring, mounting, power path, and vehicle setup before collecting more evidence."
        steps.append(_step(2, "bench_mechanical_checks", bench_action, "Mechanical faults must be cleared before interpreting tuning or requesting flight evidence.", applies_to, source))

    safety_action = None
    if symptom_class in {"yaw_misbehaviour", "ekf_gps_issue", "compass_yaw_source_issue"} or any(token in text for token in ["auto", "mission", "yaw", "gps", "compass"]):
        safety_action = "Check compass/GPS yaw behaviour, EKF/GPS messages, battery failsafe warnings, and radio failsafe warnings in the timeline."
    elif classification in {"ground_test_only", "do_not_fly_until_checked"}:
        safety_action = "Check the exact arming, pre-arm, radio, battery, GPS, EKF, compass, and safety-switch warnings before changing configuration."
    if safety_action:
        steps.append(_step(3, "safety_warning_failsafe_checks", safety_action, "Failsafe and estimator warnings can change the safe evidence-gathering path.", applies_to, source))

    capture_messages = _messages_to_capture(missing_required, missing_strongly, missing_optional)
    logging_actions = []
    if capture_messages:
        logging_actions.append("Capture or review logging for " + ", ".join(capture_messages) + ".")
    if symptom_class == "yaw_misbehaviour":
        logging_actions.append("Ensure the next relevant capture includes PIDY, PIDR/PIDP if wobble spans axes, RATE/ATT, RCOU/RCO2/RCO3, BAT/POWR, and ESC telemetry if supported.")
    if logging_health.get("limits_diagnosis"):
        logging_actions.append("Check logging dropouts, timestamp gaps, and parser health before trusting absence of short events.")
    for item in next_evidence_gathering.get("logging_profile_hints", []) or []:
        logging_actions.append(item)
    for item in next_evidence_gathering.get("hardware_support_dependent_evidence", []) or []:
        logging_actions.append(item)
    if fft_availability and fft_availability.get("fft_available") is False:
        logging_actions.append("Use ap_log_fft.py unavailable/quality details before requesting raw/high-rate IMU capture.")
    if logging_actions:
        steps.append(_step(4, "logging_configuration_checks", " ".join(_dedupe(logging_actions)), "Missing or degraded evidence should be fixed explicitly before drawing stronger conclusions.", applies_to, source))

    if classification in {"no_auto_missions", "controlled_hover_only"}:
        if symptom_class == "yaw_misbehaviour" or any(token in text for token in ["yaw", "mission", "auto", "wobble"]):
            capture_action = "Only after checks pass, capture a short controlled hover with small yaw/roll/pitch inputs; do not use AUTO/mission mode for the diagnostic capture."
        else:
            capture_action = "Only after checks pass, capture the shortest low-risk controlled hover or ground activity needed for the missing evidence."
        for item in next_evidence_gathering.get("suggested_safe_capture", []) or []:
            if item not in capture_action:
                capture_action += " " + item
        steps.append(_step(5, "controlled_evidence_capture", capture_action, "The next activity should collect evidence without repeating the higher-risk symptom scenario.", applies_to, source))

    if classification != "normal_analysis_only" or findings or missing_required or missing_strongly:
        reanalysis = "Reanalyse the new log and parameter context before tuning or returning to higher-risk operation."
        if symptom_class == "yaw_misbehaviour" and ("auto" in text or "mission" in text):
            reanalysis = "Re-run mode comparison and diagnosis before tuning or resuming AUTO/mission work."
        steps.append(_step(6, "reanalysis", reanalysis, "New evidence should be inspected before conclusions are upgraded or tuning changes are made.", applies_to, source))

    dont = []
    if classification == "do_not_fly_until_checked":
        dont.append("Do not repeat the flight or mission to see if it happens again.")
    if classification == "no_auto_missions":
        dont.append("Do not repeat the mission to see if it happens again.")
    dont.append("Do not make blind parameter changes or tune around unresolved mechanical, power, estimator, or failsafe evidence.")
    dont.append("Do not disable arming, EKF, GPS, battery, compass, radio, fence, or other failsafe protections as a routine fix.")
    for item in next_evidence_gathering.get("do_not_attempt", []) or []:
        dont.append(item)
    if classification != "normal_analysis_only" or missing_required or missing_strongly or findings:
        steps.append(_step(7, "what_not_to_do", " ".join(_dedupe(dont)), "Unsafe shortcuts can hide the root cause or increase risk.", applies_to, source))

    return {
        "flight_status": status,
        "recommended_next_steps": sorted(steps, key=lambda item: item["priority"]),
    }
