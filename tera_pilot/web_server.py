"""
Tera Pilot Web UI Server — v2.2.0.

Replaces the legacy PySide6 / QWebEngineView desktop GUI with a
plain HTTP server that serves:

  • Static frontend assets  →  GET /, /app.js, /style.css, /design-polish.css, /assets/*
  • Tera Pilot JSON REST API      →  /api/*          (delegated to TeraPilotAPIHandler)
  • Server-Sent Events      →  /api/chat/stream, /api/agent/stream (SSE)

Run it from the CLI:

    tera_pilot                       # default port 18732, host 127.0.0.1
    tera_pilot --port 8000           # custom port
    tera_pilot --host 0.0.0.0        # share on LAN
    tera_pilot --project /path/to/x  # open a project

Then point a browser at http://127.0.0.1:18732/  and the GUI loads.

Architecture
------------
    ┌─────────────────────────────────────────────┐
    │  Browser  →  http://127.0.0.1:PORT/         │
    │                                             │
    │  TeraPilotWebServer  (HTTPServer, threaded)      │
    │   └─ TeraPilotWebHandler (subclass of            │
    │        TeraPilotAPIHandler)                      │
    │       ├─ do_GET  → static OR super().do_GET │
    │       ├─ do_POST → super().do_POST (API)    │
    │       └─ do_DELETE / OPTIONS → super()      │
    └─────────────────────────────────────────────┘

Design notes
------------
* Zero Qt / PySide6 dependency. Pure stdlib http.server.
* :class:`TeraPilotWebHandler` subclasses :class:`tera_pilot.api_server.TeraPilotAPIHandler`
  so every REST + SSE endpoint stays identical to the legacy embedded
  API server — the same handler code handles both static and API paths.
* Static files are served relative to ``tera_pilot/web/`` so the existing
  HTML/CSS/JS frontend keeps working — only the QWebChannel script
  tag was removed from ``index.html``.
* The same auth bearer token that protected the legacy HTTP API
  protects the new one — it is shipped to the browser via
  ``GET /api/status`` in the initial handshake.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
import threading
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Optional
from urllib.parse import urlparse

from .api_server import TeraPilotAPIServer, TeraPilotAPIHandler, ServerContext, _find_free_port
from .utils import setup_logging

logger = logging.getLogger(__name__)

__version__ = "2.3.1"

# Default port — kept identical to the legacy embedded API server so
# existing users / scripts that hit ``http://127.0.0.1:18732`` keep
# working without configuration changes.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18732


# ── Static-file helpers ────────────────────────────────────────────────

def _web_dir() -> Path:
    """Directory that holds ``index.html`` / ``app.js`` / ``style.css``."""
    return Path(__file__).resolve().parent / "web"


def _assets_dir() -> Path:
    return Path(__file__).resolve().parent / "assets"


_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js":   "application/javascript; charset=utf-8",
    ".mjs":  "application/javascript; charset=utf-8",
    ".css":  "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg":  "image/svg+xml",
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif":  "image/gif",
    ".ico":  "image/x-icon",
    ".icns": "application/octet-stream",
    ".webp": "image/webp",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf":  "font/ttf",
    ".otf":  "font/otf",
    ".map":  "application/json; charset=utf-8",
}


def _safe_static_path(requested: str) -> Optional[Path]:
    """Resolve *requested* to a real file under ``web/`` or ``assets/``.

    Returns ``None`` if the path escapes the sandbox root or doesn't
    exist. Used by :class:`TeraPilotWebHandler` for non-API GET requests.
    """
    if not requested or requested == "/":
        requested = "/index.html"
    # Strip query string + fragment
    requested = requested.split("?", 1)[0].split("#", 1)[0]
    rel = requested.lstrip("/")
    web_root = _web_dir().resolve()
    asset_root = _assets_dir().resolve()

    # Try tera_pilot/web/ first.
    candidate = (web_root / rel).resolve()
    try:
        candidate.relative_to(web_root)
    except ValueError:
        return None
    if candidate.exists() and candidate.is_file():
        return candidate

    # Fallback: try tera_pilot/assets/. This lets the HTML reference
    # /assets/logo.png without needing a copy in tera_pilot/web/assets/.
    # We deliberately try BOTH web/ and assets/ for the SAME relative
    # path — so /assets/logo.png resolves to tera_pilot/assets/assets/logo.png
    # if the user nested it that way, OR tera_pilot/assets/logo.png directly.
    asset_candidate = (asset_root / rel).resolve()
    try:
        asset_candidate.relative_to(asset_root)
    except ValueError:
        return None
    if asset_candidate.exists() and asset_candidate.is_file():
        return asset_candidate
    # Last try: strip a leading "assets/" segment, since the URL
    # /assets/logo.png is conventionally meant to map to
    # tera_pilot/assets/logo.png (not tera_pilot/assets/assets/logo.png).
    if rel.startswith("assets/"):
        direct = (asset_root / rel[len("assets/"):]).resolve()
        try:
            direct.relative_to(asset_root)
        except ValueError:
            return None
        if direct.exists() and direct.is_file():
            return direct
    return None


def _content_type_for(path: Path) -> str:
    return _CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")


# ── Combined handler (static + API) ────────────────────────────────────

class TeraPilotWebHandler(TeraPilotAPIHandler):
    """Single handler that serves static files AND the REST + SSE API.

    Subclasses :class:`tera_pilot.api_server.TeraPilotAPIHandler` so every API
    endpoint works unchanged. Only ``do_GET`` is overridden — it
    peeks at the path; if it doesn't start with ``/api/``, the
    request is served as a static file. Otherwise it falls through
    to the parent implementation.
    """

    protocol_version = "HTTP/1.1"

    # Suppress default stderr logging — keep the console clean.
    def log_message(self, fmt, *args):
        logger.debug("[web] " + fmt, *args)

    # ── Static GET paths fall through here; /api/* goes to parent ──
    def do_GET(self):
        path = urlparse(self.path).path
        if path.startswith("/api/"):
            return super().do_GET()
        self._serve_static(path)

    def do_HEAD(self):
        path = urlparse(self.path).path
        if path.startswith("/api/"):
            # Parent doesn't implement HEAD — fall back to GET behaviour.
            return super().do_GET()
        self._serve_static(path, head_only=True)

    # ── Static serving ───────────────────────────────────────────
    def _serve_static(self, path: str, head_only: bool = False) -> None:
        file_path = _safe_static_path(path)
        if file_path is None:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        try:
            data = file_path.read_bytes()
        except OSError as e:
            logger.warning("[web] failed to read %s: %s", file_path, e)
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", _content_type_for(file_path))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if not head_only:
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass


# ── Threaded server ────────────────────────────────────────────────────

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# ── Top-level web server ───────────────────────────────────────────────

class TeraPilotWebServer:
    """Bootstraps the static + API server in a single process.

    Lifecycle::

        srv = TeraPilotWebServer(host='127.0.0.1', port=18732, project='/path')
        srv.start()           # non-blocking — runs in a daemon thread
        ...
        srv.stop()            # graceful shutdown

    Or use the CLI::

        python -m tera_pilot.web_server --port 18732
    """

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: Optional[int] = None,
        project: Optional[str] = None,
        open_browser: bool = True,
    ) -> None:
        self.host = host or DEFAULT_HOST
        self.project = project
        self.open_browser = open_browser

        # Resolve the port. If the caller explicitly supplied one we try
        # it first; if it is busy we auto-scan forward (up to +500) to
        # find a free port instead of crashing with OSError [Errno 48].
        if port is not None:
            self.port = port
        else:
            self.port = _find_free_port(DEFAULT_PORT, host=self.host)

        # The API server carries ServerContext (registry, config, agent
        # runtime). We reuse it so all the existing REST/SSE endpoints
        # work unchanged.
        self._api = TeraPilotAPIServer(port=self.port)
        # Make sure the project root is set on the shared context so
        # /api/status reports it correctly.
        if project:
            try:
                self._api.ctx.config["project_root"] = str(project)
                # Persist it so the next launch remembers.
                from .api_server import _save_config
                _save_config(self._api.ctx.config)
            except Exception as e:
                logger.warning("[web_server] failed to persist project_root: %s", e)

        # Wire the shared ServerContext onto TeraPilotWebHandler (inherited
        # from TeraPilotAPIHandler.ctx — class-level attribute).
        TeraPilotAPIHandler.ctx = self._api.ctx

        # Build a threaded HTTP server that serves both static + /api/*
        # via TeraPilotWebHandler (which inherits all API behaviour).
        # Retry up to 5 times if the port is already in use (TOCTOU race
        # between _find_free_port probe and the actual bind).
        self._http: Optional[ThreadedHTTPServer] = None
        for _attempt in range(5):
            try:
                self._http = ThreadedHTTPServer(
                    (self.host, self.port), TeraPilotWebHandler,
                )
                break
            except OSError as exc:
                logger.warning(
                    "[web_server] port %d on %s already in use (%s), "
                    "scanning for a free port…",
                    self.port, self.host, exc,
                )
                self.port = _find_free_port(self.port + 1, host=self.host)
        if self._http is None:
            # Last resort: let the OS pick any free port.
            self.port = _find_free_port(0, host=self.host)
            self._http = ThreadedHTTPServer(
                (self.host, self.port), TeraPilotWebHandler,
            )
        self._thread: Optional[threading.Thread] = None

    # ── Lifecycle ────────────────────────────────────────────────
    def start(self) -> None:
        """Start serving in a background daemon thread."""
        self._thread = threading.Thread(
            target=self._http.serve_forever,
            name="tera-pilot-web-server",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "[web_server] Tera Pilot v%s listening on http://%s:%d",
            __version__, self.host, self.port,
        )
        if self.open_browser:
            try:
                webbrowser.open(f"http://{self.host}:{self.port}/")
            except Exception:
                pass  # headless environments

    def stop(self) -> None:
        """Stop serving and release the port."""
        try:
            self._api.stop()
        except Exception:
            pass
        try:
            self._http.shutdown()
            self._http.server_close()
        except Exception:
            pass
        logger.info("[web_server] stopped")

    def serve_forever(self) -> None:
        """Block the calling thread until interrupted (Ctrl+C).

        Use this for the CLI entry point so the process stays alive.
        """
        self.start()
        try:
            while True:
                # Sleep in small chunks so KeyboardInterrupt fires fast.
                threading.Event().wait(0.5)
        except KeyboardInterrupt:
            print("\n[tera_pilot] shutting down…")
        finally:
            self.stop()

    # ── Properties ───────────────────────────────────────────────
    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def api_token(self) -> str:
        return self._api.auth_token

    @property
    def ctx(self) -> ServerContext:
        return self._api.ctx


# ── CLI ────────────────────────────────────────────────────────────────

def _parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="tera_pilot",
        description="Tera Pilot v2.3.1 — local-first AI IDE (web UI).",
    )
    p.add_argument(
        "--host", default=os.environ.get("TERA_PILOT_HOST", DEFAULT_HOST),
        help=f"Bind host (default: {DEFAULT_HOST}). Set TERA_PILOT_HOST env to override.",
    )
    p.add_argument(
        "--port", "-p", type=int,
        default=int(os.environ.get("TERA_PILOT_PORT", str(DEFAULT_PORT))),
        help=f"Bind port (default: {DEFAULT_PORT}). Set TERA_PILOT_PORT env to override.",
    )
    p.add_argument(
        "--project", "-w", default=os.getcwd(),
        help="Workspace / project root to open (default: current directory).",
    )
    p.add_argument(
        "--no-browser", action="store_true",
        help="Don't auto-open the default browser on start.",
    )
    p.add_argument(
        "--version", action="version", version=f"tera_pilot {__version__}",
    )
    return p.parse_args(argv)


def main(argv: Optional[list] = None) -> int:
    """CLI entry point. Returns the process exit code."""
    setup_logging()
    args = _parse_args(argv)
    server = TeraPilotWebServer(
        host=args.host,
        port=args.port,
        project=args.project,
        open_browser=not args.no_browser,
    )
    print(f"\n  Tera Pilot v{__version__} — Web UI")
    print(f"  ─────────────────────────────────────────────")
    print(f"  Local:   {server.base_url}/")
    print(f"  API:     {server.base_url}/api/status")
    print(f"  Project: {args.project}")
    print(f"  Token:   {server.api_token[:16]}…")
    print(f"\n  Press Ctrl+C to stop.\n")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
