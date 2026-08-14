"""Billing helpers."""


def total_amount(items):
    """Return the total amount for a list of line items."""
    return sum(item["price"] * item["qty"] for item in items)


def calc(data):
    """Deprecated alias for :func:`total_amount` (kept for old callers)."""
    return total_amount(data)
