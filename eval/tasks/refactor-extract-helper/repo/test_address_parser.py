"""Tests for address_parser after extracting the duplicated parsing."""

from address_parser import parse_home, parse_work
from helpers import parse_parts


def test_parse_parts():
    assert parse_parts("Ada Lovelace, London") == ("Ada Lovelace", "London")


def test_parse_parts_strips_whitespace():
    assert parse_parts(" Ada Lovelace , London ") == ("Ada Lovelace", "London")


def test_parse_home_still_works():
    assert parse_home("Ada Lovelace, London") == "Ada Lovelace (London)"


def test_parse_work_still_works():
    assert parse_work("Ada Lovelace, London") == "Ada Lovelace — London"
