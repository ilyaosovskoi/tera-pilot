"""
Tera Pilot API Server — Local HTTP server for model communication.

Runs alongside the PyWebView window. The HTML frontend talks to this
server via fetch() for all model/chat operations. Streaming responses
use Server-Sent Events (SSE).

Architecture:
    Browser (index.html)
        │  fetch('http://localhost:PORT/api/...')
        ▼
    TeraPilotAPIServer  (localhost, auto-assigned port)
        │
        ├── ProviderRegistry → OpenAI / Anthropic / Groq / DeepSeek / ...
        ├── Chat persistence   → ~/.tera_pilot/chats/<id>.json
        ├── Config persistence  → ~/.tera_pilot/config.json
        └── PluginManager       → ~/.tera_pilot/plugins/*.py

Endpoints:
    GET  /api/status              Provider & app status
    GET  /api/providers           List all providers
    POST /api/providers/activate  Switch active provider
    POST /api/providers/configure Update provider config
    POST /api/providers/test      Ping a provider (SSE stream)
    POST /api/chat/stream         Send message → SSE stream
    POST /api/agent/stream        Agent mode (tool-use) → SSE stream
    POST /api/agent/diff_review   Respond to pending diff review (accept/reject)
    POST /api/chat/oneshot        Single response (enhance, etc.)
    GET  /api/chat/list           List saved chats
    GET  /api/chat/load?id=       Load a chat
    POST /api/chat/create         Create empty chat
    POST /api/chat/rename         Rename a chat
    DELETE /api/chat/delete       Delete a chat
    POST /api/settings            Save settings (partial merge)
    GET  /api/templates           Prompt template library
    GET  /api/skills              Skill catalog
    GET  /api/skills/<id>         Full skill text
    GET  /api/plugins             Loaded plugins (dev)
    GET  /api/plugins/inject      Injected plugin JS/CSS
    GET  /api/activity/recent     v1.2.0 — recent activity log entries
    GET  /api/activity/stats      v1.2.0 — counts by category/status
    GET  /api/activity/export     v1.2.0 — full log as JSON download
    POST /api/activity/clear      v1.2.0 — empty the activity log

v2.2.1 extended endpoints (installed from tera_pilot.api_extended):
    Custom providers:
        GET  /api/providers/custom/list    List user-defined providers
        POST /api/providers/custom/add     Add a custom provider (incl. Nvidia NIM)
        POST /api/providers/custom/update  Update an existing custom provider
        POST /api/providers/custom/remove  Remove a custom provider
        POST /api/providers/custom/test    Test a custom-provider config
        GET  /api/providers/templates      Built-in templates (NIM / Ollama / etc.)
    Capability catalog:
        GET  /api/capabilities/list        Browse capability templates
        GET  /api/capabilities/categories  List categories
        GET  /api/capabilities/get?id=     Get a single capability
        POST /api/capabilities/run         Fill & run a template
    Second Opinion / Verify / Consensus:
        GET/POST /api/second_opinion/config  Get/set cross-model config
        POST     /api/second_opinion/run     Run second-opinion review
        GET      /api/second_opinion/providers
        POST     /api/verify/run             Verify last agent response
        GET/POST /api/consensus/config       Multi-provider consensus config
        POST     /api/consensus/run          Run consensus across providers
    Token budget / Cost router / Spend dashboard:
        GET/POST /api/budget/{get,set,reset,check}
        GET/POST /api/cost/{config,cap,route,apply}
        GET/POST /api/spend/{identity,team,budget,report,sources_*,export_*}
    Hooks / Checkpoints / Handoffs:
        GET/POST /api/hooks/{list,register,remove,toggle,test,stats}
        GET/POST /api/checkpoint/{create,list,get,rewind,rewind_to,diff,auto,stats}
        GET/POST /api/handoff/{create,list,get,block_status,todo_toggle,reorder,delete,revision_prompt,export_md}
    Agents / Audit / Learnings:
        GET  /api/agents/{identity,list}     POST /api/agents/spawn
        GET  /api/audit/{summary,filter,export_json,export_csv,signed_export}
        POST /api/audit/signed_verify
        GET/POST /api/learnings/{list,show,dismiss,restore,scan,dismissed}
    GitHub / MCP server / Notifications / Daemon:
        GET/POST /api/github/{status,set_token,set_repo,detect_repo,list_prs,
                  get_pr,create_pr,pr_context,list_issues,get_issue,
                  create_issue,comment_pr,generate_action}
        GET  /api/mcp_server/{list_tools,status}
        GET/POST /api/notify/{backends,configure,toggle,test,test_all,set_events,
                  status,remove}
        GET/POST /api/daemon/{submit,status}
    Persona / Router / Pro / Collaboration / Persistence / Slash:
        GET/POST /api/persona/{get,set,reset}
        GET/POST /api/router/mode
        GET/POST /api/pro/{status,toggle}
        GET/POST /api/collaboration/{modes,run}
        GET  /api/usage/get
        GET  /api/compaction/stats
        GET/POST /api/persistence/{backend,sessions}
        GET/POST /api/slash_commands/{list,resolve}
        GET/POST /api/section/{get,set}
        GET  /api/websearch/status
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
import uuid
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, parse_qs

from .providers import (
    ProviderRegistry, ProviderConfig, ProviderMessage,
    get_registry, ProviderError,
)
from .agent_runtime import AgentRuntime, TaskType, AgentEvent
from .auto_router import AutoRouter
from .auto_updater import get_current_version
from .memory_service import MemoryService

# GitService is imported lazily inside _get_status() to avoid a top-level
# unused import (it's only needed there).

logger = logging.getLogger(__name__)

# ── Port ───────────────────────────────────────────────────────────

DEFAULT_PORT = 18732


def _find_free_port(start: int = DEFAULT_PORT, host: str = "127.0.0.1") -> int:
    """Find a free port starting from *start* on *host*.

    Probes the actual bind address so ports are checked on the same
    interface the server will use (0.0.0.0 vs 127.0.0.1, etc.).
    """
    import socket
    for port in range(start, start + 500):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((host, port))
                return port
        except OSError:
            continue
    logger.warning("[api_server] no free port in range %d-%d on %s", start, start + 500, host)
    return start


# ── Paths ──────────────────────────────────────────────────────────

def _tera_pilot_home() -> Path:
    p = Path.home() / ".tera_pilot"
    p.mkdir(parents=True, exist_ok=True)
    (p / "chats").mkdir(exist_ok=True)
    (p / "plugins").mkdir(exist_ok=True)
    return p


def _config_path() -> Path:
    return _tera_pilot_home() / "config.json"


def _chats_dir() -> Path:
    return _tera_pilot_home() / "chats"


def _plugins_dir() -> Path:
    return _tera_pilot_home() / "plugins"


# ── Config persistence (shared with web_bridge) ───────────────────

_PROVIDER_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "local":      {"model": "", "api_key": "", "api_base": "", "temperature": 0.2, "max_tokens": 4096},
    "openrouter": {"model": "anthropic/claude-3.5-sonnet", "api_key": "", "api_base": "", "temperature": 0.2, "max_tokens": 4096},
    "groq":       {"model": "llama-3.3-70b-versatile", "api_key": "", "api_base": "", "temperature": 0.2, "max_tokens": 4096},
    "openai":     {"model": "gpt-4o", "api_key": "", "api_base": "", "temperature": 0.2, "max_tokens": 4096},
    "anthropic":  {"model": "claude-3-5-sonnet-20241022", "api_key": "", "api_base": "", "temperature": 0.2, "max_tokens": 4096},
    "deepseek":   {"model": "deepseek-chat", "api_key": "", "api_base": "", "temperature": 0.2, "max_tokens": 4096},
    "zai":        {"model": "glm-4-plus", "api_key": "", "api_base": "", "temperature": 0.2, "max_tokens": 4096},
    "gemini":     {"model": "gemini-2.5-pro", "api_key": "", "api_base": "", "temperature": 0.2, "max_tokens": 4096},
    "mistral":    {"model": "mistral-large-latest", "api_key": "", "api_base": "", "temperature": 0.2, "max_tokens": 4096},
    "together":   {"model": "meta-llama/Llama-3-70b-chat-hf", "api_key": "", "api_base": "", "temperature": 0.2, "max_tokens": 4096},
    # M4: providers that were in bridge but missing from api_server
    "fireworks":  {"model": "accounts/fireworks/models/llama-v3p1-70b-instruct", "api_key": "", "api_base": "", "temperature": 0.2, "max_tokens": 4096},
    "xai":        {"model": "grok-2", "api_key": "", "api_base": "", "temperature": 0.2, "max_tokens": 4096},
    "cerebras":   {"model": "llama-3.3-70b", "api_key": "", "api_base": "", "temperature": 0.2, "max_tokens": 4096},
    "sambanova":  {"model": "Meta-Llama-3.3-70B-Instruct", "api_key": "", "api_base": "", "temperature": 0.2, "max_tokens": 4096},
    "ollama":     {"model": "llama3.1", "api_key": "", "api_base": "http://localhost:11434/v1", "temperature": 0.2, "max_tokens": 4096},
    "lmstudio":   {"model": "", "api_key": "", "api_base": "http://localhost:1234/v1", "temperature": 0.2, "max_tokens": 4096},
}

_DEFAULT_CONFIG: Dict[str, Any] = {
    "version": 2,
    "active_provider": "groq",
    "providers": _PROVIDER_DEFAULTS,
    "ui": {"theme": "dark", "sidebar_collapsed": False, "code_viewer_width": "normal"},
    "active_chat_id": None,
    "project_root": None,
}


def _load_config() -> Dict[str, Any]:
    path = _config_path()
    if not path.exists():
        return json.loads(json.dumps(_DEFAULT_CONFIG))
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("[config] failed to load, using defaults: %s", e)
        return json.loads(json.dumps(_DEFAULT_CONFIG))
    merged = json.loads(json.dumps(_DEFAULT_CONFIG))
    for k, v in cfg.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = {**merged[k], **v}
        else:
            merged[k] = v
    for pid in _PROVIDER_DEFAULTS:
        if pid not in merged["providers"]:
            merged["providers"][pid] = dict(_PROVIDER_DEFAULTS[pid])
    return merged


def _save_config(cfg: Dict[str, Any]) -> None:
    """Persist config to disk atomically (tempfile + os.replace)."""
    path = _config_path()
    data = json.dumps(cfg, indent=2, ensure_ascii=False).encode('utf-8')
    try:
        parent = path.parent
        parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix='.config_', suffix='.tmp', dir=str(parent))
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(data)
            os.replace(tmp_path, path)
        except Exception:
            try: os.unlink(tmp_path)
            except OSError: pass
            raise
    except OSError as e:
        logger.error("[config] failed to save: %s", e)


# ── Chat persistence ───────────────────────────────────────────────

# v1.0.5-security: chat_id must be a bare identifier — never a path.
# Reject anything containing path separators or parent traversal.
_CHAT_ID_RE = re.compile(r'^[A-Za-z0-9_\-]{1,128}$')


def _validate_chat_id(chat_id: str) -> bool:
    """Return True iff *chat_id* is a safe bare identifier (no path traversal)."""
    if not chat_id or not isinstance(chat_id, str):
        return False
    return bool(_CHAT_ID_RE.match(chat_id))


def _chat_path(chat_id: str) -> Path:
    if not _validate_chat_id(chat_id):
        raise ValueError(f"invalid chat_id: {chat_id!r}")
    return _chats_dir() / f"{chat_id}.json"


# Process-wide lock guarding all chat-file load-modify-save cycles.
# Prevents the read-modify-write race documented in BUGS_REPORT H-API-8.
_CHAT_FILE_LOCK = threading.RLock()


def _load_chat(chat_id: str) -> Optional[Dict[str, Any]]:
    if not _validate_chat_id(chat_id):
        return None
    path = _chat_path(chat_id)
    with _CHAT_FILE_LOCK:
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("[chat] failed to load %s: %s", chat_id, e)
            return None


def _save_chat(chat: Dict[str, Any]) -> None:
    chat_id = chat.get("id")
    if not _validate_chat_id(chat_id):
        logger.error("[chat] refused to save with invalid id: %r", chat_id)
        return
    path = _chat_path(chat_id)
    # Atomic write: tempfile in same dir, then os.replace.
    data = json.dumps(chat, indent=2, ensure_ascii=False).encode('utf-8')
    with _CHAT_FILE_LOCK:
        try:
            parent = path.parent
            parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(prefix='.chat_', suffix='.tmp', dir=str(parent))
            try:
                with os.fdopen(fd, 'wb') as f:
                    f.write(data)
                os.replace(tmp_path, path)
            except Exception:
                try: os.unlink(tmp_path)
                except OSError: pass
                raise
        except OSError as e:
            logger.error("[chat] failed to save %s: %s", chat_id, e)


# ── Skills & Templates ────────────────────────────────────────────

PROMPT_TEMPLATES: List[Dict[str, Any]] = [
    {"id": "code_project", "name": "Code Project",
     "desc": "Scaffold a new project.", "sections": ["intent", "stack", "structure", "tests", "docs"]},
    {"id": "refactor", "name": "Refactor",
     "desc": "Reorganize existing code.", "sections": ["scope", "before", "after", "verify"]},
    {"id": "feature_spec", "name": "Feature Spec",
     "desc": "Define a feature.", "sections": ["users", "flows", "edges", "acceptance"]},
    {"id": "bug_fix", "name": "Bug Fix",
     "desc": "Reproduce, diagnose, patch.", "sections": ["repro", "diagnose", "patch", "regression"]},
    {"id": "documentation", "name": "Documentation",
     "desc": "Generate docs.", "sections": ["overview", "install", "usage", "api"]},
    {"id": "research", "name": "Research",
     "desc": "Investigate a topic.", "sections": ["question", "sources", "findings", "next"]},
]

SKILLS: List[Dict[str, Any]] = [
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

# ── System prompt (easily editable by developers) ──────────────────
# Modify TERA_PILOT_SYSTEM_PROMPT below to change Tera Pilot's personality and behavior.
# This is prepended to every chat message automatically.

TERA_PILOT_SYSTEM_PROMPT = """You are Tera Pilot, an AI coding assistant.
You help users write, debug, refactor, and understand code. You are concise, accurate, and practical.

Behavior:
- Write clean, well-structured code with proper error handling.
- When the user provides a project context, use file paths relative to the project root.
- Prefer code examples and concrete solutions over vague explanations.
- If you don't know something, say so honestly.
- Use markdown formatting for code blocks, lists, and emphasis.
"""

_SKILL_TEXTS: Dict[str, str] = {
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


# ── Plugin system ─────────────────────────────────────────────────

class PluginManager:
    """Loads and manages plugins from ~/.tera_pilot/plugins/."""

    def __init__(self):
        self.plugins: list = []
        self._extra_routes: Dict[str, callable] = {}
        self._extra_js: List[str] = []
        self._extra_css: List[str] = []
        self._extra_providers: List[Dict[str, Any]] = []

    def load_all(self, registry: ProviderRegistry) -> None:
        """Scan plugin directory and load all plugins."""
        pdir = _plugins_dir()
        if not pdir.exists():
            pdir.mkdir(parents=True, exist_ok=True)
            return

        for fname in sorted(pdir.iterdir()):
            if fname.suffix != '.py' or fname.name.startswith('_'):
                continue
            try:
                self._load_plugin(fname, registry)
            except Exception as e:
                logger.error("[plugins] failed to load %s: %s", fname.name, e)

    def _load_plugin(self, path: Path, registry: ProviderRegistry) -> None:
        """Import a single plugin file."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(path.stem, str(path))
        if not spec or not spec.loader:
            return
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        # Plugin must expose a register() function
        if not hasattr(mod, 'register'):
            logger.warning("[plugins] %s has no register()", path.name)
            return

        plugin = mod.register()
        if plugin is None:
            return

        name = getattr(plugin, 'name', path.stem)
        version = getattr(plugin, 'version', '0.0.0')
        description = getattr(plugin, 'description', '')

        # Call lifecycle hooks
        app_context = {
            'registry': registry,
            'config': _load_config,
            'save_config': _save_config,
        }
        if hasattr(plugin, 'on_register'):
            try:
                plugin.on_register(app_context)
            except Exception as e:
                logger.error("[plugins] %s on_register failed: %s", name, e)

        if hasattr(plugin, 'register_providers'):
            try:
                plugin.register_providers(registry)
            except Exception as e:
                logger.error("[plugins] %s register_providers failed: %s", name, e)

        if hasattr(plugin, 'register_routes'):
            try:
                routes = plugin.register_routes()
                if isinstance(routes, dict):
                    self._extra_routes.update(routes)
            except Exception as e:
                logger.error("[plugins] %s register_routes failed: %s", name, e)

        if hasattr(plugin, 'inject_js'):
            try:
                js = plugin.inject_js()
                if js:
                    self._extra_js.append(js)
            except Exception:
                pass

        if hasattr(plugin, 'inject_css'):
            try:
                css = plugin.inject_css()
                if css:
                    self._extra_css.append(css)
            except Exception:
                pass

        self.plugins.append({
            'name': name,
            'version': version,
            'description': description,
            'file': path.name,
        })
        logger.info("[plugins] loaded: %s v%s", name, version)

    def get_plugins_info(self) -> List[Dict[str, Any]]:
        return self.plugins

    def get_extra_routes(self) -> Dict[str, callable]:
        return self._extra_routes

    def get_injected_js(self) -> str:
        return '\n'.join(self._extra_js)

    def get_injected_css(self) -> str:
        return '\n'.join(self._extra_css)


# ── Server context (shared state across requests) ─────────────────

class ServerContext:
    """Shared state for the API server."""

    def __init__(self):
        self.registry: ProviderRegistry = get_registry()
        self.config: Dict[str, Any] = _load_config()
        self.plugin_manager = PluginManager()
        # v1.1.4-fix (bug C-API-6): the AutoRouter existed and was wired
        # into web_bridge.py's send_message(), but never into the HTTP
        # chat path — see the stream_thread() comment in
        # _handle_chat_stream() for the full explanation.
        self.router = AutoRouter()
        self._apply_all_provider_configs()
        try:
            self.registry.set_active(self.config.get("active_provider", "groq"))
        except ProviderError:
            self.registry.set_active("groq")
        self.plugin_manager.load_all(self.registry)
        self._stop_event = threading.Event()
        self._agent_runtime: Optional[AgentRuntime] = None
        self._agent_lock = threading.Lock()
        # v1.1.4-fix (bug C-API-5): AgentRuntime/ToolEngine returned by
        # get_agent_runtime() is a single process-wide object reused across
        # every HTTP request (see get_agent_runtime() below). _agent_lock
        # above only guards the brief moment of *creating* that object — it
        # is released long before agent.run() executes. If two
        # /api/agent/stream requests are in flight at once (two browser
        # tabs, or a client retry racing an in-flight request), both
        # reassign the same object's `agent.on_event`,
        # `agent.tools._diff_review_callback`, `agent.max_iterations`,
        # `agent.tools.RUN_TIMEOUT`/`MAX_OUTPUT` for their own request —
        # whichever assigns last "wins" for both requests, so one request's
        # step/thought events and diff-review confirmations can be routed to
        # the other request's SSE stream, and one request's write can be
        # approved/rejected by the other request's diff-review decision.
        # _agent_run_lock is held for the entire duration of agent.run()
        # (see _handle_agent_stream) so only one agent turn executes at a
        # time; a second concurrent request is rejected immediately with a
        # clear error instead of silently corrupting the first one.
        self._agent_run_lock = threading.Lock()
        # v1.0.5-security: lock guarding all config RMW operations
        # (prevents races documented in BUGS_REPORT H-API-9).
        self._config_lock = threading.RLock()
        # v1.0.5-security: bearer token for mutating endpoints.
        # Generated once per process; the pywebview frontend receives it
        # via the QWebChannel bridge and sends it as `Authorization: Bearer <token>`.
        # This blocks CSRF-to-localhost attacks (BUGS_REPORT C-API-1).
        self._auth_token = secrets.token_urlsafe(32)
        # v1.1.1: diff-review state for HTTP path.
        # v1.0.5-security: per-request dict keyed by review_id, so concurrent
        # agent streams no longer share a single Event/Optional[bool]
        # (BUGS_REPORT C-API-2 race condition).
        self._diff_review_lock = threading.Lock()
        self._diff_review_pending: Dict[str, Dict[str, Any]] = {}
        # Map: agent_runtime_id -> review_id (so the POST handler can route
        # the response back to the right waiting agent).
        self._diff_review_by_agent: Dict[int, str] = {}
        # v1.1.1: agent cancellation flag for the HTTP path.
        # reset_agent_cancel() is called at the start of each agent run,
        # cancel_agent() is called by POST /api/agent/stop.
        # The agent loop polls is_agent_cancelled() between iterations
        # and before each tool call.
        self._agent_cancel_event = threading.Event()

    def get_agent_runtime(self, workspace: Optional[str]) -> AgentRuntime:
        """Lazily create (or reuse) the AgentRuntime, pointed at `workspace`.

        Mirrors web_bridge.TeraPilotBridge._get_or_create_agent_runtime — the
        HTTP path needs the same tool-use runtime the QWebChannel bridge
        uses, otherwise Agent Mode over HTTP would be a no-op.
        """
        with self._agent_lock:
            if self._agent_runtime is None:
                self._agent_runtime = AgentRuntime(
                    registry=self.registry,
                    workspace=workspace,
                    max_iterations=int(self.config.get("agent_max_iterations", 8)),
                    enable_planning=bool(self.config.get("agent_enable_planning", True)),
                    memory_persist_path=str(_tera_pilot_home() / "agent_memory.json"),
                )
                # v1.0.5-correctness: wire the token tracker so the agent
                # records real token usage on every provider call (H-RT-3).
                try:
                    from .token_tracker import get_token_tracker
                    self._agent_runtime.set_token_tracker(get_token_tracker())
                except Exception as tok_err:
                    logger.warning("[api] token tracker not wired: %s", tok_err)
                # v1.1.0: wire the quota tracker (per-section daily limits)
                try:
                    from .quota import get_quota_tracker
                    self._agent_runtime.set_quota_tracker(get_quota_tracker())
                except Exception as quota_err:
                    logger.warning("[api] quota tracker not wired: %s", quota_err)
                # v1.1.0: apply run_timeout from config
                if "agent_run_timeout" in self.config:
                    try:
                        self._agent_runtime.tools.RUN_TIMEOUT = int(self.config["agent_run_timeout"])
                    except (TypeError, ValueError):
                        pass
                # v1.1.1: diff-review is now supported over HTTP.
                # The agent thread blocks on ServerContext._diff_review_event;
                # a POST /api/agent/diff_review sets it to unblock the agent.
                self._agent_runtime.tools.diff_review_enabled = self.config.get('diff_review', True)
                logger.info("[api] AgentRuntime created (workspace=%s)", workspace)
            elif workspace:
                self._agent_runtime.set_workspace(workspace)
            return self._agent_runtime

    def _apply_all_provider_configs(self) -> None:
        providers = self.config.get("providers", {})
        for pid, pcfg in providers.items():
            try:
                cfg = ProviderConfig(
                    provider_id=pid,
                    model=pcfg.get("model", ""),
                    api_key=pcfg.get("api_key") or None,
                    api_base=pcfg.get("api_base") or None,
                    temperature=float(pcfg.get("temperature", 0.2)),
                    max_tokens=int(pcfg.get("max_tokens", 4096)),
                )
                self.registry.configure(pid, cfg)
            except Exception as e:
                logger.warning("[api_server] failed to configure %s: %s", pid, e)

    def stop(self) -> None:
        self._stop_event.set()

    # ── v1.1.1: Agent cancellation (HTTP path) ───────────────────
    #
    # The bridge path cancels via AgentWorker.cancel() which sets a
    # _cancelled flag polled by the agent loop. The HTTP path has no
    # AgentWorker — the agent runs in a daemon thread inside
    # _handle_agent_stream. We use a per-context Event that the agent
    # loop polls via set_cancel_check().
    #
    # This is per-context (not per-request) — if two agent streams run
    # concurrently (rare but possible), /api/agent/stop cancels BOTH.
    # That's acceptable for a single-user desktop app.

    def reset_agent_cancel(self) -> None:
        """Clear the cancel flag. Called at the START of each agent run."""
        self._agent_cancel_event.clear()

    def cancel_agent(self) -> None:
        """Set the cancel flag. Called by POST /api/agent/stop."""
        self._agent_cancel_event.set()
        logger.info("[api] agent cancel requested via /api/agent/stop")

    def is_agent_cancelled(self) -> bool:
        """Cancel-check callable passed to agent.set_cancel_check()."""
        return self._agent_cancel_event.is_set()

    # ── Auth helpers (v1.0.5-security) ────────────────────────────

    # Endpoints that mutate state require `Authorization: Bearer <token>`.
    # GET endpoints (status, providers, chat list/load, templates, skills)
    # are public — they only expose data the local user already owns and
    # do not mutate anything.
    MUTATING_PATHS = frozenset({
        '/api/chat/stream', '/api/agent/stream', '/api/chat/oneshot',
        '/api/chat/create', '/api/chat/rename',
        '/api/providers/activate', '/api/providers/configure',
        '/api/providers/test', '/api/settings',
        '/api/agent/diff_review', '/api/chat/delete',
        # v1.1.0: MCP + quota + advanced agent settings
        '/api/mcp/add', '/api/mcp/remove', '/api/mcp/toggle',
        '/api/mcp/start', '/api/mcp/stop', '/api/mcp/start_all',
        '/api/mcp/stop_all', '/api/mcp/reload',
        '/api/quota/set_limit', '/api/quota/clear',
        '/api/agent/advanced_settings',
        # v1.1.1: agent stop (HTTP path)
        '/api/agent/stop',
        # v1.2.0: Activity Stream — clear the log (mutating)
        '/api/activity/clear',
    })

    # NOTE: _check_auth() was previously defined here on ServerContext,
    # but it was called as `self._check_auth(path)` on TeraPilotAPIHandler
    # instances (which do NOT inherit from ServerContext). This caused
    # AttributeError on every POST to a mutating endpoint — silently
    # breaking the HTTP path and forcing the frontend to fall back to
    # the QWebChannel bridge. The method is now defined on
    # TeraPilotAPIHandler below, where `self.headers` and `self.ctx` are
    # both available.


# ── Request handler ────────────────────────────────────────────────

class TeraPilotAPIHandler(BaseHTTPRequestHandler):
    """Handles HTTP requests for the Tera Pilot API."""

    # Use HTTP/1.1 so the connection stays alive during SSE streaming.
    # HTTP/1.0 (the default) closes the connection after the response,
    # which kills the wfile before the background SSE thread can write.
    protocol_version = "HTTP/1.1"

    # Shared across all handler instances (set by server init)
    ctx: ServerContext = None  # type: ignore[assignment]

    # Suppress default stderr logging
    def log_message(self, format, *args):
        logger.debug("[api] %s", format % args)

    # ── Helpers ────────────────────────────────────────────────

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get('Content-Length', 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode('utf-8'))

    def _query(self, key: str) -> str:
        qs = parse_qs(urlparse(self.path).query)
        return qs.get(key, [''])[0]

    # v1.1.1: _check_auth lives HERE on TeraPilotAPIHandler (not on
    # ServerContext). It was previously defined on ServerContext but
    # called as `self._check_auth(path)` on handler instances, which
    # don't inherit from ServerContext — causing AttributeError on
    # every POST to a mutating endpoint. This silently broke the HTTP
    # path; the frontend fell back to the QWebChannel bridge for every
    # agent/chat request.
    def _check_auth(self, path: str) -> bool:
        """Return True iff the request is authorised for *path*.

        - GET / OPTIONS / public paths: always allowed.
        - Mutating paths: require `Authorization: Bearer <token>` matching
          the per-process token.
        """
        if path not in self.ctx.MUTATING_PATHS:
            return True
        auth = self.headers.get('Authorization', '') or ''
        expected = f'Bearer {self.ctx._auth_token}'
        # Constant-time comparison to avoid timing side-channels.
        return secrets.compare_digest(auth, expected)

    def _allowed_origin(self) -> str:
        """Return the CORS origin to allow for this request.

        v1.0.5-security: restrict to localhost origins instead of `*`
        (BUGS_REPORT C-API-1).

        v1.1.3-fix: also allow null/file:// origins — QWebEngineView loads
        the page via file:// which sends Origin: null.  Without this the
        HTTP SSE streaming path fails with CORS and falls back to the
        QWebChannel bridge, whose push-based signal delivery does not work
        in this configuration.
        """
        origin = (self.headers.get('Origin', '') or '').strip()
        allowed_prefixes = (
            'http://localhost', 'http://127.0.0.1',
            'https://localhost', 'https://127.0.0.1',
        )
        if origin and any(origin.startswith(p) for p in allowed_prefixes):
            return origin
        # file:// pages (QWebEngineView) send Origin: null or empty.
        # In a desktop-app context this is safe — no external website can
        # forge a null origin.  Return '*' so the preflight passes.
        if not origin or origin == 'null' or origin.startswith('file://'):
            return '*'
        return 'http://localhost'

    def _json(self, data: Any, code: int = 200) -> None:
        body = json.dumps(data, default=str).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', self._allowed_origin())
        self.send_header('Vary', 'Origin')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse(self, data: dict) -> None:
        """Write one SSE event to the response stream."""
        try:
            line = f"data: {json.dumps(data)}\n\n"
            self.wfile.write(line.encode('utf-8'))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # Client disconnected

    def _cors_preflight(self) -> None:
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', self._allowed_origin())
        self.send_header('Vary', 'Origin')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization, X-Api-Key')
        self.send_header('Connection', 'keep-alive')
        self.end_headers()

    # ── OPTIONS (CORS preflight) ───────────────────────────────

    def do_OPTIONS(self):
        self._cors_preflight()

    # ── GET ────────────────────────────────────────────────────

    def do_GET(self):
        path = urlparse(self.path).path

        # Check plugin routes first
        plugin_routes = self.ctx.plugin_manager.get_extra_routes()
        if path in plugin_routes:
            try:
                result = plugin_routes[path](self)
                if result is not None:
                    self._json(result)
                return
            except Exception as e:
                self._json({'error': str(e)}, 500)
                return

        if path == '/api/status':
            self._json(self._get_status())
        elif path == '/api/providers':
            self._json(self._get_providers())
        elif path == '/api/chat/list':
            self._json(self._list_chats())
        elif path == '/api/chat/load':
            self._json(self._load_chat(self._query('id')))
        elif path == '/api/templates':
            self._json(PROMPT_TEMPLATES)
        elif path == '/api/skills':
            self._json(SKILLS)
        elif path.startswith('/api/skills/'):
            skill_id = path[len('/api/skills/'):]
            text = _SKILL_TEXTS.get(skill_id, '')
            self._json({'id': skill_id, 'text': text})
        elif path == '/api/plugins':
            self._json(self.ctx.plugin_manager.get_plugins_info())
        elif path == '/api/plugins/inject':
            self._json({
                'js': self.ctx.plugin_manager.get_injected_js(),
                'css': self.ctx.plugin_manager.get_injected_css(),
            })
        # v1.1.0: MCP + quota + advanced agent settings
        elif path == '/api/mcp/servers':
            self._json(self._mcp_list_servers())
        elif path == '/api/mcp/status':
            self._json(self._mcp_status())
        elif path == '/api/quota/stats':
            self._json(self._quota_stats())
        elif path == '/api/agent/advanced_settings':
            self._json(self._get_advanced_agent_settings())
        # v1.2.0: Activity Stream — recent activity log entries
        elif path == '/api/activity/recent':
            self._json(self._activity_recent())
        elif path == '/api/activity/stats':
            self._json(self._activity_stats())
        elif path == '/api/activity/export':
            self._activity_export()
        else:
            self._json({'error': 'not found'}, 404)

    # ── POST ───────────────────────────────────────────────────

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._read_json()
        except Exception as e:
            logger.error("[api] failed to read POST body: %s", e)
            self._json({'error': f'Invalid JSON body: {e}'}, 400)
            return

        # v1.0.5-security: bearer-token check on mutating endpoints.
        if not self._check_auth(path):
            logger.warning("[api] unauthorised POST to %s from Origin=%s",
                           path, self.headers.get('Origin', ''))
            self._json({'error': 'unauthorised'}, 401)
            return

        # Check plugin routes
        plugin_routes = self.ctx.plugin_manager.get_extra_routes()
        if path in plugin_routes:
            try:
                result = plugin_routes[path](self, body)
                if result is not None:
                    self._json(result)
                return
            except Exception as e:
                self._json({'error': str(e)}, 500)
                return

        if path == '/api/chat/stream':
            self._handle_chat_stream(body)
        elif path == '/api/agent/stream':
            self._handle_agent_stream(body)
        elif path == '/api/chat/oneshot':
            self._handle_oneshot(body)
        elif path == '/api/chat/create':
            self._json(self._create_chat(body))
        elif path == '/api/chat/rename':
            self._json(self._rename_chat(body))
        elif path == '/api/providers/activate':
            self._json(self._activate_provider(body))
        elif path == '/api/providers/configure':
            self._json(self._configure_provider(body))
        elif path == '/api/providers/test':
            self._handle_test_provider(body)
        elif path == '/api/settings':
            self._json(self._save_settings(body))
        elif path == '/api/agent/diff_review':
            self._json(self._handle_diff_review(body))
        # v1.1.0: MCP + quota + advanced agent settings
        elif path == '/api/mcp/add':
            self._json(self._mcp_add_server(body))
        elif path == '/api/mcp/remove':
            self._json(self._mcp_remove_server(body))
        elif path == '/api/mcp/toggle':
            self._json(self._mcp_toggle_server(body))
        elif path == '/api/mcp/start':
            self._json(self._mcp_start_server(body))
        elif path == '/api/mcp/stop':
            self._json(self._mcp_stop_server(body))
        elif path == '/api/mcp/start_all':
            self._json(self._mcp_start_all())
        elif path == '/api/mcp/stop_all':
            self._json(self._mcp_stop_all())
        elif path == '/api/mcp/reload':
            self._json(self._mcp_reload_config())
        elif path == '/api/quota/set_limit':
            self._json(self._quota_set_limit(body))
        elif path == '/api/quota/clear':
            self._json(self._quota_clear_history())
        elif path == '/api/agent/advanced_settings':
            self._json(self._save_advanced_agent_settings(body))
        # v1.1.1: agent stop (HTTP path) — cancels the running agent
        # stream by setting a flag polled by the agent loop.
        elif path == '/api/agent/stop':
            self._json(self._agent_stop())
        # v1.2.0: Activity Stream — clear the log
        elif path == '/api/activity/clear':
            self._json(self._activity_clear())
        else:
            self._json({'error': 'not found'}, 404)

    # ── DELETE ─────────────────────────────────────────────────

    def do_DELETE(self):
        path = urlparse(self.path).path
        # v1.0.5-security: bearer-token check on mutating endpoints.
        if not self._check_auth(path):
            logger.warning("[api] unauthorised DELETE to %s from Origin=%s",
                           path, self.headers.get('Origin', ''))
            self._json({'error': 'unauthorised'}, 401)
            return
        if path == '/api/chat/delete':
            body = self._read_json()
            self._json(self._delete_chat(body))
        else:
            self._json({'error': 'not found'}, 404)

    # ═══════════════════════════════════════════════════════════
    # Endpoint implementations
    # ═══════════════════════════════════════════════════════════

    def _get_status(self) -> Dict[str, Any]:
        reg = self.ctx.registry

        # Token stats — use token_tracker if available
        token_stats = None
        try:
            from .token_tracker import get_token_tracker
            token_stats = get_token_tracker().stats()
        except Exception:
            pass

        # Git status — if project root is set and is a git repo
        git_status = None
        git_available = False
        project_root = self.ctx.config.get('project_root')
        if project_root:
            try:
                from .git_service import GitService
                gs = GitService(root=project_root)
                if gs.is_available:
                    git_available = True
                    git_status = gs.status()
            except Exception:
                pass

        return {
            'version': get_current_version(),
            'provider': reg.status(),
            'active_provider': reg.active_id,
            'active_chat_id': self.ctx.config.get('active_chat_id'),
            'project': project_root,
            'templates': len(PROMPT_TEMPLATES),
            'skills': len(SKILLS),
            'plugins': len(self.ctx.plugin_manager.plugins),
            'api_port': self.server.server_address[1],
            # v1.0.5-security: bearer token for mutating endpoints.
            # The pywebview frontend picks this up via __teraPilotReady and
            # sends it as `Authorization: Bearer <token>` on POSTs.
            # This blocks CSRF-to-localhost attacks (BUGS_REPORT C-API-1).
            'api_token': self.ctx._auth_token,
            'snippets_count': len(self.ctx.config.get('snippets', [])),
            'auto_route': self.ctx.config.get('auto_route', True),
            'agent_available': True,
            'token_stats': token_stats,
            'git_status': git_status,
            'git_available': git_available,
        }

    def _get_providers(self) -> List[Dict[str, Any]]:
        out = []
        for p in self.ctx.registry.list_providers():
            pid = p['id']
            pcfg = self.ctx.config.get('providers', {}).get(pid, {})
            key = pcfg.get('api_key') or ''
            out.append({
                **p,
                'model': pcfg.get('model', p['model']),
                'api_key_set': bool(key),
                'api_key_masked': (key[:4] + '...' + key[-4:]) if len(key) > 8 else ('...' if key else ''),
                'temperature': pcfg.get('temperature', 0.2),
                'max_tokens': pcfg.get('max_tokens', 4096),
                'active': pid == self.ctx.registry.active_id,
            })
        return out

    def _activate_provider(self, body: dict) -> dict:
        pid = body.get('provider_id', '')
        try:
            self.ctx.registry.set_active(pid)
            # v1.0.5-security: hold the config lock during RMW (BUGS_REPORT H-API-9).
            with self.ctx._config_lock:
                self.ctx.config['active_provider'] = pid
                _save_config(self.ctx.config)
            return {'ok': True, 'active_provider': pid}
        except ProviderError as e:
            return {'ok': False, 'error': str(e)}

    def _configure_provider(self, body: dict) -> dict:
        pid = body.get('provider_id', '')
        try:
            # v1.0.5-security: hold the config lock during RMW (BUGS_REPORT H-API-9).
            with self.ctx._config_lock:
                if pid not in self.ctx.config['providers']:
                    if pid in _PROVIDER_DEFAULTS:
                        self.ctx.config['providers'][pid] = dict(_PROVIDER_DEFAULTS[pid])
                    else:
                        self.ctx.config['providers'][pid] = {
                            'model': '', 'api_key': '', 'api_base': '',
                            'temperature': 0.2, 'max_tokens': 4096,
                        }
                pcfg = self.ctx.config['providers'][pid]
                for k in ('model', 'api_base', 'temperature', 'max_tokens'):
                    if k in body:
                        pcfg[k] = body[k]
                if body.get('api_key'):
                    pcfg['api_key'] = body['api_key']
                _save_config(self.ctx.config)

                cfg = ProviderConfig(
                    provider_id=pid,
                    model=pcfg.get('model', ''),
                    api_key=pcfg.get('api_key') or None,
                    api_base=pcfg.get('api_base') or None,
                    temperature=float(pcfg.get('temperature', 0.2)),
                    max_tokens=int(pcfg.get('max_tokens', 4096)),
                )
            self.ctx.registry.configure(pid, cfg)
            return {'ok': True}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def _handle_test_provider(self, body: dict) -> None:
        """Test a provider — streams the result via SSE."""
        pid = body.get('provider_id', '')
        request_id = body.get('request_id', '')

        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Access-Control-Allow-Origin', self._allowed_origin())
        self.send_header('Vary', 'Origin')
        self.send_header('Connection', 'keep-alive')
        self.end_headers()
        self.close_connection = False

        def test_thread():
            # v1.0.5-security: ALWAYS restore the original provider config
            # in `finally`, even on exception. Previously the provider was
            # permanently crippled to max_tokens=100 (BUGS_REPORT H-API-6).
            provider = None
            original_config = None
            try:
                cfg = self.ctx.config['providers'].get(pid, {})
                cfg_obj = ProviderConfig(
                    provider_id=pid,
                    model=cfg.get('model', ''),
                    api_key=cfg.get('api_key') or None,
                    api_base=cfg.get('api_base') or None,
                    temperature=0.2,
                    max_tokens=100,
                )
                self.ctx.registry.configure(pid, cfg_obj)
                provider = self.ctx.registry.get(pid)
                if not provider.is_loaded:
                    provider.load()
                # Capture original config AFTER load() (load() may mutate it).
                original_config = provider.config
                provider.config = cfg_obj
                resp = provider.generate([
                    ProviderMessage(role='user', content='Say hello in one sentence.'),
                ])
                self._sse({
                    'type': 'oneshot_done',
                    'request_id': request_id,
                    'text': resp.text,
                    'model': resp.model,
                })
            except Exception as e:
                self._sse({
                    'type': 'oneshot_error',
                    'request_id': request_id,
                    'error': str(e),
                })
            finally:
                # Restore the original provider config so subsequent chats
                # to the same provider aren't crippled to max_tokens=100.
                if provider is not None and original_config is not None:
                    try:
                        provider.config = original_config
                    except Exception as restore_err:
                        logger.warning("[api] failed to restore provider config after test: %s",
                                       restore_err)
                self.close_connection = True

        threading.Thread(target=test_thread, daemon=True).start()

    # ── Chat streaming (SSE) ──────────────────────────────────

    def _handle_chat_stream(self, body: dict) -> None:
        """Send a message and stream the response via SSE.

        IMPORTANT: We must mark the connection as keep-alive BEFORE starting the
        background thread, otherwise BaseHTTPRequestHandler.finish() closes wfile
        as soon as do_POST() returns — killing the SSE stream before any data is
        written.  This was the root cause of the "Failed to fetch" browser error.
        """
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Access-Control-Allow-Origin', self._allowed_origin())
        self.send_header('Vary', 'Origin')
        self.send_header('Connection', 'keep-alive')
        self.send_header('X-Accel-Buffering', 'no')
        self.end_headers()

        # Prevent handle() from closing the connection after do_POST() returns.
        # The background stream_thread will write to self.wfile; it needs the
        # connection to stay open.  When the stream finishes (or errors),
        # _stream_done_event signals handle_one_request() to stop blocking.
        self.close_connection = False

        text = (body.get('text') or '').strip()
        if not text:
            # v1.0.5-security: mark connection for close on early return.
            # Previously `close_connection` stayed False, leaking the handler
            # thread (BUGS_REPORT H-API-5).
            self._sse({'type': 'error', 'message': 'Empty prompt'})
            self.close_connection = True
            return

        # Get or create chat
        chat_id = body.get('chat_id')
        chat = _load_chat(chat_id) if chat_id else None
        if not chat:
            chat_id = uuid.uuid4().hex[:12]
            chat = {
                'id': chat_id,
                'title': text[:60] + ('...' if len(text) > 60 else ''),
                'created_at': datetime.utcnow().isoformat() + 'Z',
                'updated_at': datetime.utcnow().isoformat() + 'Z',
                'messages': [],
                'provider': self.ctx.registry.active_id,
                'skill': body.get('skill'),
            }

        # Build provider messages from history
        messages: List[ProviderMessage] = []
        for h in chat['messages']:
            messages.append(ProviderMessage(role=h['role'], content=h['content']))
        messages.append(ProviderMessage(role='user', content=text))

        # Prepend system prompt if not already present
        if not messages or messages[0].role != 'system':
            messages.insert(0, ProviderMessage(
                role='system',
                content=TERA_PILOT_SYSTEM_PROMPT,
            ))

        # Save user message
        chat['messages'].append({
            'role': 'user', 'content': text,
            'ts': datetime.utcnow().isoformat() + 'Z',
        })
        chat['updated_at'] = datetime.utcnow().isoformat() + 'Z'
        _save_chat(chat)

        # Send chat metadata first
        self._sse({
            'type': 'chat_info',
            'chat_id': chat_id,
            'title': chat['title'],
        })

        # Stream in background thread
        ctx = self.ctx
        handler = self  # capture reference for the thread
        stream_done = threading.Event()  # signals handle() to stop looping

        def stream_thread():
            _routed_pid: Optional[str] = None
            try:
                # v1.1.4-fix (bug C-API-6): AutoRouter existed and was fully
                # wired into web_bridge.py's send_message(), but the HTTP
                # chat path (the transport app.js actually uses whenever
                # window.__apiBase is set, i.e. almost always — the bridge
                # is a fallback) never called it. The Settings toggle and
                # "router_decision" status-bar badge therefore had no effect
                # for most users. Mirror web_bridge.send_message() here.
                if ctx.config.get('auto_route', True):
                    try:
                        configured = {
                            p['id'] for p in ctx.registry.list_providers()
                            if p.get('configured') or p['id'] in ('ollama', 'lmstudio')
                        }
                        decision = ctx.router.route(text, configured_providers=configured)
                        handler._sse({'type': 'router_decision', **decision})
                        logger.info("[api] auto-route: %s", decision.get('reasoning'))
                        if decision.get('provider_id') and decision['provider_id'] != ctx.registry.active_id:
                            try:
                                ctx.registry.set_active(decision['provider_id'])
                                logger.info("[api] switched to %s per router", decision['provider_id'])
                            except ProviderError:
                                logger.warning(
                                    "[api] router suggested %s but not available",
                                    decision['provider_id'],
                                )
                    except Exception as route_err:
                        logger.warning("[api] auto-route failed: %s", route_err)

                provider = ctx.registry.active
                _routed_pid = provider.provider_id
                if not provider.is_loaded:
                    logger.info("[api] loading provider %s", provider.provider_id)
                    provider.load()

                handler._sse({
                    'type': 'step',
                    'label': f'Connecting to {provider.label}...',
                    'detail': 'provider',
                })

                skill_id = body.get('skill')
                skill_text = _SKILL_TEXTS.get(skill_id) if skill_id else None

                template_id = body.get('template')
                if template_id:
                    tpl = next((t for t in PROMPT_TEMPLATES if t['id'] == template_id), None)
                    if tpl:
                        sections = tpl.get('sections', [])
                        skeleton = f"# Template: {tpl['name']}\n\n"
                        skeleton += "\n\n".join(f"[{s.upper()}]\n<to be filled>" for s in sections)
                        skeleton += f"\n\n[USER INTENT]\n{text}"
                        messages.insert(0, ProviderMessage(
                            role='system',
                            content=f'Use this prompt structure:\n\n{skeleton}',
                        ))

                # v1.1.2: inject cross-chat memory context
                # v1.0.5-security: bugfix — `self` here is the TeraPilotAPIHandler
                # (no `.config` attr), and `workspace` is undefined in this
                # scope. Both errors were silently swallowed by the bare
                # `except Exception: pass` below, which disabled the entire
                # MemoryService cross-chat brief feature (BUGS_REPORT H-API-4).
                try:
                    _mem = MemoryService(persist_path=str(_tera_pilot_home() / "cross_chat_memory.md"))
                    project_root = ctx.config.get('project_root')
                    if project_root:
                        brief = _mem.build_context_brief(project_root=project_root, query=text)
                        if brief:
                            messages.insert(0, ProviderMessage(
                                role='system',
                                content=f'Relevant prior context from earlier sessions:\n\n{brief}',
                            ))
                except Exception as mem_err:
                    logger.warning("[api] cross-chat memory brief failed: %s", mem_err)

                full_text: List[str] = []
                token_count = 0
                start = time.time()

                for chunk in provider.stream(messages, skill=skill_text):
                    if ctx._stop_event.is_set():
                        handler._sse({'type': 'step', 'label': 'Cancelled', 'detail': 'result'})
                        break
                    if time.time() - start > 300:
                        handler._sse({'type': 'error', 'message': 'Timeout after 300s'})
                        return

                    full_text.append(chunk)
                    token_count += 1
                    handler._sse({'type': 'token', 'content': chunk})

                elapsed = time.time() - start
                text_result = ''.join(full_text)

                if token_count == 0:
                    handler._sse({
                        'type': 'error',
                        'message': 'Empty response — possible invalid model or API issue.',
                    })
                    return

                # Save assistant message
                chat = _load_chat(chat_id)
                if chat:
                    chat['messages'].append({
                        'role': 'assistant',
                        'content': text_result,
                        'ts': datetime.utcnow().isoformat() + 'Z',
                        'tokens': token_count,
                        'elapsed': elapsed,
                        'cancelled': ctx._stop_event.is_set(),
                    })
                    chat['updated_at'] = datetime.utcnow().isoformat() + 'Z'
                    _save_chat(chat)

                handler._sse({
                    'type': 'done',
                    'text': text_result,
                    'tokens': token_count,
                    'elapsed': elapsed,
                    'cancelled': False,
                    'chat_id': chat_id,
                })

                # v1.1.4-fix (bug C-API-6): confirm the routed provider
                # actually worked, so AutoRouter's availability cache
                # reflects reality (mirrors web_bridge._on_generation_done).
                if _routed_pid:
                    ctx.router.mark_provider_available(_routed_pid, True)

                # Update active chat
                with ctx._config_lock:
                    ctx.config['active_chat_id'] = chat_id
                    _save_config(ctx.config)

            except ProviderError as e:
                logger.error("[api] ProviderError: %s", e)
                # v1.1.4-fix (bug C-API-6): a real failure — skip this
                # provider for the next few minutes so a retry (or the next
                # message) doesn't immediately hit the same broken provider
                # (mirrors web_bridge._on_generation_error).
                if _routed_pid:
                    ctx.router.mark_provider_available(_routed_pid, False)
                handler._sse({'type': 'error', 'message': str(e)})
            except Exception as e:
                logger.exception("[api] unexpected error")
                if _routed_pid:
                    ctx.router.mark_provider_available(_routed_pid, False)
                handler._sse({'type': 'error', 'message': f'Unexpected error: {e}'})
            finally:
                # Signal that the stream is finished so the handler thread
                # (blocked in handle_one_request) can close the connection.
                handler.close_connection = True
                stream_done.set()

        threading.Thread(target=stream_thread, daemon=True).start()

    # ── Agent mode (tool-use: read_file, write_file, run_code, etc.) ──

    def _handle_agent_stream(self, body: dict) -> None:
        """Send a message through the AGENT runtime (tool-use mode) and
        stream progress via SSE.

        This is the HTTP-transport counterpart of web_bridge.py's
        `send_agent_message`. Without this endpoint, the frontend's
        '/api/agent/stream' fetch() would 404 on every request — Agent
        Mode would silently fall back to plain chat with no tools.
        """
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Access-Control-Allow-Origin', self._allowed_origin())
        self.send_header('Vary', 'Origin')
        self.send_header('Connection', 'keep-alive')
        self.send_header('X-Accel-Buffering', 'no')
        self.end_headers()
        self.close_connection = False

        text = (body.get('text') or '').strip()
        if not text:
            # v1.0.5-security: mark connection for close on early return
            # (BUGS_REPORT H-API-5 — handler thread leak).
            self._sse({'type': 'error', 'message': 'Empty prompt'})
            self.close_connection = True
            return

        # v1.1.0: section (general | heavy_code | office) — controls which
        # tools are advertised and which quota counter is bumped.
        section = body.get('section') or 'general'
        if section not in ('general', 'heavy_code', 'office'):
            section = 'general'

        # v1.1.0: pre-check quota — fail fast with a friendly error.
        try:
            from .quota import get_quota_tracker
            quota = get_quota_tracker()
            if quota.exhausted(section):
                limit = quota.get_daily_limit(section)
                used = quota.count_today(section)
                self._sse({
                    'type': 'error',
                    'message': (
                        f"Daily {section} limit reached ({used}/{limit} "
                        f"requests today). Limit resets at 00:00 UTC."
                    ),
                    'quota_exhausted': True,
                    'section': section,
                    'used': used,
                    'limit': limit,
                })
                self.close_connection = True
                return
        except Exception as e:
            logger.warning("[api] quota pre-check failed: %s", e)

        project_root = body.get('project_root') or self.ctx.config.get('project_root')
        if not project_root:
            self._sse({'type': 'error', 'message': 'No project open — Agent Mode needs a project folder to read/write files in.'})
            self.close_connection = True
            return
        if not os.path.isdir(project_root):
            self._sse({'type': 'error', 'message': f'Project folder not found: {project_root}'})
            self.close_connection = True
            return

        # Get or create chat
        chat_id = body.get('chat_id')
        chat = _load_chat(chat_id) if chat_id else None
        if not chat:
            chat_id = uuid.uuid4().hex[:12]
            chat = {
                'id': chat_id,
                'title': text[:60] + ('...' if len(text) > 60 else ''),
                'created_at': datetime.utcnow().isoformat() + 'Z',
                'updated_at': datetime.utcnow().isoformat() + 'Z',
                'messages': [],
                'provider': self.ctx.registry.active_id,
                'mode': 'agent',
                'section': section,
            }

        chat['messages'].append({
            'role': 'user', 'content': text,
            'ts': datetime.utcnow().isoformat() + 'Z',
        })
        chat['mode'] = 'agent'
        chat['section'] = section
        chat['updated_at'] = datetime.utcnow().isoformat() + 'Z'
        _save_chat(chat)

        self._sse({
            'type': 'chat_info',
            'chat_id': chat_id,
            'title': chat['title'],
        })

        ctx = self.ctx
        handler = self
        stream_done = threading.Event()

        def on_event(event: "AgentEvent", data: Dict[str, Any]) -> None:
            """Runs synchronously on the agent thread — forward as SSE 'step' events."""
            label_map = {
                'plan_created': 'Planning…',
                'iteration_start': f"Step {data.get('iteration', '?')}/{data.get('max', '?')}",
                'thought': (data.get('thought') or '')[:120] or 'Thinking…',
                'tool_called': f"Running {data.get('tool', 'tool')}…",
                'tool_result': f"{data.get('tool', 'tool')} done",
                'error': f"Error: {data.get('error', '')}",
                'done': 'Done',
            }
            # v1.0.8: surface the full thought/plan text + write_intent in the
            # SSE payload so the frontend can stream it into the chat body.
            # Without this the user sees only the compact label in the activity
            # panel and nothing in the chat itself.
            payload = {
                'type': 'step',
                'label': label_map.get(event.value, event.value),
                'detail': event.value,
                'tool': data.get('tool'),
                'args': data.get('args'),
            }
            if data.get('thought'):
                payload['thought'] = data['thought']
            if data.get('plan'):
                payload['plan'] = data['plan']
            if data.get('write_intent'):
                payload['write_intent'] = data['write_intent']
            handler._sse(payload)

        def stream_thread():
            try:
                provider = ctx.registry.active
                if not provider.is_loaded:
                    logger.info("[api] loading provider %s", provider.provider_id)
                    provider.load()

                handler._sse({'type': 'step', 'label': f'Connecting to {provider.label}…', 'detail': 'provider'})

                # v1.1.4-fix (bug C-API-5): see _agent_run_lock comment in
                # ServerContext.__init__. Reject immediately rather than
                # queueing silently, so the user gets clear feedback instead
                # of a long unexplained wait (and so we never block this
                # handler thread forever on someone else's agent turn).
                if not ctx._agent_run_lock.acquire(blocking=False):
                    handler._sse({
                        'type': 'error',
                        'message': (
                            'Another agent request is already running. '
                            'Wait for it to finish, or click Stop, then try again.'
                        ),
                    })
                    return
                try:
                    agent = ctx.get_agent_runtime(project_root)
                    original_callback = agent.on_event
                    agent.on_event = on_event
                    # v1.1.1: wire diff-review callback for HTTP path.
                    # v1.0.5-security: per-request review_id so concurrent agent
                    # streams don't share a single Event/Optional[bool]
                    # (BUGS_REPORT C-API-2 race condition).
                    original_diff_cb = agent.tools._diff_review_callback
                    def _http_diff_review_callback(diff_info):
                        """Send diff review request via SSE, then block until POST response."""
                        review_id = secrets.token_urlsafe(12)
                        review_event = threading.Event()
                        with ctx._diff_review_lock:
                            ctx._diff_review_pending[review_id] = {
                                'event': review_event,
                                'accepted': None,
                                'path': diff_info['path'],
                                'agent_id': id(agent),
                            }
                            ctx._diff_review_by_agent[id(agent)] = review_id
                        handler._sse({
                            'type': 'diff_review',
                            'review_id': review_id,
                            'path': diff_info['path'],
                            'diff': diff_info['diff'],
                            'lines_added': diff_info['lines_added'],
                            'lines_removed': diff_info['lines_removed'],
                        })
                        # Block the agent thread until POST /api/agent/diff_review
                        # for THIS review_id arrives, or 5 min timeout.
                        # v2.4.0-fix: also abort early when the user clicks Stop
                        # (/api/agent/stop). Previously the agent thread stayed
                        # blocked here for the full 300s (or until the browser
                        # answered), so Stop appeared to hang mid-diff-review —
                        # the engine's own `_wait_interruptible` on the second
                        # wait could never run until this one returned.
                        waited = 0.0
                        while not review_event.wait(timeout=0.25):
                            waited += 0.25
                            if ctx.is_agent_cancelled() or waited >= 300:
                                break
                        with ctx._diff_review_lock:
                            entry = ctx._diff_review_pending.pop(review_id, None)
                            if id(agent) in ctx._diff_review_by_agent and \
                                    ctx._diff_review_by_agent[id(agent)] == review_id:
                                del ctx._diff_review_by_agent[id(agent)]
                        accepted_val = entry['accepted'] if entry else False
                        agent.tools._diff_review_accepted = accepted_val
                        agent.tools._diff_review_event.set()
                    agent.tools._diff_review_callback = _http_diff_review_callback
                    # Sync diff_review_enabled from config (user may have toggled it)
                    agent.tools.diff_review_enabled = ctx.config.get('diff_review', True)
                    # v1.1.0: switch the runtime to the requested section.
                    # This affects which tools are advertised and which quota
                    # counter is bumped. Also bump max_iterations for heavy_code.
                    agent.set_section(section)
                    if section == 'heavy_code':
                        agent.max_iterations = max(
                            int(ctx.config.get('agent_max_iterations', 8)),
                            20,
                        )
                        # v1.1.3-fix (bug 2.1): scale RUN_TIMEOUT and MAX_OUTPUT
                        # for Heavy Code so long builds/tests complete and the
                        # agent can see full pytest output instead of a 2000-char
                        # truncation.
                        agent.tools.RUN_TIMEOUT = max(
                            int(ctx.config.get('agent_run_timeout', 15)),
                            60,
                        )
                        agent.tools.MAX_OUTPUT = max(
                            int(ctx.config.get('agent_max_output', 2000)),
                            8000,
                        )
                    else:
                        agent.max_iterations = int(ctx.config.get('agent_max_iterations', 8))
                        agent.tools.RUN_TIMEOUT = int(ctx.config.get('agent_run_timeout', 15))
                        agent.tools.MAX_OUTPUT = int(ctx.config.get('agent_max_output', 2000))
                    # v1.1.1: wire the cancel check so POST /api/agent/stop
                    # can actually halt this run. reset_agent_cancel() clears
                    # any stale flag from a previous run.
                    ctx.reset_agent_cancel()
                    agent.set_cancel_check(ctx.is_agent_cancelled)
                    start = time.time()
                    try:
                        result = agent.run(text, task_type=TaskType.AGENTIC)
                    finally:
                        agent.on_event = original_callback
                        agent.tools._diff_review_callback = original_diff_cb
                        # Clear the cancel check so it doesn't affect future runs
                        agent.set_cancel_check(None)
                        # v1.0.5-security: clean up any leftover pending review for this agent.
                        with ctx._diff_review_lock:
                            leftover_id = ctx._diff_review_by_agent.pop(id(agent), None)
                            if leftover_id is not None:
                                entry = ctx._diff_review_pending.pop(leftover_id, None)
                                if entry is not None:
                                    entry['event'].set()  # unblock the wait if still blocked
                finally:
                    ctx._agent_run_lock.release()
                elapsed = time.time() - start

                text_result = result.output or ''

                if not result.success and not text_result:
                    handler._sse({'type': 'error', 'message': result.error or 'Agent failed with no output.'})
                    return

                chat2 = _load_chat(chat_id)
                if chat2:
                    chat2['messages'].append({
                        'role': 'assistant',
                        'content': text_result,
                        'ts': datetime.utcnow().isoformat() + 'Z',
                        'tokens': result.iterations,
                        'elapsed': elapsed,
                        'tool_calls': [tc.name.value for tc in (result.tool_calls or [])],
                    })
                    chat2['updated_at'] = datetime.utcnow().isoformat() + 'Z'
                    _save_chat(chat2)

                handler._sse({
                    'type': 'done',
                    'text': text_result,
                    'tokens': result.iterations,
                    'elapsed': elapsed,
                    # v1.1.1: accurately report cancellation — previously
                    # hardcoded to False, so a Stop-then-partial-output
                    # would show as a normal completion.
                    'cancelled': bool(ctx.is_agent_cancelled() or (result.error == 'Cancelled by user')),
                    'chat_id': chat_id,
                })

                with ctx._config_lock:
                    ctx.config['active_chat_id'] = chat_id
                    ctx.config['project_root'] = project_root
                    _save_config(ctx.config)

            except ProviderError as e:
                logger.error("[api] agent ProviderError: %s", e)
                handler._sse({'type': 'error', 'message': str(e)})
            except Exception as e:
                logger.exception("[api] agent unexpected error")
                handler._sse({'type': 'error', 'message': f'Unexpected error: {e}'})
            finally:
                handler.close_connection = True
                stream_done.set()

        threading.Thread(target=stream_thread, daemon=True).start()

    # ── Diff review (agent pauses for user approve/reject) ──────

    def _handle_diff_review(self, body: dict) -> dict:
        """Respond to a pending diff-review request from the agent.

        v1.0.5-security: route the response back to the SPECIFIC agent
        thread that requested it, identified by `review_id`. Previously
        all concurrent agent streams shared a single Event/Optional[bool],
        so the accept/reject decision for one agent could be applied to
        another agent's file write (BUGS_REPORT C-API-2).
        """
        accepted = body.get('accepted', False)
        review_id = body.get('review_id', '')
        ctx = self.ctx
        with ctx._diff_review_lock:
            entry = ctx._diff_review_pending.get(review_id)
            if entry is None:
                return {"ok": False, "error": "no pending diff review for that review_id"}
            entry['accepted'] = bool(accepted)
            entry['event'].set()
        logger.info("[api] diff_review: review_id=%s accepted=%s",
                    review_id, 'accepted' if accepted else 'rejected')
        return {"ok": True, "accepted": accepted, "review_id": review_id}

    # ── Agent stop (v1.1.1) ─────────────────────────────────────

    def _agent_stop(self) -> dict:
        """Cancel the running agent stream (HTTP path).

        Sets a flag on ServerContext that the agent loop polls via
        set_cancel_check(). The agent loop checks this flag:
          - at the top of each iteration (before the LLM call)
          - right before executing a tool call (after the LLM returns)
          - during retry backoff sleeps (every 250ms)

        So Stop takes effect within ~250ms during a wait, or at the next
        iteration boundary during active LLM work.
        """
        self.ctx.cancel_agent()
        logger.info("[api] /api/agent/stop — cancel flag set")
        return {"ok": True, "message": "Agent stop requested"}

    # ── One-shot (enhance prompt, etc.) ───────────────────────

    def _handle_oneshot(self, body: dict) -> None:
        """Single non-streaming response via SSE (enhance prompt)."""
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Access-Control-Allow-Origin', self._allowed_origin())
        self.send_header('Vary', 'Origin')
        self.send_header('Connection', 'keep-alive')
        self.end_headers()
        self.close_connection = False

        request_id = body.get('request_id', '')
        messages_data = body.get('messages', [])
        max_tokens = body.get('max_tokens', 800)

        if not messages_data:
            # v1.0.5-security: close connection on early return (BUGS_REPORT H-API-5).
            self._sse({'type': 'oneshot_error', 'request_id': request_id, 'error': 'No messages'})
            self.close_connection = True
            return

        ctx = self.ctx
        handler = self

        def oneshot_thread():
            # v1.0.5-security: ALWAYS restore the original provider config
            # in `finally`, even on exception. Previously the provider was
            # permanently crippled to max_tokens=800 if provider.generate()
            # raised (BUGFIX_REPORT M2 / BUGS_REPORT C-API-3).
            provider = None
            original_config = None
            try:
                provider = ctx.registry.active
                if not provider.is_loaded:
                    provider.load()

                # Temporarily set lower max_tokens for oneshot
                original_config = provider.config
                temp_config = ProviderConfig(
                    provider_id=original_config.provider_id,
                    model=original_config.model,
                    api_key=original_config.api_key,
                    api_base=original_config.api_base,
                    temperature=original_config.temperature,
                    max_tokens=min(max_tokens, original_config.max_tokens),
                    top_p=original_config.top_p,
                    stream=original_config.stream,
                    timeout=original_config.timeout,
                )
                provider.config = temp_config

                msgs = [ProviderMessage(role=m['role'], content=m['content']) for m in messages_data]
                resp = provider.generate(msgs)

                handler._sse({
                    'type': 'oneshot_done',
                    'request_id': request_id,
                    'text': resp.text,
                    'model': resp.model,
                    'tokens_in': resp.tokens_in,
                    'tokens_out': resp.tokens_out,
                })
            except Exception as e:
                handler._sse({'type': 'oneshot_error', 'request_id': request_id, 'error': str(e)})
            finally:
                # Restore the original provider config regardless of success/failure.
                if provider is not None and original_config is not None:
                    try:
                        provider.config = original_config
                    except Exception as restore_err:
                        logger.warning("[api] failed to restore provider config after oneshot: %s",
                                       restore_err)
                handler.close_connection = True

        threading.Thread(target=oneshot_thread, daemon=True).start()

    # ── Chat CRUD ─────────────────────────────────────────────

    def _list_chats(self) -> List[Dict[str, Any]]:
        chats = []
        for path in _chats_dir().glob('*.json'):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    chat = json.load(f)
                chats.append({
                    'id': chat['id'],
                    'title': chat['title'],
                    'updated_at': chat.get('updated_at', chat.get('created_at', '')),
                    'message_count': len(chat.get('messages', [])),
                    'provider': chat.get('provider'),
                    'skill': chat.get('skill'),
                })
            except (OSError, json.JSONDecodeError, KeyError) as e:
                logger.warning("[chat] failed to read %s: %s", path, e)
        chats.sort(key=lambda c: c.get('updated_at', ''), reverse=True)
        return chats

    def _load_chat(self, chat_id: str) -> dict:
        chat = _load_chat(chat_id)
        if not chat:
            return {'ok': False, 'error': 'Chat not found'}
        return {'ok': True, 'chat': chat}

    def _create_chat(self, body: dict) -> dict:
        title = body.get('title', 'New chat')
        chat_id = uuid.uuid4().hex[:12]
        chat = {
            'id': chat_id,
            'title': title,
            'created_at': datetime.utcnow().isoformat() + 'Z',
            'updated_at': datetime.utcnow().isoformat() + 'Z',
            'messages': [],
            'provider': self.ctx.registry.active_id,
            'skill': None,
        }
        _save_chat(chat)
        self.ctx.config['active_chat_id'] = chat_id
        _save_config(self.ctx.config)
        return {'ok': True, 'chat_id': chat_id, 'chat': chat}

    def _rename_chat(self, body: dict) -> dict:
        chat_id = body.get('id', '')
        title = body.get('title', '')
        chat = _load_chat(chat_id)
        if not chat:
            return {'ok': False, 'error': 'Chat not found'}
        chat['title'] = title
        chat['updated_at'] = datetime.utcnow().isoformat() + 'Z'
        _save_chat(chat)
        return {'ok': True}

    def _delete_chat(self, body: dict) -> dict:
        chat_id = body.get('id', '')
        path = _chat_path(chat_id)
        if path.exists():
            try:
                path.unlink()
            except OSError:
                return {'ok': False, 'error': 'Delete failed'}
        if self.ctx.config.get('active_chat_id') == chat_id:
            self.ctx.config['active_chat_id'] = None
            _save_config(self.ctx.config)
        return {'ok': True}

    # ── Settings ──────────────────────────────────────────────

    def _save_settings(self, body: dict) -> dict:
        # v1.0.5-security: hold the config lock during the entire RMW
        # (BUGS_REPORT H-API-9 — concurrent stream threads were mutating
        # ctx.config mid-iteration).
        with self.ctx._config_lock:
            cfg = self.ctx.config
            for k in ('active_provider', 'active_chat_id', 'project_root'):
                if k in body:
                    cfg[k] = body[k]
            if 'ui' in body:
                cfg['ui'] = {**cfg.get('ui', {}), **body['ui']}
            elif 'theme' in body:
                # Forward-compat: bare `theme` top-level key.
                cfg.setdefault('ui', {})['theme'] = body['theme']
            if 'providers' in body:
                for pid, pcfg in body['providers'].items():
                    if pid not in cfg['providers']:
                        if pid in _PROVIDER_DEFAULTS:
                            cfg['providers'][pid] = dict(_PROVIDER_DEFAULTS[pid])
                        else:
                            cfg['providers'][pid] = {'model': '', 'api_key': '', 'api_base': '', 'temperature': 0.2, 'max_tokens': 4096}
                    for k in ('model', 'api_base', 'temperature', 'max_tokens'):
                        if k in pcfg:
                            cfg['providers'][pid][k] = pcfg[k]
                    if pcfg.get('api_key'):
                        cfg['providers'][pid]['api_key'] = pcfg['api_key']

            _save_config(cfg)

        # Re-apply provider configs
        if 'providers' in body:
            for pid, pcfg in body['providers'].items():
                try:
                    prov_cfg = ProviderConfig(
                        provider_id=pid,
                        model=pcfg.get('model', cfg['providers'].get(pid, {}).get('model', '')),
                        api_key=pcfg.get('api_key') or cfg['providers'].get(pid, {}).get('api_key'),
                        api_base=pcfg.get('api_base') or cfg['providers'].get(pid, {}).get('api_base'),
                        temperature=float(pcfg.get('temperature', 0.2)),
                        max_tokens=int(pcfg.get('max_tokens', 4096)),
                    )
                    self.ctx.registry.configure(pid, prov_cfg)
                except Exception as e:
                    logger.warning("[api] configure %s failed: %s", pid, e)

        if 'active_provider' in body:
            try:
                self.ctx.registry.set_active(body['active_provider'])
            except ProviderError as e:
                return {'ok': False, 'error': str(e)}

        return {'ok': True}

    # ═══════════════════════════════════════════════════════════════
    # v1.1.0 — MCP + QUOTA + ADVANCED AGENT SETTINGS ENDPOINTS
    # ═══════════════════════════════════════════════════════════════

    def _mcp_list_servers(self) -> Dict[str, Any]:
        try:
            from .mcp_manager import get_mcp_manager
            return {'ok': True, **get_mcp_manager().status()}
        except Exception as e:
            return {'ok': False, 'error': str(e), 'servers': [], 'total_tools': 0}

    def _mcp_status(self) -> Dict[str, Any]:
        return self._mcp_list_servers()

    def _mcp_add_server(self, body: dict) -> Dict[str, Any]:
        try:
            from .mcp_manager import get_mcp_manager
            name = (body.get('name') or '').strip()
            command = body.get('command', [])
            if isinstance(command, str):
                import shlex
                command = shlex.split(command)
            env = body.get('env', {}) or {}
            enabled = bool(body.get('enabled', True))
            autostart = bool(body.get('autostart', True))
            return get_mcp_manager().add_server(
                name=name, command=command, env=env,
                enabled=enabled, autostart=autostart,
            )
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def _mcp_remove_server(self, body: dict) -> Dict[str, Any]:
        try:
            from .mcp_manager import get_mcp_manager
            return get_mcp_manager().remove_server(body.get('name', ''))
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def _mcp_toggle_server(self, body: dict) -> Dict[str, Any]:
        try:
            from .mcp_manager import get_mcp_manager
            return get_mcp_manager().toggle_server(
                body.get('name', ''), bool(body.get('enabled', True))
            )
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def _mcp_start_server(self, body: dict) -> Dict[str, Any]:
        try:
            from .mcp_manager import get_mcp_manager
            return get_mcp_manager().start_server(body.get('name', ''))
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def _mcp_stop_server(self, body: dict) -> Dict[str, Any]:
        try:
            from .mcp_manager import get_mcp_manager
            return get_mcp_manager().stop_server(body.get('name', ''))
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def _mcp_start_all(self) -> Dict[str, Any]:
        try:
            from .mcp_manager import get_mcp_manager
            get_mcp_manager().start_all()
            return {'ok': True, **get_mcp_manager().status()}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def _mcp_stop_all(self) -> Dict[str, Any]:
        try:
            from .mcp_manager import get_mcp_manager
            get_mcp_manager().stop_all()
            return {'ok': True}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def _mcp_reload_config(self) -> Dict[str, Any]:
        try:
            from .mcp_manager import get_mcp_manager
            get_mcp_manager().reload_config()
            return {'ok': True, **get_mcp_manager().status()}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def _quota_stats(self) -> Dict[str, Any]:
        try:
            from .quota import get_quota_tracker
            return {'ok': True, **get_quota_tracker().stats()}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def _quota_set_limit(self, body: dict) -> Dict[str, Any]:
        try:
            from .quota import get_quota_tracker
            section = body.get('section', 'heavy_code')
            limit = int(body.get('limit', 10))
            get_quota_tracker().set_daily_limit(section, limit)
            return {'ok': True, 'section': section, 'limit': limit}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def _quota_clear_history(self) -> Dict[str, Any]:
        try:
            from .quota import get_quota_tracker
            get_quota_tracker().clear_history()
            return {'ok': True}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    # ── v1.2.0: Activity Stream handlers ────────────────────────

    def _activity_recent(self) -> Dict[str, Any]:
        """GET /api/activity/recent?n=200&category=shell&status=ok&search=foo

        Returns the most recent activity log entries, optionally
        filtered. Used by the GUI's Activity Stream panel on initial
        load and after manual refresh.
        """
        try:
            from .activity_log import get_activity_log
            log = get_activity_log()
            n = min(int(self._query('n') or 200), 2000)
            category = self._query('category') or None
            status = self._query('status') or None
            search = self._query('search') or None
            kind = self._query('kind') or None
            entries = log.recent(
                n=n, category=category, status=status, search=search, kind=kind,
            )
            return {'ok': True, 'entries': entries, 'total': len(log)}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def _activity_stats(self) -> Dict[str, Any]:
        """GET /api/activity/stats — counts by category and status."""
        try:
            from .activity_log import get_activity_log
            return {'ok': True, **get_activity_log().stats()}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def _activity_export(self) -> None:
        """GET /api/activity/export — full log as a downloadable JSON file."""
        try:
            from .activity_log import get_activity_log
            payload = get_activity_log().export_json().encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Disposition',
                             'attachment; filename="tera_pilot_activity_log.json"')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except Exception as e:
            self._json({'ok': False, 'error': str(e)}, 500)

    def _activity_clear(self) -> Dict[str, Any]:
        """POST /api/activity/clear — empty the activity log."""
        try:
            from .activity_log import get_activity_log
            n = get_activity_log().clear()
            return {'ok': True, 'removed': n}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def _get_advanced_agent_settings(self) -> Dict[str, Any]:
        try:
            cfg = self.ctx.config
            active_pid = self.ctx.registry.active_id
            pcfg = cfg.get('providers', {}).get(active_pid, {})
            agent = self.ctx._agent_runtime
            from .quota import get_quota_tracker
            return {
                'ok': True,
                'agent': {
                    'max_iterations': int(cfg.get('agent_max_iterations', 8)),
                    'enable_planning': bool(cfg.get('agent_enable_planning', True)),
                    'run_timeout': int(cfg.get('agent_run_timeout', 15)),
                    'autonomy': cfg.get('agent_autonomy', 'always_ask'),
                    'diff_review': bool(cfg.get('diff_review', True)),
                    'section': agent.section if agent else 'general',
                    'memory_max_messages': int(cfg.get('agent_memory_max_messages', 20)),
                    'memory_max_tokens': int(cfg.get('agent_memory_max_tokens', 8000)),
                },
                'inference': {
                    'temperature': float(pcfg.get('temperature', 0.2)),
                    'max_tokens': int(pcfg.get('max_tokens', 4096)),
                    'top_p': float(pcfg.get('top_p', 0.95)),
                    'active_provider': active_pid,
                },
                'heavy_code': {
                    'daily_limit': get_quota_tracker().get_daily_limit('heavy_code'),
                    'used_today': get_quota_tracker().count_today('heavy_code'),
                    'remaining': get_quota_tracker().remaining('heavy_code'),
                },
            }
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def _save_advanced_agent_settings(self, body: dict) -> Dict[str, Any]:
        try:
            cfg = self.ctx.config
            with self.ctx._config_lock:
                # Agent settings
                if 'agent' in body and isinstance(body['agent'], dict):
                    a = body['agent']
                    if 'max_iterations' in a:
                        cfg['agent_max_iterations'] = int(a['max_iterations'])
                    if 'enable_planning' in a:
                        cfg['agent_enable_planning'] = bool(a['enable_planning'])
                    if 'run_timeout' in a:
                        cfg['agent_run_timeout'] = int(a['run_timeout'])
                    if 'autonomy' in a:
                        cfg['agent_autonomy'] = a['autonomy']
                    if 'diff_review' in a:
                        cfg['diff_review'] = bool(a['diff_review'])
                    if 'memory_max_messages' in a:
                        cfg['agent_memory_max_messages'] = int(a['memory_max_messages'])
                    if 'memory_max_tokens' in a:
                        cfg['agent_memory_max_tokens'] = int(a['memory_max_tokens'])
                    # Apply to live runtime
                    if self.ctx._agent_runtime:
                        rt = self.ctx._agent_runtime
                        if 'max_iterations' in a:
                            rt.max_iterations = int(a['max_iterations'])
                        if 'enable_planning' in a:
                            rt.enable_planning = bool(a['enable_planning'])
                        if 'autonomy' in a:
                            rt.set_autonomy(a['autonomy'])
                        if 'run_timeout' in a:
                            rt.tools.RUN_TIMEOUT = int(a['run_timeout'])
                        if 'diff_review' in a:
                            rt.tools.diff_review_enabled = bool(a['diff_review'])
                        if rt.memory:
                            if 'memory_max_messages' in a:
                                rt.memory.max_messages = int(a['memory_max_messages'])
                            if 'memory_max_tokens' in a:
                                rt.memory.max_tokens = int(a['memory_max_tokens'])

                # Inference settings — apply to active provider
                if 'inference' in body and isinstance(body['inference'], dict):
                    inf = body['inference']
                    active_pid = self.ctx.registry.active_id
                    if active_pid in cfg.get('providers', {}):
                        pcfg = cfg['providers'][active_pid]
                        if 'temperature' in inf:
                            pcfg['temperature'] = float(inf['temperature'])
                        if 'max_tokens' in inf:
                            pcfg['max_tokens'] = int(inf['max_tokens'])
                        if 'top_p' in inf:
                            pcfg['top_p'] = float(inf['top_p'])
                        try:
                            prov_cfg = ProviderConfig(
                                provider_id=active_pid,
                                model=pcfg.get('model', ''),
                                api_key=pcfg.get('api_key') or None,
                                api_base=pcfg.get('api_base') or None,
                                temperature=float(pcfg.get('temperature', 0.2)),
                                max_tokens=int(pcfg.get('max_tokens', 4096)),
                                top_p=float(pcfg.get('top_p', 0.95)),
                            )
                            self.ctx.registry.configure(active_pid, prov_cfg)
                        except Exception as cfg_err:
                            logger.warning("[api] configure %s failed: %s", active_pid, cfg_err)

                # Heavy Code daily limit
                if 'heavy_code' in body and isinstance(body['heavy_code'], dict):
                    hc = body['heavy_code']
                    if 'daily_limit' in hc:
                        from .quota import get_quota_tracker
                        get_quota_tracker().set_daily_limit('heavy_code', int(hc['daily_limit']))
                        cfg['heavy_code_daily_limit'] = int(hc['daily_limit'])

                _save_config(cfg)

            return {'ok': True, 'settings': self._get_advanced_agent_settings()}
        except Exception as e:
            logger.error("[api] save_advanced_agent_settings failed: %s", e)
            return {'ok': False, 'error': str(e)}


# ── Threaded server ────────────────────────────────────────────────

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# ── Server lifecycle ───────────────────────────────────────────────

class TeraPilotAPIServer:
    """Starts and manages the local API server."""

    def __init__(self, port: Optional[int] = None):
        self.port = port or _find_free_port()
        self._server: Optional[ThreadedHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.ctx = ServerContext()

    def start(self) -> None:
        """Start the server in a background thread."""
        TeraPilotAPIHandler.ctx = self.ctx
        self._server = ThreadedHTTPServer(('127.0.0.1', self.port), TeraPilotAPIHandler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        logger.info("[api_server] listening on http://127.0.0.1:%d", self.port)

    def stop(self) -> None:
        """Stop the server and close all connections."""
        self.ctx.stop()
        if self._server:
            self._server.shutdown()
            self._server.server_close()  # Ensure all sockets are closed
            logger.info("[api_server] stopped")

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def auth_token(self) -> str:
        """v1.0.5-security: bearer token clients must send on mutating endpoints."""
        return self.ctx._auth_token


# ── v2.2.1: extended endpoints ───────────────────────────────────────
# Install the api_extended route table so the browser GUI can reach
# every backend capability that was previously only exposed via the
# TUI bridge (capabilities, hooks, checkpoints, github, handoffs, cost
# router, spend dashboard, consensus, learnings, second_opinion, budget,
# verify, persona, router mode, mcp_server, notify, daemon, custom
# providers, etc.).
#
# This is a no-op if api_extended is not importable (e.g. in stripped
# down test environments) so legacy callers keep working.
try:
    from .api_extended import install as _install_extended_routes
    _install_extended_routes()
except Exception as _api_ext_err:  # pragma: no cover
    logger.warning("[api_server] api_extended install failed: %s", _api_ext_err)