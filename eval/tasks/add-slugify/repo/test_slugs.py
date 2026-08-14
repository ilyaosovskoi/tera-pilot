"""Tests for slugs.slugify."""

from slugs import slugify


def test_lowercase_and_dashes():
    assert slugify("Hello World") == "hello-world"


def test_strips_punctuation():
    assert slugify("Hello, World!") == "hello-world"


def test_multiple_spaces():
    assert slugify("a   b") == "a-b"
