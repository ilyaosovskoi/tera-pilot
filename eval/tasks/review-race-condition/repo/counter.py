"""Shared hit counter."""

import threading

_hits = 0


def record_hit():
    """Increment the global hit counter."""
    global _hits
    _hits += 1


def total():
    """Return the current hit count."""
    return _hits
