from __future__ import annotations

from ap_common import (
    get_col,
    motor_channels_from_mapping,
    numeric_series,
    output_channel_label,
    output_mapping_from_tables,
)


def vals(s):
    if s is None:
        return []
    try:
        return [float(v) for v in s.dropna().tolist()]
    except Exception:
        return []


def _add_finding(findings, rank, possible_cause, severity, confidence, evidence, interpretation, recommended_checks):
    evidence = [e for e in evidence if e]
    if not evidence:
        return
    findings.append({
        "rank": rank,
        "possible_cause": possible_cause,
        "severity": severity,
        "confidence": confidence,
        "evidence": evidence,
        "interpretation": interpretation,
        "recommended_checks": recommended_checks,
    })


def add_motor_esc_findings(tables, findings, checked, rank=1):
    evidence = []
    if "RCOU" in tables:
        rcou = tables["RCOU"]
        channels = [c for c in rcou.columns if c.startswith("C") and c[1:].isdigit()]
        mapping = output_mapping_from_tables(tables)
        motor_channels = motor_channels_from_mapping(mapping, channels)
        for c in [ch for ch in channels if ch in motor_channels]:
            s = numeric_series(rcou, [c])
            if s is None or len(s.dropna()) == 0:
                continue
            high_pct = float((s >= 1900).mean() * 100)
            low_pct = float((s <= 1100).mean() * 100)
            if high_pct > 1 or low_pct > 1:
                evidence.append(f"RCOU.{output_channel_label(c, mapping)}: {high_pct:.1f}% >=1900us, {low_pct:.1f}% <=1100us")
    if "ESC" in tables:
        esc = tables["ESC"]
        err_col = get_col(esc, ["Err"])
        if err_col:
            err = numeric_series(esc, [err_col])
            if err is not None and len(err.dropna()) > 0 and float(err.max()) > 0:
                evidence.append(f"ESC.{err_col} max={float(err.max()):.2f}")
        status_col = get_col(esc, ["Status"])
        if status_col:
            status = numeric_series(esc, [status_col])
            if status is not None and len(status.dropna()) > 0:
                s_i = status.fillna(0).astype(int)
                alert = int(((s_i & 4) != 0).sum())
                warning = int(((s_i & 8) != 0).sum())
                error = int(((s_i & 16) != 0).sum())
                if alert or warning or error:
                    evidence.append(f"ESC status alert/warning/error counts={alert}/{warning}/{error}")
        for col in ["RPM", "RawRPM", "Curr", "Temp", "MotTemp"]:
            if col in esc.columns:
                s = numeric_series(esc, [col])
                if s is not None and len(s.dropna()) > 0:
                    evidence.append(f"ESC {col}: min={float(s.min()):.2f}, max={float(s.max()):.2f}")
    if "EDT2" in tables:
        edt2 = tables["EDT2"]
        status_col = get_col(edt2, ["Status"])
        if status_col:
            status = numeric_series(edt2, [status_col])
            if status is not None and len(status.dropna()) > 0:
                s_i = status.fillna(0).astype(int)
                alert = int(((s_i & 4) != 0).sum())
                warning = int(((s_i & 8) != 0).sum())
                error = int(((s_i & 16) != 0).sum())
                if alert or warning or error:
                    evidence.append(f"EDT2 status alert/warning/error counts={alert}/{warning}/{error}")
        for col in ["Stress", "MaxStress", "ErrCnt"]:
            if col in edt2.columns:
                s = numeric_series(edt2, [col])
                if s is not None and len(s.dropna()) > 0:
                    evidence.append(f"EDT2 {col}: min={float(s.min()):.2f}, max={float(s.max()):.2f}")
    if "ESCX" in tables:
        escx = tables["ESCX"]
        flags = numeric_series(escx, ["flags"])
        if flags is not None and len(flags.dropna()) > 0:
            count = int((flags.fillna(0).astype(int) != 0).sum())
            if count:
                evidence.append(f"ESCX flags nonzero samples={count}")
        for col in ["inpct", "outpct", "Pwr"]:
            if col in escx.columns:
                s = numeric_series(escx, [col])
                if s is not None and len(s.dropna()) > 0:
                    evidence.append(f"ESCX {col}: min={float(s.min()):.2f}, max={float(s.max()):.2f}")
    if evidence:
        _add_finding(
            findings, rank, "Motor output saturation or ESC telemetry abnormality", "safety-critical", "high" if any("RCOU" in e or "status" in e for e in evidence) else "medium",
            evidence[:14],
            "Requested outputs near conventional limits or ESC error/status evidence can indicate limited actuator headroom, wrong motor/prop setup, ESC/motor faults, or power-related thrust loss.",
            ["Verify motor order, prop direction, frame class/type and output mapping", "Bench inspect affected motor/ESC/wiring", "Correlate RCOU/ESC evidence with RATE errors, battery sag and mode changes before tuning"],
        )
    else:
        checked.append({"check": "Motor outputs / ESC telemetry", "result": "No RCOU saturation or ESC error/status issue detected by heuristic"})


def add_power_findings(tables, findings, checked, rank=3):
    evidence = []
    if "BAT" in tables:
        bat = tables["BAT"]
        volt = numeric_series(bat, ["Volt", "VoltR", "V"])
        curr = numeric_series(bat, ["Curr", "I"])
        if volt is not None and len(volt.dropna()) > 0:
            vmin, vmax = float(volt.min()), float(volt.max())
            evidence.append(f"BAT voltage min={vmin:.2f} V, max={vmax:.2f} V")
            if vmax > 0 and (vmax - vmin) / vmax > 0.15:
                evidence.append(f"BAT voltage span is {(vmax - vmin) / vmax * 100:.1f}% of max")
        if curr is not None and len(curr.dropna()) > 0:
            evidence.append(f"BAT current max={float(curr.max()):.1f} A")
    if "POWR" in tables:
        powr = tables["POWR"]
        vcc = numeric_series(powr, ["Vcc", "VCC"])
        if vcc is not None and len(vcc.dropna()) > 0:
            evidence.append(f"POWR.Vcc min={float(vcc.min()):.2f} V, max={float(vcc.max()):.2f} V")
        flags = numeric_series(powr, ["Flags", "AccFlags"])
        if flags is not None and len(flags.dropna()) > 0 and int(flags.fillna(0).astype(int).max()) != 0:
            evidence.append(f"POWR flags max={int(flags.fillna(0).astype(int).max())}")
    if evidence:
        _add_finding(
            findings, rank, "Battery or board-power evidence needs correlation with the symptom", "likely-issue", "medium", evidence,
            "Flight battery voltage must be interpreted relative to cell count, chemistry, current and calibration; board-power drops or flags can indicate power-module or regulator problems.",
            ["Correlate voltage/current/Vcc with throttle, RCOU saturation, mode changes and log end", "Verify battery health, cell count, connector resistance, power module calibration and regulator loading"],
        )
    else:
        checked.append({"check": "Battery / board power", "result": "No BAT or POWR evidence available or no heuristic power issue detected"})
