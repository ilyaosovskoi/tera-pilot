"""model_picker.py — Modal dialog for choosing model/provider.

v2.2.4-fix: converted from a broken Static subclass (whose BINDINGS
never fire) to a proper ModalScreen with working keyboard navigation
and OptionList-based selection (BUGS_REPORT — ModelPickerModal was
non-interactive).
"""

from __future__ import annotations

from typing import Any, Callable, List

from textual.app import ComposeResult
from textual.containers import Container, Vertical
from textual.screen import ModalScreen
from textual.binding import Binding
from textual.widgets import Button, Static, OptionList


class ModelPickerModal(ModalScreen[str]):
    """Modal dialog for picking a model or provider from a list.

    Uses OptionList for keyboard-navigable selection.
    """

    BINDINGS = [
        Binding("escape", "dismiss_picker", "Cancel", show=False),
        Binding("enter", "select", "Select", show=False),
        Binding("up", "navigate_up", "Up", show=False),
        Binding("down", "navigate_down", "Down", show=False),
    ]

    CSS = """
    ModelPickerModal {
        align: center middle;
    }

    #picker-box {
        width: 60;
        height: auto;
        max-height: 25;
        border: solid #505050;
        background: $surface;
        padding: 1 2;
    }

    #picker-title {
        color: white;
        text-style: bold;
        margin-bottom: 1;
    }

    #picker-list {
        height: auto;
        max-height: 15;
        margin-bottom: 1;
    }

    #picker-buttons {
        height: auto;
        align: center middle;
    }
    """

    def __init__(
        self,
        title: str,
        options: List[str],
        on_select: Callable[[str], None],
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.title = title
        self.options = options
        self.on_select = on_select
        self._highlighted = 0

    def compose(self) -> ComposeResult:
        with Container(id="picker-box"):
            yield Static(f"[bold]{self.title}[/bold]", id="picker-title")
            yield OptionList(*self.options, id="picker-list")
            with Container(id="picker-buttons"):
                yield Button("Cancel", id="cancel_btn", variant="error")
                yield Button("Select", id="select_btn", variant="primary")

    def on_mount(self) -> None:
        list_w = self.query_one("#picker-list", OptionList)
        if self.options:
            list_w.highlighted = 0
        list_w.focus()

    def action_navigate_up(self) -> None:
        try:
            list_w = self.query_one("#picker-list", OptionList)
            current = list_w.highlighted
            if current is not None and current > 0:
                list_w.highlighted = current - 1
                self._highlighted = current - 1
        except Exception:
            pass

    def action_navigate_down(self) -> None:
        try:
            list_w = self.query_one("#picker-list", OptionList)
            current = list_w.highlighted
            count = list_w.option_count
            if current is not None and current < count - 1:
                list_w.highlighted = current + 1
                self._highlighted = current + 1
        except Exception:
            pass

    def action_select(self) -> None:
        try:
            list_w = self.query_one("#picker-list", OptionList)
            idx = list_w.highlighted if list_w.highlighted is not None else 0
        except Exception:
            idx = self._highlighted
        if 0 <= idx < len(self.options):
            self.on_select(self.options[idx])
        self.dismiss()

    def action_dismiss_picker(self) -> None:
        self.dismiss()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """v2.3.4-fix: renamed from ``on_option_list_selected`` — Textual
        8.x dispatches to ``on_option_list_option_selected``; the old
        name never fired, so clicking an option did nothing.
        """
        self._highlighted = event.option_index
        self.action_select()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel_btn":
            self.dismiss()
        elif event.button.id == "select_btn":
            self.action_select()
