"""
Tera Pilot Plugin System.

Plugins live in ~/.tera_pilot/plugins/*.py and are loaded at startup.
Each plugin must expose a register() function that returns a plugin instance.

Plugin interface:
    class MyPlugin:
        name: str          = "my_plugin"
        version: str       = "1.0.0"
        description: str   = "What it does"

        def on_register(self, app_context: dict) -> None:
            '''Called when loaded. app_context = {registry, config, save_config}'''

        def register_providers(self, registry) -> None:
            '''Register custom providers.'''

        def register_routes(self) -> dict:
            '''Return {path: handler(self, body)} for custom API routes.'''

        def inject_js(self) -> str:
            '''JS to inject into the frontend.'''

        def inject_css(self) -> str:
            '''CSS to inject into the frontend.'''

    def register() -> MyPlugin:
        return MyPlugin()
"""

from __future__ import annotations

import logging
import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .providers import ProviderRegistry

logger = logging.getLogger(__name__)


def _plugins_dir() -> Path:
    p = Path.home() / ".tera_pilot" / "plugins"
    p.mkdir(parents=True, exist_ok=True)
    return p


class PluginManager:
    """Scans ~/.tera_pilot/plugins/ and loads all plugin modules."""

    def __init__(self):
        self.plugins: list[dict] = []
        self._extra_routes: dict[str, callable] = {}
        self._extra_js: list[str] = []
        self._extra_css: list[str] = []

    def load_all(self, registry: 'ProviderRegistry') -> None:
        pdir = _plugins_dir()
        for fname in sorted(pdir.iterdir()):
            if fname.suffix != '.py' or fname.name.startswith('_'):
                continue
            # v2.5.0: /plugin disable keeps the file but skips loading.
            if not is_plugin_enabled(fname.stem):
                logger.info("[plugins] disabled, skipping: %s", fname.name)
                continue
            try:
                self._load_plugin(fname, registry)
            except Exception as e:
                logger.error("[plugins] failed to load %s: %s", fname.name, e)

    def _load_plugin(self, path: Path, registry: 'ProviderRegistry') -> None:
        spec = importlib.util.spec_from_file_location(path.stem, str(path))
        if not spec or not spec.loader:
            return
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        if not hasattr(mod, 'register'):
            logger.warning("[plugins] %s has no register()", path.name)
            return

        plugin = mod.register()
        if plugin is None:
            return

        name = getattr(plugin, 'name', path.stem)
        version = getattr(plugin, 'version', '0.0.0')
        description = getattr(plugin, 'description', '')

        app_context = {
            'registry': registry,
        }

        if hasattr(plugin, 'on_register'):
            try:
                plugin.on_register(app_context)
            except Exception as e:
                logger.error("[plugins] %s on_register error: %s", name, e)

        if hasattr(plugin, 'register_providers'):
            try:
                plugin.register_providers(registry)
            except Exception as e:
                logger.error("[plugins] %s register_providers error: %s", name, e)

        if hasattr(plugin, 'register_routes'):
            try:
                routes = plugin.register_routes()
                if isinstance(routes, dict):
                    self._extra_routes.update(routes)
            except Exception as e:
                logger.error("[plugins] %s register_routes error: %s", name, e)

        if hasattr(plugin, 'inject_js'):
            try:
                js = plugin.inject_js()
                if js:
                    self._extra_js.append(js)
            except Exception:
                pass

        if hasattr(plugin, 'inject_css'):
            try:
                css = plugin.inject_css()
                if css:
                    self._extra_css.append(css)
            except Exception:
                pass

        self.plugins.append({
            'name': name,
            'version': version,
            'description': description,
            'file': path.name,
        })
        logger.info("[plugins] loaded: %s v%s — %s", name, version, description)

    def get_plugins_info(self) -> list[dict]:
        return self.plugins

    def get_extra_routes(self) -> dict[str, callable]:
        return self._extra_routes

    def get_injected_js(self) -> str:
        return '\n'.join(self._extra_js)

    def get_injected_css(self) -> str:
        return '\n'.join(self._extra_css)


# ── v2.5.0: lightweight marketplace ──────────────────────────────────
# File-based and offline-first: install a plugin from a local .py file
# or an https URL, enable/disable without deleting, reinstall-from-origin
# as the "update" path. A registry file (~/.tera_pilot/plugin-registry.json)
# lists known plugins: {"plugins": [{"name":..,"description":..,"url":..}]}.
# Nothing here phones home on its own — fetches happen only when the
# user explicitly installs/updates.

def _registry_file() -> Path:
    return Path.home() / ".tera_pilot" / "plugin-registry.json"


def _disabled_file() -> Path:
    return Path.home() / ".tera_pilot" / "plugins-disabled.json"


def _meta_path(name: str) -> Path:
    return _plugins_dir() / f".{name}.meta.json"


def list_marketplace() -> list[dict]:
    """Installed plugins merged with registry entries (installed wins)."""
    import json as _json
    installed = {p.stem: p for p in _plugins_dir().glob("*.py")
                 if not p.name.startswith(("_", "."))}
    try:
        disabled = set(_json.loads(_disabled_file().read_text(encoding="utf-8"))
                       if _disabled_file().exists() else [])
    except Exception:
        disabled = set()
    out = []
    for stem in sorted(installed):
        meta = {}
        try:
            mp = _meta_path(stem)
            if mp.exists():
                meta = _json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
        out.append({
            "name": stem, "installed": True,
            "enabled": stem not in disabled,
            "description": meta.get("description", ""),
            "origin": meta.get("origin", "local"),
            "version": meta.get("version", ""),
        })
    try:
        reg = _json.loads(_registry_file().read_text(encoding="utf-8")) \
            if _registry_file().exists() else {}
        for entry in (reg.get("plugins") or []):
            if entry.get("name") in installed:
                continue
            out.append({
                "name": entry.get("name", "?"), "installed": False,
                "enabled": False,
                "description": entry.get("description", ""),
                "origin": entry.get("url", ""),
                "version": entry.get("version", ""),
            })
    except Exception as exc:
        logger.warning("[plugins] bad registry file: %s", exc)
    return out


def _valid_plugin_source(code: str) -> tuple[bool, str]:
    """A plugin must define a register() callable. Static check only."""
    import ast as _ast
    try:
        tree = _ast.parse(code)
    except SyntaxError as exc:
        return False, f"syntax error: {exc}"
    for node in tree.body:
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)) \
                and node.name == "register":
            return True, ""
    return False, "no top-level register() found"


def install_plugin(source: str) -> dict:
    """Install from a local path or https URL. Returns status dict."""
    import json as _json
    import urllib.request as _url
    src = (source or "").strip()
    if not src:
        return {"ok": False, "error": "empty source"}
    origin = "local"
    if src.startswith(("https://", "http://")):
        if src.startswith("http://"):
            return {"ok": False, "error": "plain http is refused — use https"}
        origin = src
        try:
            req = _url.Request(src, headers={"User-Agent": "tera-pilot"})
            with _url.urlopen(req, timeout=30) as resp:
                code = resp.read().decode("utf-8", errors="replace")
            if len(code) > 500_000:
                return {"ok": False, "error": "plugin too large (500k cap)"}
        except Exception as exc:
            return {"ok": False, "error": f"download failed: {exc}"}
        name = Path(src.split("?")[0]).stem or "plugin"
    else:
        try:
            code = Path(src).expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            return {"ok": False, "error": f"cannot read file: {exc}"}
        name = Path(src).stem
    if not name.replace("_", "").replace("-", "").isalnum():
        return {"ok": False, "error": f"bad plugin name {name!r}"}
    ok, reason = _valid_plugin_source(code)
    if not ok:
        return {"ok": False, "error": reason}
    dest = _plugins_dir() / f"{name}.py"
    if dest.exists():
        return {"ok": False,
                "error": f"{name} already installed — remove it first or update"}
    dest.write_text(code, encoding="utf-8")
    try:
        _meta_path(name).write_text(_json.dumps(
            {"origin": origin, "version": "", "description": ""}), encoding="utf-8")
    except OSError:
        pass
    logger.info("[plugins] installed: %s (from %s)", name, origin)
    return {"ok": True, "name": name}


def remove_plugin(name: str) -> dict:
    target = _plugins_dir() / f"{name}.py"
    if not target.exists():
        return {"ok": False, "error": f"{name} is not installed"}
    try:
        target.unlink()
        mp = _meta_path(name)
        if mp.exists():
            mp.unlink()
        set_plugin_enabled(name, True)
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "name": name}


def set_plugin_enabled(name: str, enabled: bool) -> dict:
    import json as _json
    try:
        disabled = set(_json.loads(_disabled_file().read_text(encoding="utf-8"))
                       if _disabled_file().exists() else [])
    except Exception:
        disabled = set()
    if enabled:
        disabled.discard(name)
    else:
        disabled.add(name)
    try:
        _disabled_file().write_text(_json.dumps(sorted(disabled)), encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "name": name, "enabled": enabled}


def is_plugin_enabled(name: str) -> bool:
    import json as _json
    try:
        disabled = set(_json.loads(_disabled_file().read_text(encoding="utf-8"))
                       if _disabled_file().exists() else [])
        return name not in disabled
    except Exception:
        return True