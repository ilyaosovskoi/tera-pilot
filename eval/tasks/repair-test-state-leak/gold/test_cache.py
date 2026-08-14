"""Tests for cache.get — order-independent."""

from cache import clear, get


def test_first_populates_cache():
    clear()
    assert get("a", lambda: 1) == 1


def test_second_sees_fresh_state():
    clear()
    assert get("a", lambda: 99) == 99
