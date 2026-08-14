"""Payload decoding helpers."""


def decode_payload(data):
    """Decode UTF-8 bytes into text.

    BUG: uses ASCII, so any non-ASCII character raises UnicodeDecodeError.
    """
    return data.decode("ascii")
