"""Statistics helpers."""

import math


def mean(values):
    """Return the arithmetic mean of ``values`` (0 for an empty list)."""
    if not values:
        return 0
    return sum(values) / len(values)


def stddev(values):
    """Return the population standard deviation of ``values`` (0 for an
    empty list)."""
    if not values:
        return 0
    m = mean(values)
    variance = sum((v - m) ** 2 for v in values) / len(values)
    return math.sqrt(variance)
