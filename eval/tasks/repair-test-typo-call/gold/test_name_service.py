"""Tests for name_service.full_name."""

from name_service import full_name


def test_full_name():
    assert full_name("Ada", "Lovelace") == "Ada Lovelace"
