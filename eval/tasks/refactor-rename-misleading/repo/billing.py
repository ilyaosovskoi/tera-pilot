"""Billing helpers."""


def calc(data):
    """Return the total amount for a list of line items."""
    return sum(item["price"] * item["qty"] for item in data)
