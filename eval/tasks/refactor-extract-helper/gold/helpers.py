"""Shared parsing helpers."""


def parse_parts(raw):
    """Split a raw 'name, city' CSV line into a (name, city) tuple."""
    parts = raw.split(",")
    return parts[0].strip(), parts[1].strip()
