"""Tests for format_duration."""

from duration import format_duration


def test_format_duration_zero():
    assert format_duration(0) == "0h 0m 0s"


def test_format_duration_minutes():
    assert format_duration(125) == "0h 2m 5s"


def test_format_duration_large():
    assert format_duration(3661) == "1h 1m 1s"
