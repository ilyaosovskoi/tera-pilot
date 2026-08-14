"""Sorting helpers."""


def sorted_descending(values):
    """Return a NEW list sorted in descending order. The input list
    must not be modified.

    BUG: this sorts in place and returns None, mutating the caller's list.
    """
    return values.sort(reverse=True)
