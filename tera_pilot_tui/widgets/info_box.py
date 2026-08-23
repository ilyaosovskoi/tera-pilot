from __future__ import annotations

from typing import Any, Optional

from rich.panel import Panel
from rich.text import Text
from textual.widgets import Static


class InfoBox(Static):
    """Codex-style info box showing model, provider, directory in a panel."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._model: str = "unknown"
        self._provider: str = "unknown"
        self._directory: str = "~"
        self._version: str = "2.3.6"

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

    def _render_text(self) -> Panel:
        """Render the info box content as Rich Panel with solid border."""
        lines = [
            f"[bold]> tera_pilot[/bold] (v{self._version})",
            f"| model:     [white]{self._model:<20}[/white]  /model to change",
            f"| provider:  [white]{self._provider:<20}[/white]  /provider to change",
            f"| directory: [white]{self._directory:<20}[/white]  /cd to change",
        ]
        return Panel(
            Text.from_markup("\n".join(lines)),
            border_style="#1f1f23",
        )
