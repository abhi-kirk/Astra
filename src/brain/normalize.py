"""
Normalization helpers — turn raw quantities into comparable scores.

Every pillar builds on these so factors combine on a common scale. No trading
constants live here; all endpoints are passed in by the caller (from params).
"""

from __future__ import annotations

import math


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def smooth(x: float, a: float, b: float) -> float:
    """Linear ramp: 0 at `a`, 1 at `b`, clamped outside. Handles a>b (descending)."""
    if a == b:
        return 1.0 if x >= a else 0.0
    return clamp((x - a) / (b - a), 0.0, 1.0)


def tent(x: float, rise_to: float, fall_from: float, zero_at: float) -> float:
    """Trapezoid: 0 at 0, ramps up to 1 at `rise_to`, flat 1 until `fall_from`,
    ramps down to 0 at `zero_at`. Used for 'sweet-spot' scores (e.g. pullback depth)."""
    if x <= rise_to:
        return smooth(x, 0.0, rise_to)
    if x <= fall_from:
        return 1.0
    return smooth(x, zero_at, fall_from)  # descending ramp (a>b)


def squash(x: float, scale: float) -> float:
    """tanh squash to (-1, 1). `scale` is the value mapping to ~tanh(1)=0.76."""
    if scale == 0:
        return 0.0
    return math.tanh(x / scale)


def mean_defined(values: list[float | None]) -> float | None:
    """Mean of the non-None values; None if all are missing (graceful degradation)."""
    present = [v for v in values if v is not None]
    if not present:
        return None
    return sum(present) / len(present)
