from __future__ import annotations

from ap_common import (
    battery_instance_groups,
    combined_rcout_dataframe,
    esc_instance_groups,
    get_col,
    motor_channels_from_mapping,
    numeric_series,
    output_channel_columns,
    output_channel_label,
    output_mapping_from_tables,
)
from ap_units import unit_for_name, value_with_unit


def vals(s):
    if s is None:
        return []
    try:
        return [float(v) for v in s.dropna().tolist()]
    except Exception:
        return []


def _add_finding(findings, rank, possible_cause, severity, confidence, evidence, interpretation, recommended_checks, evidence_values=None):
    evidence = [e for e in evidence if e]
    if not evidence:
        return
    findings.append({
        "rank": rank,
        "possible_cause": possible_cause,
        "severity": severity,
        "confidence": confidence,
        "evidence": evidence,
        "evidence_values": evidence_values or [],
        "interpretation": interpretation,
        "recommended_checks": recommended_checks,
    })


def _add_context(context, source, detail, values=None):
    if detail:
        item = {"source": source, "detail": detail}
        if values:
            item["values"] = values
        context.append(item)


def add_motor_esc_findings(tables, findings, checked, context=None, rank=1):
    if context is None:
        context = []
    evidence = []
    evidence_values = []
    rcou = combined_rcout_dataframe(tables)
    if rcou is not None:
        channels = output_channel_columns(rcou)
        mapping = output_mapping_from_tables(tables)
        motor_channels = motor_channels_from_mapping(mapping, channels)
        for c in [ch for ch in channels if ch in motor_channels]:
            s = numeric_series(rcou, [c])
            if s is None or len(s.dropna()) == 0:
                continue
            high_pct = float((s >= 1900).mean() * 100)
            low_pct = float((s <= 1100).mean() * 100)
            if high_pct > 1 or low_pct > 1:
                evidence.append(f"RCOUT.{output_channel_label(c, mapping)}: {high_pct:.1f}% >=1900us, {low_pct:.1f}% <=1100us")
                evidence_values.extend([
                    value_with_unit(f"RCOUT.{c}.pct_high_ge_1900", high_pct, "%"),
                    value_with_unit(f"RCOUT.{c}.pct_low_le_1100", low_pct, "%"),
                ])
    for group in esc_instance_groups(tables):
        msg = group["message"]
        esc = group["df"]
        label = group["label"]
        if group.get("instance_note"):
            _add_context(context, msg, group["instance_note"])
        err_col = get_col(esc, ["Err"])
        if err_col:
            err = numeric_series(esc, [err_col])
            if err is not None and len(err.dropna()) > 0 and float(err.max()) > 0:
                evidence.append(f"{label}.{err_col} max={float(err.max()):.2f}")
                evidence_values.append(value_with_unit(f"{label}.{err_col}_max", float(err.max()), "count"))
        status_col = get_col(esc, ["Status"])
        if status_col:
            status = numeric_series(esc, [status_col])
            if status is not None and len(status.dropna()) > 0:
                s_i = status.fillna(0).astype(int)
                alert = int(((s_i & 4) != 0).sum())
                warning = int(((s_i & 8) != 0).sum())
                error = int(((s_i & 16) != 0).sum())
                if alert or warning or error:
                    detail = f"{label} status alert/warning/error counts={alert}/{warning}/{error}"
                    if msg != label:
                        detail = f"{msg} status alert/warning/error counts={alert}/{warning}/{error} ({label})"
                    evidence.append(detail)
        for col in ["RPM", "RawRPM", "Curr", "Temp", "MotTemp", "Stress", "MaxStress", "ErrCnt", "inpct", "outpct", "Pwr"]:
            if col in esc.columns:
                s = numeric_series(esc, [col])
                if s is not None and len(s.dropna()) > 0:
                    if msg == "EDT2" and col == "ErrCnt" and float(s.max()) > 0:
                        evidence.append(f"{msg} {col}: min={float(s.min()):.2f}, max={float(s.max()):.2f} ({label})")
                        evidence_values.extend([
                            value_with_unit(f"{msg}.{col}_min", float(s.min())),
                            value_with_unit(f"{msg}.{col}_max", float(s.max())),
                        ])
                    else:
                        prefix = label if msg == "ESC" else (msg if msg != label else label)
                        suffix = f" ({label})" if msg != label else ""
                        unit = unit_for_name(col, message=msg, field=col)
                        _add_context(context, label, f"{prefix} {col}: min={float(s.min()):.2f} {unit}, max={float(s.max()):.2f} {unit}{suffix}", [
                            value_with_unit(f"{prefix}.{col}_min", float(s.min()), unit),
                            value_with_unit(f"{prefix}.{col}_max", float(s.max()), unit),
                        ])
                        if msg == "ESC" and msg != label:
                            _add_context(context, msg, f"{msg} {col}: min={float(s.min()):.2f} {unit}, max={float(s.max()):.2f} {unit} ({label})", [
                                value_with_unit(f"{msg}.{col}_min", float(s.min()), unit),
                                value_with_unit(f"{msg}.{col}_max", float(s.max()), unit),
                            ])
        flags = numeric_series(esc, ["flags"])
        if flags is not None and len(flags.dropna()) > 0:
            count = int((flags.fillna(0).astype(int) != 0).sum())
            if count:
                evidence.append(f"{msg} flags nonzero samples={count} ({label})")
                evidence_values.append(value_with_unit(f"{msg}.flags_nonzero_samples", count, "count"))
    if evidence:
        _add_finding(
            findings, rank, "Motor output saturation or ESC telemetry abnormality", "safety-critical", "high" if any("RCOUT" in e or "status" in e for e in evidence) else "medium",
            evidence[:14],
            "Requested outputs near conventional limits or ESC error/status evidence can indicate limited actuator headroom, wrong motor/prop setup, ESC/motor faults, or power-related thrust loss.",
            ["Verify motor order, prop direction, frame class/type and output mapping", "Bench inspect affected motor/ESC/wiring", "Correlate RCOU/ESC evidence with RATE errors, battery sag and mode changes before tuning"],
            evidence_values[:20],
        )
    else:
        checked.append({"check": "Motor outputs / ESC telemetry", "result": "No RCOU/RCO2/RCO3 saturation or ESC error/status issue detected by heuristic"})


def add_power_findings(tables, findings, checked, context=None, rank=3):
    if context is None:
        context = []
    evidence = []
    evidence_values = []
    for group in battery_instance_groups(tables):
        bat = group["df"]
        label = group["label"]
        if group.get("instance_note"):
            _add_context(context, group["message"], group["instance_note"])
        volt = numeric_series(bat, ["Volt", "VoltR", "V"])
        curr = numeric_series(bat, ["Curr", "I"])
        if volt is not None and len(volt.dropna()) > 0:
            vmin, vmax = float(volt.min()), float(volt.max())
            source = group["message"] if not group.get("instance_certain") else label
            prefix = group["message"] if not group.get("instance_certain") else label
            _add_context(context, source, f"{prefix} voltage min={vmin:.2f} V, max={vmax:.2f} V", [
                value_with_unit(f"{prefix}.voltage_min", vmin, "V"),
                value_with_unit(f"{prefix}.voltage_max", vmax, "V"),
            ])
            if vmax > 0 and (vmax - vmin) / vmax > 0.15:
                span_pct = (vmax - vmin) / vmax * 100
                evidence.append(f"{prefix} voltage span is {span_pct:.1f}% of max")
                evidence_values.append(value_with_unit(f"{prefix}.voltage_span_pct_of_max", span_pct, "%"))
        if curr is not None and len(curr.dropna()) > 0:
            source = group["message"] if not group.get("instance_certain") else label
            prefix = group["message"] if not group.get("instance_certain") else label
            _add_context(context, source, f"{prefix} current max={float(curr.max()):.1f} A", [
                value_with_unit(f"{prefix}.current_max", float(curr.max()), "A"),
            ])
    if "POWR" in tables:
        powr = tables["POWR"]
        vcc = numeric_series(powr, ["Vcc", "VCC"])
        if vcc is not None and len(vcc.dropna()) > 0:
            _add_context(context, "POWR", f"POWR.Vcc min={float(vcc.min()):.2f} V, max={float(vcc.max()):.2f} V", [
                value_with_unit("POWR.Vcc_min", float(vcc.min()), "V"),
                value_with_unit("POWR.Vcc_max", float(vcc.max()), "V"),
            ])
        flags = numeric_series(powr, ["Flags", "AccFlags"])
        if flags is not None and len(flags.dropna()) > 0 and int(flags.fillna(0).astype(int).max()) != 0:
            evidence.append(f"POWR flags max={int(flags.fillna(0).astype(int).max())}")
            evidence_values.append(value_with_unit("POWR.flags_max", int(flags.fillna(0).astype(int).max()), "bitmask"))
    if evidence:
        _add_finding(
            findings, rank, "Battery or board-power evidence needs correlation with the symptom", "likely-issue", "medium", evidence,
            "Flight battery voltage must be interpreted relative to cell count, chemistry, current and calibration; board-power drops or flags can indicate power-module or regulator problems.",
            ["Correlate voltage/current/Vcc with throttle, mapped output-channel saturation, mode changes and log end", "Verify battery health, cell count, connector resistance, power module calibration and regulator loading"],
            evidence_values,
        )
    else:
        checked.append({"check": "Battery / board power", "result": "No BAT or POWR evidence available or no heuristic power issue detected"})
