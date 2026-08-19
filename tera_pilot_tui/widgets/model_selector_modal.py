"""model_selector_modal.py — Beautiful model selection modal."""

from typing import Any, Callable, List, Dict, Optional

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical, Container
from textual.widgets import Static, Input, OptionList
from textual.screen import ModalScreen
from textual.binding import Binding

from .motion import entrance


class ModelSelectorModal(ModalScreen):
    """Beautiful modal for selecting a model/provider."""

    BINDINGS = [
        Binding("escape", "dismiss", "Cancel", show=False),
        Binding("enter", "select", "Select", show=False),
        Binding("up", "navigate_up", "Up", show=False),
        Binding("down", "navigate_down", "Down", show=False),
    ]

    CSS = """
    ModelSelectorModal {
        align: center middle;
    }

    #selector-box {
        width: 80;
        height: auto;
        max-height: 30;
        border: solid #2e2e33;
        background: #111114;
        padding: 1 2;
    }

    
    #selector-title {
        color: #f5f5f7;
        text-style: bold;
        margin-bottom: 1;
    }

    #selector-filter {
        margin-bottom: 1;
        height: 3;
        border: solid #2e2e33;
    }

    #selector-list {
        height: auto;
        max-height: 20;
        margin-bottom: 1;
    }

    #selector-list OptionList {
        height: auto;
        max-height: 20;
    }

    #selector-footer {
        height: 1;
        color: #86868b;
        text-style: italic;
    }
    """

    def __init__(
        self,
        providers: List[Dict[str, Any]],
        on_select: Callable[[str], None],
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.providers = providers
        self.providers_filtered = providers  # v2.2.4-fix: initialize in __init__
        self.on_select = on_select
        self._highlighted = 0

    def compose(self) -> ComposeResult:
        with Container(id="selector-box"):
            yield Static("Select model", id="selector-title")
            yield Input(
                placeholder="Filter commands...",
                id="selector-filter",
            )
            yield OptionList(id="selector-list")
            yield Static(
                "↑↓ Navigate  •  Enter Select  •  Esc Cancel",
                id="selector-footer",
            )

    def on_mount(self) -> None:
        self._render_list()
        # v2.3.1: entrance animation — fade + slight rise.
        entrance(self.query_one("#selector-box"))
        self.query_one("#selector-filter", Input).focus()

    def _render_list(self) -> None:
        """Render the provider list."""
        list_w = self.query_one("#selector-list", OptionList)
        list_w.clear_options()

        for i, p in enumerate(self.providers, 1):
            label = p.get("label", p.get("id", "?"))
            model = p.get("model", p.get("default_model", "?"))
            is_active = p.get("active", False)

            # Format: ✓ 1. Provider — model: xxx
            marker = "[green]✓[/green]" if is_active else " "
            display = f"{marker} {i}. {label:<20} model: {model}"

            list_w.add_option(display)

        if self.providers:
            list_w.highlighted = self._highlighted

    def on_input_changed(self, event) -> None:
        """Filter providers as user types."""
        if event.input.id != "selector-filter":
            return
        query = event.value.lower()
        list_w = self.query_one("#selector-list", OptionList)
        list_w.clear_options()

        filtered_providers = []
        for p in self.providers:
            label = p.get("label", p.get("id", "?")).lower()
            model = p.get("model", p.get("default_model", "?")).lower()
            if query in label or query in model:
                filtered_providers.append(p)

        # Re-render with filtered list
        self.providers_filtered = filtered_providers
        for i, p in enumerate(filtered_providers, 1):
            label = p.get("label", p.get("id", "?"))
            model = p.get("model", p.get("default_model", "?"))
            is_active = p.get("active", False)

            marker = "[green]✓[/green]" if is_active else " "
            display = f"{marker} {i}. {label:<20} model: {model}"

            list_w.add_option(display)

        self._highlighted = 0
        if filtered_providers:
            list_w.highlighted = 0

    def action_select(self) -> None:
        """Select the highlighted provider.

        Reads the highlighted index directly from the OptionList widget
        to avoid desync with the cached self._highlighted value.
        """
        try:
            list_w = self.query_one("#selector-list", OptionList)
            idx = list_w.highlighted if list_w.highlighted is not None else 0
        except Exception:
            idx = self._highlighted
        providers = self.providers_filtered
        if 0 <= idx < len(providers):
            provider_id = providers[idx].get("id", "")
            self.on_select(provider_id)
        self.dismiss()

    def action_dismiss(self) -> None:
        """Cancel the selection."""
        self.dismiss()

    def action_navigate_up(self) -> None:
        """Move selection up (v2.2.4-fix: keyboard navigation)."""
        try:
            list_w = self.query_one("#selector-list", OptionList)
            current = list_w.highlighted
            if current is not None and current > 0:
                list_w.highlighted = current - 1
                self._highlighted = current - 1
        except Exception:
            pass

    def action_navigate_down(self) -> None:
        """Move selection down (v2.2.4-fix: keyboard navigation)."""
        try:
            list_w = self.query_one("#selector-list", OptionList)
            current = list_w.highlighted
            count = list_w.option_count
            if current is not None and current < count - 1:
                list_w.highlighted = current + 1
                self._highlighted = current + 1
        except Exception:
            pass

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle Enter key while the filter Input has focus.

        Textual delivers Enter as Input.Submitted to the focused Input,
        NOT as a binding to the screen. Without this handler, pressing
        Enter after typing a filter does nothing — the user cannot select
        any provider via keyboard.
        """
        event.prevent_default()
        event.stop()
        self.action_select()

    def on_option_list_option_selected(self, event) -> None:
        """Handle provider selection.

        v2.3.4-fix: renamed from ``on_option_list_selected`` — Textual
        8.x dispatches ``OptionList.OptionSelected`` to
        ``on_option_list_option_selected``; the old name never fired, so
        clicking a provider in this modal did nothing.
        """
        self._highlighted = event.option_index
        self.action_select()
