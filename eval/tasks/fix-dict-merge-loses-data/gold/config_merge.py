"""Config merging helpers."""


def deep_merge(base, override):
    """Merge two config dicts. Nested dicts are merged recursively;
    scalar values from ``override`` win."""
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result
