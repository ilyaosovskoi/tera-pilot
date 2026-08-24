"""Tests for currency helpers."""

from currency import currency_symbol, price_label


def test_currency_symbol():
    assert currency_symbol("USD") == "$"
    assert currency_symbol("EUR") == "€"
    assert currency_symbol("GBP") == "£"


def test_currency_symbol_unknown_code():
    assert currency_symbol("JPY") == "JPY"


def test_price_label_known_currencies():
    assert price_label(12.5, "USD") == "$12.50"
    assert price_label(12.5, "EUR") == "€12.50"
    assert price_label(12.5, "GBP") == "£12.50"


def test_price_label_unknown_currency():
    assert price_label(12.5, "JPY") == "JPY 12.50"
