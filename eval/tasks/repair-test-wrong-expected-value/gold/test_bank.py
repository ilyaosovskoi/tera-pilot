"""Tests for bank.balance_after_fee."""

from bank import balance_after_fee


def test_fee_is_subtracted():
    assert balance_after_fee(100, 5) == 95


def test_zero_fee():
    assert balance_after_fee(50, 0) == 50
