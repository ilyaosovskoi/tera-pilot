"""Tests for discount.apply_discount."""

from discount import apply_discount


def test_ten_percent_off():
    assert apply_discount(100, 10) == 90


def test_no_discount():
    assert apply_discount(50, 0) == 50
