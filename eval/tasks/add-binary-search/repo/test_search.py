"""Tests for binary_search()."""

from search import binary_search


def test_finds_element():
    assert binary_search([1, 3, 5, 7, 9], 5) == 2


def test_not_found():
    assert binary_search([1, 3, 5, 7, 9], 4) == -1


def test_empty_list():
    assert binary_search([], 1) == -1


def test_single_element():
    assert binary_search([5], 5) == 0
    assert binary_search([5], 3) == -1


def test_first_and_last():
    assert binary_search([2, 4, 6, 8], 2) == 0
    assert binary_search([2, 4, 6, 8], 8) == 3
