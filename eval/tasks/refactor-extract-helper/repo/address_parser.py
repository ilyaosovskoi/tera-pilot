"""Address formatting. Both functions duplicate the same CSV parsing."""


def parse_home(raw):
    parts = raw.split(",")
    name = parts[0].strip()
    city = parts[1].strip()
    return f"{name} ({city})"


def parse_work(raw):
    parts = raw.split(",")
    name = parts[0].strip()
    city = parts[1].strip()
    return f"{name} — {city}"
