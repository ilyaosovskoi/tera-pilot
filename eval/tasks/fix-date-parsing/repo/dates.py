"""Date helpers."""


def parse_date(text):
    """Parse 'YYYY-MM-DD' into a tuple of ints (year, month, day).

    BUG: month and day are returned as strings instead of ints.
    """
    parts = text.split("-")
    return int(parts[0]), parts[1], parts[2]
