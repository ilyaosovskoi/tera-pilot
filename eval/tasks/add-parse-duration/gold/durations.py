"""Duration parsing."""

import re

_UNITS = {"h": 3600, "m": 60, "s": 1}


def parse_duration(text):
    """Parse a duration like '1h30m' or '45s' into total seconds.
    Returns None for invalid input."""
    if not text or not re.fullmatch(r"\d+[hms](\d+[hms])*", text):
        return None
    total = 0
    for value, unit in re.findall(r"(\d+)([hms])", text):
        total += int(value) * _UNITS[unit]
    return total
