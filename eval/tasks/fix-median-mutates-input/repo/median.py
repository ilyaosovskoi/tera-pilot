"""Statistics helpers."""


def median(numbers):
    """Return the median of *numbers*."""
    numbers.sort()
    n = len(numbers)
    mid = n // 2
    if n % 2 == 1:
        return numbers[mid]
    return (numbers[mid - 1] + numbers[mid]) / 2.0
