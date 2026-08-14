"""Tests for emails.is_valid_email."""

from emails import is_valid_email


def test_valid_com():
    assert is_valid_email("ada@example.com")


def test_valid_other_tld():
    assert is_valid_email("ada@example.org")


def test_invalid():
    assert not is_valid_email("not-an-email")
