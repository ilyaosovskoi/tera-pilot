"""Widgets for the Tera Pilot TUI."""

from .approval_card import ApprovalCard, PendingApproval, hint_text, key_decision
from .approval_modal import ApprovalModal, GuardianModal
from .mascot import QUIPS, render_mascot, random_quip
from .chat_log import ChatLog
from .command_palette import CommandPalette
from .command_suggestions import CommandSuggestions
from .input_box import InputBox
from .status_bar import StatusBar
from .task_canvas_view import TaskCanvasView
from .thinking import ThinkingIndicator
from .tool_block import ToolBlock

__all__ = [
    "ApprovalCard",
    "PendingApproval",
    "hint_text",
    "key_decision",
    "ApprovalModal",
    "render_mascot",
    "random_quip",
    "QUIPS",
    "ChatLog",
    "CommandPalette",
    "CommandSuggestions",
    "GuardianModal",
    "InputBox",
    "StatusBar",
    "TaskCanvasView",
    "ThinkingIndicator",
    "ToolBlock",
]
