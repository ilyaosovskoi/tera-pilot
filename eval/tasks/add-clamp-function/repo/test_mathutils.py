"""Tests for mathutils.clamp."""

from mathutils import clamp


def test_within_range():
    assert clamp(5, 0, 10) == 5


def test_below_range():
    assert clamp(-3, 0, 10) == 0


def test_above_range():
    assert clamp(42, 0, 10) == 10


def test_edges_inclusive():
    assert clamp(0, 0, 10) == 0
    assert clamp(10, 0, 10) == 10
