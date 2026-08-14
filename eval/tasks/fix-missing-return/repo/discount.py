"""Price helpers."""


def apply_discount(price, percent):
    """Return ``price`` reduced by ``percent`` percent.

    BUG: the discounted value is computed but never returned.
    """
    discounted = price * (1 - percent / 100)
