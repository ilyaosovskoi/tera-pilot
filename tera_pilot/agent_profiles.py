"""Agent profiles — named agent personas with per-profile system prompt and security level.

An *agent profile* is a named, persisted description of "which agent am I
talking to today": a role (video maker, coder, custom…), an optional
system-prompt fragment injected into every run, and a *security level*
that maps onto the existing autonomy + Guardian controls.

Profiles are stored as JSON files under ``~/.tera_pilot/agent-profiles/``
(one file per profile) so they survive restarts, are trivially
hand-editable, and can be shared between the TUI, the Web UI and the
daemon. The ACTIVE profile id is stored in ``~/.tera_pilot/config.json``
under ``active_agent_profile`` so a restart picks the same agent.

Built-in presets (always available, cannot be deleted):
  - ``code``     — the default coding agent (no prompt override; the
                   stock section prompt applies; balanced security).
  - ``video``    — video production agent (script, storyboard, shot
                   lists, ffmpeg/office tooling guidance; stricter
                   security: commands need approval).
  - ``reviewer`` — read-only review agent (no writes; strict security).
  - ``apex``     — top-tier general assistant persona (the strongest
                   general agent; strict security).

A profile's ``security`` field is one of:
  - ``controlled`` — autonomy=always_ask, guardian=dangerous_only
  - ``balanced``   — autonomy=new_files_only, guardian=dangerous_only
  - ``free``       — autonomy=new_files_only, guardian=off

The zero value / empty profile id means "no profile" — the stock
behavior (section prompt, autonomy=always_ask) is preserved.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

PROFILES_DIR_NAME = "agent-profiles"
ACTIVE_PROFILE_CONFIG_KEY = "active_agent_profile"

VALID_SECURITY = ("controlled", "balanced", "free")

# security level → (autonomy, guardian level)
SECURITY_MAP = {
    "controlled": ("always_ask", "dangerous_only"),
    "balanced": ("new_files_only", "dangerous_only"),
    "free": ("new_files_only", "off"),
}

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

MAX_SYSTEM_PROMPT_CHARS = 8000

# ── Apex persona ───────────────────────────────────────────────────────
# A top-tier general assistant persona — the strongest general-purpose
# agent, with the tone, behaviour and safety rules that matter for
# agentic coding work.
APEX_SYSTEM_PROMPT = (
    "You are Apex, a top-tier general-purpose coding agent running inside "
    "Tera Pilot. You combine frontier-model capability with careful, kind, "
    "honest behaviour.\n\n"
    "Tone and style:\n"
    "- Be warm and direct: treat the user as a capable adult, never talk "
    "down, and avoid negative assumptions about their judgement."
    "- Be concise. Use lists, headers and bold only when the content is "
    "multifaceted enough that they genuinely help; in ordinary exchange "
    "write natural prose. Minimum formatting needed for clarity."
    "- Own your mistakes, fix them, and stay on the problem. Acknowledge "
    "errors without excessive apology or self-critique; keep your "
    "self-respect even when the user is blunt."
    "- Push back honestly when a request is a bad idea, but constructively "
    "and without lecturing. Give the reasoning, not just the refusal."
    "- Don't ask more than one question per response, and answer ambiguous "
    "queries as best you can before asking for clarification.\n\n"
    "Refusals and safety:\n"
    "- Never write or explain malicious code (malware, exploits, "
    "ransomware, viruses) — even framed as education. Do not assist with "
    "harmful-substance or weapon synthesis details. Decline firmly and "
    "briefly, state the principle rather than the mechanics, and don't "
    "suggest workarounds."
    "- Be extremely cautious with anything involving minors; never create "
    "romantic or sexual content involving or directed at them."
    "- For financial or legal questions give the facts the user needs to "
    "decide for themselves; note you are not a lawyer or financial "
    "advisor and don't overclaim confidence.\n\n"
    "Working as a coding agent:\n"
    "- Treat the repository as ground truth: read before claiming, verify "
    "before asserting, and never assume a file is present just because it "
    "was mentioned."
    "- Prefer concrete, reviewable artifacts — outline, plan, code, diff, "
    "test — over vague summaries. Run the relevant checks/tests before "
    "declaring a change done."
    "- Your reliable knowledge cutoff is around January 2026; for current "
    "facts, use the available tools rather than guessing."
)


# Built-in presets. ``code`` is the stock behavior (no prompt override).
PRESET_PROFILES: List[Dict[str, Any]] = [
    {
        "id": "code",
        "name": "Code Agent",
        "description": "Stock Tera Pilot coding agent (no prompt override).",
        "builtin": True,
        "section": "heavy_code",
        "security": "balanced",
        "system_prompt": "",
    },
    {
        "id": "video",
        "name": "Video Agent",
        "description": "Video production agent: scripts, storyboards, shot lists, ffmpeg.",
        "builtin": True,
        "section": "general",
        "security": "controlled",
        "system_prompt": (
            "You are Tera Pilot running as a VIDEO PRODUCTION agent. Your domain is "
            "video creation: scripts, storyboards, shot lists, narration, captions, "
            "and rendering pipelines (ffmpeg, image/video asset generation, .docx/.pptx "
            "storyboards). Prefer concrete, reviewable artifacts: outline → script → "
            "shot list → render commands. When a task needs rendering, propose the "
            "exact ffmpeg/command pipeline before running it. Coding is secondary — "
            "only write code when it serves the video pipeline."
        ),
    },
    {
        "id": "reviewer",
        "name": "Review Agent",
        "description": "Read-only review agent: analysis and review, no writes.",
        "builtin": True,
        "section": "general",
        "security": "controlled",
        "system_prompt": (
            "You are Tera Pilot running as a READ-ONLY REVIEW agent. Your job is to "
            "analyze, review, and report — never to modify the repository. Do not "
            "write, edit, delete, or rename files; do not create commits. Produce "
            "findings, diffs-as-suggestions, and recommendations as output instead."
        ),
    },
    {
        "id": "apex",
        "name": "Apex",
        "description": (
            "Top-tier general assistant persona — the strongest general "
            "agent (strict security)."
        ),
        "builtin": True,
        "section": "general",
        "security": "controlled",
        "system_prompt": APEX_SYSTEM_PROMPT,
    },
]


def _profiles_dir() -> Path:
    from tera_pilot.utils import get_tera_pilot_dir

    return get_tera_pilot_dir() / PROFILES_DIR_NAME


def _validate_id(profile_id: str) -> bool:
    return bool(_ID_RE.match(profile_id or ""))


def _validate_security(security: str) -> str:
    return security if security in VALID_SECURITY else "controlled"


class AgentProfileManager:
    """Registry of named agent profiles backed by JSON files on disk.

    Thread-safe. Missing/corrupt profile files are skipped (never fatal —
    a broken profile must not break the TUI). Builtin presets are always
    available and cannot be deleted or overwritten by user files.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ disk
    def _profile_path(self, profile_id: str) -> Path:
        return _profiles_dir() / f"{profile_id}.json"

    def _read_profile_file(self, path: Path) -> Optional[Dict[str, Any]]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or not data.get("id"):
                return None
            return data
        except Exception as e:
            logger.warning("[agent-profiles] failed to read %s: %s", path, e)
            return None

    def _write_profile_file(self, profile: Dict[str, Any]) -> None:
        d = _profiles_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = self._profile_path(profile["id"])
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(profile, f, indent=2)
            f.flush()
        import os

        os.replace(tmp, path)

    # ------------------------------------------------------------------ API
    def list_profiles(self) -> List[Dict[str, Any]]:
        """All profiles: builtins first, then user profiles (sorted by id)."""
        with self._lock:
            out: List[Dict[str, Any]] = []
            seen = set()
            for preset in PRESET_PROFILES:
                # A user file may override a builtin's editable fields
                # (description/system_prompt/security) but keeps its id.
                user = self._read_profile_file(self._profile_path(preset["id"]))
                merged = dict(preset)
                if user is not None:
                    for k in ("description", "system_prompt", "security", "name"):
                        if user.get(k):
                            merged[k] = user[k]
                    merged["modified"] = True
                out.append(merged)
                seen.add(preset["id"])
            d = _profiles_dir()
            if d.is_dir():
                for path in sorted(d.glob("*.json")):
                    data = self._read_profile_file(path)
                    if data is None:
                        continue
                    pid = str(data.get("id"))
                    if pid in seen or not _validate_id(pid):
                        continue
                    data["builtin"] = False
                    out.append(data)
                    seen.add(pid)
            return out

    def get_profile(self, profile_id: str) -> Optional[Dict[str, Any]]:
        for p in self.list_profiles():
            if p["id"] == profile_id:
                return p
        return None

    def upsert_profile(
        self,
        profile_id: str,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        system_prompt: Optional[str] = None,
        security: Optional[str] = None,
        section: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create or update a user profile. Returns the stored profile dict."""
        if not _validate_id(profile_id):
            return {"ok": False, "error": f"invalid profile id: {profile_id!r}"}
        builtin = next((p for p in PRESET_PROFILES if p["id"] == profile_id), None)
        with self._lock:
            existing = self._read_profile_file(self._profile_path(profile_id)) or {}
            base = dict(builtin) if builtin else dict(existing)
            profile = {
                "id": profile_id,
                "name": name or base.get("name") or profile_id,
                "description": (
                    description if description is not None
                    else base.get("description", "")
                ),
                "system_prompt": (
                    system_prompt if system_prompt is not None
                    else base.get("system_prompt", "")
                ),
                "security": _validate_security(
                    security if security is not None
                    else base.get("security", "controlled")
                ),
                "section": section or base.get("section", "general"),
                "builtin": bool(builtin),
            }
            sp = profile["system_prompt"]
            if not isinstance(sp, str):
                return {"ok": False, "error": "system_prompt must be a string"}
            if len(sp) > MAX_SYSTEM_PROMPT_CHARS:
                return {
                    "ok": False,
                    "error": f"system_prompt too long ({len(sp)} > {MAX_SYSTEM_PROMPT_CHARS})",
                }
            if profile["section"] not in ("general", "heavy_code", "office"):
                profile["section"] = "general"
            try:
                self._write_profile_file(profile)
            except Exception as e:
                return {"ok": False, "error": str(e)}
            return {"ok": True, "profile": profile}

    def delete_profile(self, profile_id: str) -> Dict[str, Any]:
        if any(p["id"] == profile_id for p in PRESET_PROFILES):
            return {"ok": False, "error": f"profile {profile_id!r} is built-in"}
        with self._lock:
            path = self._profile_path(profile_id)
            if not path.exists():
                return {"ok": False, "error": f"no such profile: {profile_id}"}
            try:
                path.unlink()
            except Exception as e:
                return {"ok": False, "error": str(e)}
        return {"ok": True}


# ── Active profile (persisted in config.json) ───────────────────────────

def get_active_profile_id() -> str:
    """Return the active profile id, or "" for no profile (stock behavior)."""
    try:
        from tera_pilot.utils import load_config

        return str((load_config() or {}).get(ACTIVE_PROFILE_CONFIG_KEY) or "")
    except Exception:
        return ""


def set_active_profile_id(profile_id: str) -> Dict[str, Any]:
    """Persist the active profile id to config.json ("" = no profile)."""
    try:
        from tera_pilot.utils import load_config, save_config

        cfg = load_config()
        if profile_id:
            cfg[ACTIVE_PROFILE_CONFIG_KEY] = profile_id
        else:
            cfg.pop(ACTIVE_PROFILE_CONFIG_KEY, None)
        save_config(cfg)
        return {"ok": True, "active": profile_id}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Singleton ───────────────────────────────────────────────────────────

_MANAGER: Optional["AgentProfileManager"] = None
_MANAGER_LOCK = threading.Lock()


def get_agent_profile_manager() -> "AgentProfileManager":
    """Return the process-wide AgentProfileManager singleton."""
    global _MANAGER
    if _MANAGER is None:
        with _MANAGER_LOCK:
            if _MANAGER is None:
                _MANAGER = AgentProfileManager()
    return _MANAGER


def get_active_profile() -> Optional[Dict[str, Any]]:
    """Return the resolved active profile, or None for stock behavior."""
    pid = get_active_profile_id()
    if not pid:
        return None
    return get_agent_profile_manager().get_profile(pid)


def apply_profile_to_runtime(runtime: Any, profile: Dict[str, Any]) -> None:
    """Apply a profile to a running AgentRuntime: the system-prompt
    fragment (persona) + the security mapping (autonomy + Guardian).

    The fragment is applied via ``set_system_prompt_fragment`` (see
    runtime injection); the security mapping via set_autonomy /
    set_guardian_level.
    """
    fragment = (profile.get("system_prompt") or "").strip() or None
    setter = getattr(runtime, "set_system_prompt_fragment", None)
    if setter is not None:
        try:
            setter(fragment)
        except Exception:
            pass
    autonomy, guardian = SECURITY_MAP.get(
        _validate_security(str(profile.get("security", "controlled"))),
        ("always_ask", "dangerous_only"),
    )
    runtime.set_autonomy(autonomy)
    # Apply the Guardian level. AgentRuntime has no set_guardian_level
    # method — the level lives on the ToolEngine's _guardian_config (the
    # TUI bridge sets it the same way). Set it via the tools object,
    # preserving any provider/model settings already configured.
    try:
        tools = getattr(runtime, "tools", None)
        if tools is not None:
            from tera_pilot.agent.guardian import GuardianConfig
            old = getattr(tools, "_guardian_config", None)
            if old is not None and getattr(old, "level", None) == guardian:
                pass  # already at this level
            else:
                tools._guardian_config = GuardianConfig(
                    level=guardian,
                    provider_id=(getattr(old, "provider_id", None) if old else None),
                    model=(getattr(old, "model", None) if old else None),
                )
    except Exception:
        pass
