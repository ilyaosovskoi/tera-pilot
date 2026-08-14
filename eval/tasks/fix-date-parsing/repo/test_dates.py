"""Tests for dates.parse_date."""

from dates import parse_date


def test_parse():
    assert parse_date("2026-08-13") == (2026, 8, 13)


def test_single_digit_month():
    assert parse_date("2024-01-05") == (2024, 1, 5)
