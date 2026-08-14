"""Tiny module-level cache."""

_cache = {}


def get(key, factory):
    """Return cached value for ``key``, computing it with ``factory`` once."""
    if key not in _cache:
        _cache[key] = factory()
    return _cache[key]


def clear():
    """Empty the cache."""
    _cache.clear()
