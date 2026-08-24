"""Currency formatting helpers."""

_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£"}


def currency_symbol(currency):
    """Return the symbol for a currency code, or the code itself if unknown."""
    return _SYMBOLS.get(currency, currency)


def price_label(amount, currency):
    """Return a human-readable price label like '$12.50'."""
    symbol = currency_symbol(currency)
    if symbol == currency:
        return f"{currency} {amount:.2f}"
    return f"{symbol}{amount:.2f}"
