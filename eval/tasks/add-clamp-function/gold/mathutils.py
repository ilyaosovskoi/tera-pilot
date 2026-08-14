"""Math utilities."""


def clamp(value, low, high):
    """Return ``value`` clamped to the inclusive range [low, high]."""
    return max(low, min(value, high))
