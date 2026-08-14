"""
Path / config / chat-store helpers for the web bridge.

Everything that reads or writes to ~/.tera_pilot/ lives here:
- _tera_pilot_home(), _config_path(), _chats_dir()
- _load_templates_from_disk(), _load_skills_from_disk()
- _classify_user_intent() (used by the composer mixin to decide
  whether to route through the agent or do a one-shot generation)
- _load_config(), _save_config()
- _chat_path(), _load_chat(), _save_chat()

Kept module-level (not on the TeraPilotBridge class) so they can be
imported by other modules (e.g. main_window.py imports
`_load_config` to read provider config without instantiating
the bridge).
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _tera_pilot_home() -> Path:
    """~/.tera_pilot — persists config, chats, skills."""
    p = Path.home() / ".tera_pilot"
    p.mkdir(parents=True, exist_ok=True)
    (p / "chats").mkdir(exist_ok=True)
    (p / "templates").mkdir(exist_ok=True)
    (p / "skills").mkdir(exist_ok=True)
    return p


def _config_path() -> Path:
    return _tera_pilot_home() / "config.json"


def _chats_dir() -> Path:
    return _tera_pilot_home() / "chats"




# ── Disk loaders for Templates & Skills ────────────────────────────

def _load_templates_from_disk() -> List[Dict[str, Any]]:
    tpl_dir = _tera_pilot_home() / "templates"
    if not tpl_dir.exists():
        return []
    templates = []
    for f in sorted(tpl_dir.glob("*.json")):
        try:
            with open(f, "r", encoding="utf-8") as fp:
                templates.append(json.load(fp))
        except Exception as e:
            logger.warning(f"[templates] failed to load {f}: {e}")
    return templates


def _load_skills_from_disk() -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    skill_dir = _tera_pilot_home() / "skills"
    if not skill_dir.exists():
        return [], {}
    skills = []
    skill_texts = {}
    for f in sorted(skill_dir.glob("*.json")):
        try:
            with open(f, "r", encoding="utf-8") as fp:
                data = json.load(fp)
                sid = data.get("id", f.stem)
                skills.append({
                    "id": sid,
                    "tag": data.get("tag", "general"),
                    "name": data.get("name", f.stem),
                    "desc": data.get("desc", ""),
                })
                skill_texts[sid] = data.get("text", data.get("instruction", ""))
        except Exception as e:
            logger.warning(f"[skills] failed to load {f}: {e}")
    return skills, skill_texts


def _classify_user_intent(text: str) -> Dict[str, Any]:
    """DEPRECATED in v1.0.7 — kept only for backward-compat with old
    frontends that still call `classify_intent`. The agent runtime is
    now ALWAYS ON, so this classifier no longer gates anything.

    Original heuristic: chat (question/discussion) vs action (file
    write/command). Returns {intent: 'chat'|'action'|'ambiguous',
    confidence: float, reason: str}.
    """
    text_lower = text.lower().strip()

    # Strong action signals — these almost certainly mean "do something"
    # v1.0.6: expanded Russian list — "запиши" was missing, which caused
    # "Привет. Запиши любой файл в тестовую директорию" to be classified
    # as 'chat' even with Agent Mode explicitly toggled on by the user.
    action_patterns = [
        # Russian imperatives — write/create/save/edit/delete/run/etc.
        r"\b(создай|сделай|напиши|запиши|сохрани|исправь|почини|запусти|удали|переименуй|сгенерируй|добавь|измени|обнови|отредактируй|поменяй|вынеси|перенеси|перемести|скопируй|вставь|замени)\b",
        # Russian noun-form fallbacks ("нужна запись", "требуется создание")
        r"\b(запис[ьи]|создани[ея]|сохранени[ея]|удалени[ея]|переименовани[ея]|обновлени[ея]|редактировани[ея])\b",
        # English imperatives
        r"\b(create|make|write|fix|run|delete|remove|rename|generate|add|change|update|implement|build|refactor|deploy|install|migrate|save|edit|patch|move|copy|insert|replace)\b",
        # English noun-form: "write a file", "create a class"
        r"\b(write|create|save|generate|output|produce)\s+(a\s+)?(file|code|script|test|class|function|module)\b",
        r"\b(fix|patch|resolve|debug|solve)\b",
        r"\b(run|execute|start|launch|test)\b",
        # "запиши файл" / "создай файл" / "save file" — noun right after verb
        r"\b(запиши|сохрани|создай|сгенерируй)\s+(файл|код|скрипт|тест|класс|функци[юю]|модул[ьь])\b",
        r"\b(save|write|create|generate)\s+(a\s+)?(file|script|test|class|function|module)\b",
    ]

    # Strong chat/question signals — these mean "discuss/think"
    chat_patterns = [
        r"^(what|how|why|when|where|who|which|can you|could you|is it|does it)\b",
        r"^(что|как|почему|когда|где|кто|какой|можешь|сможешь|объясни|расскажи|подскажи)\b",
        r"\b(объясни|расскажи|подскажи|что думаешь|как лучше|твоё мнение|что посоветуешь)\b",
        r"\b(think|explain|tell me|what do you think|opinion|suggest|recommend|compare|describe)\b",
        r"\?$",  # ends with question mark
        r"\?\s*$",
    ]

    # Ambiguous / vague signals
    vague_patterns = [
        r"^(напиши что|сделай что|любой|что-нибудь|что-то)\b",
        r"^(write something|do something|anything|whatever)\b",
    ]

    import re
    action_score = sum(1 for p in action_patterns if re.search(p, text_lower, re.IGNORECASE))
    chat_score = sum(1 for p in chat_patterns if re.search(p, text_lower, re.IGNORECASE))
    vague_score = sum(1 for p in vague_patterns if re.search(p, text_lower, re.IGNORECASE))

    # v1.0.6: removed the over-aggressive "short message without action
    # signal → chat" rule. It was firing on perfectly valid short
    # commands like "Запиши файл" (4 words, 1 action signal — but the
    # old rule checked action_score == 0, so the action signal was
    # ignored, and the message was tagged 'chat' anyway). Now we rely
    # on the explicit action/chat score comparison below.

    # Vague/ambiguous → ambiguous (ask for clarification) ONLY when
    # there's no action signal at all. "Запиши любой файл" contains
    # both "запиши" (action) and "любой" (vague) — the action should win.
    if vague_score > 0 and action_score == 0:
        return {"intent": "ambiguous", "confidence": 0.7, "reason": "vague_request_needs_clarification"}

    # Clear action — even a single strong action verb is enough.
    # v1.0.6: lowered the threshold from ">=2 or (>=1 and has_code)"
    # to ">=1". The old threshold meant "запиши файл" (1 signal) was
    # treated as ambiguous, defeating the whole point of the classifier.
    if action_score >= 1:
        return {"intent": "action", "confidence": 0.85, "reason": f"action_signals={action_score}"}

    # Clear chat/question
    if chat_score >= 1 and action_score == 0:
        return {"intent": "chat", "confidence": 0.85, "reason": f"chat_signals={chat_score}"}

    # Mixed signals → ambiguous
    if action_score > 0 and chat_score > 0:
        return {"intent": "ambiguous", "confidence": 0.5, "reason": "mixed_action_and_chat"}

    # Default: ambiguous for medium-length messages without clear signals
    return {"intent": "ambiguous", "confidence": 0.4, "reason": "no_clear_signals"}


# ── Prompt Templates (static library + disk override) ──────────────

_BUILTIN_TEMPLATES: List[Dict[str, Any]] = [
    {
        "id": "code_project",
        "name": "Code Project",
        "desc": "Scaffold a new project — files, structure, dependencies, tests.",
        "sections": ["intent", "stack", "structure", "tests", "docs"],
    },
    {
        "id": "refactor",
        "name": "Refactor",
        "desc": "Reorganize existing code with preserved behaviour.",
        "sections": ["scope", "before", "after", "verify"],
    },
    {
        "id": "feature_spec",
        "name": "Feature Spec",
        "desc": "Define a feature: users, flows, edges, acceptance criteria.",
        "sections": ["users", "flows", "edges", "acceptance"],
    },
    {
        "id": "bug_fix",
        "name": "Bug Fix",
        "desc": "Reproduce, diagnose, patch, regression-test.",
        "sections": ["repro", "diagnose", "patch", "regression"],
    },
    {
        "id": "documentation",
        "name": "Documentation",
        "desc": "Generate README, API reference, architecture notes.",
        "sections": ["overview", "install", "usage", "api"],
    },
    {
        "id": "research",
        "name": "Research",
        "desc": "Investigate a topic, summarize findings, propose next steps.",
        "sections": ["question", "sources", "findings", "next"],
    },
]

_DISK_TEMPLATES = _load_templates_from_disk()

# Merge: disk overrides built-in by id
PROMPT_TEMPLATES: List[Dict[str, Any]] = []
_seen_tpl_ids = set()
for t in _DISK_TEMPLATES + _BUILTIN_TEMPLATES:
    if t["id"] not in _seen_tpl_ids:
        PROMPT_TEMPLATES.append(t)
        _seen_tpl_ids.add(t["id"])


# ── Skill catalog (each sharpens the model on one capability) ──────

_BUILTIN_SKILLS: List[Dict[str, Any]] = [
    {"id": "python_architect", "tag": "architect", "name": "Python Architect",
     "desc": "Designs clean package structures, dependency boundaries, layered architecture."},
    {"id": "ui_polish", "tag": "frontend", "name": "UI Polish",
     "desc": "Pixel-perfect CSS, motion systems, accessibility, responsive behavior."},
    {"id": "security_auditor", "tag": "security", "name": "Security Auditor",
     "desc": "Threat models, OWASP, secrets hygiene, sandboxing, least privilege."},
    {"id": "performance", "tag": "perf", "name": "Performance",
     "desc": "Profiles bottlenecks, optimizes hot paths, measures with benchmarks."},
    {"id": "test_engineer", "tag": "testing", "name": "Test Engineer",
     "desc": "Property tests, fuzzing, fixtures, coverage of edge cases."},
    {"id": "data_engineer", "tag": "data", "name": "Data Engineer",
     "desc": "Schemas, migrations, idempotent pipelines, observability."},
    {"id": "devops", "tag": "devops", "name": "DevOps",
     "desc": "CI/CD, IaC, containers, blue-green deploys, incident response."},
    {"id": "tech_writer", "tag": "docs", "name": "Tech Writer",
     "desc": "Clear prose, diagrams, examples that compile, audience awareness."},
]

_BUILTIN_SKILL_TEXTS: Dict[str, str] = {
    "python_architect": (
        "# SKILL: Python Architect\n\n"
        "You design clean, layered Python projects.\n"
        "Rules:\n"
        "- Separate concerns: routers -> services -> repositories -> models.\n"
        "- No business logic in route handlers.\n"
        "- Type-hint every public function; run mypy --strict in your head.\n"
        "- Prefer composition over inheritance.\n"
        "- Every module has a one-line docstring stating its responsibility.\n"
        "- If a file exceeds 300 lines, propose splitting it.\n"
    ),
    "ui_polish": (
        "# SKILL: UI Polish\n\n"
        "You produce pixel-perfect frontends.\n"
        "Rules:\n"
        "- Respect a design system: spacing scale, type scale, motion tokens.\n"
        "- Never use pure black or pure white.\n"
        "- All animations use cubic-bezier easing, never linear for organic motion.\n"
        "- Test keyboard navigation and screen-reader labels.\n"
        "- Mobile-first: layout works at 375px before 1440px.\n"
    ),
    "security_auditor": (
        "# SKILL: Security Auditor\n\n"
        "You review code for security issues.\n"
        "Rules:\n"
        "- Treat all input as hostile until proven otherwise.\n"
        "- Check OWASP Top 10 by default.\n"
        "- Never log secrets, tokens, or PII.\n"
        "- Prefer parameterized queries; reject string-built SQL.\n"
        "- Sandbox subprocess calls; whitelist binaries.\n"
    ),
    "performance": (
        "# SKILL: Performance\n\n"
        "You find and fix performance bottlenecks.\n"
        "Rules:\n"
        "- Measure before optimizing — profile, don't guess.\n"
        "- Hot paths deserve careful data structures, not premature abstraction.\n"
        "- Cache only when the cost of invalidation is lower than the cost of recomputation.\n"
        "- Vectorize numerical loops.\n"
        "- Bound concurrency: every queue needs a limit.\n"
    ),
    "test_engineer": (
        "# SKILL: Test Engineer\n\n"
        "You design test suites that catch real bugs.\n"
        "Rules:\n"
        "- Test behaviour, not implementation.\n"
        "- Cover happy path, edge cases, and failure modes.\n"
        "- One assertion concept per test.\n"
        "- Fixtures should be minimal and composable.\n"
        "- Property tests over example tests where possible.\n"
    ),
    "data_engineer": (
        "# SKILL: Data Engineer\n\n"
        "You build reliable data pipelines.\n"
        "Rules:\n"
        "- Every pipeline is idempotent — re-running is safe.\n"
        "- Schema changes are backward-compatible migrations, never in-place edits.\n"
        "- Emit structured logs and metrics at every stage.\n"
        "- Backfill before deploying schema changes.\n"
    ),
    "devops": (
        "# SKILL: DevOps\n\n"
        "You ship reliable infrastructure.\n"
        "Rules:\n"
        "- All infrastructure is code, version-controlled.\n"
        "- Deploys are blue-green or canary; never big-bang.\n"
        "- Every service has a health check and a readiness probe.\n"
        "- Alerts are actionable; no alert fatigue.\n"
    ),
    "tech_writer": (
        "# SKILL: Tech Writer\n\n"
        "You write clear technical documentation.\n"
        "Rules:\n"
        "- Every example must compile and run.\n"
        "- Audience-aware: distinguish beginner / intermediate / expert sections.\n"
        "- Lead with the outcome, then the steps.\n"
        "- Diagrams explain what prose cannot.\n"
    ),
}

_DISK_SKILLS, _DISK_SKILL_TEXTS = _load_skills_from_disk()

# Merge: disk overrides built-in by id
SKILLS: List[Dict[str, Any]] = []
_seen_skill_ids = set()
for s in _DISK_SKILLS + _BUILTIN_SKILLS:
    if s["id"] not in _seen_skill_ids:
        SKILLS.append(s)
        _seen_skill_ids.add(s["id"])

_SKILL_TEXTS: Dict[str, str] = {**_BUILTIN_SKILL_TEXTS, **_DISK_SKILL_TEXTS}


# ── Default per-provider config ────────────────────────────────────

_PROVIDER_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "openrouter": {
        "model": "anthropic/claude-3.5-sonnet",
        "api_key": "",
        "api_base": "",
        "temperature": 0.2,
        "max_tokens": 4096,
    },
    "groq": {
        "model": "llama-3.3-70b-versatile",
        "api_key": "",
        "api_base": "",
        "temperature": 0.2,
        "max_tokens": 4096,
    },
    "openai": {
        "model": "gpt-4o",
        "api_key": "",
        "api_base": "",
        "temperature": 0.2,
        "max_tokens": 4096,
    },
    "anthropic": {
        "model": "claude-3-5-sonnet-20241022",
        "api_key": "",
        "api_base": "",
        "temperature": 0.2,
        "max_tokens": 4096,
    },
    "deepseek": {
        "model": "deepseek-chat",
        "api_key": "",
        "api_base": "",
        "temperature": 0.2,
        "max_tokens": 4096,
    },
    "zai": {
        "model": "glm-4-plus",
        "api_key": "",
        "api_base": "",
        "temperature": 0.2,
        "max_tokens": 4096,
    },
    "gemini": {
        "model": "gemini-2.5-pro",
        "api_key": "",
        "api_base": "",
        "temperature": 0.2,
        "max_tokens": 4096,
    },
    "mistral": {
        "model": "mistral-large-latest",
        "api_key": "",
        "api_base": "",
        "temperature": 0.2,
        "max_tokens": 4096,
    },
    "together": {
        "model": "meta-llama/Llama-3-70b-chat-hf",
        "api_key": "",
        "api_base": "",
        "temperature": 0.2,
        "max_tokens": 4096,
    },
    "fireworks": {
        "model": "accounts/fireworks/models/llama-v3p1-70b-instruct",
        "api_key": "",
        "api_base": "",
        "temperature": 0.2,
        "max_tokens": 4096,
    },
    "xai": {
        "model": "grok-2",
        "api_key": "",
        "api_base": "",
        "temperature": 0.2,
        "max_tokens": 4096,
    },
    "cerebras": {
        "model": "llama-3.3-70b",
        "api_key": "",
        "api_base": "",
        "temperature": 0.2,
        "max_tokens": 4096,
    },
    "sambanova": {
        "model": "Meta-Llama-3.3-70B-Instruct",
        "api_key": "",
        "api_base": "",
        "temperature": 0.2,
        "max_tokens": 4096,
    },
    "ollama": {
        "model": "llama3.1",
        "api_key": "",
        "api_base": "http://localhost:11434/v1",
        "temperature": 0.2,
        "max_tokens": 4096,
    },
    "lmstudio": {
        "model": "",
        "api_key": "",
        "api_base": "http://localhost:1234/v1",
        "temperature": 0.2,
        "max_tokens": 4096,
    },
}


# ── Config persistence ─────────────────────────────────────────────

_DEFAULT_CONFIG: Dict[str, Any] = {
    "version": 2,  # M8: aligned with api_server config schema
    "active_provider": "ollama",
    "providers": _PROVIDER_DEFAULTS,
    "ui": {
        "theme": "dark",
        "sidebar_collapsed": False,
        "code_viewer_width": "normal",   # "normal" | "wide" | "closed"
        "budget_usd": 20.0,               # monthly spend budget shown in Usage
        "text_size": "medium",            # "small" | "medium" | "large"
    },
    "active_chat_id": None,
    "project_root": None,
    "close_on_switch": False,  # v2.0.0-tui: close GUI/TUI window after switching
}


def _load_config() -> Dict[str, Any]:
    """Load ~/.tera_pilot/config.json, merging with defaults for any missing keys."""
    path = _config_path()
    if not path.exists():
        return json.loads(json.dumps(_DEFAULT_CONFIG))  # deep copy

    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"[config] failed to load, using defaults: {e}")
        return json.loads(json.dumps(_DEFAULT_CONFIG))

    # Merge with defaults (one level deep)
    merged = json.loads(json.dumps(_DEFAULT_CONFIG))
    for k, v in cfg.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = {**merged[k], **v}
        else:
            merged[k] = v
    # Ensure every provider has a config
    for pid in _PROVIDER_DEFAULTS:
        if pid not in merged["providers"]:
            merged["providers"][pid] = dict(_PROVIDER_DEFAULTS[pid])
    return merged


def _save_config(cfg: Dict[str, Any]) -> None:
    """Persist config to ~/.tera_pilot/config.json."""
    path = _config_path()
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except OSError as e:
        logger.error(f"[config] failed to save: {e}")


# ── Chat history persistence ───────────────────────────────────────

def _chat_path(chat_id: str) -> Path:
    return _chats_dir() / f"{chat_id}.json"


def _load_chat(chat_id: str) -> Optional[Dict[str, Any]]:
    path = _chat_path(chat_id)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"[chat] failed to load {chat_id}: {e}")
        return None


def _save_chat(chat: Dict[str, Any]) -> None:
    path = _chat_path(chat["id"])
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(chat, f, indent=2, ensure_ascii=False)
    except OSError as e:
        logger.error(f"[chat] failed to save {chat['id']}: {e}")


# ── Worker thread for streaming generation ─────────────────────────

