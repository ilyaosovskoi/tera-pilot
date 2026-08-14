"""Tests for dictutils.find_key."""

from dictutils import find_key


def test_finds_first_key():
    assert find_key({"a": 1, "b": 2, "c": 2}, 2) == "b"


def test_missing_value():
    assert find_key({"a": 1}, 99) is None


def test_empty():
    assert find_key({}, 1) is None
