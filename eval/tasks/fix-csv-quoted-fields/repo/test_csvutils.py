"""Tests for csvutils.parse_csv_line."""

from csvutils import parse_csv_line


def test_simple_fields():
    assert parse_csv_line("ada,42") == ["ada", "42"]


def test_quoted_field_with_comma():
    assert parse_csv_line('"Doe, John",42') == ["Doe, John", "42"]
