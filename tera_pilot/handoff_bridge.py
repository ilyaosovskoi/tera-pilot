"""
Tera Pilot v2.0.2 — Post-task "bridge" / editable handoff (Goal G6).

**Problem (from CLAUDE.md):**
    "After task delivery, no interface for edits without a developer."
    Vibe coders / non-technical users receive an agent's output and
    have no easy way to ask for small revisions without rephrasing the
    whole request from scratch, or to mark parts of the output for
    "keep / change / explain" without typing prose.

**Design:**

1.  **HandoffDocument** — a structured, editable snapshot of an
    agent's final output. It is composed of *blocks*; each block is
    one of:
      * ``text``        — prose paragraph
      * ``code``        — fenced code with language
      * ``file_diff``   — a file write/patch the agent produced
      * ``todo``        — a checkbox the user can tick
      * ``note``        — user-visible callout (warning / tip)
    Every block has a stable ``id`` and a mutable ``status``:
    ``pending`` (default), ``accepted``, ``rejected``, ``edited``.

2.  **HandoffStore** — persists handoff documents to
    ``~/.tera_pilot/handoffs/<id>.json``. Each doc has:
      * ``id``          — uuid
      * ``title``       — auto-derived from the user's prompt
      * ``created_at``  — ISO timestamp
      * ``updated_at``  — ISO timestamp (every save bumps this)
      * ``prompt``      — the user prompt that produced this handoff
      * ``agent``       — the AgentIdentity that produced it (G5)
      * ``blocks``      — list of HandoffBlock dicts

3.  **Revision requests.** When the user marks a block ``rejected``
    or ``edited`` (with a replacement), the bridge can compile a
    *revision prompt* — a structured instruction that goes back to the
    agent: "The user rejected block 3 (code: doubler.py). They said:
    'use float division'. Please regenerate." This means the agent
    gets a precise edit request instead of a vague "do it again".

4.  **CMS-style ops.** Block-level CRUD + reorder + comment. The UI
    surface is intentionally tiny — a non-technical user only sees:
    "Accept / Reject / Edit" per block, "Send revisions" at the end.

5.  **No telemetry, no network.** Everything is local files under
    ``~/.tera_pilot/handoffs/``. The export is a single JSON or Markdown
    file the user can email / print.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── Block types ──────────────────────────────────────────────────────

BLOCK_TEXT = "text"
BLOCK_CODE = "code"
BLOCK_FILE_DIFF = "file_diff"
BLOCK_TODO = "todo"
BLOCK_NOTE = "note"

ALL_BLOCK_TYPES = (
    BLOCK_TEXT, BLOCK_CODE, BLOCK_FILE_DIFF, BLOCK_TODO, BLOCK_NOTE,
)

BLOCK_STATUS_PENDING = "pending"
BLOCK_STATUS_ACCEPTED = "accepted"
BLOCK_STATUS_REJECTED = "rejected"
BLOCK_STATUS_EDITED = "edited"

ALL_BLOCK_STATUSES = (
    BLOCK_STATUS_PENDING, BLOCK_STATUS_ACCEPTED,
    BLOCK_STATUS_REJECTED, BLOCK_STATUS_EDITED,
)


# ── Block ────────────────────────────────────────────────────────────

@dataclass
class HandoffBlock:
    """One editable piece of an agent handoff."""
    id: str
    type: str                       # one of ALL_BLOCK_TYPES
    content: str = ""               # main body (prose, code, diff, todo text, note)
    language: str = ""              # for code blocks: "python", "bash", ...
    path: str = ""                  # for file_diff blocks: target file path
    diff_stat: str = ""             # for file_diff blocks: "+N -M"
    checked: bool = False           # for todo blocks
    status: str = BLOCK_STATUS_PENDING
    comment: str = ""               # user's freeform comment on this block
    replacement: str = ""           # user-proposed replacement (status="edited")
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "HandoffBlock":
        return cls(
            id=str(d.get("id") or _new_block_id()),
            type=str(d.get("type") or BLOCK_TEXT),
            content=str(d.get("content") or ""),
            language=str(d.get("language") or ""),
            path=str(d.get("path") or ""),
            diff_stat=str(d.get("diff_stat") or ""),
            checked=bool(d.get("checked", False)),
            status=str(d.get("status") or BLOCK_STATUS_PENDING),
            comment=str(d.get("comment") or ""),
            replacement=str(d.get("replacement") or ""),
            meta=dict(d.get("meta") or {}),
        )


def _new_block_id() -> str:
    return "blk_" + uuid.uuid4().hex[:10]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ── HandoffDocument ──────────────────────────────────────────────────

@dataclass
class HandoffDocument:
    """A full editable handoff: prompt, agent, blocks."""
    id: str
    title: str
    prompt: str
    agent: Dict[str, Any] = field(default_factory=dict)
    blocks: List[HandoffBlock] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = _now_iso()
        if not self.updated_at:
            self.updated_at = self.created_at
        if not self.id:
            self.id = _new_doc_id()

    def touch(self) -> None:
        self.updated_at = _now_iso()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "prompt": self.prompt,
            "agent": dict(self.agent),
            "blocks": [b.to_dict() for b in self.blocks],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "meta": dict(self.meta),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "HandoffDocument":
        return cls(
            id=str(d.get("id") or _new_doc_id()),
            title=str(d.get("title") or "Untitled handoff"),
            prompt=str(d.get("prompt") or ""),
            agent=dict(d.get("agent") or {}),
            blocks=[HandoffBlock.from_dict(b) for b in (d.get("blocks") or [])],
            created_at=str(d.get("created_at") or ""),
            updated_at=str(d.get("updated_at") or ""),
            meta=dict(d.get("meta") or {}),
        )


def _new_doc_id() -> str:
    return "hdf_" + uuid.uuid4().hex[:12]


# ── Output parser ────────────────────────────────────────────────────
# Splits a raw agent output string into a list of HandoffBlock objects.
# Recognises fenced code blocks (```lang ... ```), file-write markers
# ([WRITTEN] path, [REPLACED] path, [CREATED] path, [SAVED AS] path),
# and todo lines (- [ ] / - [x]). Everything else becomes a text block.

_FENCE_RE = re.compile(r"^```(\w*)\s*$", re.MULTILINE)
_FILE_MARKER_RE = re.compile(
    r"^\s*\[(WRITTEN|CREATED|REPLACED|SAVED AS|ADDED|RENAMED|DELETED)\]\s+(.+?)\s*$",
    re.IGNORECASE,
)
_TODO_RE = re.compile(r"^\s*[-*]\s+\[( |x|X)\]\s+(.+?)\s*$")


def parse_agent_output(
    output: str,
    prompt: str = "",
    agent: Optional[Dict[str, Any]] = None,
    title: str = "",
) -> HandoffDocument:
    """Parse a raw agent response into a structured HandoffDocument.

    The parser is intentionally lenient: any unrecognised line becomes
    part of a ``text`` block. The goal is to give the user *something*
    to react to, not to enforce a format on the agent.
    """
    blocks: List[HandoffBlock] = []
    lines = output.splitlines()
    i = 0
    text_buf: List[str] = []

    def flush_text() -> None:
        if not text_buf:
            return
        blocks.append(HandoffBlock(
            id=_new_block_id(),
            type=BLOCK_TEXT,
            content="\n".join(text_buf).strip(),
        ))
        text_buf.clear()

    while i < len(lines):
        line = lines[i]
        # Fenced code block
        m = _FENCE_RE.match(line)
        if m:
            flush_text()
            lang = m.group(1) or ""
            code_lines: List[str] = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                code_lines.append(lines[i])
                i += 1
            # Skip the closing fence (if present)
            if i < len(lines):
                i += 1
            blocks.append(HandoffBlock(
                id=_new_block_id(),
                type=BLOCK_CODE,
                content="\n".join(code_lines),
                language=lang,
            ))
            continue
        # File marker
        fm = _FILE_MARKER_RE.match(line)
        if fm:
            flush_text()
            verb = fm.group(1).upper()
            path = fm.group(2).strip()
            # Strip any trailing parenthetical, e.g. "[WRITTEN] foo.py (12 chars)"
            path = re.sub(r"\s*\(.*\)\s*$", "", path)
            blocks.append(HandoffBlock(
                id=_new_block_id(),
                type=BLOCK_FILE_DIFF,
                content=line.strip(),
                path=path,
                diff_stat=verb,
                meta={"verb": verb.lower().replace(" ", "_")},
            ))
            i += 1
            continue
        # Todo line
        tm = _TODO_RE.match(line)
        if tm:
            flush_text()
            checked = tm.group(1).lower() == "x"
            todo_text = tm.group(2).strip()
            blocks.append(HandoffBlock(
                id=_new_block_id(),
                type=BLOCK_TODO,
                content=todo_text,
                checked=checked,
            ))
            i += 1
            continue
        # Default: accumulate as text
        text_buf.append(line)
        i += 1
    flush_text()

    # Derive a title from the prompt (first non-empty line, truncated)
    if not title:
        first_line = next((l.strip() for l in (prompt or "").splitlines() if l.strip()), "")
        title = first_line[:80] or "Untitled handoff"

    return HandoffDocument(
        id=_new_doc_id(),
        title=title,
        prompt=prompt,
        agent=agent or {},
        blocks=blocks,
    )


# ── HandoffStore ─────────────────────────────────────────────────────

class HandoffStore:
    """Persists HandoffDocuments to ~/.tera_pilot/handoffs/<id>.json."""

    def __init__(self, root: Optional[Path] = None) -> None:
        self._root = root or (Path.home() / ".tera_pilot" / "handoffs")
        self._root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    # ── CRUD ─────────────────────────────────────────────────────

    def save(self, doc: HandoffDocument) -> Path:
        """Persist a handoff document. Returns the file path."""
        doc.touch()
        path = self._root / f"{doc.id}.json"
        with self._lock:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(doc.to_dict(), indent=2, default=str), encoding="utf-8")
            tmp.replace(path)
        return path

    def load(self, doc_id: str) -> Optional[HandoffDocument]:
        path = self._root / f"{doc_id}.json"
        if not path.exists():
            return None
        try:
            with self._lock:
                data = json.loads(path.read_text(encoding="utf-8"))
            return HandoffDocument.from_dict(data)
        except Exception as e:
            logger.warning(f"[handoff] load error for {doc_id}: {e}")
            return None

    def delete(self, doc_id: str) -> bool:
        path = self._root / f"{doc_id}.json"
        with self._lock:
            if not path.exists():
                return False
            try:
                path.unlink()
                return True
            except Exception:
                return False

    def list_docs(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Return a list of handoff metadata (no block contents)."""
        out: List[Dict[str, Any]] = []
        with self._lock:
            paths = sorted(self._root.glob("*.json"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
        for p in paths[:limit]:
            try:
                with self._lock:
                    data = json.loads(p.read_text(encoding="utf-8"))
                out.append({
                    "id": data.get("id", p.stem),
                    "title": data.get("title", "Untitled"),
                    "created_at": data.get("created_at", ""),
                    "updated_at": data.get("updated_at", ""),
                    "block_count": len(data.get("blocks", [])),
                    "prompt_preview": (data.get("prompt") or "")[:120],
                })
            except Exception:
                continue
        return out

    # ── Block-level ops ──────────────────────────────────────────

    def set_block_status(
        self,
        doc_id: str,
        block_id: str,
        status: str,
        comment: str = "",
        replacement: str = "",
    ) -> Optional[HandoffDocument]:
        """Update a single block's status (and optional comment / replacement)."""
        if status not in ALL_BLOCK_STATUSES:
            raise ValueError(f"Invalid status: {status}")
        doc = self.load(doc_id)
        if doc is None:
            return None
        for b in doc.blocks:
            if b.id == block_id:
                b.status = status
                if comment:
                    b.comment = comment
                if replacement:
                    b.replacement = replacement
                self.save(doc)
                return doc
        return doc

    def toggle_todo(self, doc_id: str, block_id: str) -> Optional[HandoffDocument]:
        """Flip a todo block's ``checked`` flag."""
        doc = self.load(doc_id)
        if doc is None:
            return None
        for b in doc.blocks:
            if b.id == block_id and b.type == BLOCK_TODO:
                b.checked = not b.checked
                self.save(doc)
                return doc
        return doc

    def reorder_blocks(
        self,
        doc_id: str,
        new_order: List[str],
    ) -> Optional[HandoffDocument]:
        """Reorder blocks by id. Ids not in ``new_order`` are appended in
        their original relative order."""
        doc = self.load(doc_id)
        if doc is None:
            return None
        by_id = {b.id: b for b in doc.blocks}
        new_blocks: List[HandoffBlock] = []
        used: set = set()
        for bid in new_order:
            if bid in by_id and bid not in used:
                new_blocks.append(by_id[bid])
                used.add(bid)
        for b in doc.blocks:
            if b.id not in used:
                new_blocks.append(b)
        doc.blocks = new_blocks
        self.save(doc)
        return doc

    def add_block(
        self,
        doc_id: str,
        block: HandoffBlock,
        after_id: Optional[str] = None,
    ) -> Optional[HandoffDocument]:
        """Insert a new block. If ``after_id`` is None, append at the end."""
        doc = self.load(doc_id)
        if doc is None:
            return None
        if after_id is None:
            doc.blocks.append(block)
        else:
            for i, b in enumerate(doc.blocks):
                if b.id == after_id:
                    doc.blocks.insert(i + 1, block)
                    break
            else:
                doc.blocks.append(block)
        self.save(doc)
        return doc

    def delete_block(self, doc_id: str, block_id: str) -> Optional[HandoffDocument]:
        doc = self.load(doc_id)
        if doc is None:
            return None
        doc.blocks = [b for b in doc.blocks if b.id != block_id]
        self.save(doc)
        return doc

    # ── Revision requests ────────────────────────────────────────

    def build_revision_prompt(self, doc_id: str) -> str:
        """Compile a structured revision request from the user's edits.

        Walks the blocks; for each one with status != 'accepted',
        emits a short instruction the agent can act on. Returns the
        prompt string (or "" if there are no pending revisions).
        """
        doc = self.load(doc_id)
        if doc is None:
            return ""
        parts: List[str] = []
        for idx, b in enumerate(doc.blocks, 1):
            if b.status == BLOCK_STATUS_ACCEPTED:
                continue
            if b.status == BLOCK_STATUS_REJECTED:
                parts.append(
                    f"Block {idx} ({b.type}"
                    + (f", {b.path}" if b.path else "")
                    + f"): REJECT. The user does not want this output."
                    + (f" Reason: {b.comment}" if b.comment else "")
                )
            elif b.status == BLOCK_STATUS_EDITED:
                parts.append(
                    f"Block {idx} ({b.type}"
                    + (f", {b.path}" if b.path else "")
                    + f"): REPLACE WITH the user-provided version below."
                    + (f" Note: {b.comment}" if b.comment else "")
                    + f"\n\n--- replacement ---\n{b.replacement}\n--- end replacement ---"
                )
            elif b.status == BLOCK_STATUS_PENDING and b.comment:
                parts.append(
                    f"Block {idx} ({b.type}): the user left a comment: {b.comment}"
                )
        if not parts:
            return ""
        header = (
            f"The user reviewed your previous response (handoff {doc_id}, "
            f"\"{doc.title}\") and has the following revision requests. "
            f"Please address each one precisely:\n\n"
        )
        return header + "\n".join(parts)

    # ── Export ───────────────────────────────────────────────────

    def export_markdown(self, doc_id: str) -> str:
        """Render a handoff document as a single Markdown string."""
        doc = self.load(doc_id)
        if doc is None:
            return ""
        out: List[str] = [f"# {doc.title}", ""]
        if doc.prompt:
            out.append(f"> **Original prompt:** {doc.prompt}")
            out.append("")
        for b in doc.blocks:
            if b.type == BLOCK_TEXT:
                out.append(b.content)
                out.append("")
            elif b.type == BLOCK_CODE:
                out.append(f"```{b.language}")
                out.append(b.content)
                out.append("```")
                out.append("")
            elif b.type == BLOCK_FILE_DIFF:
                out.append(f"**[{b.diff_stat or 'FILE'}]** `{b.path}`")
                if b.content:
                    out.append(f"```\n{b.content}\n```")
                out.append("")
            elif b.type == BLOCK_TODO:
                box = "[x]" if b.checked else "[ ]"
                out.append(f"- {box} {b.content}")
            elif b.type == BLOCK_NOTE:
                out.append(f"> {b.content}")
                out.append("")
            if b.status != BLOCK_STATUS_PENDING or b.comment:
                tag = f"_{b.status}_" if b.status != BLOCK_STATUS_PENDING else ""
                cmt = f" — {b.comment}" if b.comment else ""
                out.append(f"  {tag}{cmt}".rstrip())
                out.append("")
        return "\n".join(out).rstrip() + "\n"


# ── Module-level singleton ────────────────────────────────────────────

_store: Optional[HandoffStore] = None
_store_lock = threading.Lock()


def get_handoff_store() -> HandoffStore:
    """Return the process-wide HandoffStore singleton."""
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = HandoffStore()
    return _store


def reset_handoff_store_for_test(root: Optional[Path] = None) -> HandoffStore:
    """Test-only: forget the cached store and return a fresh one."""
    global _store
    with _store_lock:
        _store = HandoffStore(root=root) if root else None
    return get_handoff_store()
