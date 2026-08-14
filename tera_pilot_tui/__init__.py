"""tera_pilot_tui — a full-screen Textual TUI for the Tera Pilot agent.

Kept in a separate top-level package (not inside tera_pilot/) and talking to the core
only through tera_pilot_tui.bridge.TeraPilotBridge, so it never becomes another parallel
agent-loop path.
"""

from .app import TeraPilotTUIApp
from .bridge import TeraPilotBridge, ProviderChoice

__all__ = ["TeraPilotTUIApp", "TeraPilotBridge", "ProviderChoice"]
