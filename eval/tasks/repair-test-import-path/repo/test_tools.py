"""Tests for tools.reverse.

BUG: imports from a module that does not exist.
"""

from toolsx import reverse


def test_reverse():
    assert reverse("abc") == "cba"
