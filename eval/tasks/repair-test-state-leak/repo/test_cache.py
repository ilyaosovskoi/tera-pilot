"""Tests for cache.get.

BUG: test_second_sees_fresh_state depends on test execution order — it
passes only when run before test_first_populates_cache. Fix it so the
test is order-independent (the cache module is correct).
"""

from cache import clear, get


def test_first_populates_cache():
    clear()
    assert get("a", lambda: 1) == 1


def test_second_sees_fresh_state():
    assert get("a", lambda: 99) == 99
