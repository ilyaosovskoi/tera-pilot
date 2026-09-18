"""
Environment Doctor — ``tera-pilot doctor`` (P0 onboarding).

One command that answers the question "is this machine ready to run
Tera Pilot?":

    tera-pilot doctor                 # human-readable report
    tera-pilot doctor --json          # machine-readable JSON (for CI / scripts)
    tera-pilot doctor --project DIR   # check a specific working directory

Check statuses:

    ok    — ready to work
    warn  — works, but something is missing or optional
            (e.g. local Ollama/LM Studio not running, no cloud
            keys set, Rust acceleration not built)
    fail  — blocks normal operation (Python < 3.11, a missing
            critical dependency, an unreachable working directory)

Exit code: 0 — if there is not a single ``fail``, else 1. Warnings
alone never fail the check: a fully local setup without
cloud keys is a valid configuration.

See also ``THREAT_MODEL.md`` — the network probes here are limited to
localhost endpoints of local models and send nothing outside.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"

# Critical dependencies — Tera Pilot won't start without them.
CORE_DEPS = [
    ("pydantic", "configuration and types"),
    ("textual", "full-screen TUI (tera-pilot-tui)"),
    ("requests", "HTTP client (providers, daemon, web search)"),
    ("aiohttp", "HTTP client (streaming)"),
    ("toml", "configuration (TOML)"),
    ("yaml", "configuration (YAML)"),
    ("rich", "terminal output"),
]

# Optional dependencies — individual sections degrade without them.
OPTIONAL_DEPS = [
    ("cryptography", "signed audit (Ed25519)"),
    ("docx", "Office: .docx"),
    ("openpyxl", "Office: .xlsx"),
    ("pptx", "Office: .pptx"),
]


@dataclass
class CheckResult:
    """Result of a single doctor check."""

    name: str
    status: str          # ok | warn | fail
    detail: str = ""
    hint: str = ""       # what to do if status is not ok

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── Individual checks ─────────────────────────────────────────────


def _check_python() -> CheckResult:
    v = sys.version_info
    ok = (v.major, v.minor) >= (3, 11)
    return CheckResult(
        name="python",
        status=STATUS_OK if ok else STATUS_FAIL,
        detail=f"Python {v.major}.{v.minor}.{v.micro} (requires >= 3.11)",
        hint="" if ok else "Install Python 3.11+ and re-run doctor.",
    )


def _check_package() -> CheckResult:
    try:
        import tera_pilot
        return CheckResult(
            name="package",
            status=STATUS_OK,
            detail=f"tera_pilot v{tera_pilot.__version__} imports from {tera_pilot.__file__}",
        )
    except Exception as e:
        return CheckResult(
            name="package",
            status=STATUS_FAIL,
            detail=f"failed to import tera_pilot: {e}",
            hint="Install the package: pip install -e . (from the project root)",
        )


def _check_config_dir() -> CheckResult:
    from tera_pilot.utils import get_tera_pilot_dir
    try:
        d = get_tera_pilot_dir()
    except Exception as e:
        return CheckResult(
            name="config_dir",
            status=STATUS_FAIL,
            detail=f"~/.tera_pilot unavailable: {e}",
            hint="Check permissions on the home directory.",
        )
    writable = os.access(d, os.W_OK)
    files = sorted(p.name for p in d.iterdir()) if d.exists() else []
    detail = f"~/.tera_pilot — files: {', '.join(files) if files else 'empty (created on first run)'}"
    return CheckResult(
        name="config_dir",
        status=STATUS_OK if writable else STATUS_WARN,
        detail=detail,
        hint="" if writable else "No write access to ~/.tera_pilot — fix permissions.",
    )


def _check_dependencies() -> List[CheckResult]:
    import importlib
    out: List[CheckResult] = []
    for mod, purpose in CORE_DEPS:
        try:
            importlib.import_module(mod)
        except Exception:
            out.append(CheckResult(
                name=f"dep:{mod}",
                status=STATUS_FAIL,
                detail=f"{mod} not installed — {purpose}",
                hint=f"pip install {mod}",
            ))
        else:
            out.append(CheckResult(name=f"dep:{mod}", status=STATUS_OK, detail=f"{mod} — {purpose}"))
    for mod, purpose in OPTIONAL_DEPS:
        try:
            importlib.import_module(mod)
        except Exception:
            out.append(CheckResult(
                name=f"dep:{mod}",
                status=STATUS_WARN,
                detail=f"{mod} not installed — {purpose}",
                hint=f"pip install {mod} (optional)",
            ))
        else:
            out.append(CheckResult(name=f"dep:{mod}", status=STATUS_OK, detail=f"{mod} — {purpose}"))
    return out


def _provider_env_vars() -> Dict[str, str]:
    """provider_id -> key env-var name (from the registered providers)."""
    try:
        from tera_pilot.providers import get_registry
        reg = get_registry()
        out: Dict[str, str] = {}
        for pid, cls in getattr(reg, "_classes", {}).items():
            ev = getattr(cls, "env_var", None)
            if ev:
                out[pid] = ev
        return out
    except Exception:
        return {}


def _check_providers() -> List[CheckResult]:
    out: List[CheckResult] = []
    env_vars = _provider_env_vars()
    set_vars = {pid: ev for pid, ev in env_vars.items() if os.environ.get(ev)}

    cfg_keys: List[str] = []
    try:
        from tera_pilot.utils import load_config
        cfg = load_config() or {}
        for pid, p in (cfg.get("providers") or {}).items():
            if isinstance(p, dict) and p.get("api_key"):
                cfg_keys.append(str(pid))
    except Exception:
        pass

    if set_vars or cfg_keys:
        detail = "keys found"
        if set_vars:
            detail += " · env: " + ", ".join(sorted(set_vars.values()))
        if cfg_keys:
            detail += " · config.json: " + ", ".join(sorted(cfg_keys))
        out.append(CheckResult(name="providers:keys", status=STATUS_OK, detail=detail))
    else:
        out.append(CheckResult(
            name="providers:keys",
            status=STATUS_WARN,
            detail="No API keys found in the environment or ~/.tera_pilot/config.json",
            hint="Configure a provider in the UI (Settings → Providers) or set a key "
                 "env variable (e.g. OPENAI_API_KEY). Local models need no keys.",
        ))
    return out


def _probe_http(url: str, timeout: float = 2.0) -> Tuple[bool, str]:
    """Lightweight localhost-endpoint probe. Sends nothing outside."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status == 200, f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, str(e)


def _check_ollama() -> CheckResult:
    ok, _ = _probe_http("http://127.0.0.1:11434/api/tags")
    return CheckResult(
        name="ollama",
        status=STATUS_OK if ok else STATUS_WARN,
        detail="Ollama responds on 127.0.0.1:11434" if ok else "Ollama not running on 127.0.0.1:11434",
        hint="" if ok else "Run `ollama serve` or use a cloud provider.",
    )


def _check_lmstudio() -> CheckResult:
    ok, _ = _probe_http("http://127.0.0.1:1234/v1/models")
    return CheckResult(
        name="lmstudio",
        status=STATUS_OK if ok else STATUS_WARN,
        detail="LM Studio responds on 127.0.0.1:1234" if ok else "LM Studio not running on 127.0.0.1:1234",
        hint="" if ok else "Start LM Studio with the local server enabled.",
    )


def _check_native() -> CheckResult:
    try:
        from tera_pilot.agent.native import NATIVE_AVAILABLE
    except Exception as e:
        return CheckResult(
            name="native",
            status=STATUS_WARN,
            detail=f"could not check native acceleration: {e}",
        )
    if NATIVE_AVAILABLE:
        return CheckResult(
            name="native",
            status=STATUS_OK,
            detail="Rust acceleration active (tera_pilot_native)",
        )
    return CheckResult(
        name="native",
        status=STATUS_WARN,
        detail="Rust acceleration not installed — using pure-Python fallbacks",
        hint="Optional: build tera-pilot-native (see pyproject.toml, native section).",
    )


def _check_websearch() -> CheckResult:
    try:
        from tera_pilot.web_search_backend import get_websearch_status
        st = get_websearch_status() or {}
        active = st.get("active_backend") or ""
        if active:
            return CheckResult(
                name="websearch",
                status=STATUS_OK,
                detail=f"active search backend: {active}",
            )
        return CheckResult(
            name="websearch",
            status=STATUS_WARN,
            detail="search backend not configured (needs an MCP server with the search role)",
            hint="See .tera_pilot/skills/web-research/SKILL.md — connect an MCP search server "
                 "for the web_search/web_fetch tools.",
        )
    except Exception as e:
        return CheckResult(
            name="websearch",
            status=STATUS_WARN,
            detail=f"web search unavailable: {e}",
        )


def _check_workspace(project: Optional[str]) -> CheckResult:
    w = Path(project).expanduser() if project else Path.cwd()
    if not w.exists():
        return CheckResult(
            name="workspace",
            status=STATUS_FAIL,
            detail=f"path does not exist: {w}",
            hint="Point to an existing directory via --project.",
        )
    writable = os.access(w, os.W_OK)
    return CheckResult(
        name="workspace",
        status=STATUS_OK if writable else STATUS_FAIL,
        detail=f"{w} — {'writable' if writable else 'not writable'}",
        hint="" if writable else "Choose a working directory with write access.",
    )


# ── Report assembly ───────────────────────────────────────────────


def run_checks(project: Optional[str] = None) -> List[CheckResult]:
    """Run all checks. No network calls beyond localhost."""
    checks: List[CheckResult] = []
    checks.append(_check_python())
    checks.append(_check_package())
    checks.append(_check_config_dir())
    checks.extend(_check_dependencies())
    checks.extend(_check_providers())
    checks.append(_check_ollama())
    checks.append(_check_lmstudio())
    checks.append(_check_native())
    checks.append(_check_websearch())
    checks.append(_check_workspace(project))
    return checks


def build_json_report(checks: List[CheckResult]) -> Dict[str, Any]:
    """Machine-readable report (schema v1) — for CI and scripts."""
    by_status = {STATUS_OK: 0, STATUS_WARN: 0, STATUS_FAIL: 0}
    for c in checks:
        by_status[c.status] = by_status.get(c.status, 0) + 1
    return {
        "schema_version": 1,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "product": "tera-pilot",
        "ready": by_status[STATUS_FAIL] == 0,
        "counts": by_status,
        "checks": [c.to_dict() for c in checks],
    }


def print_human_report(checks: List[CheckResult]) -> None:
    """Human-readable report (rich, with a plain fallback)."""
    try:
        from rich.console import Console
        from rich.table import Table
    except Exception:
        _print_plain(checks)
        return

    console = Console()
    table = Table(title="Tera Pilot — Environment Doctor", header_style="bold", show_lines=False)
    table.add_column("Status", width=8, justify="center")
    table.add_column("Check", style="bold")
    table.add_column("Detail")
    icons = {
        STATUS_OK: "[green]✓ ok[/green]",
        STATUS_WARN: "[yellow]⚠ warn[/yellow]",
        STATUS_FAIL: "[red]✗ fail[/red]",
    }
    for c in checks:
        table.add_row(icons.get(c.status, c.status), c.name, c.detail)
    console.print(table)
    console.print()
    for c in checks:
        if c.status != STATUS_OK and c.hint:
            arrow = {"warn": "[yellow]→[/yellow]", "fail": "[red]→[/red]"}.get(c.status, "→")
            console.print(f"  {arrow} [b]{c.name}[/b]: {c.hint}")
    console.print()
    fails = [c for c in checks if c.status == STATUS_FAIL]
    warns = [c for c in checks if c.status == STATUS_WARN]
    if fails:
        console.print("[red]Blocking issues found. Fix them and run doctor again.[/red]")
    elif warns:
        console.print("[bold]Ready to work[/bold] (with non-critical warnings).")
    else:
        console.print("[green]All set — launch tera-pilot![/green]")


def _print_plain(checks: List[CheckResult]) -> None:
    icons = {STATUS_OK: "✓", STATUS_WARN: "!", STATUS_FAIL: "✗"}
    print("Tera Pilot — Environment Doctor")
    print("=" * 60)
    for c in checks:
        print(f"  [{icons.get(c.status, '?')}] {c.name}: {c.detail}")
        if c.status != STATUS_OK and c.hint:
            print(f"      → {c.hint}")
    fails = [c for c in checks if c.status == STATUS_FAIL]
    print("=" * 60)
    print("FAIL" if fails else "READY")


def run_doctor(json_output: bool = False, project: Optional[str] = None) -> int:
    """Run doctor. Returns the exit code (0 — ready, 1 — has fail)."""
    checks = run_checks(project=project)
    if json_output:
        print(json.dumps(build_json_report(checks), ensure_ascii=False, indent=2))
    else:
        print_human_report(checks)
    return 0 if all(c.status != STATUS_FAIL for c in checks) else 1


def run_doctor_cli(argv: Optional[List[str]] = None) -> int:
    """CLI entry point: tera-pilot doctor [--json] [--project DIR]."""
    args = list(argv or [])
    json_output = "--json" in args
    project: Optional[str] = None
    if "--project" in args:
        i = args.index("--project")
        if i + 1 < len(args):
            project = args[i + 1]
    return run_doctor(json_output=json_output, project=project)
