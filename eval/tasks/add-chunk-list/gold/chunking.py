"""Chunking helpers."""


def chunks(items, size):
    """Yield successive ``size``-sized chunks of ``items``."""
    for i in range(0, len(items), size):
        yield items[i : i + size]
