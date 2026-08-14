"""Tests for config_merge.deep_merge."""

from config_merge import deep_merge


def test_scalar_override():
    assert deep_merge({"a": 1}, {"a": 2}) == {"a": 2}


def test_nested_merge_preserves_base_keys():
    base = {"server": {"host": "localhost", "port": 8080}}
    override = {"server": {"port": 9090}}
    assert deep_merge(base, override) == {"server": {"host": "localhost", "port": 9090}}
