"""Tests for name_service.full_name.

BUG: the test calls a function that does not exist.
"""

from name_service import full_name


def test_full_name():
    assert fullname("Ada", "Lovelace") == "Ada Lovelace"
