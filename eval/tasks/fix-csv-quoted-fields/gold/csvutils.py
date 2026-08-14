"""CSV helpers."""

import csv
import io


def parse_csv_line(line):
    """Parse one CSV line into a list of fields.

    Fields may be quoted with double quotes and contain commas.
    """
    return next(csv.reader(io.StringIO(line)))
