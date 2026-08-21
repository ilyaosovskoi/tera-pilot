"""
Web search backend (G18) — MCP-first search + ordered fallback + direct fetch.

This module is the single place where Tera Pilot reaches the internet. It is
deliberately small and has zero third-party dependencies beyond what
``requirements.txt`` already lists (urllib for fetch; MCPManager for
search). Three responsibilities:

1. ``run_web_search(query, num_results)`` — search the web. Routes
   through ``MCPManager`` (reuses the existing ~/.tera_pilot/mcp.json config
   + process lifecycle). Uses an **ordered-fallback** pattern: if the
   primary configured search backend is unavailable, fall back to the
   next one. Whichever backend actually served the request is recorded
   and returned so the agent + audit trail can see which one won.

2. ``fetch_url_as_text(url, max_chars)`` — direct HTTP GET + HTML-to-
   text extraction with stdlib urllib. No new dependency. Caps the
   returned text at ``max_chars``.

3. ``get_websearch_status()`` — doctor-style health visibility: which
   backend is active, whether the last probe succeeded, why the
   primary was skipped (if a fallback was used). Surfaced via the
   ``/websearch status`` slash command.

Zero-config path: a default ``~/.tera_pilot/mcp.json`` template entry for a
working no-API-key search server is documented in
``.tera_pilot/skills/web-research/SKILL.md`` and shipped as
``docs/mcp_search_template.json`` (not force-installed — the user
opts in by copying it to ``~/.tera_pilot/mcp.json``).

Zero-telemetry: nothing here phones home to Tera Pilot's own servers (it
doesn't have any). The only network traffic is the search/fetch
request the user explicitly triggered via a tool call.
"""

from __future__ import annotations

import gzip
import html
import ipaddress
import logging
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── Backend health tracking ────────────────────────────────────────────
# Tracks the last probe result for each backend so /websearch status can
# report "primary failed because X, fell back to Y". Process-scoped
# (cleared on restart) — we don't persist this, it's just a live snapshot.

@dataclass
class BackendHealth:
    """Health snapshot for one search backend."""
    name: str
    last_probe_ok: bool = False
    last_probe_ts: float = 0.0
    last_probe_error: str = ""
    last_served_ts: float = 0.0  # last time this backend actually served a request

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "last_probe_ok": self.last_probe_ok,
            "last_probe_ts": self.last_probe_ts,
            "last_probe_error": self.last_probe_error,
            "last_served_ts": self.last_served_ts,
        }


_health: Dict[str, BackendHealth] = {}
_last_active_backend: str = ""
_last_status_msg: str = ""


def _record_probe(name: str, ok: bool, error: str = "") -> None:
    h = _health.get(name)
    if h is None:
        h = BackendHealth(name=name)
        _health[name] = h
    h.last_probe_ok = ok
    h.last_probe_ts = time.time()
    h.last_probe_error = error


def _record_served(name: str) -> None:
    global _last_active_backend
    h = _health.get(name)
    if h is None:
        h = BackendHealth(name=name)
        _health[name] = h
    h.last_served_ts = time.time()
    _last_active_backend = name


def get_websearch_status() -> Dict[str, Any]:
    """Return a status dict for the ``/websearch status`` command.

    Reports:
    - active_backend: which backend served the most recent successful
      request (or "" if no request has succeeded yet).
    - backends: per-backend health (last probe ok/error/ts, last served ts).
    - last_status_msg: human-readable summary of the most recent
      search attempt (e.g. "primary 'exa' failed: not running; fell
      back to 'duckduckgo'").
    - mcp_servers: list of MCP servers from MCPManager (so the user
      can see what's configured without opening mcp.json).
    """
    try:
        from tera_pilot.mcp_manager import get_mcp_manager
        manager = get_mcp_manager()
        mcp_servers = manager.list_servers()
    except Exception:
        mcp_servers = []
    return {
        "active_backend": _last_active_backend,
        "backends": {n: h.to_dict() for n, h in _health.items()},
        "last_status_msg": _last_status_msg,
        "mcp_servers": mcp_servers,
    }


# ── Search backend discovery ───────────────────────────────────────────
# We look for search-capable MCP servers in two ways:
# 1. Servers explicitly tagged with ``"role": "search"`` in mcp.json
#    (the user can add this to mark a server as the search backend).
# 2. Servers whose name or tool names contain "search" / "web" / "exa"
#    (heuristic — picks up common search MCP servers like Exa,
#    Tavily, Brave Search, ddg-search, etc.).

_SEARCH_NAME_HINTS = ("search", "web", "exa", "tavily", "brave", "ddg", "google")
_SEARCH_TOOL_HINTS = ("search", "web_search", "query", "fetch")


def _discover_search_backends() -> List[Tuple[str, str]]:
    """Return a list of (server_name, tool_name) tuples for search-
    capable MCP servers, in priority order.

    Priority:
    1. Test-injected backends (so unit tests don't need a real MCP
       server — see ``_inject_backend_for_test``).
    2. Servers with ``"role": "search"`` in mcp.json (explicit).
    3. Servers whose name contains a search hint.
    4. Servers exposing a tool whose name contains a search hint.
    """
    # Test backends first.
    if _test_backends:
        return [(name, "_test") for name in _test_backends.keys()]
    try:
        from tera_pilot.mcp_manager import get_mcp_manager
        manager = get_mcp_manager()
        catalog = manager.tool_catalog()
    except Exception as e:
        logger.debug("[web_search] MCPManager unavailable: %s", e)
        return []
    # Read mcp.json directly to check for the "role": "search" tag
    # (MCPManager doesn't expose arbitrary config fields).
    roles: Dict[str, str] = {}
    try:
        import json
        from pathlib import Path
        cfg_path = Path.home() / ".tera_pilot" / "mcp.json"
        if cfg_path.exists():
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f) or {}
            for name, srv in (cfg.get("servers") or {}).items():
                role = str((srv or {}).get("role", "")).lower().strip()
                if role:
                    roles[name] = role
    except Exception:
        pass

    explicit: List[Tuple[str, str]] = []
    by_name: List[Tuple[str, str]] = []
    by_tool: List[Tuple[str, str]] = []
    for server_name, tool in catalog:
        tool_name = (tool.name or "").lower()
        server_lower = server_name.lower()
        # Find the search-like tool name on this server.
        is_search_tool = any(h in tool_name for h in _SEARCH_TOOL_HINTS)
        is_search_server = any(h in server_lower for h in _SEARCH_NAME_HINTS)
        if roles.get(server_name) == "search":
            explicit.append((server_name, tool.name))
        elif is_search_server and is_search_tool:
            by_name.append((server_name, tool.name))
        elif is_search_tool:
            by_tool.append((server_name, tool.name))
    # Dedup while preserving order.
    seen: set = set()
    out: List[Tuple[str, str]] = []
    for pair in explicit + by_name + by_tool:
        if pair not in seen:
            seen.add(pair)
            out.append(pair)
    return out


def _probe_backend(server_name: str, tool_name: str) -> bool:
    """Cheap live probe: call the search tool with a 1-result query
    to confirm the backend actually works (not just that it's running).

    A channel that only checks ``shutil.which()`` / process-exists is
    not proof the backend actually works — the search index might be
    empty, the API key might be invalid, etc. We do a real (tiny)
    search and check we got a non-error response.
    """
    try:
        from tera_pilot.mcp_manager import get_mcp_manager
        manager = get_mcp_manager()
        # Use a trivial query that any search backend can answer.
        result = manager.call_tool(server_name, tool_name, {"query": "test", "num_results": 1})
        ok = isinstance(result, str) and not result.startswith("[MCP ERROR]")
        _record_probe(server_name, ok, "" if ok else (result[:200] if isinstance(result, str) else "no result"))
        return ok
    except Exception as e:
        _record_probe(server_name, False, str(e)[:200])
        return False


def _call_search_backend(
    server_name: str, tool_name: str, query: str, num_results: int,
) -> Tuple[List[Dict[str, Any]], str]:
    """Call one search backend. Returns (results, error_or_empty).

    Test backends registered via ``_inject_backend_for_test`` are
    checked first so unit tests can avoid hitting the real network.
    """
    # Test-backend override.
    if server_name in _test_backends:
        try:
            results = _test_backends[server_name](query, num_results)
            if results:
                _record_served(server_name)
                return results, ""
            _record_probe(server_name, False, "test backend returned no results")
            return [], "test backend returned no results"
        except Exception as e:
            _record_probe(server_name, False, f"test backend error: {e}")
            return [], str(e)
    try:
        from tera_pilot.mcp_manager import get_mcp_manager
        manager = get_mcp_manager()
        # Different MCP search servers accept slightly different arg
        # names — try the common ones in order.
        for args in (
            {"query": query, "num_results": num_results},
            {"query": query, "max_results": num_results},
            {"query": query, "limit": num_results},
            {"query": query, "count": num_results},
            {"query": query},
            {"q": query, "num": num_results},
            {"q": query},
        ):
            try:
                raw = manager.call_tool(server_name, tool_name, args)
            except Exception:
                continue
            if isinstance(raw, str) and raw.startswith("[MCP ERROR]"):
                continue
            parsed = _parse_search_response(raw)
            if parsed:
                _record_served(server_name)
                return parsed, ""
        # If we got here, none of the arg shapes worked.
        _record_probe(server_name, False, "no arg shape matched")
        return [], "no arg shape matched"
    except Exception as e:
        _record_probe(server_name, False, str(e)[:200])
        return [], str(e)


def _parse_search_response(raw: Any) -> List[Dict[str, Any]]:
    """Parse a search backend's response into a list of {title, url, snippet}.

    MCP search servers return wildly different shapes — JSON strings,
    plain text with URLs, structured objects. We try (in order):
    1. JSON parse → list of dicts with title/url/snippet fields.
    2. JSON parse → object with a results / items / data field.
    3. Regex extraction of URLs + surrounding text (best-effort).
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        return _normalise_results(raw)
    if isinstance(raw, dict):
        for key in ("results", "items", "data", "matches"):
            if key in raw and isinstance(raw[key], list):
                return _normalise_results(raw[key])
        return _normalise_results([raw])
    if isinstance(raw, str):
        # Try JSON parse first.
        try:
            import json
            parsed = json.loads(raw)
            return _parse_search_response(parsed)
        except Exception:
            pass
        # Fall back to URL regex extraction.
        return _extract_urls_from_text(raw)
    return []


def _normalise_results(items: List[Any]) -> List[Dict[str, Any]]:
    """Normalise a list of result items to {title, url, snippet} dicts."""
    out: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        # Find the URL field (common names).
        url = ""
        for k in ("url", "link", "href", "uri", "address"):
            v = item.get(k)
            if isinstance(v, str) and v.startswith("http"):
                url = v
                break
        if not url:
            continue
        # Find the title field.
        title = ""
        for k in ("title", "name", "heading"):
            v = item.get(k)
            if isinstance(v, str) and v.strip():
                title = v.strip()
                break
        # Find the snippet field.
        snippet = ""
        for k in ("snippet", "description", "summary", "text", "excerpt", "abstract"):
            v = item.get(k)
            if isinstance(v, str) and v.strip():
                snippet = v.strip()
                break
        out.append({"title": title, "url": url, "snippet": snippet})
    return out


def _extract_urls_from_text(text: str) -> List[Dict[str, Any]]:
    """Best-effort: extract URLs + surrounding text from a plain-text
    search response. Used when the MCP server returns unstructured text
    instead of JSON.
    """
    out: List[Dict[str, Any]] = []
    seen: set = set()
    # Match http(s) URLs.
    for m in re.finditer(r"https?://[^\s<>\"]+", text):
        url = m.group(0).rstrip(".,;)")
        if url in seen:
            continue
        seen.add(url)
        # Grab ~80 chars of context before the URL as a snippet.
        start = max(0, m.start() - 80)
        snippet = text[start:m.start()].strip().replace("\n", " ")
        out.append({"title": url.split("//")[-1].split("/")[0], "url": url, "snippet": snippet})
    return out


# ── Public search entry point ──────────────────────────────────────────


def run_web_search(
    query: str, num_results: int = 5,
) -> Tuple[List[Dict[str, Any]], str]:
    """Search the web via the configured MCP search backend(s).

    Ordered-fallback: try the primary search backend first. If it's
    unavailable or returns no results, try the next configured
    backend. Whichever backend actually served the request is
    recorded and returned as the second tuple element (``served_by``).

    Returns:
        (results, served_by) — results is a list of {title, url, snippet}
        dicts; served_by is the backend name that produced them (or ""
        if none succeeded).
    """
    global _last_status_msg
    backends = _discover_search_backends()
    if not backends:
        _last_status_msg = (
            "no search MCP backend configured. Add a search server to "
            "~/.tera_pilot/mcp.json — see .tera_pilot/skills/web-research/SKILL.md "
            "for a no-API-key template."
        )
        return [], ""
    errors: List[str] = []
    for server_name, tool_name in backends:
        # Probe (cheap) — but don't refuse to call if the probe failed,
        # because the probe might be wrong. Just call and see.
        results, err = _call_search_backend(server_name, tool_name, query, num_results)
        if results:
            _last_status_msg = f"served by {server_name}.{tool_name}"
            return results, server_name
        errors.append(f"{server_name}.{tool_name}: {err or 'no results'}")
    _last_status_msg = "all search backends failed — " + "; ".join(errors[:3])
    return [], ""


# ── Direct fetch (no MCP) ──────────────────────────────────────────────


# User-Agent — some sites reject requests with the default Python UA.
_DEFAULT_UA = "Mozilla/5.0 (compatible; TeraPilotAgent/2.1; +https://github.com/ilyaosovskoi/tera-pilot)"
_FETCH_TIMEOUT = 15.0
_MAX_REDIRECTS = 5


# ── SSRF-safe URL validation (P0.2) ────────────────────────────────────
# Closing the redirect / private-IP / DNS-rebinding class of SSRF in
# web_fetch:
#
#   * literal loopback/private/link-local/metadata addresses (IPv4 + IPv6)
#     are rejected up front (ToolEngine pre-check) AND at fetch time;
#   * the hostname is resolved and EVERY resolved address is checked, so
#     a hostname that DNS-resolves to an internal address (DNS rebinding)
#     is rejected before we connect;
#   * every redirect hop is re-validated with the same rules — a 302 to
#     localhost, to a private IP, or to a hostname resolving internally is
#     blocked before it is followed.
#
# Best-effort, not a full OS sandbox: urllib re-resolves the host when it
# connects (a TOCTOU window remains), but the check runs immediately
# before the request and on every redirect hop, which closes the practical
# classes of SSRF the agent could hit.


def _classify_ip(ip_str: str) -> Optional[str]:
    """Classify an IP literal as 'loopback' / 'private' / 'link_local' /
    'unspecified' / 'multicast' / 'reserved', or None when it is a
    public address. Handles IPv4 and IPv6 (including IPv4-mapped IPv6
    like ``::ffff:127.0.0.1``, which is classified by its embedded IPv4).

    Cloud metadata endpoints (169.254.169.254) fall under 'link_local';
    IPv6 unique-local (fc00::/7) and link-local (fe80::/10) ranges are
    covered by ipaddress' is_private / is_link_local.
    """
    ip_str = (ip_str or "").strip()
    if not ip_str:
        return None
    low = ip_str.lower()
    # IPv4-mapped IPv6 — classify by the embedded IPv4 address.
    if low.startswith("::ffff:") and "." in ip_str:
        try:
            return _classify_ip(ip_str.rsplit(":", 1)[1])
        except Exception:
            pass
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return None
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link_local"  # 169.254.0.0/16 incl. cloud metadata; fe80::/10
    if ip.is_unspecified:
        return "unspecified"  # 0.0.0.0 / :: — "this host", never a valid target
    if ip.is_private:
        return "private"  # 10/8, 172.16/12, 192.168/16; IPv6 fc00::/7
    if ip.is_multicast:
        return "multicast"
    if ip.is_reserved:
        return "reserved"
    return None


def _looks_like_internal_host(host: str) -> bool:
    """Cheap, DNS-free check whether a hostname is an internal address.

    Used by the tool-engine pre-check for a fast rejection BEFORE any
    network access: literal loopback/private/link-local IPs (IPv4 + IPv6)
    and ``localhost``-style names. Hostnames that only *resolve* to an
    internal address are caught later by ``_check_fetch_target`` (which
    does the DNS lookup at fetch time).
    """
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return False
    if host in ("localhost", "localhost.localdomain"):
        return True
    if host.startswith("127."):
        # 127.x.x.x loopback range — also catches 127.0.0.1.evil.com-style
        # rebinding hostnames. Conservative, harmless to over-block.
        return True
    return _classify_ip(host) is not None


def _resolve_host_ips(host: str) -> List[str]:
    """Resolve a hostname to its IP addresses (IPv4 + IPv6).

    Module-level so tests can monkeypatch it to simulate DNS rebinding
    (a hostname that resolves to an internal address). Returns an empty
    list on resolution failure — the caller then rejects nothing (the
    request will fail on its own), which keeps legitimate fetches from
    being blocked by a transient resolver hiccup.
    """
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, OSError):
        return []
    ips: List[str] = []
    for info in infos:
        addr = info[4][0]
        # Strip IPv6 scope/zone ids (fe80::1%en0) — ipaddress can't parse them.
        if "%" in addr:
            addr = addr.split("%", 1)[0]
        if addr not in ips:
            ips.append(addr)
    return ips


def _check_fetch_target(url: str, _resolver: Optional[Callable[[str], List[str]]] = None) -> Optional[str]:
    """Return a rejection reason if ``url`` targets an internal address.

    Checks the scheme, the literal hostname, and — via DNS resolution —
    every IP the hostname resolves to (DNS-rebinding defense). Returns
    None when the target is acceptable.

    ``_resolver`` is an injectable ``host -> List[str]`` for tests; it
    defaults to ``_resolve_host_ips``.
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return "malformed URL"
    if parsed.scheme not in ("http", "https"):
        return f"only http(s) URLs are allowed (got {parsed.scheme!r})"
    try:
        host = (parsed.hostname or "").lower()
    except ValueError:
        return "malformed URL"
    if not host:
        return "URL has no host"
    if _looks_like_internal_host(host):
        return f"target {host!r} is an internal/loopback address"
    resolver = _resolver or _resolve_host_ips
    for ip in resolver(host):
        cat = _classify_ip(ip)
        if cat is not None:
            return (
                f"target {host!r} resolves to {ip} ({cat} address) — "
                f"refusing to fetch internal/loopback targets"
            )
    return None


class _SSRFSafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """HTTPRedirectHandler that re-validates every redirect target.

    A redirect can point anywhere — including back at localhost, a
    private network, or a cloud-metadata endpoint — so each hop is
    checked with the same SSRF rules as the initial request (including
    DNS resolution, catching redirects to hostnames that resolve
    internally). A blocked redirect raises ``urllib.error.URLError``
    with a message starting with 'redirect blocked:'.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        reason = _check_fetch_target(newurl)
        if reason is not None:
            raise urllib.error.URLError(f"redirect blocked: {reason}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_url_as_text(url: str, max_chars: int = 8000) -> Tuple[str, int, str]:
    """Fetch a URL and return (text, http_status, final_url).

    Uses stdlib urllib (no new dependency). Handles gzip/deflate
    encoding. Extracts text from HTML by stripping tags + collapsing
    whitespace. Caps the returned text at ``max_chars``.

    SSRF defense (P0.2): the URL is validated (scheme + literal
    internal addresses + DNS resolution) BEFORE the request is made, and
    every redirect hop is re-validated by :class:`_SSRFSafeRedirectHandler`.

    Raises ``urllib.error.URLError`` (or similar) on network errors or
    blocked targets — callers should catch and surface a friendly error
    string.
    """
    reason = _check_fetch_target(url)
    if reason is not None:
        raise urllib.error.URLError(f"blocked: {reason}")
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": _DEFAULT_UA,
            "Accept": "text/html, application/xhtml+xml, text/plain, */*;q=0.5",
            "Accept-Encoding": "gzip, deflate",
        },
    )
    # urllib's default redirect handler follows redirects — we want
    # to know the final URL so we can record it, AND to re-validate
    # every hop (SSRF-safe).
    opener = urllib.request.build_opener(_SSRFSafeRedirectHandler())
    try:
        resp = opener.open(req, timeout=_FETCH_TIMEOUT)
    except urllib.error.HTTPError as e:
        # HTTP error code — return it so the caller can show "HTTP 404" etc.
        return "", e.code, url
    status = resp.getcode() if hasattr(resp, "getcode") else 200
    final_url = resp.geturl() if hasattr(resp, "geturl") else url
    raw = resp.read()
    # Decode based on Content-Encoding.
    encoding = (resp.headers.get("Content-Encoding") or "").lower()
    if encoding == "gzip":
        try:
            raw = gzip.decompress(raw)
        except Exception:
            pass
    elif encoding == "deflate":
        try:
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
        except Exception:
            try:
                raw = zlib.decompress(raw)
            except Exception:
                pass
    # Determine content type.
    content_type = (resp.headers.get("Content-Type") or "").lower()
    # Decode bytes to text.
    charset = "utf-8"
    if "charset=" in content_type:
        charset = content_type.split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
    try:
        text = raw.decode(charset, errors="replace")
    except LookupError:
        text = raw.decode("utf-8", errors="replace")
    # Strip HTML if it looks like HTML.
    if "html" in content_type or text.lstrip().lower().startswith("<!doctype html") or text.lstrip().startswith("<html"):
        text = _html_to_text(text)
    # Collapse excessive whitespace.
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Cap at max_chars.
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n... [truncated, {len(text)} total chars]"
    return text, status, final_url


def _html_to_text(html_text: str) -> str:
    """Minimal HTML-to-text converter.

    We avoid pulling in BeautifulSoup (extra dep) — for the agent's
    use case (extracting the textual content of a page), stripping
    scripts/styles + tags + unescaping entities is enough. The result
    is NOT a perfect rendering, but it's good enough for the LLM to
    reason about.
    """
    # Remove script/style blocks entirely.
    html_text = re.sub(r"<script\b[^>]*>.*?</script>", "", html_text, flags=re.DOTALL | re.IGNORECASE)
    html_text = re.sub(r"<style\b[^>]*>.*?</style>", "", html_text, flags=re.DOTALL | re.IGNORECASE)
    html_text = re.sub(r"<!--.*?-->", "", html_text, flags=re.DOTALL)
    # Convert <br>, <p>, <div> breaks to newlines so paragraphing survives.
    html_text = re.sub(r"<br\s*/?>", "\n", html_text, flags=re.IGNORECASE)
    html_text = re.sub(r"</(p|div|li|h[1-6]|tr|table)>", "\n", html_text, flags=re.IGNORECASE)
    # Strip all remaining tags.
    html_text = re.sub(r"<[^>]+>", "", html_text)
    # Unescape HTML entities.
    html_text = html.unescape(html_text)
    return html_text


# ── Test helpers ────────────────────────────────────────────────────────
# These exist so unit tests can inject fake backends without monkey-
# patching MCPManager. Production code never calls them.

def _reset_health_for_test() -> None:
    """Clear all health + test-backend state — used by the test suite
    to isolate tests. Production code never calls this."""
    global _last_active_backend, _last_status_msg, _test_backends
    _health.clear()
    _last_active_backend = ""
    _last_status_msg = ""
    _test_backends = {}


def _inject_backend_for_test(name: str, search_fn) -> None:
    """Inject a fake search backend for testing.

    ``search_fn`` is a callable(query, num_results) -> List[dict] that
    returns results. The test suite uses this to avoid hitting the
    real network.
    """
    # We store the fake in a module-level dict that _call_search_backend
    # checks before falling back to MCPManager. This keeps the test
    # surface tiny.
    global _test_backends
    _test_backends[name] = search_fn
    _record_probe(name, True)


_test_backends: Dict[str, Any] = {}
