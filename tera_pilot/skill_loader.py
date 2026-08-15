"""
Tera Pilot v1.0.11 — Skill Loader (SKILL.md format).

Implements the reusable instruction-package model:

  SKILL.md    — a Markdown file with YAML frontmatter describing when
                the skill should be used, plus a body with step-by-step
                instructions. Loaded automatically from:
                  - <project>/.tera_pilot/skills/*/SKILL.md  (project-level)
                  - ~/.tera_pilot/skills/*/SKILL.md          (user-level)
                  - <project>/.tera_pilot/skills/*.md         (single-file)
                  - ~/.tera_pilot/skills/*.md                (single-file)

  Activation  — manual (user picks the skill from the UI) or automatic
                (the agent reads skill descriptions and decides which
                one fits the task). Automatic activation is done by
                including all skill descriptions in the system prompt
                and letting the model emit a {"tool": "use_skill", ...}
                call — but for v1.0.11 we keep it simpler: the agent
                gets a "skills catalog" section in the system prompt
                and can request the full text of a skill via the
                get_skill tool.

This mirrors how skills are treated as "instruction packages
that get injected into context when needed" rather than separate
programs.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── Data classes ─────────────────────────────────────────────────────

@dataclass
class Skill:
    """One reusable instruction package."""
    id: str                        # unique identifier (slug)
    name: str                      # human-readable name
    description: str               # when to use this skill
    tag: str = "general"           # category: backend, frontend, devops, etc.
    body: str = ""                 # the actual instructions (markdown)
    source_path: str = ""          # where it was loaded from (for debugging)
    project_level: bool = False    # True = from project, False = user-global

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "tag": self.tag,
            "source_path": self.source_path,
            "project_level": self.project_level,
            "body_chars": len(self.body),
        }


# ── Frontmatter parser ───────────────────────────────────────────────

_FRONTMATTER_RE = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n(.*)$",
    re.DOTALL,
)


def _parse_frontmatter(content: str) -> Tuple[Dict[str, Any], str]:
    """Parse YAML-like frontmatter from a markdown file.

    We use a minimal parser (not full YAML) because skills should be
    simple key:value pairs. Returns (metadata_dict, body_markdown).
    """
    m = _FRONTMATTER_RE.match(content)
    if not m:
        return {}, content

    raw_meta = m.group(1)
    body = m.group(2)

    meta: Dict[str, Any] = {}
    for line in raw_meta.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower().replace("-", "_").replace(" ", "_")
        value = value.strip().strip('"').strip("'")
        meta[key] = value

    return meta, body


def _slugify(name: str) -> str:
    """Convert a skill name to a URL-safe slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "unnamed"


# ── Skill loader ─────────────────────────────────────────────────────

def _load_skill_file(path: Path, project_level: bool) -> Optional[Skill]:
    """Load a single SKILL.md or *.md file into a Skill object."""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        logger.warning("[skills] failed to read %s: %s", path, e)
        return None

    meta, body = _parse_frontmatter(content)

    # Determine id: frontmatter > filename > slugified name
    skill_id = meta.get("id") or path.stem
    name = meta.get("name") or path.stem.replace("_", " ").replace("-", " ").title()
    description = meta.get("description") or meta.get("desc") or ""
    tag = meta.get("tag") or meta.get("category") or "general"

    # v1.2.0: Validate required fields
    if not name or len(name.strip()) == 0:
        logger.warning("[skills] %s missing required field: name", path)
        return None

    if not description or len(description.strip()) == 0:
        logger.warning("[skills] %s missing required field: description", path)
        return None

    # v1.2.0: Validate body is not empty
    if not body or len(body.strip()) == 0:
        logger.warning("[skills] %s has empty body", path)
        return None

    # v1.2.0: Validate body has minimum content (at least 50 chars)
    if len(body.strip()) < 50:
        logger.warning("[skills] %s body too short (%d chars, minimum 50)", path, len(body.strip()))
        return None

    return Skill(
        id=skill_id,
        name=name,
        description=description,
        tag=tag,
        body=body.strip(),
        source_path=str(path),
        project_level=project_level,
    )


def load_all_skills(project_root: Optional[Path] = None) -> List[Skill]:
    """Load all skills from project-level and user-level directories.

    Priority (later sources override earlier ones with the same id):
      1. User-global: ~/.tera_pilot/skills/*/SKILL.md and ~/.tera_pilot/skills/*.md
      2. Project-level: <project>/.tera_pilot/skills/*/SKILL.md and
         <project>/.tera_pilot/skills/*.md

    Project-level skills override user-global ones with the same id —
    this lets a project customise a global skill for its own needs.
    """
    skills_by_id: Dict[str, Skill] = {}

    # Normalise project_root to Path
    if project_root:
        project_root = Path(project_root)

    # 1. User-global skills
    user_skill_dir = Path.home() / ".tera_pilot" / "skills"
    if user_skill_dir.is_dir():
        for skill in _iter_skill_dir(user_skill_dir, project_level=False):
            skills_by_id[skill.id] = skill

    # 2. Project-level skills (override user-global)
    if project_root and project_root.is_dir():
        proj_skill_dir = project_root / ".tera_pilot" / "skills"
        if proj_skill_dir.is_dir():
            for skill in _iter_skill_dir(proj_skill_dir, project_level=True):
                skills_by_id[skill.id] = skill

    return list(skills_by_id.values())


def _iter_skill_dir(skill_dir: Path, project_level: bool) -> List[Skill]:
    """Iterate a skills directory, loading both SKILL.md and *.md files.

    Supports two layouts:
      1. Flat:    skills/python-architect.md, skills/ui-polish.md
      2. Nested:  skills/python-architect/SKILL.md, skills/ui-polish/SKILL.md
                  (the directory name is the skill id if frontmatter
                   doesn't specify one; supplementary files in the same
                   directory are NOT loaded automatically — the skill
                   body can reference them by relative path and the
                   agent can read_file them)
    """
    skills: List[Skill] = []
    try:
        entries = sorted(skill_dir.iterdir())
    except OSError as e:
        logger.warning("[skills] failed to list %s: %s", skill_dir, e)
        return skills

    for entry in entries:
        if entry.name.startswith("."):
            continue
        if entry.is_dir():
            # Look for SKILL.md inside
            skill_md = entry / "SKILL.md"
            if skill_md.exists():
                skill = _load_skill_file(skill_md, project_level=project_level)
                # v1.0.11: if the frontmatter didn't specify an id,
                # use the directory name as the skill id (not "SKILL")
                if skill and (not skill.id or skill.id.lower() == "skill"):
                    skill.id = _slugify(entry.name)
                if skill:
                    skills.append(skill)
        elif entry.is_file() and entry.suffix.lower() == ".md":
            skill = _load_skill_file(entry, project_level=project_level)
            if skill:
                skills.append(skill)

    return skills


# ── Skill catalog (for system prompt injection) ──────────────────────

def build_skill_catalog(skills: List[Skill]) -> str:
    """Build a compact catalog of available skills for the system prompt.

    The catalog lists each skill's id, name, tag, and description so
    the agent can decide which skill to request via get_skill. This
    keeps the full skill bodies OUT of the system prompt (saving
    context tokens) until the agent explicitly asks for one.
    """
    if not skills:
        return ""

    lines = ["# Available skills", ""]
    lines.append("You can request the full instructions for any skill by calling:")
    lines.append('  {"tool": "get_skill", "args": {"id": "skill_id"}}')
    lines.append("")
    lines.append("Skills are reusable instruction packages. Activate one when the")
    lines.append("task matches its description. Do NOT activate a skill unless it fits.")
    lines.append("")
    for s in skills:
        tag_str = f" [{s.tag}]" if s.tag and s.tag != "general" else ""
        level_str = " (project)" if s.project_level else ""
        lines.append(f"- **{s.id}**{tag_str}{level_str}: {s.name}")
        if s.description:
            lines.append(f"  {s.description}")
    return "\n".join(lines)


def get_skill_body(skills: List[Skill], skill_id: str) -> Optional[str]:
    """Return the full body text of a skill by id, or None if not found."""
    for s in skills:
        if s.id == skill_id:
            return s.body
    return None


# ── Built-in skills (legacy from v1.0.3) ─────────────────────────────
# These are kept as a fallback so users without any SKILL.md files
# still see a non-empty skill list. They can be overridden by
# user-global or project-level skills with the same id.

_BUILTIN_SKILLS: List[Skill] = [
    Skill(
        id="python_architect",
        name="Python Architect",
        description="Designs clean package structures, dependency boundaries, layered architecture. Use when scaffolding a new Python project or reorganising an existing one.",
        tag="architect",
        body=(
            "# SKILL: Python Architect\n\n"
            "You design clean, layered Python projects.\n"
            "Rules:\n"
            "- Separate concerns: routers -> services -> repositories -> models.\n"
            "- No business logic in route handlers.\n"
            "- Type-hint every public function; run mypy --strict in your head.\n"
            "- Prefer composition over inheritance.\n"
            "- Every module has a one-line docstring stating its responsibility.\n"
            "- If a file exceeds 300 lines, propose splitting it.\n"
        ),
    ),
    Skill(
        id="ui_polish",
        name="UI Polish",
        description="Pixel-perfect CSS, motion systems, accessibility, responsive behavior. Use when the task involves frontend styling or layout.",
        tag="frontend",
        body=(
            "# SKILL: UI Polish\n\n"
            "You produce pixel-perfect frontends.\n"
            "Rules:\n"
            "- Respect a design system: spacing scale, type scale, motion tokens.\n"
            "- Never use pure black or pure white.\n"
            "- All animations use cubic-bezier easing, never linear for organic motion.\n"
            "- Test keyboard navigation and screen-reader labels.\n"
            "- Mobile-first: layout works at 375px before 1440px.\n"
        ),
    ),
    Skill(
        id="security_auditor",
        name="Security Auditor",
        description="Threat models, OWASP, secrets hygiene, sandboxing, least privilege. Use when reviewing code for security issues.",
        tag="security",
        body=(
            "# SKILL: Security Auditor\n\n"
            "You review code for security issues.\n"
            "Rules:\n"
            "- Treat all input as hostile until proven otherwise.\n"
            "- Check OWASP Top 10 by default.\n"
            "- Never log secrets, tokens, or PII.\n"
            "- Prefer parameterized queries; reject string-built SQL.\n"
            "- Sandbox subprocess calls; whitelist binaries.\n"
        ),
    ),
    Skill(
        id="test_engineer",
        name="Test Engineer",
        description="Property tests, fuzzing, fixtures, coverage of edge cases. Use when writing or reviewing tests.",
        tag="testing",
        body=(
            "# SKILL: Test Engineer\n\n"
            "You write comprehensive tests.\n"
            "Rules:\n"
            "- Cover happy path, edge cases, error paths.\n"
            "- Use property-based tests where applicable (hypothesis).\n"
            "- One assertion per test function when possible.\n"
            "- Name tests after the behaviour, not the implementation.\n"
            "- Mock at the boundary, not at the core.\n"
        ),
    ),
    Skill(
        id="devops",
        name="DevOps",
        description="CI/CD, IaC, containers, blue-green deploys, incident response. Use for deployment or infrastructure tasks.",
        tag="devops",
        body=(
            "# SKILL: DevOps\n\n"
            "You automate deployments and infrastructure.\n"
            "Rules:\n"
            "- Infrastructure as code (Terraform, Pulumi).\n"
            "- CI runs on every push; CD requires approval.\n"
            "- Blue-green or canary for production deploys.\n"
            "- Rollback plan for every change.\n"
            "- Monitor after deploy; alert on SLO breach.\n"
        ),
    ),
    # ── v1.2.0 skills ────────────────────────────────────────────────
    Skill(
        id="office_document_author",
        name="Office Document Author",
        description="v1.2.0 — Build clean .docx reports with proper heading hierarchy, tables, and consistent styling via the office_* tools. Use when the user asks for a Word document, report, memo, or written deliverable.",
        tag="office",
        body=(
            "# SKILL: Office Document Author (v1.2.0)\n\n"
            "You create well-structured Word documents (.docx) using Tera Pilot's\n"
            "office_* tools. You never generate Python boilerplate that\n"
            "imports python-docx — call the tools directly.\n\n"
            "## Workflow\n\n"
            "1. `office_create(path='report.docx')` — start with a blank doc.\n"
            "2. Add ONE Heading 1 for the title, then a short intro paragraph.\n"
            "3. For each major section: Heading 2 → paragraph(s) → optional table.\n"
            "4. For data: prefer `office_add_table` over bulleted prose — a table\n"
            "   with `header=true` is the cleanest way to present structured info.\n"
            "5. End with a `## Summary` Heading 2 and a one-paragraph wrap-up.\n\n"
            "## Style rules\n\n"
            "- Never nest headings deeper than Heading 3 — flatten the outline.\n"
            "- Tables: 3-7 columns, ≤10 rows. Bigger tables go on a separate\n"
            "  .xlsx worksheet (switch to the spreadsheet skill).\n"
            "- Paragraphs: 2-5 sentences. No wall-of-text.\n"
            "- Headings are sentence case, not Title Case: \"Summary of findings\",\n"
            "  not \"Summary Of Findings\".\n"
            "- Use `office_add_paragraph` with `bold=true` only for the lead-in\n"
            "  of a paragraph that defines a term; never bold whole sentences.\n\n"
            "## Common pitfalls\n\n"
            "- DO NOT call `write_file` to create a .docx — it produces a binary\n"
            "  blob, not a real Word document. Always use `office_create`.\n"
            "- DO NOT call `run_code` to import python-docx and write the file\n"
            "  that way — it wastes tokens and breaks if the library isn't\n"
            "  installed. The office_* tools are the supported path.\n"
            "- DO NOT use `office_save_as` to convert formats — it only saves a\n"
            "  copy in the SAME format. For .pdf output, tell the user to open\n"
            "  the file in Word/LibreOffice and export.\n\n"
            "## Verification\n\n"
            "Before emitting `final_answer`, call `office_view(path='report.docx',\n"
            "mode='outline')` and confirm the outline matches what the user asked\n"
            "for. If a section is missing, add it. If a section is in the wrong\n"
            "order, tell the user — `office_*` tools don't reorder sections.\n"
        ),
    ),
    Skill(
        id="office_spreadsheet_analyst",
        name="Office Spreadsheet Analyst",
        description="v1.2.0 — Build .xlsx workbooks with formulas, formatting, and charts via the office_* tools. Use when the user asks for an Excel file, financial model, data table, or chart-bearing spreadsheet.",
        tag="office",
        body=(
            "# SKILL: Office Spreadsheet Analyst (v1.2.0)\n\n"
            "You create Excel workbooks (.xlsx) using Tera Pilot's office_* tools.\n"
            "You never generate Python boilerplate that imports openpyxl —\n"
            "call the tools directly.\n\n"
            "## Workflow\n\n"
            "1. `office_create(path='model.xlsx')` — start blank.\n"
            "2. Rename Sheet1 to a descriptive name via `office_add_sheet` (then\n"
            "   write into the new sheet; Sheet1 stays empty).\n"
            "3. Lay out a header row in row 1 using `office_fill_sheet` with a\n"
            "   2D array — first row = headers, subsequent rows = data.\n"
            "4. Format the header row: `office_set_cell_format` for each header\n"
            "   cell with `bold=true`, `bg_color=#1F2937`, `font_color=#FFFFFF`.\n"
            "5. Add formulas via `office_set_cell` with a string starting with `=`\n"
            "   (e.g. `=SUM(B2:B10)`). openpyxl evaluates them on open.\n"
            "6. Add a chart with `office_add_chart` — bar for comparisons, line\n"
            "   for trends, pie for parts-of-a-whole (≤6 slices).\n\n"
            "## Layout rules\n\n"
            "- One sheet per concern. Don't cram 5 tables onto one sheet.\n"
            "- Header row frozen (set via format on row 1, not via a tool —\n"
            "  openpyxl freeze_panes isn't exposed; just leave row 1 as header).\n"
            "- Numeric columns right-aligned, text columns left-aligned.\n"
            "- Never use a cell as both a label and a value.\n"
            "- For dates: write ISO strings (YYYY-MM-DD); Excel parses them.\n\n"
            "## Formula rules\n\n"
            "- Prefer `SUM`/`AVERAGE`/`COUNTIF` over manual arithmetic.\n"
            "- Use absolute refs (`$B$2`) for constants the user might tweak.\n"
            "- Never hard-code a value that's already in another cell — reference it.\n"
            "- For totals: sum the data range, not the individual cells.\n\n"
            "## Chart rules\n\n"
            "- Bar chart: ≤8 bars, sorted descending by value.\n"
            "- Line chart: ≤4 series. More than 4 = unreadable.\n"
            "- Pie chart: ≤6 slices. Aggregate the rest into \"Other\".\n"
            "- Always pass `title=...` — a chart without a title is useless.\n"
            "- Anchor the chart to the right of the data (e.g. `anchor='D2'`).\n\n"
            "## Verification\n\n"
            "Before `final_answer`, call `office_view(path='model.xlsx',\n"
            "mode='stats')` and confirm sheet count + cell counts match\n"
            "expectations. If a sheet is missing, you forgot `office_add_sheet`.\n"
        ),
    ),
    Skill(
        id="office_presentation_designer",
        name="Office Presentation Designer",
        description="v1.2.0 — Build .pptx decks with clear slide hierarchy, readable text boxes, and meaningful shapes via the office_* tools. Use when the user asks for a PowerPoint, slide deck, or presentation.",
        tag="office",
        body=(
            "# SKILL: Office Presentation Designer (v1.2.0)\n\n"
            "You create PowerPoint decks (.pptx) using Tera Pilot's office_* tools.\n"
            "You never generate Python boilerplate that imports python-pptx —\n"
            "call the tools directly.\n\n"
            "## Workflow\n\n"
            "1. `office_create(path='deck.pptx')` — start blank (1 title slide).\n"
            "2. Add a title slide: `office_add_slide(layout='title',\n"
            "   title='...', subtitle='...')`.\n"
            "3. For each content section, add a `title` slide first, then 2-4\n"
            "   `blank` slides with text boxes and shapes for the content.\n"
            "4. Use `office_add_text` on blank slides — place at (1in, 1in) with\n"
            "   width 8in, height 4in. Don't fill the whole slide edge-to-edge.\n"
            "5. Use `office_add_shape` for emphasis — a rectangle behind a key\n"
            "   stat, an arrow between two ideas, a diamond for a decision.\n\n"
            "## Slide-count discipline\n\n"
            "- ≤12 slides for a 5-minute talk (≈30s/slide).\n"
            "- ≤25 slides for a 15-minute talk.\n"
            "- One idea per slide. If you have 3 ideas, split into 3 slides.\n"
            "- Title slide + agenda + content + summary + Q&A = standard arc.\n\n"
            "## Text rules\n\n"
            "- ≤6 bullets per slide. ≤8 words per bullet. ALWAYS.\n"
            "- Font size ≥24pt for body text, ≥36pt for slide titles.\n"
            "  (Use `size=24` or `size=36` in `office_add_text`.)\n"
            "- Use `bold=true` for the FIRST 2-3 words of a bullet to signal\n"
            "  the key noun. Never bold the whole bullet.\n"
            "- No paragraphs on slides. Paragraphs go in the speaker notes —\n"
            "  but office_* tools don't expose notes, so put them in your\n"
            "  `final_answer` instead and tell the user to copy them in.\n\n"
            "## Shape rules\n\n"
            "- Rectangle: callouts, stat cards, process steps.\n"
            "- Arrow: flow direction, cause → effect.\n"
            "- Diamond: decisions, branches.\n"
            "- Ellipse: highlights, people, roles.\n"
            "- Always pass `text=...` on a shape if it's labelled. A shape\n"
            "  without text is just decoration — usually noise.\n"
            "- Shapes anchored at (1in, 1in) with 2in × 1in are a good default.\n\n"
            "## Verification\n\n"
            "Before `final_answer`, call `office_view(path='deck.pptx',\n"
            "mode='outline')` and confirm the slide list reads like a coherent\n"
            "story. If two adjacent slides have the same title, merge them.\n"
        ),
    ),
    Skill(
        id="agent_orchestrator",
        name="Agent Orchestrator",
        description="v1.2.0 — Heavy Code skill. Decompose multi-file tasks into disjoint slices, dispatch parallel subagents, run an adversarial review, and act on watchdog evidence. Use when the Heavy Code task touches 3+ unrelated files.",
        tag="heavy_code",
        body=(
            "# SKILL: Agent Orchestrator (v1.2.0)\n\n"
            "You orchestrate multi-agent Heavy Code runs. You decompose the task\n"
            "into disjoint slices, dispatch subagents, review the combined diff,\n"
            "and act on watchdog evidence — never killing subagents yourself.\n\n"
            "## When to orchestrate (and when NOT to)\n\n"
            "Orchestrate when:\n"
            "- The task touches ≥3 files that are NOT in the same tight cluster.\n"
            "- Files have independent change sets (no shared data flow).\n"
            "- The work is large enough that one agent would hit iteration cap.\n\n"
            "Do NOT orchestrate when:\n"
            "- The task is a single-file edit. Just do it.\n"
            "- The files are tightly coupled (one change forces another).\n"
            "  Use a single agent instead — coordination cost > parallelism gain.\n"
            "- The task is exploratory (\"investigate why X is slow\"). Use a\n"
            "  single agent with `read_file` + `git_diff`.\n\n"
            "## Slice decomposition\n\n"
            "Before dispatching, write a one-paragraph change-skeleton PER SLICE\n"
            "in your thought. Each skeleton must name:\n"
            "1. The file(s) the slice touches (disjoint from other slices).\n"
            "2. The function/class signatures that change.\n"
            "3. The data flow in/out of the slice.\n"
            "4. The invariant the slice preserves.\n\n"
            "Slices with overlapping file sets will conflict — reject and\n"
            "re-decompose before dispatching.\n\n"
            "## Dispatching\n\n"
            "Use `spawn_multi_agents` with one task per slice. Each task gets:\n"
            "- `goal`: the change-skeleton paragraph.\n"
            "- `role`: 'implementer' for code, 'tester' for tests, 'reviewer'\n"
            "  for review-only (read_file + git_diff).\n"
            "- `max_iterations`: 4-6 for narrow slices, 8 for complex ones.\n\n"
            "Never dispatch >5 subagents in parallel — the daily quota is shared.\n\n"
            "## Adversarial review\n\n"
            "After all builders return and you've integrated their results,\n"
            "spawn ONE `role='reviewer'` subagent with the goal:\n"
            "  \"Review the combined diff in <files>. Identify gaps, missing\n"
            "  tests, broken imports, type errors, and integration issues.\n"
            "  Return a numbered list of issues.\"\n\n"
            "If the reviewer finds gaps, fix them directly (no second builder\n"
            "wave — your context is already loaded). Then call `self_verify`\n"
            "before `final_answer`.\n\n"
            "## Watchdog evidence\n\n"
            "The runtime exposes `_watchdog_check` (called automatically between\n"
            "subagent waves). It returns typed evidence:\n"
            "- `ALL_DONE` — all subagents finished. Proceed.\n"
            "- `STALL` — a subagent has been in-flight >120s. Decide:\n"
            "  wait one more iteration (if the task is long but progressing),\n"
            "  OR proceed without its result and note the gap in `final_answer`.\n"
            "- `REPEAT` — a subagent is producing the same error 3+ times.\n"
            "  This is a stuck loop — proceed without its result, note it.\n\n"
            "The watchdog NEVER kills subagents. You decide what to do with\n"
            "the evidence. Killing a subagent mid-LLM-call can corrupt file\n"
            "state and leak handles — let it finish or time out.\n\n"
            "## Quota awareness\n\n"
            "Subagents inherit YOUR daily quota counter. Spawning 5 implementers\n"
            "costs 5+ LLM round-trips against the daily limit. Be deliberate.\n"
            "If you're at 7/10 heavy_code runs, prefer a single-agent approach.\n"
        ),
    ),
    Skill(
        id="self_verifier",
        name="Self Verifier",
        description="v1.2.0 — General/Heavy Code skill. Run a closing verification pass on every task that touches files. Re-reads the touched files as a fresh observation so you can confirm the goal is actually met before reporting back to the user.",
        tag="general",
        body=(
            "# SKILL: Self Verifier (v1.2.0)\n\n"
            "You verify your own work before reporting back to the user. This is\n"
            "the lightweight version of the architect-loop \"fresh-context\n"
            "verifier subagent\" — instead of spawning a fresh-context agent\n"
            "(which costs 2-4 extra LLM round-trips), you call `self_verify`\n"
            "which re-reads the touched files as a deterministic tool, and your\n"
            "NEXT iteration is the verification LLM call.\n\n"
            "## When to call self_verify\n\n"
            "Call `self_verify` ONCE, right before `final_answer`, on every task\n"
            "that touched files (write_file, str_replace, apply_diff, office_*).\n\n"
            "Do NOT call self_verify when:\n"
            "- The task was purely informational (\"explain what X does\").\n"
            "- The task was a single read-only inspection.\n"
            "- You're mid-iteration — it's a CLOSING check, not a mid-run check.\n\n"
            "## How to call it\n\n"
            "```\n"
            "{\"tool\": \"self_verify\",\n"
            " \"args\": {\n"
            "   \"goal\": \"<restate the user's actual goal in one sentence>\",\n"
            "   \"touched_files\": [\"path/to/file1.py\", \"path/to/file2.py\"]\n"
            " }}\n"
            "```\n\n"
            "The `touched_files` list is auto-populated by the runtime — you can\n"
            "pass an empty list and the runtime fills it from its tracking.\n"
            "But passing the explicit list is safer (you know what YOU intended\n"
            "to touch, the runtime knows what you ACTUALLY touched).\n\n"
            "## Reading the verify output\n\n"
            "`self_verify` returns the contents of every touched file as a fresh\n"
            "observation. Treat it like a code review of your own diff:\n\n"
            "1. Compare the actual file state against the stated `goal`.\n"
            "2. If a file is missing a change you intended → fix it now, then\n"
            "   re-call self_verify (only if you made non-trivial edits).\n"
            "3. If a file has a bug you didn't notice → fix it now.\n"
            "4. If everything matches → emit `final_answer`.\n\n"
            "## Common pitfalls\n\n"
            "- DO NOT skip self_verify because \"the task was simple\". The simple\n"
            "  tasks are where off-by-one errors and missing imports hide.\n"
            "- DO NOT call self_verify more than twice. If the second verify\n"
            "  still shows gaps, surface them in `final_answer` instead of\n"
            "  looping — the user needs to know.\n"
            "- DO NOT include self_verify's output in `final_answer`. The user\n"
            "  doesn't need to see the verification log — they need the result.\n"
            "  Just say \"Verified — <one-line summary of what was checked>\".\n\n"
            "## Costs\n\n"
            "`self_verify` itself is FREE — it's a deterministic file re-read,\n"
            "no LLM call. Your NEXT iteration (the verification LLM call) is the\n"
            "only cost. That call would happen anyway as you compose your\n"
            "final answer — so self_verify is essentially zero-overhead.\n"
        ),
    ),
    Skill(
        id="activity_auditor",
        name="Activity Auditor",
        description="v1.2.0 — Meta-skill. When the user asks 'what did you do?' or 'show me your work' or 'undo what you just did', consult the activity log to ground your answer in what actually happened — not what you think happened.",
        tag="general",
        body=(
            "# SKILL: Activity Auditor (v1.2.0)\n\n"
            "You answer questions about your own past actions by consulting the\n"
            "activity log — the process-wide audit trail of every tool call,\n"
            "file write, shell command, office operation, and subagent spawn\n"
            "you've made in this Tera Pilot session.\n\n"
            "## When to apply this skill\n\n"
            "Apply when the user asks any of:\n"
            "- \"What did you do?\" / \"What did you change?\"\n"
            "- \"Show me your work\" / \"Show me the steps you took.\"\n"
            "- \"Undo what you just did\" / \"Revert that.\"\n"
            "- \"Why did you run X?\" / \"What was that command for?\"\n"
            "- \"Did you touch file Y?\"\n\n"
            "Do NOT apply when the user is asking about the codebase, the\n"
            "task itself, or future plans. This skill is for past actions\n"
            "in the current session only.\n\n"
            "## How to consult the log\n\n"
            "The activity log is visible to the user in the Activity Stream\n"
            "panel (sidebar → Activity, or ⌘⇧A). You don't have a tool to\n"
            "query it directly, but your conversation history already\n"
            "contains your tool calls and their results. To answer \"what\n"
            "did you do?\":\n\n"
            "1. Re-read your own tool calls from this conversation.\n"
            "2. Group them by category: shell commands, file writes, code\n"
            "   edits, office operations, subagent spawns.\n"
            "3. Present a concise summary, grouped, with the most recent\n"
            "   first. For each group, give: count + paths + brief purpose.\n\n"
            "## Format\n\n"
            "```\n"
            "Here's what I did in this session:\n\n"
            "**Files written (3):**\n"
            "- `report.md` — created the initial report skeleton\n"
            "- `utils.py` — refactored the auth helper\n"
            "- `tests/test_utils.py` — added 4 unit tests\n\n"
            "**Shell commands (2):**\n"
            "- `pytest tests/test_utils.py -v` — ran the new tests (all passed)\n"
            "- `git status` — checked working tree before committing\n\n"
            "**Office operations (1):**\n"
            "- `deck.pptx` — added 5 slides (title + 3 content + summary)\n\n"
            "Want me to undo any of these? Just say which.\n"
            "```\n\n"
            "## Undo support\n\n"
            "If the user asks to undo something:\n"
            "1. Identify the file from the activity summary.\n"
            "2. If you wrote it earlier in THIS session, you can re-write the\n"
            "   previous version (you have it in conversation history).\n"
            "3. If the file existed before your changes, use `undo_write` if\n"
            "   available, OR ask the user to confirm before overwriting.\n"
            "4. For shell commands: most are non-reversible. Tell the user\n"
            "   what was done and offer to do the opposite if reasonable.\n\n"
            "## Honesty rule\n\n"
            "If the activity log shows you did something the user didn't\n"
            "expect (e.g. you ran a command they didn't ask for, or wrote a\n"
            "file they didn't mention), ACKNOWLEDGE IT. Don't bury it.\n"
            "\"I also ran X because Y\" is correct. Hiding it is wrong.\n"
        ),
    ),
]


def load_all_skills_with_builtins(project_root: Optional[Path] = None) -> List[Skill]:
    """Load user + project skills, plus built-in skills as a fallback.

    Built-in skills have the lowest priority — a user-global or
    project-level skill with the same id overrides them.
    """
    skills_by_id: Dict[str, Skill] = {}

    # 1. Built-in skills (lowest priority)
    for s in _BUILTIN_SKILLS:
        skills_by_id[s.id] = s

    # 2. User-global + project-level skills (override built-ins)
    for s in load_all_skills(project_root):
        skills_by_id[s.id] = s

    return list(skills_by_id.values())
