"""Tests for the refactored modules."""

from numeric import double
from strings import upper_first
from utils import double as utils_double, upper_first as utils_upper_first


def test_strings_module():
    assert upper_first("hello") == "Hello"


def test_numbers_module():
    assert double(21) == 42


def test_utils_still_exports():
    assert utils_double(2) == 4
    assert utils_upper_first("abc") == "Abc"
