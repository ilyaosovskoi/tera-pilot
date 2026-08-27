"""Unit tests for tera_pilot.web_server._safe_static_path.

The static resolver is the boundary between HTTP URLs and the files
served to the browser — a traversal bug here would let a request read
arbitrary files from disk (or worse, the token-bearing HTML is the
frontend's trusted channel, so the resolver must be strict).

Covers:
  - root / maps to index.html
  - web/ assets resolve by name
  - /assets/* falls back to tera_pilot/assets/
  - parent traversal (raw and URL-encoded) is rejected
  - missing files → None (404)
  - unknown content types fall back to octet-stream
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tera_pilot.web_server import (  # noqa: E402
    _safe_static_path,
    _content_type_for,
    _web_dir,
    _assets_dir,
)


def test_root_maps_to_index_html():
    p = _safe_static_path("/")
    assert p is not None
    assert p == _web_dir() / "index.html"


def test_index_html_explicit():
    p = _safe_static_path("/index.html")
    assert p is not None
    assert p.name == "index.html"


def test_web_asset_resolves():
    p = _safe_static_path("/app.js")
    assert p is not None
    assert p == _web_dir() / "app.js"


def test_assets_prefix_falls_back_to_assets_dir():
    # /assets/logo.png is served from tera_pilot/assets/logo.png
    p = _safe_static_path("/assets/logo.png")
    assert p is not None
    assert p == _assets_dir() / "logo.png"


def test_query_string_and_fragment_stripped():
    p = _safe_static_path("/app.js?cache=1#top")
    assert p is not None
    assert p.name == "app.js"


def test_parent_traversal_rejected():
    for bad in (
        "/../etc/passwd",
        "/../../etc/passwd",
        "/%2e%2e/etc/passwd",
        "/web/../../etc/passwd",
        "/assets/../config.json",
        "/..",
        "/..%2f..%2fetc%2fpasswd",
    ):
        assert _safe_static_path(bad) is None, f"traversal not rejected: {bad}"


def test_absolute_path_rejected():
    assert _safe_static_path("/etc/passwd") is None


def test_missing_file_returns_none():
    assert _safe_static_path("/no-such-file-xyz.js") is None


def test_content_type_known():
    from pathlib import Path
    assert _content_type_for(Path("x.html")).startswith("text/html")
    assert _content_type_for(Path("x.js")).startswith("application/javascript")
    assert _content_type_for(Path("x.css")).startswith("text/css")


def test_content_type_unknown_falls_back():
    from pathlib import Path
    assert _content_type_for(Path("x.unknown_ext")) == "application/octet-stream"
