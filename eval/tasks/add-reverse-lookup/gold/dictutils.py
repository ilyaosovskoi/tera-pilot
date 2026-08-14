"""Dict utilities."""


def find_key(mapping, value):
    """Return the first key in ``mapping`` whose value equals ``value``,
    or None when no key matches."""
    for key, val in mapping.items():
        if val == value:
            return key
    return None
