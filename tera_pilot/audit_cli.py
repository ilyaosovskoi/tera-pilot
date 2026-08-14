"""
Audit export & verification CLI — ``tera-pilot audit`` (P0).

    tera-pilot audit export [--out PATH] [--unsigned]
    tera-pilot audit verify PATH

Export
    Пишет текущий activity log как JSON. По умолчанию — в подписанном
    формате (Ed25519 + hash chain через ``tera_pilot.audit_signing``),
    чтобы подмену / переупорядочивание / удаление записей можно было
    детектировать. ``--unsigned`` — старый плоский формат.

    Важно: activity log живёт в памяти процесса. В свежем CLI-процессе
    он пуст — для экспорта реальной активности используйте команды
    внутри запущенного TUI/Web (slash-команды /audit, /audit-signed),
    либо запускайте export из того же процесса, где работал агент.

Verify
    Проверяет подписи и цепочку хешей экспортированного файла.
    Код возврата: 0 — цепочка цела, 1 — обнаружена подмена.

Формат и криптография описаны в ``tera_pilot/audit_signing.py`` и в
``THREAT_MODEL.md`` (раздел «Верификация и доказательства»).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def export_entries(
    entries: List[Dict[str, Any]],
    out_path: str,
    unsigned: bool = False,
) -> Tuple[int, int]:
    """Экспортировать список записей в файл. Возвращает (exit_code, count).

    ``unsigned=False`` — подписанный формат через ``audit_signing``
    (генерирует ключи при первом вызове, кладёт их в ~/.tera_pilot/).
    """
    if unsigned:
        data = json.dumps(entries, indent=2, default=str, ensure_ascii=False)
    else:
        from tera_pilot.audit_signing import export_signed_json
        data = export_signed_json(entries)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(data, encoding="utf-8")
    return 0, len(entries)


def _cmd_export(out: Optional[str], unsigned: bool) -> int:
    from tera_pilot.activity_log import get_activity_log
    log = get_activity_log()
    try:
        if unsigned:
            data = log.export_json()
            fmt = "unsigned"
        else:
            data = log.export_signed_json()
            fmt = "signed (Ed25519 + hash chain)"
    except Exception as e:
        print(f"[audit] подписанный экспорт недоступен ({e}); использую unsigned")
        data = log.export_json()
        fmt = "unsigned (fallback)"

    if not out:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = str(Path.home() / ".tera_pilot" / f"audit_export_{stamp}.json")
    dest = Path(out)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(data, encoding="utf-8")
    count = len(json.loads(data))
    print(f"[audit] экспортировано записей: {count} ({fmt})")
    print(f"[audit] файл: {out}")
    if count == 0:
        print("[audit] note: activity log — process-scoped; в свежем CLI-процессе он пуст. "
              "Экспортируйте внутри запущенного TUI/Web (slash-команды /audit, /audit-signed).")
    return 0


def _cmd_verify(path: str) -> int:
    from tera_pilot.audit_signing import verify_signed_file
    report = verify_signed_file(path)
    print(f"[audit] проверено записей: {report.entries_checked}")
    if report.ok:
        print(f"[audit] OK — подписей проверено: {report.signatures_valid}, цепочка хешей цела.")
        return 0
    print(f"[audit] НАРУШЕНИЕ — {report.first_failure or 'неизвестная ошибка'}"
          + (f" (запись #{report.first_failure_index})" if report.first_failure_index is not None else ""))
    return 1


def _print_usage() -> None:
    print("Tera Pilot audit CLI")
    print("Команды:")
    print("  tera-pilot audit export [--out PATH] [--unsigned]   экспорт activity log (по умолчанию signed)")
    print("  tera-pilot audit verify PATH                        проверить подписи и цепочку хешей")


def run_audit_cli(argv: Optional[List[str]] = None) -> int:
    """CLI-точка входа: tera-pilot audit <export|verify> ..."""
    args = list(argv if argv is not None else sys.argv[1:])
    if not args:
        _print_usage()
        return 2
    sub = args[0]
    if sub == "export":
        rest = args[1:]
        unsigned = "--unsigned" in rest
        out: Optional[str] = None
        if "--out" in rest:
            i = rest.index("--out")
            if i + 1 < len(rest):
                out = rest[i + 1]
        return _cmd_export(out, unsigned)
    if sub == "verify":
        if len(args) < 2:
            print("usage: tera-pilot audit verify <file>")
            return 2
        return _cmd_verify(args[1])
    _print_usage()
    return 2
