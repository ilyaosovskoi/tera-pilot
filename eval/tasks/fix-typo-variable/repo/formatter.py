"""Currency formatting helpers."""


def format_amount(value):
    """Format a number as a currency string with two decimals.

    BUG: the result is stored in a misspelled variable, so this
    raises NameError.
    """
    formated = f"${value:.2f}"
    return formatted
