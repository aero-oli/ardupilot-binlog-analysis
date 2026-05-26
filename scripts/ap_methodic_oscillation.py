#!/usr/bin/env python3
from __future__ import annotations

import math
from statistics import mean, pstdev
from typing import Any, Iterable

from ap_common import safe_float


def _clean_values(values: Iterable[Any]) -> list[float]:
    out = []
    for value in values:
        f = safe_float(value)
        if f is not None:
            out.append(f)
    return out


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * pct / 100.0))))
    return float(ordered[idx])


def _rolling_mean(values: list[float], window_samples: int) -> list[float]:
    if window_samples <= 1:
        return list(values)
    half = max(1, window_samples // 2)
    out = []
    for idx in range(len(values)):
        lo = max(0, idx - half)
        hi = min(len(values), idx + half + 1)
        out.append(float(mean(values[lo:hi])))
    return out


def _duration_s(times: list[float] | None) -> float | None:
    if not times or len(times) < 2:
        return None
    return max(times) - min(times)


def _sign_change_rate(values: list[float], center: float, duration_s: float | None, deadband: float) -> tuple[int, float | None]:
    signs = []
    for value in values:
        delta = value - center
        if abs(delta) <= deadband:
            signs.append(0)
        else:
            signs.append(1 if delta > 0 else -1)
    compact = [s for s in signs if s != 0]
    changes = sum(1 for prev, cur in zip(compact, compact[1:]) if prev != cur)
    if duration_s is None or duration_s <= 0:
        return changes, None
    return changes, changes / duration_s


def classify_oscillation(
    values: Iterable[Any],
    times: Iterable[Any] | None = None,
    *,
    threshold: float = 0.15,
    highpass_window_samples: int | None = None,
    min_samples: int = 20,
    min_duration_s: float = 1.0,
) -> dict[str, Any]:
    """Classify a controller-output signal as oscillatory or steady-biased.

    The helper reports descriptive metrics only. It does not recommend gain
    changes and does not decide whether a Methodic step is complete.
    """
    y = _clean_values(values)
    t = _clean_values(times or []) if times is not None else []
    duration = _duration_s(t) if len(t) == len(y) else None
    result: dict[str, Any] = {
        "classification": "inconclusive",
        "reason": [],
        "samples": len(y),
        "duration_s": duration,
        "threshold_abs": float(threshold),
        "metrics": {},
    }
    if len(y) < min_samples:
        result["reason"].append(f"Too few samples for oscillation classification: {len(y)} < {min_samples}.")
        return result
    if duration is not None and duration < min_duration_s:
        result["reason"].append(f"Signal duration is too short for oscillation classification: {duration:.3f}s < {min_duration_s:.3f}s.")
        return result

    abs_values = [abs(v) for v in y]
    mu = float(mean(y))
    rms = math.sqrt(sum(v * v for v in y) / len(y))
    p95_abs = _percentile(abs_values, 95)
    p99_abs = _percentile(abs_values, 99)
    max_abs = max(abs_values)
    pct_above = 100.0 * sum(1 for v in abs_values if v >= threshold) / len(abs_values)

    if highpass_window_samples is None:
        highpass_window_samples = max(5, min(101, len(y) // 10 if len(y) >= 100 else len(y) // 4))
    highpass_window_samples = max(3, int(highpass_window_samples))
    trend = _rolling_mean(y, highpass_window_samples)
    residual = [value - base for value, base in zip(y, trend)]
    residual_abs = [abs(v) for v in residual]
    residual_rms = math.sqrt(sum(v * v for v in residual) / len(residual))
    residual_p95_abs = _percentile(residual_abs, 95)
    residual_ratio = residual_rms / rms if rms > 1e-9 else 0.0
    sign_deadband = max(threshold * 0.1, 0.01)
    sign_changes, sign_change_rate_hz = _sign_change_rate(y, mu, duration, sign_deadband)

    result["metrics"] = {
        "mean": mu,
        "rms": rms,
        "p95_abs": p95_abs,
        "p99_abs": p99_abs,
        "max_abs": max_abs,
        "percent_above_threshold": pct_above,
        "highpass_window_samples": highpass_window_samples,
        "highpass_residual_rms": residual_rms,
        "highpass_residual_p95_abs": residual_p95_abs,
        "highpass_residual_ratio": residual_ratio,
        "sign_changes_around_mean": sign_changes,
        "sign_change_rate_hz": sign_change_rate_hz,
    }

    bias_large = abs(mu) >= threshold * 0.6 and pct_above >= 25.0
    residual_large = residual_p95_abs is not None and residual_p95_abs >= threshold * 0.45
    sign_changes_enough = sign_changes >= 6 if sign_change_rate_hz is None else sign_change_rate_hz >= 0.8 and sign_changes >= 4

    if max_abs < threshold and (residual_p95_abs or 0.0) < threshold * 0.35:
        result["classification"] = "not_oscillatory"
        result["reason"].append("Controller output stayed below the configured absolute threshold and high-pass residual was small.")
    elif residual_large and sign_changes_enough and bias_large:
        result["classification"] = "mixed"
        result["reason"].append("Signal has both a sustained bias and repeated high-pass sign changes.")
    elif residual_large and sign_changes_enough:
        result["classification"] = "oscillatory"
        result["reason"].append("High-pass residual is large and the signal repeatedly changes sign around its mean.")
    elif bias_large:
        result["classification"] = "steady_bias"
        result["reason"].append("Mean output is biased for a large fraction of the samples without enough sign changes to indicate oscillation.")
    elif pct_above > 0.0:
        result["classification"] = "mixed"
        result["reason"].append("Output crosses the threshold, but oscillation and steady-bias evidence are not cleanly separable.")
    else:
        result["classification"] = "not_oscillatory"
        result["reason"].append("No threshold-crossing or repeated oscillatory residual pattern was found.")
    return result
