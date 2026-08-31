"""
Tera Pilot v2.2.0 — Auto-Updater (Qt-free).

Checks the GitHub Releases API for a newer version and invokes a
callback when one is available. Uses stdlib urllib + threading only
— no Qt / PySide6 dependency.

v2.2.0: the legacy QObject / Signal / QThread based AutoUpdater has
been rewritten on top of plain :mod:`threading`. The public API is
preserved as closely as possible:

    updater = AutoUpdater()
    updater.on_update_available = lambda info: ...
    updater.check_for_updates()

Differences from the v2.1 API:
- ``update_available`` is now a plain callback property
  (``on_update_available``) instead of a Qt Signal. Multiple
  listeners can be registered via ``add_listener(fn)``.
- ``no_update`` is folded into the same listener mechanism —
  listeners receive ``{"update_available": False}`` when no update
  is found or when the check fails.
- ``check_for_updates()`` returns immediately; the check runs in a
  daemon thread, just like before.

v1.1.5-fix (tera_pilot_bug_report.md bug #11): previously the default
``repo`` argument was the placeholder string ``"user/tera_pilot"`` and
both call sites in the codebase (``main_window.py:240`` and
``web_bridge.py:835``) constructed ``AutoUpdater(parent=self)``
without ever passing a real ``repo``. The constructor left
``self._repo == "user/tera_pilot"`` forever, and ``check_for_updates()``
explicitly skipped when it saw that string, so the auto-update
feature was effectively a no-op. Fixed in v1.1.5 by making the
default the real Tera Pilot repo and skipping only on falsy repo values.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import urllib.request
import urllib.error
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

__version__ = "2.4.0"

# v1.1.5-fix (bug #11): real default repo. The old placeholder
# "user/tera_pilot" caused every update check to be silently skipped.
# This constant can be overridden by the caller (e.g. config-driven
# ``update_repo``), but the out-of-the-box behaviour now actually
# hits the GitHub Releases API for the real project.
DEFAULT_REPO = "ilyaosovskoi/tera-pilot"


def _parse_version(version_str: str) -> tuple:
    """Parse 'v1.0.3' or '1.0.3' into (1, 0, 3)."""
    cleaned = version_str.strip().lstrip("vV")
    parts = re.split(r"[.\-]", cleaned)
    result = []
    for p in parts:
        m = re.match(r"(\d+)", p)
        if m:
            result.append(int(m.group(1)))
        else:
            break
    return tuple(result) if result else (0, 0, 0)


def get_current_version() -> str:
    """Return the current Tera Pilot version string."""
    try:
        from . import __version__ as pkg_version
        return pkg_version
    except (ImportError, AttributeError):
        pass
    return __version__


# Listener type: callable that takes a single ``info: dict`` argument.
UpdateListener = Callable[[Dict[str, Any]], None]


class AutoUpdater:
    """Checks GitHub for a newer Tera Pilot release.

    Usage::

        updater = AutoUpdater()
        updater.add_listener(lambda info: print(info))
        updater.check_for_updates()

    v2.2.0: rewritten on plain ``threading`` — no Qt / PySide6.
    """

    def __init__(
        self,
        repo: Optional[str] = DEFAULT_REPO,
        parent: Any = None,  # ignored — kept for backward compat
        current_version: Optional[str] = None,
    ) -> None:
        # parent is intentionally swallowed — it was only used to attach
        # the QObject to the Qt parent's lifetime. Plain Python objects
        # are GC'd when the last reference goes away.
        self._repo: Optional[str] = self._normalise_repo(repo)
        self._current_version: Optional[str] = current_version
        self._worker: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._listeners: List[UpdateListener] = []

    # ── Listeners (replacement for Qt signals) ──────────────────

    def add_listener(self, fn: UpdateListener) -> None:
        """Register a callback invoked with the release info dict.

        The dict shape::

            {"update_available": bool,
             "latest": str, "current": str,
             "url": str, "body": str, "published_at": str}

        When ``update_available`` is ``False``, only that key is
        present (plus optionally ``error`` if the check failed).
        """
        with self._lock:
            self._listeners.append(fn)

    def remove_listener(self, fn: UpdateListener) -> None:
        with self._lock:
            try:
                self._listeners.remove(fn)
            except ValueError:
                pass

    # Backward-compat shim for code that assigned to ``on_update_available``.
    @property
    def on_update_available(self) -> Optional[UpdateListener]:
        """Return the first registered listener (or None)."""
        with self._lock:
            return self._listeners[0] if self._listeners else None

    @on_update_available.setter
    def on_update_available(self, fn: Optional[UpdateListener]) -> None:
        with self._lock:
            if fn is None:
                self._listeners.clear()
            else:
                # Replace any existing listeners — matches Qt semantics
                # where connecting the same slot twice still emits once.
                self._listeners.clear()
                self._listeners.append(fn)

    def _emit(self, info: Dict[str, Any]) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for fn in listeners:
            try:
                fn(info)
            except Exception:
                logger.exception("[updater] listener raised")

    # ── Repo management ─────────────────────────────────────────

    @staticmethod
    def _normalise_repo(repo: Optional[str]) -> Optional[str]:
        """Return a clean repo slug, or *None* if updates are disabled.

        v1.1.5-fix (bug #11): we still treat the legacy placeholder
        ``"user/tera_pilot"`` as "disabled" so that old config files which
        explicitly stored that string don't suddenly start hitting the
        GitHub API for a repo that doesn't exist.
        """
        if not repo:
            return None
        repo = repo.strip()
        if not repo or repo == "user/tera_pilot":
            return None
        return repo

    def set_repo(self, repo: Optional[str]) -> None:
        """v1.1.5 — override the GitHub repo at runtime.

        Reads from ``config["update_repo"]`` in the bridge. Pass *None*
        or an empty string to disable update checks for this instance.
        The legacy placeholder ``"user/tera_pilot"`` is also treated as
        "disabled" so old config files don't 404 on every check.
        """
        self._repo = self._normalise_repo(repo)

    @property
    def repo(self) -> Optional[str]:
        """The current GitHub ``owner/name`` slug, or *None* if disabled."""
        return self._repo

    # ── Check ────────────────────────────────────────────────────

    def check_for_updates(self, current_version: Optional[str] = None) -> None:
        """Start a background check. Results arrive via listeners.

        v1.0.5-hotfix: skip the check entirely if the repo is *falsy*
        (None / empty / legacy placeholder). Previously every startup
        fired two HTTP requests to
        ``api.github.com/repos/user/tera_pilot/releases/latest`` which 404'd
        every time, wasting ~2 seconds and cluttering the log.

        v1.1.5-fix (bug #11): the placeholder check is now done in
        ``_normalise_repo`` at construction time, so by the time we
        get here ``self._repo`` is either a real slug or *None*. The
        same logic is also applied to ``set_repo()`` so callers can
        cleanly disable updates at runtime.
        """
        version = current_version or self._current_version or get_current_version()
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(
                target=self._run_check,
                args=(version,),
                name="tera-pilot-update-checker",
                daemon=True,
            )
            self._worker.start()

    def _run_check(self, current_version: str) -> None:
        """Background check — runs in a daemon thread."""
        # Skip if the repo is None / empty — checks are disabled for
        # this instance (e.g. air-gapped build or user opted out).
        if not self._repo:
            logger.debug("[updater] skipping check — repo is disabled/empty")
            return

        url = f"https://api.github.com/repos/{self._repo}/releases/latest"
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "Tera Pilot-Updater/2.4.0",
                },
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            tag = data.get("tag_name", "")
            latest_parsed = _parse_version(tag)
            current_parsed = _parse_version(current_version)

            if latest_parsed > current_parsed:
                body = data.get("body", "") or ""
                self._emit({
                    "update_available": True,
                    "latest": tag,
                    "current": current_version,
                    "url": data.get("html_url", ""),
                    "body": body[:500],
                    "published_at": data.get("published_at", ""),
                })
            else:
                self._emit({"update_available": False})

        except urllib.error.HTTPError as e:
            if e.code == 404:
                logger.info(
                    "[updater] repo/releases not found (private or no releases): %s",
                    self._repo,
                )
            else:
                logger.warning("[updater] HTTP %s: %s", e.code, e.reason)
            self._emit({"update_available": False, "error": f"HTTP {e.code}"})
        except Exception as e:
            logger.warning("[updater] check failed: %s", e)
            self._emit({"update_available": False, "error": str(e)})


__all__ = [
    "AutoUpdater",
    "UpdateListener",
    "DEFAULT_REPO",
    "get_current_version",
    "_parse_version",
]
