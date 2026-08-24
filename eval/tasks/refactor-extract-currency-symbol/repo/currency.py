"""Currency formatting helpers."""


def price_label(amount, currency):
    """Return a human-readable price label like '$12.50'."""
    if currency == "USD":
        return f"${amount:.2f}"
    elif currency == "EUR":
        return f"€{amount:.2f}"
    elif currency == "GBP":
        return f"£{amount:.2f}"
    else:
        return f"{currency} {amount:.2f}"
