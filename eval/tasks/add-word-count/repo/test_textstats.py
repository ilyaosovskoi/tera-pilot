"""Tests for textstats.word_count."""

from textstats import word_count


def test_basic():
    assert word_count("hello world") == 2


def test_multiple_spaces():
    assert word_count("a   b  c") == 3


def test_empty():
    assert word_count("") == 0
