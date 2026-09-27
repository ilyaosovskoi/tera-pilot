"""
Smart routing + failover (LOCAL PROTOTYPE — not committed).

Two behaviours on top of ``auto_router`` (which already classifies
task complexity and builds tiered fallback chains, but only *plans*):

1. **Failover at call time.** When the active provider errors or a
   call exceeds the per-call timeout, the run continues on the next
   healthy candidate instead of dying after same-provider retries.
   Every outcome (latency / error / timeout) feeds a process-wide
   health store, so a flapping provider is skipped for a cooldown and
   a chronically slow one sinks to the back of the line.

2. **Cheap-first for simple tasks.** For trivial/simple prompts the
   reachable local providers (Ollama / LM Studio / local endpoint)
   move to the front of the candidate list — no cloud cost, no data
   leaving the machine, usually faster for small work.

The fast local gate (complexity heuristic, <1ms, no network) runs
before every expensive call — the same System-1-before-System-2 idea
as tiny on-device decision models: a cheap classifier decides *where*
to spend the expensive inference.

Timeouts abandon the attempt thread (daemon thread, result ignored)
and move on — documented, not hidden: attempts are logged with the
outcome and latency of every candidate tried.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

LOCAL_PROVIDER_IDS = ("ollama", "lmstudio", "local")
LOCAL_SIMPLE_COMPLEXITIES = ("trivial", "simple")

_DEFAULTS = {
    "router_smart_failover": False,
    "router_local_first": True,
    "router_slow_secs": 120.0,
    "router_call_timeout_s": 180.0,
    "router_max_failovers": 3,
    "router_down_after_errors": 3,
    "router_down_cooldown_s": 300.0,
    "router_probe_ttl_s": 60.0,
}


def load_smart_config() -> Dict[str, Any]:
    """Read router_* keys from config.json over built-in defaults."""
    cfg = dict(_DEFAULTS)
    try:
        from tera_pilot.utils import load_config
        saved = load_config() or {}
        for key in _DEFAULTS:
            if key in saved:
                cfg[key] = saved[key]
    except Exception as exc:
        logger.debug("[smart] config load failed: %s", exc)
    return cfg


# ── Health store ────────────────────────────────────────────────────

@dataclass
class ProviderHealth:
    provider_id: str
    ema_latency_s: float = 0.0
    consec_errors: int = 0
    slow_trips: int = 0
    last_ok_ts: float = 0.0
    last_error_ts: float = 0.0
    last_latency_s: float = 0.0
    calls: int = 0


class HealthStore:
    """Process-wide, thread-safe provider health. Never raises."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._health: Dict[str, ProviderHealth] = {}

    def _get(self, pid: str) -> ProviderHealth:
        entry = self._health.get(pid)
        if entry is None:
            entry = ProviderHealth(provider_id=pid)
            self._health[pid] = entry
        return entry

    def record_success(self, pid: str, latency_s: float) -> None:
        try:
            with self._lock:
                h = self._get(pid)
                h.calls += 1
                h.last_latency_s = latency_s
                h.last_ok_ts = time.time()
                h.consec_errors = 0
                alpha = 0.3
                h.ema_latency_s = latency_s if h.ema_latency_s <= 0 else (
                    alpha * latency_s + (1 - alpha) * h.ema_latency_s)
        except Exception:
            pass

    def record_error(self, pid: str, kind: str = "error") -> None:
        try:
            with self._lock:
                h = self._get(pid)
                h.calls += 1
                h.consec_errors += 1
                h.last_error_ts = time.time()
                if kind in ("timeout", "slow"):
                    h.slow_trips += 1
        except Exception:
            pass

    def status(self, pid: str, cfg: Optional[Dict[str, Any]] = None) -> str:
        """ok | slow | down. Down recovers after the cooldown (half-open)."""
        cfg = cfg or _DEFAULTS
        try:
            with self._lock:
                h = self._health.get(pid)
                if h is None:
                    return "ok"
                now = time.time()
                if (h.consec_errors >= int(cfg.get("router_down_after_errors", 3))
                        and now - h.last_error_ts < float(cfg.get("router_down_cooldown_s", 300.0))):
                    return "down"
                if (h.ema_latency_s > float(cfg.get("router_slow_secs", 120.0))
                        and h.calls >= 2):
                    return "slow"
                return "ok"
        except Exception:
            return "ok"

    def snapshot(self, cfg: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        cfg = cfg or _DEFAULTS
        try:
            with self._lock:
                items = list(self._health.values())
            out = []
            for h in items:
                out.append({
                    "provider_id": h.provider_id,
                    "status": self.status(h.provider_id, cfg),
                    "ema_latency_s": round(h.ema_latency_s, 1),
                    "last_latency_s": round(h.last_latency_s, 1),
                    "consec_errors": h.consec_errors,
                    "slow_trips": h.slow_trips,
                    "calls": h.calls,
                })
            return sorted(out, key=lambda d: d["provider_id"])
        except Exception:
            return []

    def reset_for_test(self) -> None:
        with self._lock:
            self._health.clear()


_HEALTH: Optional[HealthStore] = None
_HEALTH_LOCK = threading.Lock()


def get_health_store() -> HealthStore:
    global _HEALTH
    if _HEALTH is None:
        with _HEALTH_LOCK:
            if _HEALTH is None:
                _HEALTH = HealthStore()
    return _HEALTH


# ── Local reachability probes ───────────────────────────────────────

_probe_cache: Dict[str, Tuple[float, bool]] = {}
_probe_lock = threading.Lock()


def _default_base(pid: str) -> str:
    if pid == "ollama":
        return "http://localhost:11434"
    if pid == "lmstudio":
        return "http://localhost:1234/v1"
    return ""


def local_reachable(pid: str, api_base: str = "", timeout: float = 1.0,
                    cfg: Optional[Dict[str, Any]] = None) -> bool:
    """Cheap HTTP probe: is the local server actually up? Cached ~60s.

    Timeout defaults to 1s (localhost answers in ms; the probe runs
    synchronously inside candidate ordering, so 2s × 3 locals would
    stall the first turn for seconds when nothing is running)."""
    if pid not in LOCAL_PROVIDER_IDS:
        return False
    base = (api_base or "").rstrip("/") or _default_base(pid)
    if not base:
        return False
    url = base + ("/api/tags" if pid == "ollama" else "/models")
    ttl = float((cfg or _DEFAULTS).get("router_probe_ttl_s", 60.0))
    key = f"{pid}@{url}"
    now = time.time()
    with _probe_lock:
        hit = _probe_cache.get(key)
        if hit is not None and now - hit[0] < ttl:
            return hit[1]
    ok = False
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "tera-pilot-probe"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ok = 200 <= resp.status < 300
    except Exception:
        ok = False
    with _probe_lock:
        _probe_cache[key] = (now, ok)
    return ok


def reset_probe_cache_for_test() -> None:
    with _probe_lock:
        _probe_cache.clear()


# ── Candidate ordering ──────────────────────────────────────────────

@dataclass
class Candidate:
    provider_id: str
    model: str = ""
    reason: str = ""


def prompt_text(messages: Any, limit: int = 2000) -> str:
    """Last user content from a provider message list (for classification).

    Accepts ProviderMessage objects, dicts (``{"role": .., "content": ..}``)
    and tuples — the runtime and tests pass a mix."""
    def _role(msg: Any) -> str:
        if isinstance(msg, dict):
            return str(msg.get("role", ""))
        return str(getattr(msg, "role", "") or "")

    def _content(msg: Any) -> str:
        if isinstance(msg, dict):
            return str(msg.get("content", "") or "")
        return str(getattr(msg, "content", "") or "")

    try:
        for msg in reversed(list(messages or [])):
            if _role(msg) == "user" and _content(msg):
                return _content(msg)[-limit:]
        if messages:
            return _content(messages[-1])[-limit:]
    except Exception:
        pass
    return ""


def order_candidates(prompt: str, registry: Any,
                     router: Any = None,
                     cfg: Optional[Dict[str, Any]] = None) -> Tuple[List[Candidate], Dict[str, Any]]:
    """Complexity + health + local-first ordering over AutoRouter tiers.

    Reuses ``AutoRouter.route()`` for complexity classification and the
    configured-provider filtering, then re-ranks: reachable locals first
    for trivial/simple prompts, down providers out, slow providers last.
    Returns (candidates, decision-info).
    """
    cfg = cfg or load_smart_config()
    health = get_health_store()
    if router is None:
        from tera_pilot.auto_router import AutoRouter
        router = AutoRouter()
    try:
        configured = {str(p.get("id")) for p in registry.list_providers()
                      if p.get("configured")}
    except Exception:
        configured = set()
    try:
        decision = router.route(prompt, configured_providers=configured or None)
    except Exception as exc:
        logger.debug("[smart] router.route failed: %s", exc)
        return [], {"complexity": "unknown", "error": str(exc)[:200]}
    complexity = str(decision.get("complexity", "unknown"))
    ordered: List[Candidate] = [Candidate(
        provider_id=str(decision.get("provider_id", "")),
        model=str(decision.get("model", "") or ""),
        reason=f"router pick ({complexity})",
    )]
    for fb in decision.get("fallbacks", []) or []:
        ordered.append(Candidate(
            provider_id=str(fb.get("provider_id", "")),
            model=str(fb.get("model", "") or ""),
            reason="router fallback",
        ))
    ordered = [c for c in ordered if c.provider_id]
    dropped_down: List[str] = []
    if ordered:
        kept = [c for c in ordered if health.status(c.provider_id, cfg) != "down"]
        if kept:
            kept_ids = {c.provider_id for c in kept}
            dropped_down = [c.provider_id for c in ordered if c.provider_id not in kept_ids]
            ordered = kept
    # Reachable locals are ALWAYS appended for cheap tasks when the tier
    # catalog has none (local providers only ship in the TRIVIAL tier,
    # but a full-context prompt usually classifies SIMPLE or higher).
    # This is the "simple → local, it's cheaper" rule: free, private,
    # and already running beats any cloud call for small work.
    try:
        present = {c.provider_id for c in ordered}
        if complexity in LOCAL_SIMPLE_COMPLEXITIES:
            for pid in LOCAL_PROVIDER_IDS:
                if pid in present or pid not in configured:
                    continue
                base = ""
                try:
                    existing = registry.get(pid).config
                    base = getattr(existing, "api_base", "") or ""
                    default_model = getattr(existing, "model", "") or ""
                except Exception:
                    default_model = ""
                if local_reachable(pid, base, cfg=cfg):
                    ordered.append(Candidate(
                        provider_id=pid, model=default_model,
                        reason="local fallback (reachable, free)"))
    except Exception as exc:
        logger.debug("[smart] local-append failed: %s", exc)
    # Local-first for cheap tasks: reachable local servers jump the queue.
    local_first_applied = False
    if (cfg.get("router_local_first", True)
            and complexity in LOCAL_SIMPLE_COMPLEXITIES):
        reachable: List[Candidate] = []
        rest: List[Candidate] = []
        for c in ordered:
            base = ""
            if c.provider_id in LOCAL_PROVIDER_IDS:
                try:
                    existing = registry.get(c.provider_id).config
                    base = getattr(existing, "api_base", "") or ""
                except Exception:
                    base = ""
                if local_reachable(c.provider_id, base, cfg=cfg):
                    c.reason += " + local-first (reachable, free)"
                    reachable.append(c)
                    continue
            rest.append(c)
        if reachable:
            ordered = reachable + rest
            local_first_applied = True
    # Slow providers sink to the back (stable, by provider id).
    slow_ids = {c.provider_id for c in ordered
                if health.status(c.provider_id, cfg) == "slow"}
    if slow_ids and len(slow_ids) < len(ordered):
        slow = [c for c in ordered if c.provider_id in slow_ids]
        for c in slow:
            c.reason += " + demoted (slow)"
        ordered = [c for c in ordered if c.provider_id not in slow_ids] + slow
    max_total = 1 + int(cfg.get("router_max_failovers", 3))
    return ordered[:max_total], {
        "complexity": complexity,
        "local_first_applied": local_first_applied,
        "dropped_down": dropped_down,
        "reasoning": str(decision.get("reasoning", "")),
    }


# ── Failover executor ───────────────────────────────────────────────

def _call_in_thread(generate_fn: Callable[[], Any],
                    timeout_s: float,
                    cancel_check: Optional[Callable[[], bool]] = None) -> Any:
    """Run generate_fn with a timeout. Abandoned threads are daemonized
    (documented leak, bounded: one per failed attempt)."""
    box: "queue.Queue[Any]" = queue.Queue(maxsize=1)

    def _target() -> None:
        try:
            box.put(("ok", generate_fn()))
        except Exception as exc:  # noqa: BLE001 — shipped to caller below
            box.put(("err", exc))

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    deadline = time.time() + max(1.0, timeout_s)
    while True:
        if cancel_check is not None:
            try:
                if cancel_check():
                    raise RuntimeError("cancelled by user")
            except RuntimeError:
                raise
            except Exception:
                pass
        try:
            kind, payload = box.get(timeout=min(0.25, max(0.0, deadline - time.time())))
            if kind == "ok":
                return payload
            raise payload
        except queue.Empty:
            if time.time() >= deadline:
                raise TimeoutError(f"provider call timed out after {timeout_s:.0f}s")


def generate_with_failover(
    registry: Any,
    messages: Any,
    candidates: List[Candidate],
    tools: Optional[List[Dict[str, Any]]] = None,
    timeout_s: float = 180.0,
    is_retryable: Optional[Callable[[Exception], bool]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
    on_switch: Optional[Callable[[Dict[str, Any]], None]] = None,
    cfg: Optional[Dict[str, Any]] = None,
) -> Tuple[Any, Dict[str, Any]]:
    """Try candidates in order; return (response, report).

    Non-retryable errors (auth, bad request) raise immediately — failing
    over on those just burns other providers' quota. Everything else
    (errors, timeouts) records health, marks the AutoRouter cache, and
    moves to the next candidate. Raises the last error if all fail.
    """
    cfg = cfg or _DEFAULTS
    health = get_health_store()
    attempts: List[Dict[str, Any]] = []
    last_exc: Optional[Exception] = None
    used: Dict[str, Any] = {}

    def _retryable(exc: Exception) -> bool:
        if isinstance(exc, (TimeoutError,)):
            return True
        if is_retryable is not None:
            try:
                return bool(is_retryable(exc))
            except Exception:
                pass
        msg = str(exc).lower()
        return "429" in msg or "500" in msg or "503" in msg or "timeout" in msg

    for index, cand in enumerate(candidates):
        if cancel_check is not None:
            try:
                if cancel_check():
                    raise RuntimeError("cancelled by user")
            except RuntimeError:
                raise
            except Exception:
                pass
        started = time.time()
        try:
            try:
                provider = registry.get(cand.provider_id)
            except Exception:
                provider = None
            if provider is None:
                # Routing miss (not in registry) — skip without touching
                # health: nothing actually failed.
                attempts.append({"provider_id": cand.provider_id, "model": cand.model,
                                 "index": index, "latency_s": 0.0,
                                 "outcome": "skipped",
                                 "error": "not in registry"})
                logger.info("[smart] skipping %s (not in registry)", cand.provider_id)
                continue

            def _gen(_p=provider, _m=cand.model) -> Any:
                kwargs: Dict[str, Any] = {}
                if tools:
                    kwargs["tools"] = tools
                if _m:
                    kwargs["model"] = _m
                try:
                    return _p.generate(messages, **kwargs)
                except TypeError as te:
                    # Narrow test doubles / legacy adapters that don't
                    # accept the tools kwarg: retry once without it.
                    if tools and "tools" in str(te).lower():
                        kwargs.pop("tools", None)
                        return _p.generate(messages, **kwargs)
                    raise

            resp = _call_in_thread(_gen, timeout_s, cancel_check)
            latency = time.time() - started
            health.record_success(cand.provider_id, latency)
            try:
                from tera_pilot.auto_router import get_auto_router
                get_auto_router().mark_provider_available(cand.provider_id, True)
            except Exception:
                pass
            used = {"provider_id": cand.provider_id, "model": cand.model,
                    "index": index, "latency_s": round(latency, 1)}
            attempts.append({**used, "outcome": "ok"})
            if index > 0 and on_switch is not None:
                try:
                    on_switch({"used": used, "attempts": attempts})
                except Exception:
                    pass
            logger.info("[smart] served by %s/%s in %.1fs (attempt %d)",
                        cand.provider_id, cand.model or "default", latency, index + 1)
            return resp, {"used": used, "attempts": attempts}
        except Exception as exc:  # noqa: BLE001 — classified below
            last_exc = exc
            latency = time.time() - started
            kind = "timeout" if isinstance(exc, TimeoutError) else "error"
            health.record_error(cand.provider_id, kind)
            try:
                from tera_pilot.auto_router import get_auto_router
                get_auto_router().mark_provider_available(cand.provider_id, False)
            except Exception:
                pass
            attempts.append({"provider_id": cand.provider_id, "model": cand.model,
                             "index": index, "latency_s": round(latency, 1),
                             "outcome": kind, "error": str(exc)[:200]})
            logger.warning("[smart] candidate %s/%s failed (%s, %.1fs): %s",
                           cand.provider_id, cand.model or "default",
                           kind, latency, str(exc)[:160])
            if isinstance(exc, RuntimeError) and "cancelled" in str(exc).lower():
                raise
            if not _retryable(exc):
                logger.warning("[smart] non-retryable — not failing over: %s",
                               str(exc)[:160])
                raise
            continue
    if last_exc is not None:
        raise last_exc
    summary = "; ".join(f"{a['provider_id']}:{a['outcome']}" for a in attempts) or "empty chain"
    raise RuntimeError(f"smart routing failed [{summary}]")


def describe_health(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = cfg or load_smart_config()
    return {"enabled": bool(cfg.get("router_smart_failover", False)),
            "local_first": bool(cfg.get("router_local_first", True)),
            "call_timeout_s": cfg.get("router_call_timeout_s"),
            "slow_secs": cfg.get("router_slow_secs"),
            "max_failovers": cfg.get("router_max_failovers"),
            "health": get_health_store().snapshot(cfg)}
