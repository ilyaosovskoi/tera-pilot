"""Tests for formatter.format_amount."""

from formatter import format_amount


def test_two_decimals():
    assert format_amount(1234.5) == "$1234.50"


def test_integer():
    assert format_amount(7) == "$7.00"
