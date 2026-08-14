"""Tests for listutils.unique."""

from listutils import unique


def test_removes_duplicates():
    assert unique([1, 2, 1, 3, 2]) == [1, 2, 3]


def test_preserves_order():
    assert unique(["b", "a", "b", "c"]) == ["b", "a", "c"]


def test_empty():
    assert unique([]) == []
