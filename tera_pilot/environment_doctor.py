"""
Environment Doctor — ``tera-pilot doctor`` (P0 onboarding).

Одна команда, которая отвечает на вопрос «готова ли эта машина запустить
Tera Pilot?»:

    tera-pilot doctor                 # человекочитаемый отчёт
    tera-pilot doctor --json          # machine-readable JSON (для CI / скриптов)
    tera-pilot doctor --project DIR   # проверить конкретную рабочую директорию

Статусы проверок:

    ok    — готово к работе
    warn  — работает, но чего-то не хватает или что-то опционально
            (например, не запущен локальный Ollama/LM Studio, не заданы
            облачные ключи, не собрано Rust-ускорение)
    fail  — блокирует нормальную работу (Python < 3.11, отсутствует
            критичная зависимость, недоступная рабочая директория)

Код возврата: 0 — если нет ни одного ``fail``, иначе 1. Предупреждения
сами по себе не валят проверку: полностью локальная конфигурация без
облачных ключей — это валидный сетап.

См. также ``THREAT_MODEL.md`` — сетевые пробы здесь ограничены
localhost-эндпоинтами локальных моделей и ничего не отправляют наружу.
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

# Критичные зависимости — без них Tera Pilot не запустится.
CORE_DEPS = [
    ("pydantic", "конфигурация и типы"),
    ("textual", "полноэкранный TUI (tera-pilot-tui)"),
    ("requests", "HTTP-клиент (провайдеры, daemon, web search)"),
    ("aiohttp", "HTTP-клиент (стриминг)"),
    ("toml", "конфигурация (TOML)"),
    ("yaml", "конфигурация (YAML)"),
    ("rich", "вывод в терминале"),
]

# Опциональные зависимости — без них деградируют отдельные секции.
OPTIONAL_DEPS = [
    ("cryptography", "подписанный аудит (Ed25519)"),
    ("docx", "Office: .docx"),
    ("openpyxl", "Office: .xlsx"),
    ("pptx", "Office: .pptx"),
]


@dataclass
class CheckResult:
    """Результат одной проверки doctor."""

    name: str
    status: str          # ok | warn | fail
    detail: str = ""
    hint: str = ""       # что сделать, если статус не ok

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── Отдельные проверки ──────────────────────────────────────────────


def _check_python() -> CheckResult:
    v = sys.version_info
    ok = (v.major, v.minor) >= (3, 11)
    return CheckResult(
        name="python",
        status=STATUS_OK if ok else STATUS_FAIL,
        detail=f"Python {v.major}.{v.minor}.{v.micro} (требуется >= 3.11)",
        hint="" if ok else "Установите Python 3.11+ и перезапустите doctor.",
    )


def _check_package() -> CheckResult:
    try:
        import tera_pilot
        return CheckResult(
            name="package",
            status=STATUS_OK,
            detail=f"tera_pilot v{tera_pilot.__version__} импортируется из {tera_pilot.__file__}",
        )
    except Exception as e:
        return CheckResult(
            name="package",
            status=STATUS_FAIL,
            detail=f"не удалось импортировать tera_pilot: {e}",
            hint="Установите пакет: pip install -e . (из корня проекта)",
        )


def _check_config_dir() -> CheckResult:
    from tera_pilot.utils import get_tera_pilot_dir
    try:
        d = get_tera_pilot_dir()
    except Exception as e:
        return CheckResult(
            name="config_dir",
            status=STATUS_FAIL,
            detail=f"~/.tera_pilot недоступен: {e}",
            hint="Проверьте права на домашнюю директорию.",
        )
    writable = os.access(d, os.W_OK)
    files = sorted(p.name for p in d.iterdir()) if d.exists() else []
    detail = f"~/.tera_pilot — файлы: {', '.join(files) if files else 'пусто (создастся при первом запуске)'}"
    return CheckResult(
        name="config_dir",
        status=STATUS_OK if writable else STATUS_WARN,
        detail=detail,
        hint="" if writable else "Нет прав на запись в ~/.tera_pilot — исправьте права.",
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
                detail=f"{mod} не установлен — {purpose}",
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
                detail=f"{mod} не установлен — {purpose}",
                hint=f"pip install {mod} (опционально)",
            ))
        else:
            out.append(CheckResult(name=f"dep:{mod}", status=STATUS_OK, detail=f"{mod} — {purpose}"))
    return out


def _provider_env_vars() -> Dict[str, str]:
    """provider_id -> имя env-переменной ключа (из зарегистрированных провайдеров)."""
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
        detail = "ключи найдены"
        if set_vars:
            detail += " · env: " + ", ".join(sorted(set_vars.values()))
        if cfg_keys:
            detail += " · config.json: " + ", ".join(sorted(cfg_keys))
        out.append(CheckResult(name="providers:keys", status=STATUS_OK, detail=detail))
    else:
        out.append(CheckResult(
            name="providers:keys",
            status=STATUS_WARN,
            detail="API-ключи не найдены ни в окружении, ни в ~/.tera_pilot/config.json",
            hint="Настройте провайдера в UI (Settings → Providers) либо задайте env-переменную "
                 "ключа (например OPENAI_API_KEY). Для локальных моделей ключи не нужны.",
        ))
    return out


def _probe_http(url: str, timeout: float = 2.0) -> Tuple[bool, str]:
    """Лёгкая проба localhost-эндпоинта. Ничего не отправляет наружу."""
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
        detail="Ollama отвечает на 127.0.0.1:11434" if ok else "Ollama не запущен на 127.0.0.1:11434",
        hint="" if ok else "Запустите `ollama serve` или используйте облачного провайдера.",
    )


def _check_lmstudio() -> CheckResult:
    ok, _ = _probe_http("http://127.0.0.1:1234/v1/models")
    return CheckResult(
        name="lmstudio",
        status=STATUS_OK if ok else STATUS_WARN,
        detail="LM Studio отвечает на 127.0.0.1:1234" if ok else "LM Studio не запущен на 127.0.0.1:1234",
        hint="" if ok else "Запустите LM Studio с включённым local server.",
    )


def _check_native() -> CheckResult:
    try:
        from tera_pilot.agent.native import NATIVE_AVAILABLE
    except Exception as e:
        return CheckResult(
            name="native",
            status=STATUS_WARN,
            detail=f"не удалось проверить native-ускорение: {e}",
        )
    if NATIVE_AVAILABLE:
        return CheckResult(
            name="native",
            status=STATUS_OK,
            detail="Rust-ускорение активно (tera_pilot_native)",
        )
    return CheckResult(
        name="native",
        status=STATUS_WARN,
        detail="Rust-ускорение не установлено — используются pure-Python fallback'и",
        hint="Опционально: соберите tera-pilot-native (см. pyproject.toml, секция native).",
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
                detail=f"активный search-бэкенд: {active}",
            )
        return CheckResult(
            name="websearch",
            status=STATUS_WARN,
            detail="search-бэкенд не настроен (нужен MCP-сервер с ролью search)",
            hint="См. .tera_pilot/skills/web-research/SKILL.md — подключите MCP search-сервер "
                 "для web_search/web_fetch инструментов.",
        )
    except Exception as e:
        return CheckResult(
            name="websearch",
            status=STATUS_WARN,
            detail=f"web search недоступен: {e}",
        )


def _check_workspace(project: Optional[str]) -> CheckResult:
    w = Path(project).expanduser() if project else Path.cwd()
    if not w.exists():
        return CheckResult(
            name="workspace",
            status=STATUS_FAIL,
            detail=f"путь не существует: {w}",
            hint="Укажите существующую директорию через --project.",
        )
    writable = os.access(w, os.W_OK)
    return CheckResult(
        name="workspace",
        status=STATUS_OK if writable else STATUS_FAIL,
        detail=f"{w} — {'можно писать' if writable else 'нет прав на запись'}",
        hint="" if writable else "Выберите рабочую директорию с правами на запись.",
    )


# ── Сборка отчёта ───────────────────────────────────────────────────


def run_checks(project: Optional[str] = None) -> List[CheckResult]:
    """Выполнить все проверки. Никаких сетевых вызовов за пределы localhost."""
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
    """Machine-readable отчёт (схема v1) — для CI и скриптов."""
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
    """Человекочитаемый отчёт (rich, с fallback на plain)."""
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
        console.print("[red]Найдены блокирующие проблемы. Устраните их и запустите doctor ещё раз.[/red]")
    elif warns:
        console.print("[bold]Готово к работе[/bold] (есть некритичные предупреждения).")
    else:
        console.print("[green]Всё готово — можно запускать tera-pilot![/green]")


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
    """Запустить doctor. Возвращает код выхода (0 — готово, 1 — есть fail)."""
    checks = run_checks(project=project)
    if json_output:
        print(json.dumps(build_json_report(checks), ensure_ascii=False, indent=2))
    else:
        print_human_report(checks)
    return 0 if all(c.status != STATUS_FAIL for c in checks) else 1


def run_doctor_cli(argv: Optional[List[str]] = None) -> int:
    """CLI-точка входа: tera-pilot doctor [--json] [--project DIR]."""
    args = list(argv or [])
    json_output = "--json" in args
    project: Optional[str] = None
    if "--project" in args:
        i = args.index("--project")
        if i + 1 < len(args):
            project = args[i + 1]
    return run_doctor(json_output=json_output, project=project)
