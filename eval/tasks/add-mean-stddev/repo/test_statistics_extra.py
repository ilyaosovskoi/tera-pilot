"""Tests for statistics_extra.mean/stddev."""

import math

from statistics_extra import mean, stddev


def test_mean():
    assert mean([2, 4, 6]) == 4


def test_mean_empty():
    assert mean([]) == 0


def test_stddev():
    assert math.isclose(stddev([2, 4, 4, 4, 5, 5, 7, 9]), 2.0)


def test_stddev_empty():
    assert stddev([]) == 0
