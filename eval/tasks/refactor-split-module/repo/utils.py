"""Mixed string/number utilities. TODO: split into focused modules."""


def upper_first(text):
    """Return ``text`` with the first character uppercased."""
    return text[:1].upper() + text[1:]


def double(n):
    """Return ``n`` doubled."""
    return n * 2
