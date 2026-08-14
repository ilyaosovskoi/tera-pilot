"""
Slash Commands — reusable prompt snippets stored as .md files in .claude/commands/.

Follows the Boris methodology: "Everything you do more than twice becomes a tool."
Users create .md files in .claude/commands/ and invoke them with /command-name
in the composer. The file content is injected into the prompt.

Each .md file can contain:
- The prompt template text
- $ARGUMENTS placeholder that gets replaced with user input after the command
- Bash blocks wrapped in ```bash that get pre-executed for context
"""

import os
import glob
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Any
from pathlib import Path


@dataclass
class SlashCommand:
    id: str           # filename without extension
    name: str         # display name (id with dashes replaced)
    description: str  # first line or first paragraph
    body: str         # full file content
    source: str       # "project" or "global"
    path: str         # full file path
    has_arguments: bool  # True if body contains $ARGUMENTS


class SlashCommandManager:
    """Discovers and manages slash commands from .claude/commands/ directories."""

    def __init__(self):
        self._commands: Dict[str, SlashCommand] = {}
        self._project_root: Optional[str] = None

    def set_project_root(self, root: str):
        self._project_root = root
        self.reload()

    def reload(self):
        """Reload all commands from disk."""
        self._commands.clear()
        self._load_from_dir(
            os.path.expanduser("~/.tera_pilot/commands"),
            "global"
        )
        if self._project_root:
            self._load_from_dir(
                os.path.join(self._project_root, ".claude", "commands"),
                "project"
            )

    def _load_from_dir(self, directory: str, source: str):
        if not os.path.isdir(directory):
            return
        for filepath in sorted(glob.glob(os.path.join(directory, "*.md"))):
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                if not content:
                    continue

                basename = os.path.basename(filepath)
                cmd_id = basename[:-3]  # remove .md
                name = cmd_id.replace("-", " ").replace("_", " ").title()

                # Extract description: first non-empty line
                description = ""
                for line in content.split("\n"):
                    stripped = line.strip()
                    if stripped and not stripped.startswith("#"):
                        description = stripped[:120]
                        break

                has_args = "$ARGUMENTS" in content

                cmd = SlashCommand(
                    id=cmd_id,
                    name=name,
                    description=description,
                    body=content,
                    source=source,
                    path=filepath,
                    has_arguments=has_args,
                )
                # Project commands override global ones
                if source == "project" or cmd_id not in self._commands:
                    self._commands[cmd_id] = cmd
            except Exception:
                continue

    def list_commands(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": c.id,
                "name": c.name,
                "description": c.description,
                "source": c.source,
                "has_arguments": c.has_arguments,
            }
            for c in sorted(self._commands.values(), key=lambda x: (x.source != "project", x.id))
        ]

    def get_command(self, cmd_id: str) -> Optional[SlashCommand]:
        return self._commands.get(cmd_id)

    def resolve(self, text: str) -> Optional[Dict[str, Any]]:
        """Check if text starts with a slash command. Returns {command, arguments, expanded} or None."""
        text = text.strip()
        if not text.startswith("/"):
            return None

        # Split "/command-name optional arguments"
        parts = text.split(None, 1)
        cmd_id = parts[0][1:]  # remove leading /
        arguments = parts[1] if len(parts) > 1 else ""

        cmd = self._commands.get(cmd_id)
        if not cmd:
            return None

        # Expand the command body
        expanded = cmd.body
        if cmd.has_arguments:
            expanded = expanded.replace("$ARGUMENTS", arguments)

        return {
            "command": cmd_id,
            "arguments": arguments,
            "expanded": expanded,
            "description": cmd.description,
        }

    def create_command(self, name: str, body: str, source: str = "project") -> Dict[str, Any]:
        """Create a new command file."""
        if source == "global":
            dir_path = os.path.expanduser("~/.tera_pilot/commands")
        elif self._project_root:
            dir_path = os.path.join(self._project_root, ".claude", "commands")
        else:
            dir_path = os.path.expanduser("~/.tera_pilot/commands")

        os.makedirs(dir_path, exist_ok=True)
        filename = name.lower().replace(" ", "-") + ".md"
        filepath = os.path.join(dir_path, filename)

        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(body.strip() + "\n")

        self.reload()
        return {"ok": True, "path": filepath, "id": name.lower().replace(" ", "-")}

    def delete_command(self, cmd_id: str) -> Dict[str, Any]:
        """Delete a command file."""
        cmd = self._commands.get(cmd_id)
        if not cmd:
            return {"ok": False, "error": "Command not found"}
        try:
            os.remove(cmd.path)
            self.reload()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}