"""TUI-backed task runner for automation integrations.

This module is deliberately not a second user-facing CLI.  The Textual
application remains the interactive product; this small adapter lets CI,
GitHub Actions, and other integrations call the same ``TeraPilotBridge`` backend
without exposing the old command-oriented ``tera-pilot-cli`` product.

The returned envelope implements the machine-readable backend report
contract (schema v1, see ``backend_report_schema.json`` and
``backend_report.validate_report``). Tool arguments are never exported
because MCP and command payloads can contain secrets; ``final_output`` and
``test_result.output`` are potentially sensitive.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .bridge import TeraPilotBridge, ProviderChoice


def _json_safe(value: Any) -> Any:
    """Recursively coerce values into JSON-serializable types.

    Report metadata can come from runtime internals (enums, Paths, sets,
    datetimes...). A report that fails ``json.dumps`` is useless to CI, so
    we sanitize defensively instead of crashing.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(v) for v in value]
    if hasattr(value, "value"):  # enums and simple value objects
        return _json_safe(value.value)
    return str(value)


def run_test_command(
    command: List[str],
    *,
    workspace: str,
    timeout_sec: int = 300,
) -> Dict[str, Any]:
    """Run a test command inside the workspace and return test_result.

    The output is truncated defensively — it can be large and sensitive.
    """
    try:
        proc = subprocess.run(
            command,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        return {
            "command": command,
            "passed": proc.returncode == 0,
            "exit_code": proc.returncode,
            "output": output[:20_000],
        }
    except subprocess.TimeoutExpired:
        return {
            "command": command,
            "passed": False,
            "exit_code": None,
            "output": f"timed out after {timeout_sec}s",
        }
    except FileNotFoundError as e:
        return {
            "command": command,
            "passed": False,
            "exit_code": None,
            "output": f"command not found: {e}",
        }


def run_task(
    prompt: str,
    *,
    workspace: Optional[str] = None,
    provider_id: Optional[str] = None,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    api_base: Optional[str] = None,
    section: str = "general",
    max_iterations: int = 8,
    enable_planning: bool = True,
    test_command: Optional[List[str]] = None,
    audit_ref: Optional[str] = None,
) -> Dict[str, Any]:
    """Run one task through the same backend used by the TUI.

    Returns a schema-v1 report (see ``backend_report.validate_report``).
    ``test_command`` (optional) runs inside the workspace after the agent
    turn and is recorded as ``test_result``. ``audit_ref`` (optional) links
    the report to a signed audit export.
    """
    from .backend_report import validate_report

    start = time.monotonic()
    provider = ProviderChoice(
        provider_id=provider_id,
        model=model,
        api_key=api_key,
        api_base=api_base,
    )
    bridge = TeraPilotBridge(
        workspace=workspace,
        provider=provider,
        section=section,
        max_iterations=max_iterations,
        enable_planning=enable_planning,
    )
    result = bridge.run_prompt(prompt)
    duration_sec = round(time.monotonic() - start, 3)

    tool_calls: List[Dict[str, Any]] = []
    verification_status = "not_requested"
    for call in getattr(result, "tool_calls", []) or []:
        name = getattr(getattr(call, "name", None), "value", None) or str(
            getattr(call, "name", "")
        )
        if name == "self_verify":
            verification_status = "failed" if getattr(call, "error", None) else "unknown"
            call_result = str(getattr(call, "result", "") or "")
            if call_result:
                verification_status = (
                    "failed"
                    if "[SELF-VERIFY ERROR]" in call_result
                    or "[SELF-VERIFY FAILED]" in call_result
                    else "ran"
                )
        tool_calls.append({
            "tool": name,
            "error": getattr(call, "error", None),
            "duration_ms": round(float(getattr(call, "duration_ms", 0.0) or 0.0), 3),
        })

    active_provider = provider_id
    active_model = model
    try:
        registry = bridge.ensure_agent()._registry
        active_provider = getattr(registry, "active_id", None) or active_provider
        for provider_info in registry.list_providers():
            if provider_info.get("id") == active_provider or provider_info.get("active"):
                active_provider = provider_info.get("id") or active_provider
                active_model = (
                    provider_info.get("model")
                    or provider_info.get("default_model")
                    or active_model
                )
                break
    except Exception:
        # The task result is still useful even if provider introspection fails.
        pass

    # Usage metadata from the bridge's token tracker (best-effort).
    tokens = 0
    cost_usd = 0.0
    try:
        usage = bridge.status()
        tokens = int(usage.get("tokens", 0) or 0)
        cost_usd = round(float(usage.get("cost", 0.0) or 0.0), 6)
    except Exception:
        pass

    success = bool(getattr(result, "success", False))
    error = getattr(result, "error", None)
    if success:
        status = "success"
    elif error:
        status = "error"
    else:
        status = "failed"

    test_result = None
    if test_command:
        test_result = run_test_command(
            test_command, workspace=str(Path(workspace or ".").resolve())
        )

    report = {
        "schema_version": 1,
        "ok": success,
        "status": status,
        "workspace": str(Path(workspace or ".").resolve()),
        "provider": active_provider,
        "model": active_model,
        "iterations": int(getattr(result, "iterations", 0) or 0),
        "duration_sec": duration_sec,
        "tokens": tokens,
        "cost_usd": cost_usd,
        "tools": tool_calls,
        "verification": {
            "self_verify_called": verification_status != "not_requested",
            "status": verification_status,
        },
        "test_result": test_result,
        # Potentially sensitive — the agent's final answer.
        "final_output": getattr(result, "output", "") or "",
        "error": error,
        "audit_ref": audit_ref,
        "metadata": _json_safe(getattr(result, "metadata", {}) or {}),
    }
    # Backwards-compatible aliases for existing consumers.
    report["tool_calls"] = report["tools"]
    report["output"] = report["final_output"]

    # Validate before returning — a malformed report must not reach CI.
    validate_report(report)
    return report


def report_to_json(report: Dict[str, Any]) -> str:
    """Serialize a report to JSON, guaranteeing JSON-safety."""
    return json.dumps(_json_safe(report), ensure_ascii=False, indent=2)
