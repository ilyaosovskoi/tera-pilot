#!/usr/bin/env python3
"""Insert an API key into ~/.tera_pilot/config.json safely.

Usage:
    python3 set_key.py gemini AIza...            # set key + activate provider
    python3 set_key.py groq  gsk_...             # another provider
    python3 set_key.py gemini                    # prompts for the key

Backs up the existing config to config.json.bak before writing, and saves
atomically (same as the app does). Only touches the given provider's
api_key field and the top-level active_provider.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

CONFIG = Path.home() / ".tera_pilot" / "config.json"

VALID = {
    "gemini", "openai", "anthropic", "openrouter", "groq", "deepseek",
    "mistral", "together", "fireworks", "xai", "cerebras", "sambanova",
    "zai", "ollama", "lmstudio",
}


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python3 set_key.py <provider> [api_key]")
        print(f"Valid providers: {', '.join(sorted(VALID))}")
        return 1

    provider = sys.argv[1].lower()
    if provider not in VALID:
        print(f"Unknown provider '{provider}'. Valid: {', '.join(sorted(VALID))}")
        return 1

    key = sys.argv[2] if len(sys.argv) > 2 else None
    if not key:
        key = input(f"Paste {provider} API key: ").strip()
    if not key:
        print("No key provided — aborting.")
        return 1

    # Load existing config (or start fresh)
    if CONFIG.exists():
        try:
            cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print(f"Warning: could not read existing config ({e}) — starting fresh.")
            cfg = {}
    else:
        cfg = {}
        CONFIG.parent.mkdir(parents=True, exist_ok=True)

    # Backup (keeps last backup, not a history)
    try:
        shutil.copy2(CONFIG, CONFIG.with_suffix(".json.bak"))
    except OSError:
        pass

    providers = cfg.setdefault("providers", {})
    entry = providers.setdefault(provider, {})
    entry["api_key"] = key
    if not entry.get("model"):
        # Sensible default per provider (only fills if unset)
        defaults = {
            "gemini": "gemini-2.5-pro",
            "groq": "llama-3.3-70b-versatile",
            "openai": "gpt-4o",
            "anthropic": "claude-3-5-sonnet-20241022",
            "deepseek": "deepseek-chat",
            "mistral": "mistral-large-latest",
        }
        entry["model"] = defaults.get(provider, entry.get("model", ""))
    cfg["active_provider"] = provider

    # Atomic write (temp + replace), same as the app
    tmp = CONFIG.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    tmp.replace(CONFIG)

    print(f"✅ Key saved for '{provider}' → {CONFIG}")
    print(f"   active_provider: {cfg['active_provider']}")
    print("Now run a real test:")
    print('   python3 e2e_agent_test.py "your task" --workspace /path/to/project')
    return 0


if __name__ == "__main__":
    sys.exit(main())
