"""ACP (Agent Client Protocol) server endpoint.

Ported from Grok Build's `xai-acp-lib` design. Allows IDEs (Zed, Cursor, etc.)
to connect to Tera Pilot via the open Agent Client Protocol and drive the agent
over stdio or HTTP.

This is a minimal implementation: it speaks the subset of ACP that's
necessary for editor integration:
- session/new, session/load, session/info
- prompt/send (turn-driven message send)
- turn/cancel
- session/update (server -> client stream)

For the full ACP spec see: https://github.com/agent-client-protocol/agent-client-protocol

NOTE: This is the minimal viable ACP server. It does not implement the
reverse-request `x.ai/mcp/sdk_call` bridge that Grok Build has — that
requires a more complete MCP-over-ACP implementation.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)

ACP_PROTOCOL_VERSION = "0.10.4"


@dataclass
class ACPSession:
    session_id: str
    cwd: str
    last_prompt: Optional[str] = None
    turn_in_flight: bool = False


class ACPServer:
    """Minimal ACP server. Speaks JSON-RPC 2.0 over stdio.

    Usage:
        server = ACPServer(runtime_factory=my_runtime_factory)
        await server.run_stdio()
    """

    def __init__(
        self,
        runtime_factory: Callable[[str, str], Awaitable[Any]],
        *,
        cwd: Optional[str] = None,
    ):
        """
        Args:
            runtime_factory: async callable(session_id: str, cwd: str) -> AgentRuntime.
                Called when a new session is created.
            cwd: default working directory if not specified in session/new.
        """
        self._runtime_factory = runtime_factory
        self._default_cwd = cwd
        self._sessions: Dict[str, ACPSession] = {}
        self._runtimes: Dict[str, Any] = {}

    async def run_stdio(self) -> None:
        """Run the ACP server over stdio. Blocks until stdin closes."""
        logger.info("ACP server starting on stdio (protocol=%s)", ACP_PROTOCOL_VERSION)
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await asyncio.get_event_loop().connect_read_pipe(lambda: protocol, __import__("sys").stdin)

        writer_transport, writer_protocol = await asyncio.get_event_loop().connect_write_pipe(
            asyncio.streams.FlowControlMixin,
            __import__("sys").stdout,
        )
        writer = asyncio.StreamWriter(writer_transport, writer_protocol, reader, asyncio.get_event_loop())

        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                line = line.decode("utf-8").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError as e:
                    await self._send_error(writer, None, -32700, f"parse error: {e}")
                    continue
                await self._handle_message(writer, msg)
        finally:
            logger.info("ACP server shutting down")

    async def _handle_message(self, writer: asyncio.StreamWriter, msg: Dict[str, Any]) -> None:
        method = msg.get("method")
        msg_id = msg.get("id")
        params = msg.get("params", {})

        try:
            if method == "initialize":
                await self._send_result(writer, msg_id, {
                    "protocolVersion": ACP_PROTOCOL_VERSION,
                    "serverCapabilities": {
                        "session": True,
                        "prompt": True,
                        "turnCancel": True,
                    },
                })
            elif method == "session/new":
                session_id = await self._create_session(params)
                await self._send_result(writer, msg_id, {"sessionId": session_id})
            elif method == "session/load":
                session_id = params.get("sessionId")
                if not session_id or session_id not in self._sessions:
                    await self._send_error(writer, msg_id, -32602, "session not found")
                    return
                await self._send_result(writer, msg_id, {"sessionId": session_id})
            elif method == "session/info":
                session_id = params.get("sessionId")
                if not session_id or session_id not in self._sessions:
                    await self._send_error(writer, msg_id, -32602, "session not found")
                    return
                await self._send_result(writer, msg_id, self._session_info(session_id))
            elif method == "prompt/send":
                await self._handle_prompt(writer, msg_id, params)
            elif method == "turn/cancel":
                session_id = params.get("sessionId")
                if session_id and session_id in self._runtimes:
                    rt = self._runtimes[session_id]
                    if hasattr(rt, "tools"):
                        rt.tools.cancel("ACP turn/cancel")
                await self._send_result(writer, msg_id, {"ok": True})
            else:
                await self._send_error(writer, msg_id, -32601, f"method not found: {method}")
        except Exception as e:
            logger.exception("ACP method %s failed", method)
            await self._send_error(writer, msg_id, -32603, f"internal error: {e}")

    async def _create_session(self, params: Dict[str, Any]) -> str:
        import uuid
        session_id = str(uuid.uuid4())
        cwd = params.get("cwd") or self._default_cwd or "."
        self._sessions[session_id] = ACPSession(session_id=session_id, cwd=cwd)
        # Build the runtime asynchronously.
        self._runtimes[session_id] = await self._runtime_factory(session_id, cwd)
        logger.info("ACP session created: %s (cwd=%s)", session_id, cwd)
        return session_id

    def _session_info(self, session_id: str) -> Dict[str, Any]:
        s = self._sessions[session_id]
        return {
            "sessionId": s.session_id,
            "cwd": s.cwd,
            "turnInFlight": s.turn_in_flight,
            "lastPrompt": s.last_prompt,
        }

    async def _handle_prompt(
        self, writer: asyncio.StreamWriter, msg_id: Any, params: Dict[str, Any]
    ) -> None:
        session_id = params.get("sessionId")
        if not session_id or session_id not in self._runtimes:
            await self._send_error(writer, msg_id, -32602, "session not found")
            return
        prompt = params.get("prompt", "")
        s = self._sessions[session_id]
        s.last_prompt = prompt
        s.turn_in_flight = True

        # Acknowledge.
        await self._send_result(writer, msg_id, {"ok": True})

        # Run the turn asynchronously and stream updates.
        rt = self._runtimes[session_id]
        try:
            # Stream events back as session/update notifications.
            async for event in _run_and_stream(rt, prompt):
                await self._send_notification(writer, "session/update", {
                    "sessionId": session_id,
                    "event": event,
                })
        finally:
            s.turn_in_flight = False
            await self._send_notification(writer, "session/update", {
                "sessionId": session_id,
                "event": {"type": "turn_end"},
            })

    async def _send_result(self, writer: asyncio.StreamWriter, msg_id: Any, result: Any) -> None:
        if msg_id is None:
            return
        msg = {"jsonrpc": "2.0", "id": msg_id, "result": result}
        writer.write((json.dumps(msg) + "\n").encode("utf-8"))
        await writer.drain()

    async def _send_error(self, writer: asyncio.StreamWriter, msg_id: Any, code: int, message: str) -> None:
        if msg_id is None:
            return
        msg = {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}
        writer.write((json.dumps(msg) + "\n").encode("utf-8"))
        await writer.drain()

    async def _send_notification(self, writer: asyncio.StreamWriter, method: str, params: Any) -> None:
        msg = {"jsonrpc": "2.0", "method": method, "params": params}
        writer.write((json.dumps(msg) + "\n").encode("utf-8"))
        await writer.drain()


async def _run_and_stream(runtime: Any, prompt: str):
    """Run a turn on the runtime and yield events as dicts.

    This expects the runtime to expose an async generator method `run_stream(prompt)`.
    The legacy `AgentRuntime` doesn't have this; v2 `AgentRuntimeV2` does.
    """
    if hasattr(runtime, "run_stream"):
        async for event in runtime.run_stream(prompt):
            yield event
    else:
        # Fallback: run synchronously and yield a single result.
        result = runtime.run(prompt)
        yield {"type": "result", "result": str(result)}


def cli_main(argv=None) -> int:
    """CLI entry point: `tera-pilot-acp` — run the ACP server over stdio.

    Reads TERA_PILOT_WORKSPACE env var for the default working directory.

    Usage from an IDE that supports ACP:
        command: tera-pilot-acp
        args: (none)
        cwd: /path/to/project

    For backwards compatibility, `--mcp-server` (and any `--workspace` /
    `--allow-writes` flags) are dispatched to the MCP server mode
    (tera_pilot.mcp_server), matching the documented
    `tera-pilot-acp --mcp-server --workspace /path` usage.
    """
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "--mcp-server":
        from tera_pilot.mcp_server import main as mcp_main
        # mcp_server.main() parses sys.argv itself and does not know the
        # `--mcp-server` dispatch flag — drop it before delegating.
        sys.argv = [sys.argv[0], *args[1:]]
        return mcp_main()
    # v2.3.4-security (P0.4): --no-confirm opts into headless auto-
    # approve; without it side-effecting agent actions are blocked.
    if "--no-confirm" in args:
        os.environ["TERA_PILOT_ACP_NO_CONFIRM"] = "1"
        args.remove("--no-confirm")

    import asyncio
    import os
    import sys

    logging.basicConfig(
        level=os.environ.get("TERA_PILOT_ACP_LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,  # stdout is the ACP wire; logs go to stderr
    )

    workspace = os.environ.get("TERA_PILOT_WORKSPACE", os.getcwd())

    async def runtime_factory(session_id: str, cwd: str):
        """Construct a fresh runtime per ACP session."""
        # Lazy import to avoid importing PySide6 etc. for headless ACP use.
        from tera_pilot.providers import ProviderRegistry, ProviderConfig
        from tera_pilot.agent_runtime import AgentRuntime

        registry = ProviderRegistry()
        registry.load_from_config(os.path.expanduser("~/.tera_pilot/config.json"))

        runtime = AgentRuntime(
            registry=registry,
            workspace=cwd,
            max_iterations=15,
            enable_planning=True,
        )
        runtime.tools.diff_review_enabled = False  # ACP has no UI for diff review
        runtime.tools._diff_review_callback = None
        # v2.3.4-security (P0.4): ACP has no confirmation UI — side-
        # effecting actions fail CLOSED unless the user explicitly opts in
        # via TERA_PILOT_ACP_NO_CONFIRM=1 (or `--no-confirm`). No silent
        # fail-open.
        no_confirm = os.environ.get("TERA_PILOT_ACP_NO_CONFIRM", "") == "1"
        if no_confirm:
            runtime.tools.headless_confirm = "allow"
        return runtime

    server = ACPServer(runtime_factory=runtime_factory, cwd=workspace)
    try:
        asyncio.run(server.run_stdio())
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as e:
        logger.exception("ACP server crashed: %s", e)
        return 1


if __name__ == "__main__":
    import sys
    sys.exit(cli_main())
