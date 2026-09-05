"""info_box.py — top header with brand, live status and provider/meta chips.

v2.4.1 (visual refresh): the old render was a Rich ``Panel`` with
hard-coded dark-palette colors (``[white]`` labels + a ``#1f1f23``
border), which became nearly unreadable after switching to the light
theme. The header is now theme-aware: ``set_theme(dark)`` swaps the
whole palette (accent / value / muted), and the animated status line
(the braille spinner the app drives via ``update_status``) is shown
as a colored chip instead of a plain text row.

The public API used by app.py and the test suite is unchanged:
    update_info(model, provider, directory, version)
    update_status(text) / clear_status()
    _status  — the currently displayed status text
"""

from __future__ import annotations

from typing import Any, Optional

from rich.text import Text
from textual.widgets import Static

# ── Per-theme palettes (dark / light) ────────────────────────────────
# Accent = brand + status chip; value = primary text; muted = secondary.
_PALETTES = {
    True: {   # dark
        "accent": "#d77757",
        "value": "#e8e8ec",
        "muted": "#8a8a92",
    },
    False: {  # light
        "accent": "#b34d2e",
        "value": "#26262e",
        "muted": "#6b6b76",
    },
}

_BRAND = "❯ tera_pilot"


class InfoBox(Static):
    """Theme-aware top header: brand line + model/provider/directory chips.

    v2.4.1 (visual refresh): colors are chosen from the active theme
    palette instead of hard-coded dark values, so the header stays
    readable in both dark and light mode.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._model: str = "unknown"
        self._provider: str = "unknown"
        self._directory: str = "~"
        self._version: str = "2.4.0"
        # v2.3.6: animated status line (spinner + word) shown while a turn
        # runs. Cleared when idle.
        self._status: str = ""
        # v2.4.1: active theme (True = dark). Set by the app on mount and
        # whenever the user switches theme, so the palette always matches.
        self._dark: bool = True

    # ── Public API (used by app.py / tests) ───────────────────────

    def update_info(
        self,
        model: Optional[str] = None,
        provider: Optional[str] = None,
        directory: Optional[str] = None,
        version: Optional[str] = None,
    ) -> None:
        """Update displayed info and refresh."""
        if model is not None:
            self._model = model
        if provider is not None:
            self._provider = provider
        if directory is not None:
            self._directory = directory
        if version is not None:
            self._version = version
        self.update(self._render_text())

    def update_status(self, text: str) -> None:
        """Set the animated status line (e.g. the braille thinking chip)."""
        if text != self._status:
            self._status = text
            self.update(self._render_text())

    def clear_status(self) -> None:
        """Hide the status line when the turn ends."""
        if self._status:
            self._status = ""
            self.update(self._render_text())

    def set_theme(self, dark: bool) -> None:
        """v2.4.1: switch the palette (called by the app on theme change)."""
        self._dark = bool(dark)
        try:
            self.update(self._render_text())
        except Exception:
            pass

    @property
    def dark(self) -> bool:
        return self._dark

    # ── Rendering ──────────────────────────────────────────────────

    def _render_text(self) -> Text:
        pal = _PALETTES[bool(self._dark)]

        brand = Text.assemble(
            (f"{_BRAND} ", f"bold {pal['accent']}"),
            (f"v{self._version}", pal["muted"]),
        )

        # Meta chips: model / provider / directory, muted labels + values.
        meta = Text.assemble(
            ("model  ", pal["muted"]),
            (self._model, f"bold {pal['value']}"),
            ("   ", pal["muted"]),
            ("provider  ", pal["muted"]),
            (self._provider, f"bold {pal['value']}"),
            ("   ", pal["muted"]),
            ("dir  ", pal["muted"]),
            (self._directory, pal["value"]),
        )

        lines: list[Text] = [brand, meta]
        if self._status:
            lines.append(
                Text.assemble(("  ", pal["muted"]), (self._status, f"bold {pal['accent']}"))
            )
        out = lines[0]
        for ln in lines[1:]:
            out.append_text(Text("\n"))
            out.append_text(ln)
        return out
