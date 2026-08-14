"""Config merging helpers."""


def deep_merge(base, override):
    """Merge two config dicts. Nested dicts are merged recursively;
    scalar values from ``override`` win.

    BUG: nested dicts are replaced wholesale, dropping keys that
    only exist in ``base``.
    """
    result = dict(base)
    for key, value in override.items():
        result[key] = value
    return result
