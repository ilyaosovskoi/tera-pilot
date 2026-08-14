"""Tests for registry.get_user_name."""

from registry import get_user_name


def test_known_user():
    assert get_user_name({"name": "Ada"}) == "Ada"


def test_missing_user():
    assert get_user_name(None) == "Anonymous"


def test_user_without_name():
    assert get_user_name({}) == "Anonymous"
