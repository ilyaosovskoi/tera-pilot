"""Payload decoding helpers."""


def decode_payload(data):
    """Decode UTF-8 bytes into text."""
    return data.decode("utf-8")
