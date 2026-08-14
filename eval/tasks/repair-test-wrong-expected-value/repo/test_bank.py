"""Tests for bank.balance_after_fee.

BUG: the expected value below is wrong — the code is correct.
"""

from bank import balance_after_fee


def test_fee_is_subtracted():
    assert balance_after_fee(100, 5) == 105
