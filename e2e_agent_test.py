#!/usr/bin/env python3
"""Real end-to-end agent test — runs the actual AgentRuntime on a task.

Reads provider config + API key from ~/.tera_pilot/config.json (the same
source the TUI / web UI / daemon use). To use Google AI Studio:

    1. Get a key:  https://aistudio.google.com/apikey
    2. Either paste it into ~/.tera_pilot/config.json → providers.gemini.api_key
       (and set "active_provider": "gemini"), or export GOOGLE_API_KEY=...
    3. Run:
         python3 e2e_agent_test.py "Refactor X" --workspace /path/to/project
         python3 e2e_agent_test.py "Explain this repo"   # uses cwd

Prints every agent step (thought / tool call / result) plus the final
answer, iteration count, token usage and cost — so we can judge quality
and catch failures.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tera_pilot.agent_runtime import AgentEvent  # noqa: E402
from tera_pilot.daemon import _build_registry  # noqa: E402


def _print_step(kind: str, data: dict) -> None:
    """Pretty-print agent events to stdout for the real-run log."""
    try:
        kind = AgentEvent(kind).value if kind in AgentEvent._value2member_map_ else kind
    except Exception:
        pass

    if kind == AgentEvent.THOUGHT.value:
        text = (data.get("thought") or data.get("text") or "").strip()
        if text:
            print(f"\n  💭 {text}")
    elif kind == AgentEvent.TOOL_CALLED.value:
        tool = data.get("tool") or data.get("name") or "?"
        args = data.get("args") or data.get("arguments") or {}
        if isinstance(args, dict):
            args_str = ", ".join(f"{k}={str(v)[:80]}" for k, v in args.items())
        else:
            args_str = str(args)[:200]
        print(f"\n  🔧 {tool}({args_str})")
    elif kind == AgentEvent.TOOL_RESULT.value:
        res = data.get("result") or data.get("output") or data.get("text") or ""
        err = data.get("error")
        snippet = str(res)[:300].replace("\n", " ")
        if err:
            print(f"  ⚠️  tool error: {str(err)[:200]}")
        elif snippet:
            print(f"  📥 {snippet}")
    elif kind == AgentEvent.ITERATION_START.value:
        print(f"\n── Iteration {data.get('iteration', '?')} ──")
    elif kind == AgentEvent.ERROR.value:
        print(f"\n  ❌ {data}")
    elif kind == AgentEvent.DONE.value:
        pass  # printed separately
    elif kind == AgentEvent.PLAN_CREATED.value:
        print(f"\n  🗺  Plan: {str(data.get('plan') or data)[:200]}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Real agent end-to-end test")
    parser.add_argument("prompt", help="Task for the agent")
    parser.add_argument("--workspace", default=None,
                        help="Project directory (default: cwd)")
    parser.add_argument("--provider", default=None,
                        help="Force provider id, e.g. gemini (default: config)")
    parser.add_argument("--max-iterations", type=int, default=8)
    parser.add_argument("--verbose", action="store_true",
                        help="Print full tool results")
    args = parser.parse_args()

    workspace = args.workspace or os.getcwd()
    print(f"⚙️  Workspace: {workspace}")

    # 1. Build the registry from ~/.tera_pilot/config.json
    registry = _build_registry()

    # 2. Optional env-var override (GOOGLE_API_KEY etc.)
    key = os.environ.get("GOOGLE_API_KEY")
    if key:
        from tera_pilot.providers import ProviderConfig
        registry.configure("gemini", ProviderConfig(
            provider_id="gemini",
            model="gemini-2.5-pro",
            api_key=key,
        ))
        registry.set_active("gemini")

    provider = args.provider
    if provider:
        try:
            registry.set_active(provider)
        except Exception as e:
            print(f"❌ Cannot activate provider '{provider}': {e}")
            return 1

    active = registry.active_id
    prov = registry.get(active)
    if not (prov.config.api_key or os.environ.get(
            getattr(prov, "env_var", ""), "")):
        print(f"❌ No API key for active provider '{active}'.")
        print("   Paste it into ~/.tera_pilot/config.json → providers."
              f"{active}.api_key, or export {getattr(prov, 'env_var', active.upper() + '_API_KEY')}.")
        return 1
    print(f"🤖 Provider: {active} · model: {prov.config.model or prov.default_model}")

    # 3. Build the agent and run
    from tera_pilot.agent_runtime import AgentRuntime
    from tera_pilot.token_tracker import get_token_tracker

    agent = AgentRuntime(
        registry=registry,
        workspace=workspace,
        max_iterations=args.max_iterations,
        enable_planning=True,
        on_event=_print_step,
        token_tracker=get_token_tracker(),
        verbose=args.verbose,
    )

    print(f"\n🚀 Running: {args.prompt}\n" + "=" * 60)
    t0 = time.time()
    try:
        result = agent.run(args.prompt)
    except KeyboardInterrupt:
        print("\n⏹  Interrupted by user")
        return 130
    duration = time.time() - t0

    print("\n" + "=" * 60)
    if result.error:
        print(f"❌ ERROR: {result.error}")
    print(f"✅ Success: {result.success}  ·  iterations: {result.iterations}  ·  "
          f"{duration:.1f}s")
    print("\n── FINAL ANSWER ──")
    print(result.output)
    print("─" * 60)

    try:
        stats = agent.get_token_stats()
        print(f"Tokens: in={stats.get('total_tokens_in', 0)} "
              f"out={stats.get('total_tokens_out', 0)} "
              f"cost=${stats.get('total_cost_usd', 0.0):.4f}")
    except Exception as e:
        print(f"(token stats unavailable: {e})")

    print(f"\n{'✅ AGENT RAN OK' if result.success else '❌ AGENT FAILED'}")
    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())
