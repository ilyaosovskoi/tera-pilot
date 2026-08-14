"""
Tera Pilot v2.0.2 — Team Spend Dashboard (Goal M3).

**Problem (from CLAUDE.md):**
    "Aggregate ``token_history.jsonl`` by org. Useful for Enterprise gate."

    Individual users have ``~/.tera_pilot/token_history.jsonl`` (per-process
    token usage log). Enterprises need to **aggregate** these across:

      * Multiple users (e.g. the whole "platform-team" group)
      * Multiple machines (each Tera Pilot install writes its own jsonl)
      * Multiple sessions / chats within a single Tera Pilot install

    The dashboard answers questions like:
      * "How much did the platform team spend this month?"
      * "Which user is responsible for 80% of the cost?"
      * "Which provider is most cost-effective for us?"
      * "Are we on track to exceed our $X monthly team budget?"

**Design:**

1.  **Org membership.** A user is identified by their
    ``~/.tera_pilot/identity.json`` file, which carries:
        { "user_id": "usr_abc...", "name": "alice",
          "email": "alice@example.com", "team": "platform" }
    The ``team`` field is the org/grouping key. The dashboard reads
    the local identity and aggregates the local token_history.jsonl
    under that team.

2.  **Multi-source aggregation.** The dashboard accepts any number
    of *sources* — each source is either:
        (a) A local ``token_history.jsonl`` file path (the user's own),
        (b) A directory of ``*.jsonl`` files (e.g. a shared network
            drive where team members drop their history files), or
        (c) A URL (future enhancement — not in v2.0.2).

    For v2.0.2, only (a) and (b) are supported, and the aggregation
    is purely local (no network calls). This is the "Enterprise gate"
    foundation — the actual cross-machine sync is a deployment concern.

3.  **Reports.** ``TeamSpendDashboard.report()`` returns a
    ``TeamSpendReport`` with:
      * ``total_cost_usd``
      * ``total_tokens_in`` / ``total_tokens_out``
      * ``by_user``: list of per-user stats (sorted by cost desc)
      * ``by_provider``: list of per-provider stats
      * ``by_model``: list of per-model stats
      * ``by_day``: list of per-day stats (last 30 days)
      * ``team_budget_usd``: from config (optional)
      * ``team_budget_used_pct``: 0..100
      * ``top_consumer_user_id``: the user with the highest spend

4.  **Privacy.** No PII is added — only the ``user_id`` and ``name``
    from ``identity.json`` are propagated. Email is included only if
    the user explicitly opts in (``share_email: true`` in identity.json).
    The dashboard never transmits data — it produces a local report.

5.  **Export.** ``TeamSpendDashboard.export_report_json()`` and
    ``export_report_csv()`` for sharing with finance / ops.
"""

from __future__ import annotations

import csv
import datetime as _dt
import io
import json
import logging
import os
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── Identity ────────────────────────────────────────────────────────

IDENTITY_PATH = Path.home() / ".tera_pilot" / "identity.json"
TEAM_BUDGET_PATH = Path.home() / ".tera_pilot" / "team_budget.json"


@dataclass
class UserIdentity:
    """The local user's identity, for team aggregation."""
    user_id: str
    name: str = ""
    email: str = ""
    team: str = "default"
    share_email: bool = False

    def to_dict(self) -> Dict[str, Any]:
        # Only include email if the user opted in.
        d = {
            "user_id": self.user_id,
            "name": self.name,
            "team": self.team,
        }
        if self.share_email and self.email:
            d["email"] = self.email
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "UserIdentity":
        return cls(
            user_id=str(d.get("user_id") or _new_user_id()),
            name=str(d.get("name") or ""),
            email=str(d.get("email") or ""),
            team=str(d.get("team") or "default"),
            share_email=bool(d.get("share_email", False)),
        )


def _new_user_id() -> str:
    return "usr_" + uuid.uuid4().hex[:12]


def load_identity() -> UserIdentity:
    """Load the local user identity, creating a default one if absent."""
    try:
        if IDENTITY_PATH.exists():
            return UserIdentity.from_dict(
                json.loads(IDENTITY_PATH.read_text(encoding="utf-8"))
            )
    except Exception as e:
        logger.warning(f"[spend_dashboard] load_identity error: {e}")
    # Create a default identity.
    ident = UserIdentity(
        user_id=_new_user_id(),
        name=os.environ.get("USER") or os.environ.get("USERNAME") or "anonymous",
        team="default",
    )
    save_identity(ident)
    return ident


def save_identity(ident: UserIdentity) -> None:
    """Persist the local user identity to disk."""
    try:
        IDENTITY_PATH.parent.mkdir(parents=True, exist_ok=True)
        IDENTITY_PATH.write_text(
            json.dumps(ident.to_dict(), indent=2), encoding="utf-8"
        )
    except Exception as e:
        logger.warning(f"[spend_dashboard] save_identity error: {e}")


def set_team(team: str) -> UserIdentity:
    """Update the local user's team and persist."""
    ident = load_identity()
    ident.team = team
    save_identity(ident)
    return ident


# ── Team budget ─────────────────────────────────────────────────────

@dataclass
class TeamBudget:
    """Team-level monthly USD cap, set by an admin (or the user themselves)."""
    team: str
    monthly_usd: float = 0.0    # 0 = no cap
    alert_pct: float = 80.0     # warn at this % of monthly_usd

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TeamBudget":
        return cls(
            team=str(d.get("team") or "default"),
            monthly_usd=float(d.get("monthly_usd", 0.0) or 0.0),
            alert_pct=float(d.get("alert_pct", 80.0) or 80.0),
        )


def load_team_budget(team: str = "default") -> TeamBudget:
    """Load the team budget; returns a zero-cap default if absent."""
    try:
        if TEAM_BUDGET_PATH.exists():
            data = json.loads(TEAM_BUDGET_PATH.read_text(encoding="utf-8"))
            # File may contain a single TeamBudget or a dict of team -> budget
            if "team" in data:
                return TeamBudget.from_dict(data)
            if team in data:
                return TeamBudget.from_dict({"team": team, **data[team]})
    except Exception as e:
        logger.warning(f"[spend_dashboard] load_team_budget error: {e}")
    return TeamBudget(team=team)


def save_team_budget(budget: TeamBudget) -> None:
    """Persist the team budget to disk."""
    try:
        TEAM_BUDGET_PATH.parent.mkdir(parents=True, exist_ok=True)
        existing = {}
        if TEAM_BUDGET_PATH.exists():
            existing = json.loads(TEAM_BUDGET_PATH.read_text(encoding="utf-8"))
        if "team" in existing and "monthly_usd" in existing:
            # Convert single-budget file to multi-team dict
            existing = {existing.get("team", "default"): existing}
        existing[budget.team] = budget.to_dict()
        TEAM_BUDGET_PATH.write_text(
            json.dumps(existing, indent=2), encoding="utf-8"
        )
    except Exception as e:
        logger.warning(f"[spend_dashboard] save_team_budget error: {e}")


# ── Aggregated report ───────────────────────────────────────────────

@dataclass
class UserSpendRow:
    user_id: str
    name: str
    team: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    request_count: int = 0
    last_active_iso: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ProviderSpendRow:
    provider: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    request_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ModelSpendRow:
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    request_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class DaySpendRow:
    date: str  # YYYY-MM-DD
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    request_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TeamSpendReport:
    team: str
    total_cost_usd: float = 0.0
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    total_request_count: int = 0
    by_user: List[UserSpendRow] = field(default_factory=list)
    by_provider: List[ProviderSpendRow] = field(default_factory=list)
    by_model: List[ModelSpendRow] = field(default_factory=list)
    by_day: List[DaySpendRow] = field(default_factory=list)
    team_budget_usd: float = 0.0
    team_budget_used_pct: float = 0.0
    top_consumer_user_id: str = ""
    sources_scanned: int = 0
    entries_processed: int = 0
    generated_at_iso: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d


# ── Dashboard ───────────────────────────────────────────────────────

class TeamSpendDashboard:
    """Aggregates token_history.jsonl entries into a team spend report.

    The dashboard is read-only: it never modifies the source files.
    """

    def __init__(
        self,
        sources: Optional[List[Path]] = None,
        identity: Optional[UserIdentity] = None,
        team_budget: Optional[TeamBudget] = None,
    ) -> None:
        # Default sources: the local user's token_history.jsonl.
        if sources is None:
            default_path = Path.home() / ".tera_pilot" / "token_history.jsonl"
            sources = [default_path] if default_path.exists() else []
        self._sources = sources
        self._identity = identity or load_identity()
        self._team_budget = team_budget or load_team_budget(self._identity.team)
        self._lock = threading.RLock()

    # ── Configuration ───────────────────────────────────────────

    def add_source(self, path: Path) -> None:
        """Add a token_history.jsonl source (file or directory of *.jsonl)."""
        with self._lock:
            if path not in self._sources:
                self._sources.append(path)

    def list_sources(self) -> List[str]:
        return [str(p) for p in self._sources]

    # ── Report generation ───────────────────────────────────────

    def report(self, days: int = 30) -> TeamSpendReport:
        """Aggregate every source into a single TeamSpendReport.

        ``days`` limits the per-day breakdown to the last N days. The
        totals (total_cost_usd, by_user, by_provider, by_model) cover
        ALL entries in the sources, not just the last N days — this
        matches what a finance team would want to see.
        """
        cutoff_ts = time.time() - (days * 86400.0)
        team = self._identity.team

        by_user: Dict[str, UserSpendRow] = {}
        by_provider: Dict[str, ProviderSpendRow] = {}
        by_model: Dict[str, ModelSpendRow] = {}
        by_day: Dict[str, DaySpendRow] = {}

        total_cost = 0.0
        total_in = 0
        total_out = 0
        total_reqs = 0
        sources_scanned = 0
        entries_processed = 0

        for source in self._sources:
            try:
                files = self._expand_source(source)
            except Exception as e:
                logger.warning(f"[spend_dashboard] cannot expand source {source}: {e}")
                continue
            for f in files:
                sources_scanned += 1
                try:
                    with open(f, "r", encoding="utf-8") as fh:
                        for line in fh:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                entry = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            entries_processed += 1
                            try:
                                cost = float(entry.get("cost", 0.0) or 0.0)
                                tokens_in = int(entry.get("tokens_in", 0) or 0)
                                tokens_out = int(entry.get("tokens_out", 0) or 0)
                                provider = str(entry.get("provider", "unknown"))
                                model = str(entry.get("model", "unknown"))
                                ts = float(entry.get("ts", 0.0) or 0.0)
                                # Per-user: use the session_id's user prefix or
                                # the local identity if no session_id present.
                                user_id = self._extract_user_id(entry)
                                user_name = self._extract_user_name(entry)
                            except Exception:
                                continue

                            total_cost += cost
                            total_in += tokens_in
                            total_out += tokens_out
                            total_reqs += 1

                            # by_user
                            u = by_user.setdefault(user_id, UserSpendRow(
                                user_id=user_id, name=user_name, team=team,
                            ))
                            u.tokens_in += tokens_in
                            u.tokens_out += tokens_out
                            u.cost_usd += cost
                            u.request_count += 1
                            iso = self._iso_from_ts(ts)
                            if iso > u.last_active_iso:
                                u.last_active_iso = iso

                            # by_provider
                            p = by_provider.setdefault(provider, ProviderSpendRow(provider=provider))
                            p.tokens_in += tokens_in
                            p.tokens_out += tokens_out
                            p.cost_usd += cost
                            p.request_count += 1

                            # by_model
                            m = by_model.setdefault(model, ModelSpendRow(model=model))
                            m.tokens_in += tokens_in
                            m.tokens_out += tokens_out
                            m.cost_usd += cost
                            m.request_count += 1

                            # by_day (only last N days)
                            if ts >= cutoff_ts:
                                day_key = self._date_from_ts(ts)
                                d = by_day.setdefault(day_key, DaySpendRow(date=day_key))
                                d.tokens_in += tokens_in
                                d.tokens_out += tokens_out
                                d.cost_usd += cost
                                d.request_count += 1
                except Exception as e:
                    logger.warning(f"[spend_dashboard] error reading {f}: {e}")
                    continue

        # Sort the by_user list by cost desc, find top consumer
        by_user_list = sorted(by_user.values(), key=lambda x: x.cost_usd, reverse=True)
        top_consumer = by_user_list[0].user_id if by_user_list else ""

        # Sort by_provider, by_model by cost desc
        by_provider_list = sorted(by_provider.values(), key=lambda x: x.cost_usd, reverse=True)
        by_model_list = sorted(by_model.values(), key=lambda x: x.cost_usd, reverse=True)
        # Sort by_day chronologically
        by_day_list = sorted(by_day.values(), key=lambda x: x.date)

        # Compute team budget usage
        team_budget_usd = self._team_budget.monthly_usd
        team_budget_used_pct = 0.0
        if team_budget_usd > 0:
            # Filter to current calendar month for the budget check.
            now = _dt.datetime.now()
            month_start = _dt.datetime(now.year, now.month, 1).timestamp()
            month_cost = 0.0
            for source in self._sources:
                try:
                    for f in self._expand_source(source):
                        with open(f, "r", encoding="utf-8") as fh:
                            for line in fh:
                                line = line.strip()
                                if not line:
                                    continue
                                try:
                                    entry = json.loads(line)
                                    if float(entry.get("ts", 0.0) or 0.0) >= month_start:
                                        month_cost += float(entry.get("cost", 0.0) or 0.0)
                                except (json.JSONDecodeError, ValueError):
                                    continue
                except Exception:
                    continue
            team_budget_used_pct = round(
                min(100.0, (month_cost / team_budget_usd) * 100), 1
            )

        return TeamSpendReport(
            team=team,
            total_cost_usd=round(total_cost, 4),
            total_tokens_in=total_in,
            total_tokens_out=total_out,
            total_request_count=total_reqs,
            by_user=[UserSpendRow(**asdict(u)) for u in by_user_list],
            by_provider=[ProviderSpendRow(**asdict(p)) for p in by_provider_list],
            by_model=[ModelSpendRow(**asdict(m)) for m in by_model_list],
            by_day=[DaySpendRow(**asdict(d)) for d in by_day_list],
            team_budget_usd=team_budget_usd,
            team_budget_used_pct=team_budget_used_pct,
            top_consumer_user_id=top_consumer,
            sources_scanned=sources_scanned,
            entries_processed=entries_processed,
            generated_at_iso=_now_iso(),
        )

    # ── Export ──────────────────────────────────────────────────

    def export_report_json(self, days: int = 30) -> str:
        return json.dumps(self.report(days=days).to_dict(), indent=2, default=str)

    def export_report_csv(self, days: int = 30) -> str:
        """Multi-section CSV. Sections separated by a blank line."""
        r = self.report(days=days)
        buf = io.StringIO()
        writer = csv.writer(buf)

        writer.writerow(["# Team Spend Report"])
        writer.writerow(["team", r.team])
        writer.writerow(["generated_at", r.generated_at_iso])
        writer.writerow(["total_cost_usd", r.total_cost_usd])
        writer.writerow(["total_tokens_in", r.total_tokens_in])
        writer.writerow(["total_tokens_out", r.total_tokens_out])
        writer.writerow(["total_request_count", r.total_request_count])
        writer.writerow(["team_budget_usd", r.team_budget_usd])
        writer.writerow(["team_budget_used_pct", r.team_budget_used_pct])
        writer.writerow(["top_consumer_user_id", r.top_consumer_user_id])
        writer.writerow(["sources_scanned", r.sources_scanned])
        writer.writerow(["entries_processed", r.entries_processed])
        writer.writerow([])

        writer.writerow(["# By User"])
        writer.writerow(["user_id", "name", "team", "tokens_in", "tokens_out",
                         "cost_usd", "request_count", "last_active_iso"])
        for u in r.by_user:
            writer.writerow([u.user_id, u.name, u.team, u.tokens_in, u.tokens_out,
                             f"{u.cost_usd:.6f}", u.request_count, u.last_active_iso])
        writer.writerow([])

        writer.writerow(["# By Provider"])
        writer.writerow(["provider", "tokens_in", "tokens_out", "cost_usd", "request_count"])
        for p in r.by_provider:
            writer.writerow([p.provider, p.tokens_in, p.tokens_out,
                             f"{p.cost_usd:.6f}", p.request_count])
        writer.writerow([])

        writer.writerow(["# By Model"])
        writer.writerow(["model", "tokens_in", "tokens_out", "cost_usd", "request_count"])
        for m in r.by_model:
            writer.writerow([m.model, m.tokens_in, m.tokens_out,
                             f"{m.cost_usd:.6f}", m.request_count])
        writer.writerow([])

        writer.writerow(["# By Day (last 30 days)"])
        writer.writerow(["date", "tokens_in", "tokens_out", "cost_usd", "request_count"])
        for d in r.by_day:
            writer.writerow([d.date, d.tokens_in, d.tokens_out,
                             f"{d.cost_usd:.6f}", d.request_count])

        return buf.getvalue()

    # ── Helpers ─────────────────────────────────────────────────

    def _expand_source(self, source: Path) -> List[Path]:
        """Expand a source path into a list of *.jsonl files."""
        if source.is_dir():
            return sorted(source.glob("*.jsonl"))
        if source.is_file():
            return [source]
        return []

    def _extract_user_id(self, entry: Dict[str, Any]) -> str:
        """Extract the user id from an entry.

        Token entries don't carry a user_id directly — they carry a
        ``session_id`` and ``chat_id``. We use the local identity's
        user_id by default. If the entry has an explicit ``user_id``
        field (set by a future enhanced TokenTracker), we use that.
        """
        if "user_id" in entry:
            return str(entry["user_id"])
        return self._identity.user_id

    def _extract_user_name(self, entry: Dict[str, Any]) -> str:
        if "user_name" in entry:
            return str(entry["user_name"])
        return self._identity.name

    @staticmethod
    def _iso_from_ts(ts: float) -> str:
        try:
            return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "Z")
        except Exception:
            return ""

    @staticmethod
    def _date_from_ts(ts: float) -> str:
        try:
            return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).strftime("%Y-%m-%d")
        except Exception:
            return "1970-01-01"


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


# ── Module-level singleton ────────────────────────────────────────────

_dashboard: Optional[TeamSpendDashboard] = None
_dashboard_lock = threading.Lock()


def get_spend_dashboard() -> TeamSpendDashboard:
    """Return the process-wide TeamSpendDashboard singleton."""
    global _dashboard
    if _dashboard is None:
        with _dashboard_lock:
            if _dashboard is None:
                _dashboard = TeamSpendDashboard()
    return _dashboard


def reset_spend_dashboard_for_test(
    sources: Optional[List[Path]] = None,
    identity: Optional[UserIdentity] = None,
) -> TeamSpendDashboard:
    """Test-only: forget the cached dashboard and return a fresh one."""
    global _dashboard
    with _dashboard_lock:
        _dashboard = TeamSpendDashboard(
            sources=sources,
            identity=identity,
        )
    return _dashboard
