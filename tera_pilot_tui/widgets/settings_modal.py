"""settings_modal.py — Simplified two-tier settings for the TUI.

v2.2.4: Most users just want to pick a provider, enter an API key,
choose a model, and go. The old TUI required memorising slash commands
(/model, /guardian, /budget, /cost, /second_opinion …) which was
intimidating for new users.

This widget provides a simple, visual settings screen with:
  - Provider selection (top 6 most popular)
  - API key input
  - Model name input
  - Theme toggle (Dark / Light)

An "Advanced" section at the bottom links to the existing slash commands
for full configuration.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static, Label


# The 6 most popular providers — keep this list short and simple.
QUICK_PROVIDERS = [
    ("openai",    "OpenAI",        "🟢", True),
    ("anthropic", "Anthropic",     "🟠", True),
    ("gemini",    "Google Gemini", "🔵", True),
    ("deepseek",  "DeepSeek",      "🟣", True),
    ("groq",      "Groq",          "⚡", True),
    ("ollama",    "Ollama (local)", "🦙", False),
]


class QuickSettingsModal(ModalScreen[None]):
    """Simplified settings modal — provider, model, API key, theme."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close", priority=True),
    ]

    CSS = """
    QuickSettingsModal {
        align: center middle;
    }
    #qs-container {
        width: 60;
        height: auto;
        max-height: 28;
        border: thick $border;
        background: $surface;
        padding: 1 2;
    }
    #qs-title {
        text-style: bold;
        margin-bottom: 1;
    }
    .qs-section-label {
        text-style: bold;
        color: $text-muted;
        margin-top: 1;
        margin-bottom: 0;
    }
    #qs-provider-grid {
        height: auto;
        margin-top: 0;
        margin-bottom: 0;
    }
    .qs-provider-btn {
        width: 1fr;
        margin: 0 1;
    }
    .qs-provider-btn.active {
        text-style: bold;
        background: $accent-darken-2;
    }
    #qs-model-input, #qs-key-input {
        margin-top: 0;
        margin-bottom: 0;
    }
    #qs-footer {
        height: auto;
        margin-top: 1;
    }
    #qs-advanced-hint {
        color: $text-muted;
        text-style: italic;
        margin-top: 1;
    }
    """

    def __init__(self, bridge, **kwargs):
        super().__init__(**kwargs)
        self.bridge = bridge
        self._selected_provider = bridge.get_provider() or "openai"

    def compose(self) -> ComposeResult:
        with Vertical(id="qs-container"):
            yield Label("Settings", id="qs-title")

            yield Label("Provider", classes="qs-section-label")
            with Horizontal(id="qs-provider-grid"):
                for pid, label, icon, needs_key in QUICK_PROVIDERS:
                    btn = Button(
                        f"{icon} {label}",
                        id=f"qs-prov-{pid}",
                        classes="qs-provider-btn",
                    )
                    if pid == self._selected_provider:
                        btn.classes = "qs-provider-btn active"
                    yield btn

            yield Label("Model", classes="qs-section-label")
            yield Input(
                id="qs-model-input",
                placeholder="e.g. gpt-4o, claude-sonnet-4",
            )

            yield Label("API Key", classes="qs-section-label")
            yield Input(
                id="qs-key-input",
                placeholder="sk-... (leave empty to keep current)",
                password=True,
            )

            with Horizontal(id="qs-footer"):
                yield Button("Save", variant="primary", id="qs-save")
                yield Button("Advanced…", variant="default", id="qs-advanced")
                yield Button("Cancel", variant="default", id="qs-cancel")

            yield Static(
                "Advanced: /model  /guardian  /budget  /cost  /second_opinion",
                id="qs-advanced-hint",
            )

    def on_mount(self) -> None:
        # Pre-fill model from bridge
        try:
            status = self.bridge.get_status()
            model = status.get("model", "")
            if model:
                self.query_one("#qs-model-input", Input).value = model
        except Exception:
            pass

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle Enter key in Model / API Key input fields.

        Without this, pressing Enter is silently swallowed by the Input
        widget and the user has no keyboard way to save settings.
        """
        event.prevent_default()
        event.stop()
        # Trigger the same save logic as the Save button.
        self._save_and_dismiss()

    def _save_and_dismiss(self) -> None:
        """Persist settings and close the modal."""
        model = self.query_one("#qs-model-input", Input).value.strip()
        api_key = self.query_one("#qs-key-input", Input).value.strip()
        pid = self._selected_provider

        # Switch provider
        try:
            self.bridge.set_provider(pid)
        except Exception:
            pass

        # Set model
        if model:
            try:
                registry = self.bridge._registry
                if registry:
                    registry.configure(pid, model=model)
            except Exception:
                pass

        # Set API key
        if api_key:
            try:
                registry = self.bridge._registry
                if registry:
                    registry.configure(pid, api_key=api_key)
            except Exception:
                pass

        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id

        # Provider selection
        if btn_id and btn_id.startswith("qs-prov-"):
            pid = btn_id[len("qs-prov-"):]
            self._selected_provider = pid
            # Update active button styles
            for _, label, icon, _ in QUICK_PROVIDERS:
                b = self.query_one(f"#qs-prov-{_}", Button)
                if _ == pid:
                    b.classes = "qs-provider-btn active"
                else:
                    b.classes = "qs-provider-btn"
            # Ollama doesn't need an API key
            if pid == "ollama":
                self.query_one("#qs-key-input", Input).disabled = True
            else:
                self.query_one("#qs-key-input", Input).disabled = False
            return

        # Save
        if btn_id == "qs-save":
            self._save_and_dismiss()
            return

        # Advanced → dismiss and let caller open full settings
        if btn_id == "qs-advanced":
            self.dismiss(None)
            # The caller will open the full model palette
            return

        # Cancel
        if btn_id == "qs-cancel":
            self.dismiss(None)
            return
