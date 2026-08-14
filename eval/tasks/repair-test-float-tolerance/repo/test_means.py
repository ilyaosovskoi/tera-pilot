"""Tests for means.mean.

BUG: compares floats for exact equality — use pytest.approx instead.
"""

from means import mean


def test_mean_exact():
    assert mean(1, 2, 3) == 2


def test_mean_floats():
    assert mean(0.1, 0.2, 0.3) == 0.2
