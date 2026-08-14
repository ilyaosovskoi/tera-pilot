"""Marker-based context fragments for compaction.

Issue #5: tools and the agent loop emit tagged fragments inside the
conversation history. During compaction, instead of dropping old
messages wholesale, the compactor can:

1. Preserve the *latest* occurrence of each fragment type (so the most
   recent state — e.g. the current file outline, the most recent test
   run — survives).
2. Replace the body of older occurrences with a small tombstone marker
   that records the fragment id, the type, and a one-line digest. The
   *header* of the fragment (the opening tag) is kept so the LLM still
   knows that a fragment of that type used to exist, but the bulky body
   is collapsed.

Fragment format (XML-style, mirrors the progressive_tools announcement
blocks already used elsewhere in Tera Pilot):

    <context_fragment type="file_outline" id="auth.py">
    ...outline body...
    </context_fragment>

The format is intentionally simple so that:

- Tools can emit fragments by string concatenation (no extra dep).
- The fragment scanner is a single regex.
- The format degrades gracefully: unrecognised fragment bodies are
  left untouched.

This module is self-contained — it has no hard dependency on the rest
of the compaction engine so it can be used standalone from
``ContextMemory.compact`` or from the v2 ``CompactionEngine``.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── Fragment parsing ─────────────────────────────────────────────────────

# Opening tag: <context_fragment type="..." id="...">
_OPEN_RE = re.compile(
    r"<context_fragment\s+"
    r'type="(?P<type>[^"]+)"\s+'
    r'id="(?P<id>[^"]+)"\s*'
    r">",
    re.IGNORECASE,
)
# Closing tag (case-insensitive on the tag name).
_CLOSE_RE = re.compile(r"</context_fragment\s*>", re.IGNORECASE)
_CLOSE_TAG = "</context_fragment>"


@dataclass(frozen=True)
class ContextFragment:
    """A parsed context fragment from a message body.

    ``start`` and ``end`` are absolute offsets into the original
    message content. ``body_start``/``body_end`` exclude the tags.
    """

    type: str
    id: str
    start: int  # offset of '<' of opening tag
    end: int  # offset AFTER closing tag
    body_start: int  # offset of first body char
    body_end: int  # offset of last body char + 1
    body: str

    def header(self) -> str:
        """Return just the opening tag (used in tombstone reconstruction)."""
        return f'<context_fragment type="{self.type}" id="{self.id}">'

    def digest(self, n: int = 60) -> str:
        """One-line digest of the body, for the tombstone."""
        flat = " ".join(self.body.split())
        if len(flat) <= n:
            return flat
        return flat[: n - 1] + "…"


def parse_fragments(text: str) -> List[ContextFragment]:
    """Find all well-formed fragments in ``text``.

    A fragment is "well-formed" if it has a matching ``</context_fragment>``
    after the opening tag. Malformed fragments are silently skipped (the
    body is left untouched by the compactor).
    """
    out: List[ContextFragment] = []
    if not text:
        return out
    for m in _OPEN_RE.finditer(text):
        body_start = m.end()
        close_m = _CLOSE_RE.search(text, body_start)
        if close_m is None:
            continue
        body_end = close_m.start()
        end = close_m.end()
        out.append(
            ContextFragment(
                type=m.group("type"),
                id=m.group("id"),
                start=m.start(),
                end=end,
                body_start=body_start,
                body_end=body_end,
                body=text[body_start:body_end],
            )
        )
    return out


# ── Tombstone rendering ──────────────────────────────────────────────────


def render_tombstone(fragment: ContextFragment, max_digest_chars: int = 60) -> str:
    """Render a collapsed fragment: header + digest + closing tag."""
    digest = fragment.digest(max_digest_chars)
    return (
        f"{fragment.header()}\n"
        f"[COMPACTED] {digest}\n"
        f"{_CLOSE_TAG}"
    )


def render_full(fragment: ContextFragment) -> str:
    """Render a fragment in its original (preserved) form."""
    return f"{fragment.header()}\n{fragment.body}\n{_CLOSE_TAG}"


# ── Compaction policy ────────────────────────────────────────────────────


@dataclass
class FragmentCompactionConfig:
    """Configuration for marker-based fragment compaction.

    - ``keep_latest_per_id``: keep the latest fragment body for each
      (type, id) pair; collapse older occurrences to tombstones.
    - ``keep_latest_per_type``: keep the latest fragment body for each
      *type* regardless of id; collapse older ones.
    - ``digest_chars``: length of the digest embedded in tombstones.
    - ``collapse_types``: if non-empty, only fragments whose type is in
      this set are eligible for compaction. Empty = all types.
    """

    keep_latest_per_id: bool = True
    keep_latest_per_type: bool = False
    digest_chars: int = 60
    collapse_types: Tuple[str, ...] = ()


# ── Compaction engine ────────────────────────────────────────────────────


def compact_fragments(
    text: str,
    config: Optional[FragmentCompactionConfig] = None,
) -> str:
    """Compact fragment bodies in ``text`` according to ``config``.

    Returns a new string with old fragment occurrences replaced by
    tombstones. The latest occurrence (per the policy) keeps its full
    body. Non-fragment text is left untouched.

    If there are no fragments, returns ``text`` unchanged.
    """
    if not text:
        return text
    cfg = config or FragmentCompactionConfig()
    fragments = parse_fragments(text)
    if not fragments:
        return text

    # Decide which fragments to keep.
    keep_indices = _select_keep_indices(fragments, cfg)

    # Build the output by walking the original text and substituting
    # only the bodies of fragments not in keep_indices.
    out: List[str] = []
    cursor = 0
    for i, frag in enumerate(fragments):
        out.append(text[cursor : frag.start])
        if i in keep_indices:
            out.append(render_full(frag))
        else:
            out.append(render_tombstone(frag, cfg.digest_chars))
        cursor = frag.end
    out.append(text[cursor:])
    return "".join(out)


def _select_keep_indices(
    fragments: List[ContextFragment],
    cfg: FragmentCompactionConfig,
) -> set:
    """Return the set of fragment indices whose body should be kept.

    Fragments whose type is not in ``cfg.collapse_types`` (when that
    filter is non-empty) are *always* preserved — they are not eligible
    for compaction in the first place.
    """
    keep: set = set()
    for i, f in enumerate(fragments):
        if cfg.collapse_types and f.type not in cfg.collapse_types:
            keep.add(i)

    if cfg.keep_latest_per_id:
        latest_per_key: Dict[Tuple[str, str], int] = {}
        for i, f in enumerate(fragments):
            if cfg.collapse_types and f.type not in cfg.collapse_types:
                continue
            key = (f.type, f.id)
            latest_per_key[key] = i  # last write wins → latest
        keep.update(latest_per_key.values())
    if cfg.keep_latest_per_type:
        latest_per_type: Dict[str, int] = {}
        for i, f in enumerate(fragments):
            if cfg.collapse_types and f.type not in cfg.collapse_types:
                continue
            latest_per_type[f.type] = i
        keep.update(latest_per_type.values())
    return keep


# ── Per-message wrapper ──────────────────────────────────────────────────


def compact_message_content(
    content: str,
    config: Optional[FragmentCompactionConfig] = None,
) -> Tuple[str, "FragmentCompactionStats"]:
    """Compact fragment bodies inside a single message's content.

    Returns the new content plus a :class:`FragmentCompactionStats` so
    the caller can report token savings.
    """
    if not content:
        return content, FragmentCompactionStats()
    cfg = config or FragmentCompactionConfig()
    before_len = len(content)
    fragments = parse_fragments(content)

    new_content = compact_fragments(content, cfg)
    after_len = len(new_content)

    compacted_count = 0
    preserved_count = 0
    for i in range(len(fragments)):
        if i in _select_keep_indices(fragments, cfg):
            preserved_count += 1
        else:
            compacted_count += 1

    return new_content, FragmentCompactionStats(
        fragments_total=len(fragments),
        fragments_compacted=compacted_count,
        fragments_preserved=preserved_count,
        chars_before=before_len,
        chars_after=after_len,
    )


@dataclass
class FragmentCompactionStats:
    """Stats returned by :func:`compact_message_content`."""

    fragments_total: int = 0
    fragments_compacted: int = 0
    fragments_preserved: int = 0
    chars_before: int = 0
    chars_after: int = 0

    @property
    def chars_saved(self) -> int:
        return max(0, self.chars_before - self.chars_after)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fragments_total": self.fragments_total,
            "fragments_compacted": self.fragments_compacted,
            "fragments_preserved": self.fragments_preserved,
            "chars_before": self.chars_before,
            "chars_after": self.chars_after,
            "chars_saved": self.chars_saved,
        }


# ── Helper for tools: build a fragment string ────────────────────────────


def build_fragment(type_: str, id_: str, body: str) -> str:
    """Helper for tools that want to emit a fragment.

    >>> build_fragment("file_outline", "auth.py", "def login(): ...")
    '<context_fragment type="file_outline" id="auth.py">\\ndef login(): ...\\n</context_fragment>'
    """
    if not type_ or not id_:
        raise ValueError("fragment type and id are required")
    return f'<context_fragment type="{type_}" id="{id_}">\n{body}\n</context_fragment>'


# ── Hash-based id helper ─────────────────────────────────────────────────


def stable_id(*parts: str) -> str:
    """Build a short stable id from arbitrary string parts.

    Useful when a tool wants to emit a fragment with an id that is
    deterministic across runs (so the compactor can dedupe properly).
    """
    h = hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()
    return h[:12]
