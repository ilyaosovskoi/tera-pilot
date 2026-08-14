"""URL slug helpers."""

import re


def slugify(text):
    """Return a URL-safe slug: lowercase, spaces -> dashes, strip
    non-alphanumeric characters."""
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("-")
