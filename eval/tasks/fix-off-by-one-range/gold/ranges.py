"""Inclusive range helpers."""


def inclusive_range(start, end):
    """Return all integers from start to end, both inclusive."""
    return list(range(start, end + 1))
