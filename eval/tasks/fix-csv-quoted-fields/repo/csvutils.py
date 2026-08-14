"""CSV helpers."""


def parse_csv_line(line):
    """Parse one CSV line into a list of fields.

    Fields may be quoted with double quotes and contain commas.

    BUG: naive split on commas breaks quoted fields.
    """
    return line.split(",")
