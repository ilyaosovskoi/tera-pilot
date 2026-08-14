"""Tests for the renamed billing API."""

from billing import total_amount


def test_total_amount():
    items = [
        {"price": 10, "qty": 2},
        {"price": 5, "qty": 1},
    ]
    assert total_amount(items) == 25
