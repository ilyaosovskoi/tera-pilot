"""Tests for durations.parse_duration."""

from durations import parse_duration


def test_minutes_and_seconds():
    assert parse_duration("1m30s") == 90


def test_hours():
    assert parse_duration("2h") == 7200


def test_seconds_only():
    assert parse_duration("45s") == 45


def test_invalid():
    assert parse_duration("bogus") is None
