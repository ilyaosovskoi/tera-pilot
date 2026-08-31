"""key_cli.py — `tera-pilot key`: convenient API-key setup.

Saves keys to ~/.tera_pilot/config.json (same atomic writer the app
uses) and prints masked confirmation. Keys are never echoed back.

Usage:
    tera-pilot key                      interactive: pick provider, paste key
    tera-pilot key list                 table of providers + key presence (masked)
    tera-pilot key set <provider>       prompts for the key (hidden input)
    tera-pilot key set <provider> <key> saves directly
    tera-pilot key remove <provider>    removes the stored key
"""

from __future__ import annotations

import getpass
import sys
from typing import Any, Dict, List, Optional


def _mask(key: str) -> str:
    if not key:
        return ""
    return (key[:4] + "…" + key[-4:]) if len(key) > 8 else "••••"


def _load_config() -> Dict[str, Any]:
    from tera_pilot.utils import load_config

    return load_config() or {}


def _save_config(cfg: Dict[str, Any]) -> None:
    from tera_pilot.utils import save_config

    save_config(cfg)


def _provider_ids() -> List[str]:
    """All known provider ids: registry providers + anything in config."""
    ids: List[str] = []
    try:
        from tera_pilot.providers import get_registry

        reg = get_registry()
        try:
            if not reg.list_providers():
                reg.register_default()
        except Exception:
            reg.register_default()
        ids = [str(p.get("id")) for p in reg.list_providers()]
    except Exception:
        pass
    cfg = _load_config()
    for pid in (cfg.get("providers") or {}):
        if pid not in ids:
            ids.append(str(pid))
    return sorted(ids)


def _config_status() -> List[Dict[str, Any]]:
    cfg = _load_config()
    providers = cfg.get("providers") or {}
    active = cfg.get("active_provider", "")
    out = []
    for pid in _provider_ids():
        key = (providers.get(pid) or {}).get("api_key") or ""
        model = (providers.get(pid) or {}).get("model") or ""
        out.append({
            "provider": pid,
            "set": bool(key),
            "masked": _mask(key),
            "model": model,
            "active": pid == active,
        })
    return out


def cmd_list() -> int:
    status = _config_status()
    if not status:
        print("No providers found. Run: tera-pilot key  (interactive setup)")
        return 0
    print(f"{'Provider':<14} {'Key':<12} {'Model':<24} Active")
    print("-" * 60)
    for s in status:
        key = s["masked"] if s["set"] else "(not set)"
        act = "●" if s["active"] else ""
        print(f"{s['provider']:<14} {key:<12} {s['model']:<24} {act}")
    return 0


def _apply_to_registry(provider_id: str, api_key: str, model: Optional[str]) -> None:
    """Reconfigure the live registry so the running app picks the key up."""
    try:
        from tera_pilot.providers import get_registry, ProviderConfig

        reg = get_registry()
        existing = None
        try:
            existing = reg.get(provider_id).config
        except Exception:
            pass
        reg.configure(
            provider_id,
            ProviderConfig(
                provider_id=provider_id,
                model=model or (existing.model if existing else ""),
                api_key=api_key,
                api_base=existing.api_base if existing else None,
                temperature=float(getattr(existing, "temperature", 0.2) or 0.2),
                max_tokens=int(getattr(existing, "max_tokens", 4096) or 4096),
            ),
        )
        reg.set_active(provider_id)
    except Exception:
        pass


def cmd_set(provider_id: str, key: Optional[str], model: Optional[str] = None) -> int:
    provider_id = (provider_id or "").strip().lower()
    if not provider_id:
        print("Usage: tera-pilot key set <provider> [api_key]")
        return 1
    if provider_id not in _provider_ids():
        known = ", ".join(_provider_ids()) or "(none — check tera-pilot doctor)"
        print(f"Unknown provider '{provider_id}'. Known: {known}")
        return 1
    if not key:
        key = getpass.getpass(f"Paste {provider_id} API key (hidden): ").strip()
    if not key:
        print("No key provided — aborting.")
        return 1

    cfg = _load_config()
    providers = cfg.setdefault("providers", {})
    entry = providers.setdefault(provider_id, {})
    entry["api_key"] = key
    if model:
        entry["model"] = model
    elif not entry.get("model"):
        # Sensible default per provider (only fills if unset)
        defaults = {
            "gemini": "gemini-2.5-pro",
            "groq": "llama-3.3-70b-versatile",
            "openai": "gpt-4o",
            "anthropic": "claude-sonnet-4-6",
            "deepseek": "deepseek-chat",
            "openrouter": "openrouter/auto",
        }
        entry["model"] = defaults.get(provider_id, "")
    cfg["active_provider"] = provider_id
    _save_config(cfg)
    _apply_to_registry(provider_id, key, entry.get("model"))
    print(f"✅ Key saved for '{provider_id}' → ~/.tera_pilot/config.json ({_mask(key)})")
    print(f"   active_provider: {cfg['active_provider']}  model: {entry.get('model', '')}")
    return 0


def cmd_remove(provider_id: str) -> int:
    provider_id = (provider_id or "").strip().lower()
    if not provider_id:
        print("Usage: tera-pilot key remove <provider>")
        return 1
    cfg = _load_config()
    providers = cfg.get("providers") or {}
    if provider_id not in providers or not (providers.get(provider_id) or {}).get("api_key"):
        print(f"No key stored for '{provider_id}'.")
        return 0
    providers[provider_id]["api_key"] = ""
    _save_config(cfg)
    print(f"🗑  Key removed for '{provider_id}'.")
    return 0


def cmd_interactive() -> int:
    status = _config_status()
    if not status:
        print("No providers available. Check: tera-pilot doctor")
        return 1
    print("Providers:")
    for i, s in enumerate(status, 1):
        key = s["masked"] if s["set"] else "(not set)"
        print(f"  {i}. {s['provider']:<14} {key}")
    try:
        choice = input("\nPick a number (or Enter to quit): ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return 0
    if not choice:
        return 0
    try:
        idx = int(choice) - 1
        provider = status[idx]["provider"]
    except (ValueError, IndexError):
        print(f"Invalid choice: {choice}")
        return 1
    key = getpass.getpass(f"Paste {provider} API key (hidden): ").strip()
    if not key:
        print("No key provided — aborting.")
        return 1
    return cmd_set(provider, key)


def run_key_cli(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[2:] if argv is None else argv)
    if not args:
        return cmd_interactive()
    sub = args[0].lower()
    rest = args[1:]
    if sub in ("list", "ls", "status", "--list"):
        return cmd_list()
    if sub in ("set", "add", "--set"):
        provider = rest[0] if rest else ""
        key = rest[1] if len(rest) > 1 else None
        model = rest[2] if len(rest) > 2 else None
        return cmd_set(provider, key, model)
    if sub in ("remove", "rm", "del", "delete", "--remove"):
        provider = rest[0] if rest else ""
        return cmd_remove(provider)
    print(f"Unknown subcommand: {sub}")
    print("Usage: tera-pilot key [list|set <provider> [key]|remove <provider>]")
    return 1


if __name__ == "__main__":
    raise SystemExit(run_key_cli())
