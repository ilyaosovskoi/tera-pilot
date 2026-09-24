# Tera Pilot Bridge — example VS Code extension

Minimal client for `tera_pilot/ide_bridge.py`. It is intentionally small
(~150 lines): connection, selection-as-context, event polling, approval
dialogs, diff preview. Copy it as the starting point for a full
marketplace extension.

## Run the agent side

```bash
tera-pilot-bridge --port 8766
# prints the port; the token lives in ~/.tera_pilot/config.json (bridge_token)
```

Or from inside the TUI: `/bridge start` (shows port + token hint).

## Install the example extension

```bash
cd editors/vscode
npm install -g @vscode/vsce
vsce package
code --install-extension tera-pilot-bridge-0.1.0.vsix
```

Set `teraPilot.port` / `teraPilot.token` in VS Code settings, then run
“Tera Pilot: Connect to bridge” from the command palette.

## What flows where

- VS Code → agent: `provide_context` (selection, open files) via the
  “Send selection as context” command. The TUI surfaces it with
  `/bridge context`.
- Agent → VS Code: `request_approval` renders as a modal Allow/Deny
  dialog; `show_diff` opens the built-in diff viewer; `open_file`
  jumps to a file; `notify` is a toast.

Security: localhost TCP only, bearer token on every request. The bridge
never executes IDE input — context is text the agent may read, approvals
resolve prompts the agent created.
