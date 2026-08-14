"""Currency formatting helpers."""


def format_amount(value):
    """Format a number as a currency string with two decimals."""
    formatted = f"${value:.2f}"
    return formatted
