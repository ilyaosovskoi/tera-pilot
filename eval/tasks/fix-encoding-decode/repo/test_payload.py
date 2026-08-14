"""Tests for payload.decode_payload."""

from payload import decode_payload


def test_ascii():
    assert decode_payload(b"hello") == "hello"


def test_utf8():
    assert decode_payload("café — déjà vu".encode("utf-8")) == "café — déjà vu"
