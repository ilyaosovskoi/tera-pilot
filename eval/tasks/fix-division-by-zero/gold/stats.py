"""Small statistics helpers."""


def average(numbers):
    """Return the arithmetic mean of ``numbers``, or 0 for an empty list."""
    if not numbers:
        return 0
    return sum(numbers) / len(numbers)
