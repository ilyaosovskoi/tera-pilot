"""Tests for chunking.chunks."""

from chunking import chunks


def test_even_split():
    assert list(chunks([1, 2, 3, 4], 2)) == [[1, 2], [3, 4]]


def test_last_partial_chunk():
    assert list(chunks([1, 2, 3, 4, 5], 2)) == [[1, 2], [3, 4], [5]]


def test_empty():
    assert list(chunks([], 3)) == []
