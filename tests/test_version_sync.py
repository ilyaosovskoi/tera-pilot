"""Version-consistency test (v2.3.4).

The version must stay in sync everywhere: npm (package.json), pip
(pyproject.toml), the Python package (tera_pilot/__init__.py), the
auto-updater, the web server, the TUI, the browser GUI (index.html +
app.js) and the npm postinstall marker test. This module treats
``package.json`` as the canonical source and asserts every other
location matches it, so a version bump can't silently ship in one place
and not another (that was the v2.3.2 release's headline fix).
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _extract(pattern: str, text: str, what: str) -> str:
    m = re.search(pattern, text)
    assert m is not None, f"could not find {what!r} in {text[:200]!r}"
    return m.group(1)


def test_version_format_is_semver():
    pkg = json.loads(_read("package.json"))
    assert re.fullmatch(r"\d+\.\d+\.\d+", pkg["version"]), pkg["version"]


def test_all_version_strings_match_package_json():
    version = json.loads(_read("package.json"))["version"]

    # Python package
    assert _extract(r'__version__\s*=\s*"([^"]+)"', _read("tera_pilot/__init__.py"), "tera_pilot/__init__.py") == version
    assert _extract(r'__version__\s*=\s*"([^"]+)"', _read("tera_pilot/agent/__init__.py"), "tera_pilot/agent/__init__.py") == version
    assert _extract(r'__version__\s*=\s*"([^"]+)"', _read("tera_pilot/auto_updater.py"), "tera_pilot/auto_updater.py") == version
    assert _extract(r'__version__\s*=\s*"([^"]+)"', _read("tera_pilot/web_server.py"), "tera_pilot/web_server.py") == version

    # pyproject.toml (line 7 is the real version; the py2app block is commented out)
    assert _extract(r'(?m)^version\s*=\s*"([^"]+)"', _read("pyproject.toml"), "pyproject.toml") == version
    assert f"Tera Pilot v{version}" in _read("pyproject.toml")

    # Auto-updater user agent + web server CLI description
    assert f"Tera Pilot-Updater/{version}" in _read("tera_pilot/auto_updater.py")
    assert f"Tera Pilot v{version}" in _read("tera_pilot/web_server.py")

    # TUI
    assert _extract(r'self\._version:\s*str\s*=\s*"([^"]+)"', _read("tera_pilot_tui/widgets/info_box.py"), "info_box.py") == version
    assert _extract(r'_tera_pilot_version\s*=\s*"([^"]+)"', _read("tera_pilot_tui/app.py"), "app.py") == version
    assert f"TUI v{version}" in _read("tera_pilot_tui/smoke_test.py")

    # Browser GUI
    index_html = _read("tera_pilot/web/index.html")
    assert f"<title>Tera Pilot v{version} — Web UI</title>" in index_html
    assert f'<div class="brand-version">v{version}</div>' in index_html
    assert _extract(r"APP_VERSION\s*=\s*'([^']+)'", _read("tera_pilot/web/app.js"), "app.js APP_VERSION") == version

    # requirements.txt header comment
    assert f"Tera Pilot v{version}" in _read("requirements.txt")


def test_no_stale_major_labels():
    """v2.4.x / v2.4.0 / v2.4.1 were speculative labels for changes that
    shipped as 2.3.4 — none may remain in the tree."""
    for rel in (
        "tera_pilot",
        "tera_pilot_tui",
        "tests",
    ):
        for p in (ROOT / rel).rglob("*.py"):
            if p.name == "test_version_sync.py":
                continue  # this file's own docstring describes the labels
            text = p.read_text(encoding="utf-8", errors="replace")
            assert "v2.4" not in text, f"{p}: stale v2.4 label"
    # The web assets too (a few labels referenced v2.4)
    for rel in ("index.html", "app.js", "style.css", "design-polish.css"):
        assert "v2.4" not in (ROOT / "tera_pilot" / "web" / rel).read_text(encoding="utf-8")
