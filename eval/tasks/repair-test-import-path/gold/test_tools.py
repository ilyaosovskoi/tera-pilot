"""Tests for tools.reverse."""

from tools import reverse


def test_reverse():
    assert reverse("abc") == "cba"
