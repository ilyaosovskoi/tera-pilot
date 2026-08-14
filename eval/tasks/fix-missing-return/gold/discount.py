"""Price helpers."""


def apply_discount(price, percent):
    """Return ``price`` reduced by ``percent`` percent."""
    return price * (1 - percent / 100)
