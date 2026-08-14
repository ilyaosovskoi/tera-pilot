"""Tests for stats.average."""

from stats import average


def test_nonempty():
    assert average([2, 4, 6]) == 4


def test_empty_returns_zero():
    assert average([]) == 0


def test_single():
    assert average([7]) == 7
