"""Fibonacci sequence computation."""


def fibonacci(n):
    """Return the n-th Fibonacci number.

    WARNING: this function has no base case — it recurses forever.
    """
    return fibonacci(n - 1) + fibonacci(n - 2)
