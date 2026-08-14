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