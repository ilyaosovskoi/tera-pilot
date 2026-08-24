"""Tests for format_duration.

BUG: test_format_duration_large asserts the OLD output format
('1 hours 1 minutes 1 seconds'). The module is correct — fix the test.
"""

from duration import format_duration


def test_format_duration_zero():
    assert format_duration(0) == "0h 0m 0s"


def test_format_duration_minutes():
    assert format_duration(125) == "0h 2m 5s"


def test_format_duration_large():
    # BUG: stale expected value from an earlier (buggy) implementation.
    assert format_duration(3661) == "1 hours 1 minutes 1 seconds"
