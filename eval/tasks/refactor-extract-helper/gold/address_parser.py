"""Address formatting — parsing extracted into helpers."""

from helpers import parse_parts


def parse_home(raw):
    name, city = parse_parts(raw)
    return f"{name} ({city})"


def parse_work(raw):
    name, city = parse_parts(raw)
    return f"{name} — {city}"
