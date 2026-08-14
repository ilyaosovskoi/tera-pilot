"""Tests for sorter.sorted_descending."""

from sorter import sorted_descending


def test_returns_sorted_copy():
    original = [3, 1, 2]
    result = sorted_descending(original)
    assert result == [3, 2, 1]
    assert original == [3, 1, 2]  # input must not change
