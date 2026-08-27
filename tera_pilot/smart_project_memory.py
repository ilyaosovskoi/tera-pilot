"""Tera Pilot Pro feature — ``smart_project_memory``.

Offline, deterministic search + dedup over a project's written memory
(``TERA_PILOT.md`` plus, optionally, a ``notes`` folder). Designed for the
local-first / self-hosted audience: everything runs locally, no network, no
telemetry, no external vector DB required.

Value this adds beyond the free core:
  - Projects grow messy memory files. This indexes them and lets the agent
    (or the user) "ask" for the relevant past lessons before acting.
  - Dedup: near-duplicate lesson lines (e.g. the same convention written
    three times) are collapsed, so the project memory stays clean.

Gating (matches the monetization constraint "don't cut the core"):
  - The free core (reading/writing TERA_PILOT.md, learning loop) is
    untouched; this is an optional *search/dedup overlay*.
  - Pro-gated via ``licensing.is_feature_licensed("smart_project_memory")``,
    fail-closed: an unlicensed caller gets a structured error, never a
    crash and never fabricated results.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .licensing import LicenseRequiredError, is_feature_licensed as _licensed

#: The Pro feature id this module gates on.
FEATURE_ID = "smart_project_memory"


class SmartMemoryError(Exception):
    """Raised for problematic memory indexing operations."""


@dataclass
class MemoryEntry:
    """A single deduplicated memory entry parsed from a source file."""

    text: str
    source: str
    heading: str = ""          # nearest markdown heading
    fingerprint: str = ""      # content hash used for dedup
    line: int = 0


@dataclass
class SearchResult:
    """One ranked search hit."""

    entry: MemoryEntry
    score: float = 0.0


@dataclass
class IndexReport:
    """Summary of an indexing pass."""

    indexed: int = 0
    duplicates_collapsed: int = 0
    sources: List[str] = field(default_factory=list)


def _require_license() -> None:
    if not _licensed(FEATURE_ID):
        raise LicenseRequiredError(
            f"{FEATURE_ID} is a Pro feature — activate a license with: "
            "tera-pilot license activate <key>"
        )


def _norm(text: str) -> str:
    """Lowercase + collapse punctuation/whitespace for fuzzy matching."""
    t = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE).lower()
    return re.sub(r"\s+", " ", t).strip()


def _tokens(text: str):
    return [t for t in _norm(text).split() if len(t) > 1]


def _fingerprint(text: str) -> str:
    return hashlib.sha256(_norm(text).encode("utf-8")).hexdigest()


def _split_notes(body: str) -> List[str]:
    """Split a memory body into non-empty, dedup candidate lines."""
    out: List[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        # Drop markdown list bullets / banner markers for matching.
        line = re.sub(r"^[-*]+\s*", "", line)
        if len(line) < 8:  # too short to be a useful lesson
            continue
        out.append(line)
    return out


class SmartProjectMemory:
    """Index & search a project's memory file(s)."""

    def __init__(self, workspace: Optional[str] = None,
                 resolve_path_fn=None):
        self._workspace = workspace
        self._resolve = resolve_path_fn or (lambda p: Path(p).resolve())
        self._entries: List[MemoryEntry] = []
        self._last_report = IndexReport()

    # ── Public API (safe dict returns) ────────────────────────────

    def index(self, memory_file: Optional[str] = None,
              notes_dir: Optional[str] = None) -> Dict[str, Any]:
        """(Re)index the project memory.

        ``memory_file``: path to a TERA_PILOT.md-style file (default
        ``<workspace>/TERA_PILOT.md``). ``notes_dir``: optional folder of
        ``.md`` files to include.

        Returns ``{ok: True, report: {...}}``, or ``{ok: False,
        error: "pro_required"}`` when unlicensed.
        """
        try:
            _require_license()
        except LicenseRequiredError:
            return {"ok": False, "error": "pro_required"}
        try:
            entries: List[MemoryEntry] = []
            sources: List[str] = []

            mem_path = self._resolve_path(memory_file)
            if mem_path and mem_path.is_file():
                heading = ""
                for i, line in enumerate(mem_path.read_text(encoding="utf-8").splitlines(), 1):
                    stripped = line.strip()
                    if stripped.startswith("#"):
                        heading = stripped.lstrip("#").strip()
                        continue
                    for cand in _split_notes(stripped):
                        if cand:
                            entries.append(MemoryEntry(
                                text=cand, source=str(mem_path),
                                heading=heading, line=i,
                            ))
                sources.append(str(mem_path))

            if notes_dir:
                nd = self._resolve_path(notes_dir)
                if nd and nd.is_dir():
                    for f in sorted(nd.glob("*.md")):
                        heading = ""
                        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                            stripped = line.strip()
                            if stripped.startswith("#"):
                                heading = stripped.lstrip("#").strip()
                                continue
                            for cand in _split_notes(stripped):
                                if cand:
                                    entries.append(MemoryEntry(
                                        text=cand, source=str(f),
                                        heading=heading, line=i,
                                    ))
                        sources.append(str(f))

            # Assign fingerprints and collapse near-duplicates.
            seen: Dict[str, MemoryEntry] = {}
            collapse = 0
            ordered = []
            for e in entries:
                fp = _fingerprint(e.text)
                e.fingerprint = fp
                prev = seen.get(fp)
                if prev is not None and _norm(prev.text) == _norm(e.text):
                    collapse += 1
                    continue
                seen[fp] = e
                ordered.append(e)

            self._entries = ordered
            self._last_report = IndexReport(
                indexed=len(ordered),
                duplicates_collapsed=collapse,
                sources=sources,
            )
            return {"ok": True, "report": {
                "indexed": len(ordered),
                "duplicates_collapsed": collapse,
                "sources": sources,
            }}
        except Exception as e:  # pragma: no cover - defensive
            return {"ok": False, "error": f"index failed: {e}"}

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        """Ranked full-text search over the last indexed entries.

        Returns a dict (never raises):
          - unlicensed  -> ``{ok: False, error: "pro_required"}``
          - success     -> ``{ok: True, results: [{text, source, heading, score}]}``
        """
        try:
            _require_license()
        except LicenseRequiredError:
            return {"ok": False, "error": "pro_required"}
        q = _tokens(query)
        if not q:
            return {"ok": False, "error": "query is empty"}
        scored: List[SearchResult] = []
        for e in self._entries:
            et = _tokens(e.text)
            score = 0.0
            for t in q:
                # Exact token match, OR a cheap stemming check so a query
                # like "webhook" also matches "webhooks"/"webhook"-ish
                # tokens (and vice-versa). Keeps offline search useful for
                # real prose memory files.
                if t in et or any(
                    tt.startswith(t) or t.startswith(tt)
                    for tt in et
                ):
                    score += 1.0
            if score <= 0:
                continue
            # Prefer entries where all query terms appear together.
            density = score / max(1, len(et))
            scored.append(SearchResult(entry=e, score=round(score + density, 4)))
        scored.sort(key=lambda r: (r.score, len(r.entry.text)), reverse=True)
        results = [{
            "text": r.entry.text,
            "source": r.entry.source,
            "heading": r.entry.heading,
            "score": r.score,
        } for r in scored[:max(1, int(limit))]]
        return {"ok": True, "results": results}

    def dedup_overview(self) -> Dict[str, Any]:
        """Return the current index dedup state (for UI display)."""
        try:
            _require_license()
        except LicenseRequiredError:
            return {"ok": False, "error": "pro_required"}
        return {"ok": True, "report": {
            "indexed": self._last_report.indexed,
            "duplicates_collapsed": self._last_report.duplicates_collapsed,
            "sources": self._last_report.sources,
        }}

    # ── Strict API ────────────────────────────────────────────────

    def require(self) -> None:
        _require_license()

    # ── Helpers ───────────────────────────────────────────────────

    def _resolve_path(self, p: Optional[str]) -> Optional[Path]:
        if not p:
            if not self._workspace:
                return None
            p = str(Path(self._workspace) / "TERA_PILOT.md")
        try:
            return self._resolve(p)
        except Exception:
            return None