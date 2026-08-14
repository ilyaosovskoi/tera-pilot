"""Small statistics helpers."""


def average(numbers):
    """Return the arithmetic mean of ``numbers``, or 0 for an empty list.

    BUG: an empty list raises ZeroDivisionError instead of returning 0.
    """
    return sum(numbers) / len(numbers)
