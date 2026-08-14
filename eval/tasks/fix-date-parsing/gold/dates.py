"""Date helpers."""


def parse_date(text):
    """Parse 'YYYY-MM-DD' into a tuple of ints (year, month, day)."""
    parts = text.split("-")
    return int(parts[0]), int(parts[1]), int(parts[2])
