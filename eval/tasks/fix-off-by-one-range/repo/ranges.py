"""Inclusive range helpers."""


def inclusive_range(start, end):
    """Return all integers from start to end, both inclusive.

    BUG: the stop bound is exclusive, so ``end`` is dropped.
    """
    return list(range(start, end))
