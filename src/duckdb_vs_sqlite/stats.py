"""Summary statistics for a list of timing samples (seconds)."""

from __future__ import annotations

import math

# Above this many samples the raw list is replaced by a 101-point percentile curve.
RAW_SAMPLE_LIMIT = 40


def percentile(sorted_values: list[float], p: float) -> float:
    """Linear-interpolated percentile (p in 0..100) of an already sorted list."""
    if not sorted_values:
        return math.nan
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (len(sorted_values) - 1) * p / 100.0
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return sorted_values[lo]
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (rank - lo)


def _r(value: float) -> float:
    """Round to nanoseconds; keeps the JSON compact without losing real precision."""
    return round(value, 9)


def summarize(samples: list[float]) -> dict:
    n = len(samples)
    if n == 0:
        return {"n": 0}
    ordered = sorted(samples)
    total = sum(ordered)
    mean = total / n
    stdev = math.sqrt(sum((x - mean) ** 2 for x in ordered) / (n - 1)) if n > 1 else 0.0
    out = {
        "n": n,
        "total_s": _r(total),
        "min": _r(ordered[0]),
        "median": _r(percentile(ordered, 50)),
        "mean": _r(mean),
        "p90": _r(percentile(ordered, 90)),
        "p95": _r(percentile(ordered, 95)),
        "p99": _r(percentile(ordered, 99)),
        "max": _r(ordered[-1]),
        "stdev": _r(stdev),
        "ops_per_s": round(n / total, 3) if total > 0 else None,
    }
    if n <= RAW_SAMPLE_LIMIT:
        out["samples"] = [_r(x) for x in samples]  # run order preserved
    else:
        out["quantiles"] = [_r(percentile(ordered, p)) for p in range(101)]
    return out
