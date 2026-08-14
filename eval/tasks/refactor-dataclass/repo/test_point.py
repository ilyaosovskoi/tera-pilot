"""Tests for the refactored point module."""

from dataclasses import is_dataclass

from point import Point


def test_point_is_a_dataclass():
    assert is_dataclass(Point)


def test_distance():
    assert Point(0, 0).distance_to(Point(3, 4)) == 5
