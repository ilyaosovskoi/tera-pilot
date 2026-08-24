"""
Tera Pilot — A native, local-first AI IDE.

v2.0.0: Major refactoring.
       - New `tera_pilot.agent` package with v2 agent runtime (AgentRuntimeV2).
         Wraps the legacy AgentRuntime and adds:
         * asyncio ChatStateActor (no-lock state ownership, ported from
           Grok Build's xai-chat-state pattern)
         * InterjectionBuffer (mid-turn user messages, drained at safe points)
         * Optional OS-level sandbox (Landlock/Seatbelt via Rust extension)
         * Three-tier compaction (intra/inter/code, ported from Grok Build's
           xai-grok-compaction crate)
         * Circuit breaker around provider calls (sliding-window, per-key
           registry, replaces heuristic rate-limit string matching)
         * Sub-agent v2 with toolset-level read-only guarantee (built-in
           `explore` / `plan` / `general-purpose`)
         * EncryptedPromptStore (ChaCha20-Poly1305, for enterprise deployment)
         * ACP server endpoint (tera-pilot-acp command, for IDE integration)
       - New `tera-pilot-native/` Cargo workspace with PyO3 bindings exposing
         the Rust subsystems to Python. Pure-Python fallbacks are used
         automatically when the native extension is not built.
       - Refactored `agent_runtime.py` to be legacy alias (file unchanged
         from v1.3.0, just re-tagged as legacy). New code should prefer
         `tera_pilot.agent.AgentRuntimeV2`.
       - Fixed smoke_tests.py:
         * Removed hardcoded Gemini API key (REVOKED and rotated).
         * Read API key from $GEMINI_API_KEY env var.
         * Fixed `task_result.metadata['tool_calls']` bug — tool calls
           live in `task_result.tool_calls` (List[ToolCall]), each with
           a `.name` (ToolName enum), not a dict.
         * Tests are skipped (not failed) if no API key or provider error.

v1.3.0: Kimi Code-inspired agent architecture rewrite.
       - Modular agent loop: TurnLoop (stateless turns) + ToolScheduler
         (parallel tool execution with resource-conflict detection)
       - SubagentHost + SubagentBatch: rate-limited, resumable subagent
         scheduling with projected history (ported from Kimi Code)
       - Multi-level compaction: FullCompaction (LLM-summarized) +
         MicroCompaction (lightweight tool-result truncation) + media-aware
         content stripping
       - Working SwarmMode with toggle and auto-exit (manual/task/tool)
       - Progressive tool disclosure: select_tools meta-tool loads tool
         definitions on demand, reducing prompt size for simple tasks
       - CancelToken: AbortSignal-pattern cooperative cancellation
       - Apple Design-inspired GUI enhancements (SKILL-2.md): spring
         animations, translucent materials, accessibility (reduced motion,
         reduced transparency, high contrast), instant press feedback
       - All Tera Pilot-unique features preserved: command whitelist, path
         sandbox, diff-review gate, role-based subagent tool whitelists,
         STALL/REPEAT watchdog, autonomy levels, activity log
v1.2.0: Office Worker section released — new office_* tool family.
v1.1.0: Heavy Code section released (multi-agent + subagents + 10/day free),
       MCP support, expanded Agent settings GUI.

Tera Pilot-unique features preserved in v2.0:
- Multi-provider support (16 providers + AutoRouter)
- Web UI (HTTP server + browser, replaces Qt)
- Office Worker section (.docx/.xlsx/.pptx generation)
- Local-first philosophy (Ollama/LM Studio default, no telemetry)
- Slash commands (Markdown-based)
- Memory service (human-readable tera_pilot_memory.md with JSON metadata)
"""

__version__ = "2.3.7"
__all__ = ["__version__"]
