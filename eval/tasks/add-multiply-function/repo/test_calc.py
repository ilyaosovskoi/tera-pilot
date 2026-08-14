"""Tests for calc. Existing tests pass; a multiply test is expected after
the feature is added."""

from calc import add, subtract


def test_add():
    assert add(2, 3) == 5


def test_subtract():
    assert subtract(5, 2) == 3
