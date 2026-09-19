"""Small statistics helpers.

We deliberately avoid numpy/scipy: the datasets here are in the hundreds to
low thousands of rows, and a stdlib-only tool has no install step.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolated percentile (same definition numpy uses).

    `q` is in [0, 100]. Returns None for an empty input so callers can keep
    "no data" distinguishable from "zero".
    """
    if not values:
        return None
    if not 0.0 <= q <= 100.0:
        raise ValueError(f"q must be in [0, 100], got {q}")
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    # position in the 0..n-1 index space
    pos = (q / 100.0) * (len(ordered) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    frac = pos - lo
    return ordered[lo] + (ordered[hi] - ordered[lo]) * frac


# Below this many samples a percentile is not a median of anything: with n=2
# the "p50" is just the midpoint of the two values, and with n=1 it is the
# value itself. Renderers use `enough` to refuse to present those as medians.
MIN_PERCENTILE_SAMPLES = 3


def summarize(values: Iterable[float | int | None]) -> dict[str, Any]:
    """Return count/mean/min/p50/p90/p95/p99/max/sum for a numeric series.

    The `enough` flag says whether the sample is large enough for the
    percentiles to mean anything (see MIN_PERCENTILE_SAMPLES).
    """
    clean = [float(v) for v in values if v is not None]
    if not clean:
        return {
            "count": 0,
            "mean": None,
            "min": None,
            "p50": None,
            "p90": None,
            "p95": None,
            "p99": None,
            "max": None,
            "sum": None,
            "enough": False,
        }
    return {
        "count": len(clean),
        "enough": len(clean) >= MIN_PERCENTILE_SAMPLES,
        "mean": sum(clean) / len(clean),
        "min": min(clean),
        "p50": percentile(clean, 50),
        "p90": percentile(clean, 90),
        "p95": percentile(clean, 95),
        "p99": percentile(clean, 99),
        "max": max(clean),
        "sum": sum(clean),
    }


def rate(numerator: float | int | None, denominator: float | int | None) -> float | None:
    """Safe division that returns None (not 0) when the denominator is empty."""
    if not denominator:
        return None
    return float(numerator or 0) / float(denominator)


def count_by(rows: Iterable[Any], key) -> dict[str, int]:
    """Group rows by a key function / column name into a count map."""
    out: dict[str, int] = {}
    for row in rows:
        k = key(row) if callable(key) else row[key]
        k = "-" if k is None else str(k)
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def human_ms(value: float | int | None) -> str:
    """Render a millisecond duration compactly for tables."""
    if value is None:
        return "-"
    v = float(value)
    if v >= 1000:
        return f"{v / 1000:.2f}s"
    return f"{v:.0f}ms"


def human_int(value: float | int | None) -> str:
    if value is None:
        return "-"
    return f"{int(value):,}"


def human_float(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "-"
    return f"{value:.{digits}f}"
