"""
Capability Catalog — discoverable templates for non-technical users.

Goal (G7): "I don't know what I can ask the agent to do."
Solution: a curated catalog of pre-built capability templates that users
can browse, pick, fill in placeholders, and send to the agent — without
needing to know the agent's tool vocabulary.

Design mirrors `slash_commands.py` and `skill_loader.py` (front-matter
driven, project + user-global layers), but with three key differences:

1. **Capabilities are task recipes**, not raw prompt snippets. Each one
   ships with: title, category, description, example output, the
   template body with $PLACEHOLDERS, and an optional `follow_ups` list
   pointing to related capability ids.
2. **Built-in catalog** ships with Tera Pilot (~20 templates across 7
   categories) so a fresh install is immediately useful. Users can
   override any built-in by placing a file with the same id under
   ``~/.tera_pilot/capabilities/`` or ``<project>/.tera_pilot/capabilities/``.
3. **No code execution at load time** — capabilities are pure data.
   Placeholder substitution happens in `fill_template()` when the user
   actually picks a capability.

File format (Markdown + YAML front-matter, same as SKILL.md):

    ---
    id: refactor-extract-function
    name: Extract Function
    category: refactor
    description: Pull a code block out into a named function
    placeholders:
      - name: file_path
        description: Path to the source file
        required: true
      - name: line_range
        description: "Lines to extract (e.g. 42-58)"
        required: true
    follow_ups:
      - write-unit-tests
      - add-type-annotations
    ---
    Open $file_path$, find the code at lines $line_range$, and extract
    it into a new function named after what it does. Replace the
    original block with a call to the new function. Preserve behaviour.

The body uses ``$placeholder$`` (dollar-delimited) instead of
``$placeholder`` (single-dollar) because the latter collides with
shell-variable syntax inside bash code blocks in some templates.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── Data classes ─────────────────────────────────────────────────────

@dataclass
class Placeholder:
    """One substitution slot in a capability template."""
    name: str
    description: str = ""
    required: bool = True
    default: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "required": self.required,
            "default": self.default,
        }


@dataclass
class Capability:
    """One discoverable capability template."""
    id: str
    name: str
    category: str
    description: str
    body: str = ""
    placeholders: List[Placeholder] = field(default_factory=list)
    follow_ups: List[str] = field(default_factory=list)
    source_path: str = ""
    project_level: bool = False
    builtin: bool = False

    def to_dict(self, *, include_body: bool = False) -> Dict[str, Any]:
        out = {
            "id": self.id,
            "name": self.name,
            "category": self.category,
            "description": self.description,
            "placeholders": [p.to_dict() for p in self.placeholders],
            "follow_ups": list(self.follow_ups),
            "source_path": self.source_path,
            "project_level": self.project_level,
            "builtin": self.builtin,
            "body_chars": len(self.body),
        }
        if include_body:
            out["body"] = self.body
        return out


# ── Frontmatter parser (same minimal style as skill_loader) ──────────

_FRONTMATTER_RE = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n(.*)$",
    re.DOTALL,
)

# Matches $placeholder_name$ (dollar-delimited, allows underscores/dashes)
_PLACEHOLDER_USE_RE = re.compile(r"\$([a-zA-Z_][a-zA-Z0-9_-]*)\$")


def _parse_frontmatter(content: str) -> Tuple[Dict[str, Any], str]:
    """Parse YAML-like frontmatter. Returns (metadata, body)."""
    m = _FRONTMATTER_RE.match(content)
    if not m:
        return {}, content

    raw_meta = m.group(1)
    body = m.group(2)

    meta: Dict[str, Any] = {}
    current_key: Optional[str] = None
    current_list: List[str] = []

    def _flush_list() -> None:
        nonlocal current_key, current_list
        if current_key and current_list:
            meta[current_key] = list(current_list)
        current_key = None
        current_list = []

    for line in raw_meta.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # List item under a previous key
        if stripped.startswith("- ") and current_key is not None:
            item = stripped[2:].strip().strip('"').strip("'")
            current_list.append(item)
            continue
        # New key
        _flush_list()
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key = key.strip().lower().replace("-", "_").replace(" ", "_")
        value = value.strip().strip('"').strip("'")
        if value:
            meta[key] = value
        else:
            # Could be a multi-line list — start collecting
            current_key = key
            current_list = []

    _flush_list()
    return meta, body


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "unnamed"


def _parse_placeholder_list(raw: Any) -> List[Placeholder]:
    """Parse the ``placeholders`` frontmatter entry.

    Accepts either a list of strings (treated as names with no metadata)
    or a list of "name|description|required" strings.
    """
    if not raw:
        return []
    if isinstance(raw, str):
        raw = [raw]
    out: List[Placeholder] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        parts = [p.strip() for p in item.split("|")]
        name = parts[0]
        if not name:
            continue
        desc = parts[1] if len(parts) > 1 else ""
        required_str = parts[2].lower() if len(parts) > 2 else "true"
        required = required_str not in ("false", "no", "0", "optional")
        default = parts[3] if len(parts) > 3 else ""
        out.append(Placeholder(
            name=name, description=desc,
            required=required, default=default,
        ))
    return out


def _parse_follow_ups(raw: Any) -> List[str]:
    if not raw:
        return []
    if isinstance(raw, str):
        return [raw]
    return [str(x).strip() for x in raw if str(x).strip()]


def _scan_placeholders_in_body(body: str) -> List[str]:
    """Find every $name$ occurrence in the body, in order of first use."""
    seen: List[str] = []
    for m in _PLACEHOLDER_USE_RE.finditer(body):
        name = m.group(1)
        if name not in seen:
            seen.append(name)
    return seen


# ── Built-in catalog ─────────────────────────────────────────────────

# Curated starter set. Each entry is (id, name, category, description,
# placeholders, follow_ups, body). Body uses $placeholder$ syntax.
_BUILTIN_CAPABILITIES: List[Tuple[str, str, str, str, List[Placeholder], List[str], str]] = [
    # ── Code ──────────────────────────────────────────────────────
    (
        "write-new-feature",
        "Write a New Feature",
        "code",
        "Describe a feature in plain English; the agent designs, writes, and verifies it.",
        [
            Placeholder("language", "Programming language (python, typescript, rust, ...)", True, "python"),
            Placeholder("feature", "What the feature should do, in 1-2 sentences", True),
            Placeholder("file_path", "Path to the file to create or edit (optional)", False),
        ],
        ["write-unit-tests", "add-type-annotations"],
        "Implement the following feature in $language$.\n\n"
        "Feature: $feature$\n\n"
        "File: $file_path$\n\n"
        "Steps:\n"
        "1. Read the surrounding code to match existing style.\n"
        "2. Implement the feature with clear, idiomatic $language$.\n"
        "3. Add a docstring / comment block explaining the design choice.\n"
        "4. Verify the file parses (run the linter or compiler).\n"
        "5. Summarise the change in 3 bullets at the end.",
    ),
    (
        "fix-bug",
        "Fix a Bug",
        "code",
        "Point the agent at a failing test or an error message; it locates and patches the root cause.",
        [
            Placeholder("symptom", "Error message, failing test, or observed misbehaviour", True),
            Placeholder("file_hint", "File or directory where you suspect the bug lives (optional)", False),
        ],
        ["write-unit-tests", "add-type-annotations"],
        "A bug is reported. Reproduce and fix it.\n\n"
        "Symptom: $symptom$\n"
        "Suspected location: $file_hint$\n\n"
        "Steps:\n"
        "1. Search the codebase for the symptom's signature.\n"
        "2. Identify the root cause (not just the symptom).\n"
        "3. Write or update a test that reproduces the bug.\n"
        "4. Apply the minimal fix that resolves the root cause.\n"
        "5. Re-run the test to confirm. Summarise cause + fix.",
    ),
    (
        "explain-code",
        "Explain a File or Function",
        "code",
        "Plain-English walkthrough of what a chunk of code does, line-by-line where it matters.",
        [
            Placeholder("file_path", "Path to the file to explain", True),
            Placeholder("depth", "brief | detailed | line-by-line", False, "detailed"),
        ],
        [],
        "Explain the code at $file_path$.\n\n"
        "Depth: $depth$\n\n"
        "Cover:\n"
        "- Overall purpose of the file / module.\n"
        "- Public API (functions / classes / exports).\n"
        "- Key data structures and their roles.\n"
        "- Non-obvious control flow or side effects.\n"
        "- Dependencies on other modules.\n\n"
        "Use plain English. Avoid restating the code verbatim — explain WHY, not WHAT.",
    ),
    # ── Refactor ──────────────────────────────────────────────────
    (
        "refactor-extract-function",
        "Extract Function",
        "refactor",
        "Pull a code block out into a named function so it can be reused and tested.",
        [
            Placeholder("file_path", "Path to the source file", True),
            Placeholder("line_range", "Lines to extract (e.g. 42-58)", True),
            Placeholder("function_name", "Suggested function name (optional)", False),
        ],
        ["write-unit-tests"],
        "Open $file_path$, find the code at lines $line_range$, and extract it into a new function.\n\n"
        "Suggested name: $function_name$\n\n"
        "Steps:\n"
        "1. Read the block to understand its inputs and outputs.\n"
        "2. Create a new function with appropriate parameters and return type.\n"
        "3. Replace the original block with a call to the new function.\n"
        "4. Preserve behaviour exactly.\n"
        "5. Add a one-line docstring to the new function.",
    ),
    (
        "refactor-simplify",
        "Simplify Complex Code",
        "refactor",
        "Reduce nesting, remove dead branches, and clarify intent without changing behaviour.",
        [
            Placeholder("file_path", "Path to the file", True),
        ],
        ["write-unit-tests"],
        "Simplify the code at $file_path$ while preserving behaviour.\n\n"
        "Apply:\n"
        "- Flatten nested conditionals (guard clauses).\n"
        "- Remove dead code and unreachable branches.\n"
        "- Replace magic numbers with named constants.\n"
        "- Split long functions (>40 lines) at natural seams.\n"
        "- Rename misleading variables.\n\n"
        "Verify with the existing test suite. Summarise each change as a bullet.",
    ),
    (
        "add-type-annotations",
        "Add Type Annotations",
        "refactor",
        "Backfill type hints across a file or module for safer refactoring.",
        [
            Placeholder("file_path", "Path to the file", True),
            Placeholder("language", "python | typescript | ...", False, "python"),
        ],
        ["write-unit-tests"],
        "Add type annotations to $file_path$.\n\n"
        "Language: $language$\n\n"
        "Rules:\n"
        "- Annotate every function signature (params + return).\n"
        "- Use the most specific type that fits (e.g. `list[int]` not `list`).\n"
        "- Avoid `Any`; prefer `object` or a Protocol when truly dynamic.\n"
        "- Run the type checker after edits and fix any errors YOU introduced.\n"
        "- Do not change runtime behaviour.",
    ),
    # ── Test ──────────────────────────────────────────────────────
    (
        "write-unit-tests",
        "Write Unit Tests",
        "test",
        "Generate focused unit tests for a specific function or module.",
        [
            Placeholder("target", "Function / class / module to test", True),
            Placeholder("file_path", "Path to the source file (optional)", False),
            Placeholder("framework", "pytest | unittest | jest | ...", False, "pytest"),
        ],
        [],
        "Write unit tests for: $target$\n"
        "Source file: $file_path$\n"
        "Framework: $framework$\n\n"
        "Cover:\n"
        "- Happy path (typical inputs → expected output).\n"
        "- Boundary conditions (empty, max, min, off-by-one).\n"
        "- Error paths (invalid input, exceptions).\n"
        "- Edge cases specific to the function's domain.\n\n"
        "Tests must be deterministic — no network, no wall-clock, no random without a seed.\n"
        "Run the suite at the end and confirm everything passes.",
    ),
    (
        "generate-test-plan",
        "Generate a Test Plan",
        "test",
        "Strategic list of what to test, before writing any test code.",
        [
            Placeholder("feature", "Feature or module to plan tests for", True),
        ],
        ["write-unit-tests"],
        "Produce a test plan for: $feature$\n\n"
        "Output a markdown table with columns: ID | Layer | Scenario | Risk | Priority.\n"
        "Layers: unit | integration | e2e | smoke.\n"
        "Risks: regression, security, performance, data-loss, ux.\n"
        "Priorities: P0 (blocker) → P3 (nice-to-have).\n\n"
        "Aim for 8-20 rows. Then list 3 'smoke tests' that should always pass before merge.",
    ),
    # ── Debug ─────────────────────────────────────────────────────
    (
        "debug-stack-trace",
        "Diagnose a Stack Trace",
        "debug",
        "Paste a stack trace; the agent reproduces, isolates, and proposes a fix.",
        [
            Placeholder("trace", "The full stack trace or error log", True),
            Placeholder("repro", "Steps to reproduce (optional)", False),
        ],
        ["fix-bug"],
        "Diagnose this stack trace and propose a fix.\n\n"
        "Stack trace:\n```\n$trace\n```\n\n"
        "Reproduction steps: $repro$\n\n"
        "Steps:\n"
        "1. Identify the failing component and the exact line.\n"
        "2. Trace the call path back to the user-facing trigger.\n"
        "3. Hypothesise the root cause (state, race, input validation, ...).\n"
        "4. Propose a minimal fix with a code snippet.\n"
        "5. Suggest one test that would have caught this.",
    ),
    (
        "debug-performance",
        "Profile & Speed Up Slow Code",
        "debug",
        "Find the hotspot in slow code and propose concrete optimisations.",
        [
            Placeholder("file_path", "Path to the slow file or module", True),
            Placeholder("scenario", "What the user did that felt slow (optional)", False),
        ],
        [],
        "Profile and speed up the code at $file_path$.\n\n"
        "Scenario: $scenario$\n\n"
        "Steps:\n"
        "1. Identify the likely hotspot from code structure (loops, recursion, I/O).\n"
        "2. Write a minimal benchmark that reproduces the slowness.\n"
        "3. Propose the smallest change that yields the biggest win.\n"
        "4. Apply the optimisation, re-run the benchmark, report before/after.\n"
        "5. Flag any trade-offs (memory, readability, accuracy).",
    ),
    # ── Document ──────────────────────────────────────────────────
    (
        "write-readme",
        "Generate a README",
        "document",
        "Scan the project and write a README that actually matches what's there.",
        [
            Placeholder("project_root", "Project directory (default: current)", False, "."),
            Placeholder("audience", "new contributors | end users | both", False, "both"),
        ],
        [],
        "Generate a README.md for the project at $project_root$.\n"
        "Audience: $audience$\n\n"
        "Sections (skip any that don't apply):\n"
        "1. One-paragraph summary — what this project IS and DOES.\n"
        "2. Install — exact commands a fresh machine needs.\n"
        "3. Quick start — copy-pasteable 'hello world' in <60 seconds.\n"
        "4. Project layout — top-level dirs and their purpose.\n"
        "5. Configuration — environment variables and config files.\n"
        "6. Testing — how to run the test suite.\n"
        "7. Contributing — link to CONTRIBUTING.md if present.\n"
        "8. License — detect from LICENSE file.\n\n"
        "Verify every command you put in the README actually runs.",
    ),
    (
        "document-api",
        "Document an API",
        "document",
        "Turn a route or function into OpenAPI / docstring / usage example.",
        [
            Placeholder("target", "Function, class, or route to document", True),
            Placeholder("format", "docstring | openapi | markdown", False, "docstring"),
        ],
        [],
        "Document the API surface of: $target$\n"
        "Format: $format$\n\n"
        "Include for each public entry point:\n"
        "- Signature (params + return type).\n"
        "- One-sentence purpose.\n"
        "- Parameter semantics (units, ranges, defaults).\n"
        "- Side effects and exceptions raised.\n"
        "- A working usage example that the reader can copy-paste.\n"
        "- Links to related entry points.",
    ),
    # ── Review ────────────────────────────────────────────────────
    (
        "review-pull-request",
        "Review a Pull Request",
        "review",
        "Read a PR diff and produce structured feedback (blockers, nits, praise).",
        [
            Placeholder("pr_ref", "PR number, branch, or diff path", True),
        ],
        [],
        "Review the pull request: $pr_ref$\n\n"
        "Produce three sections:\n"
        "1. **Blockers** — must-fix before merge (correctness, security, data-loss).\n"
        "2. **Nits** — should-fix soon (style, naming, minor perf).\n"
        "3. **Praise** — call out things done well so they get repeated.\n\n"
        "For each Blocker, quote the offending line, explain the risk, and suggest a fix.\n"
        "End with a verdict: APPROVE | REQUEST_CHANGES | NEEDS_DISCUSSION.",
    ),
    (
        "security-audit",
        "Security Audit a File",
        "review",
        "Look for the OWASP-Top-10 style issues in one file.",
        [
            Placeholder("file_path", "Path to the file to audit", True),
        ],
        [],
        "Perform a focused security audit of $file_path$.\n\n"
        "Check for:\n"
        "- Injection (SQL, shell, template, LDAP).\n"
        "- Path traversal and unsafe file ops.\n"
        "- Insecure deserialization.\n"
        "- Hardcoded secrets / credentials.\n"
        "- Weak crypto or predictable randomness.\n"
        "- Missing input validation.\n"
        "- Improper error handling that leaks state.\n\n"
        "For each finding: severity (Critical/High/Medium/Low), location, exploitation scenario, fix.\n"
        "If no issues, say so explicitly — do not invent findings.",
    ),
    # ── Deploy / DevOps ──────────────────────────────────────────
    (
        "dockerize-project",
        "Dockerize a Project",
        "deploy",
        "Generate a working Dockerfile + docker-compose.yml for the project.",
        [
            Placeholder("project_root", "Project directory", False, "."),
            Placeholder("port", "Port the app listens on (optional)", False),
        ],
        [],
        "Create a Dockerfile and docker-compose.yml for the project at $project_root$.\n"
        "App port: $port$\n\n"
        "Dockerfile rules:\n"
        "- Multi-stage build if it reduces image size.\n"
        "- Non-root user.\n"
        "- Pin base image to a specific digest, not `latest`.\n"
        "- Only copy what's needed (use .dockerignore).\n"
        "- Healthcheck if the app exposes an HTTP port.\n\n"
        "Compose file:\n"
        "- One service for the app.\n"
        "- Volumes for any persistent state.\n"
        "- Environment variables sourced from .env.example.\n"
        "- Restart policy: unless-stopped.\n\n"
        "Build and run once to verify the image starts cleanly.",
    ),
    (
        "write-ci-pipeline",
        "Generate a CI Pipeline",
        "deploy",
        "GitHub Actions / GitLab CI config that runs lint, test, build on every PR.",
        [
            Placeholder("platform", "github | gitlab", False, "github"),
            Placeholder("language", "python | node | rust | go", True),
        ],
        [],
        "Generate a CI pipeline for $platform$ targeting a $language$ project.\n\n"
        "Jobs:\n"
        "1. lint — run the canonical linter for $language$.\n"
        "2. test — run the test suite on the project's minimum and maximum supported runtime versions.\n"
        "3. build — produce a distributable artifact.\n"
        "4. (optional) security-scan — run a free SAST tool if one exists for $language$.\n\n"
        "Rules:\n"
        "- Cache dependencies.\n"
        "- Fail fast on lint, but always run all matrix jobs for tests.\n"
        "- Trigger on PR and on push to main.\n"
        "- Pin action versions to a full SHA, not a floating tag.",
    ),
    # ── Office / Productivity ────────────────────────────────────
    (
        "office-report-from-data",
        "Build a Report from Data",
        "office",
        "Generate a .docx / .xlsx / .pptx report summarising a CSV or JSON dataset.",
        [
            Placeholder("data_path", "Path to the data file (CSV/JSON)", True),
            Placeholder("format", "docx | xlsx | pptx", False, "docx"),
            Placeholder("title", "Report title (optional)", False),
        ],
        [],
        "Build a $format$ report from the data at $data_path$.\n"
        "Title: $title$\n\n"
        "Steps:\n"
        "1. Load and summarise the data (row count, columns, types).\n"
        "2. Compute 3-5 key metrics relevant to the data's domain.\n"
        "3. Add a chart visualising the most interesting dimension.\n"
        "4. Add a 1-paragraph narrative interpretation.\n"
        "5. Save the file next to the source data, with a timestamp suffix.",
    ),
    (
        "office-meeting-notes",
        "Summarise Meeting Transcript",
        "office",
        "Turn a raw transcript into structured notes with decisions and action items.",
        [
            Placeholder("transcript_path", "Path to the transcript file", True),
        ],
        [],
        "Read the transcript at $transcript_path$ and produce structured meeting notes.\n\n"
        "Sections:\n"
        "1. **Attendees** — names mentioned (best-effort).\n"
        "2. **Summary** — 3-sentence overview of what was discussed.\n"
        "3. **Decisions** — bullet list, each with the rationale quoted from the transcript.\n"
        "4. **Action items** — table: Owner | Action | Due (if mentioned).\n"
        "5. **Open questions** — items that need a follow-up meeting.\n\n"
        "Save as meeting_notes_<date>.docx in the same directory as the transcript.",
    ),
    # ── Learn / Onboard ──────────────────────────────────────────
    (
        "onboard-to-codebase",
        "Onboard Me to This Codebase",
        "learn",
        "Guided tour: entry points, request flow, where to add a new feature.",
        [
            Placeholder("project_root", "Project directory", False, "."),
            Placeholder("goal", "What you want to do first (add a feature / fix a bug / just learn)", False),
        ],
        [],
        "Onboard me to the codebase at $project_root$.\n"
        "My goal: $goal$\n\n"
        "Produce a markdown guide covering:\n"
        "1. **What this project does** — one paragraph, plain English.\n"
        "2. **Entry points** — `main()`, route handlers, CLI dispatch — with file:line.\n"
        "3. **Request lifecycle** — trace one typical user action from entry to response.\n"
        "4. **Where to add a new feature** — concrete file paths and the pattern to follow.\n"
        "5. **Testing strategy** — where tests live, how to run them.\n"
        "6. **Glossary** — domain terms and project-specific jargon.\n\n"
        "End with a 3-step 'first contribution' path tailored to my goal.",
    ),
    (
        "explain-error-message",
        "Explain an Error Message",
        "learn",
        "Plain-English decode of a cryptic compiler or runtime error.",
        [
            Placeholder("error", "The full error message", True),
            Placeholder("language", "Language / runtime (optional)", False),
        ],
        ["fix-bug"],
        "Explain this error message in plain English.\n\n"
        "Error:\n```\n$error\n```\n"
        "Language/runtime: $language$\n\n"
        "Cover:\n"
        "- What the error literally means.\n"
        "- The 2-3 most common root causes.\n"
        "- How to confirm which cause applies here.\n"
        "- The canonical fix for each cause.\n"
        "- One link to the relevant docs (if you can name the URL).",
    ),
]


def _make_builtin(c: Tuple[str, str, str, str, List[Placeholder], List[str], str]) -> Capability:
    cid, name, category, description, placeholders, follow_ups, body = c
    # Auto-discover any placeholders used in the body but not declared
    declared = {p.name for p in placeholders}
    for missing in _scan_placeholders_in_body(body):
        if missing not in declared:
            placeholders.append(Placeholder(
                name=missing,
                description=f"(auto-discovered) {missing}",
                required=True,
            ))
    return Capability(
        id=cid,
        name=name,
        category=category,
        description=description,
        body=body.strip(),
        placeholders=placeholders,
        follow_ups=list(follow_ups),
        source_path="(builtin)",
        project_level=False,
        builtin=True,
    )


# ── File loader (project + user-global layers) ───────────────────────

def _load_capability_file(path: Path, project_level: bool) -> Optional[Capability]:
    """Load a single capability .md file."""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        logger.warning("[capabilities] failed to read %s: %s", path, e)
        return None

    meta, body = _parse_frontmatter(content)

    cid = meta.get("id") or path.stem
    name = meta.get("name") or path.stem.replace("_", " ").replace("-", " ").title()
    description = meta.get("description") or meta.get("desc") or ""
    category = meta.get("category") or meta.get("tag") or "general"
    placeholders = _parse_placeholder_list(meta.get("placeholders"))
    follow_ups = _parse_follow_ups(meta.get("follow_ups"))

    if not name or not description:
        logger.warning("[capabilities] %s missing required field: name or description", path)
        return None
    if not body or len(body.strip()) < 30:
        logger.warning("[capabilities] %s body too short", path)
        return None

    # Auto-discover placeholders used in body but not declared
    declared = {p.name for p in placeholders}
    for missing in _scan_placeholders_in_body(body):
        if missing not in declared:
            placeholders.append(Placeholder(
                name=missing,
                description=f"(auto-discovered) {missing}",
                required=True,
            ))

    return Capability(
        id=cid,
        name=name,
        category=category,
        description=description,
        body=body.strip(),
        placeholders=placeholders,
        follow_ups=follow_ups,
        source_path=str(path),
        project_level=project_level,
        builtin=False,
    )


def _iter_capability_dir(cap_dir: Path, project_level: bool) -> List[Capability]:
    """Iterate a capabilities directory, loading both SKILL.md and *.md files."""
    caps: List[Capability] = []
    if not cap_dir.is_dir():
        return caps

    for entry in sorted(cap_dir.iterdir()):
        if entry.name.startswith("."):
            continue
        if entry.is_dir():
            # Nested: capabilities/<id>/SKILL.md
            skill_file = entry / "SKILL.md"
            if skill_file.is_file():
                cap = _load_capability_file(skill_file, project_level)
                if cap:
                    caps.append(cap)
        elif entry.is_file() and entry.suffix.lower() == ".md":
            # Flat: capabilities/<id>.md
            if entry.name.upper() == "README.MD":
                continue
            cap = _load_capability_file(entry, project_level)
            if cap:
                caps.append(cap)
    return caps


# ── Catalog manager ──────────────────────────────────────────────────

class CapabilityCatalog:
    """Holds every loaded capability (built-in + user-global + project).

    Priority (later overrides earlier, keyed by id):
      1. Built-in catalog (shipped with Tera Pilot)
      2. User-global: ~/.tera_pilot/capabilities/*.md
      3. Project-level: <project>/.tera_pilot/capabilities/*.md
    """

    def __init__(self) -> None:
        self._caps: Dict[str, Capability] = {}
        self._project_root: Optional[str] = None

    def set_project_root(self, root: str) -> None:
        self._project_root = root
        self.reload()

    def reload(self) -> None:
        """Reload everything from disk + built-ins."""
        caps_by_id: Dict[str, Capability] = {}

        # 1. Built-ins (lowest priority)
        for spec in _BUILTIN_CAPABILITIES:
            cap = _make_builtin(spec)
            caps_by_id[cap.id] = cap

        # 2. User-global
        user_dir = Path.home() / ".tera_pilot" / "capabilities"
        for cap in _iter_capability_dir(user_dir, project_level=False):
            caps_by_id[cap.id] = cap

        # 3. Project-level (highest priority)
        if self._project_root:
            proj_dir = Path(self._project_root) / ".tera_pilot" / "capabilities"
            for cap in _iter_capability_dir(proj_dir, project_level=True):
                caps_by_id[cap.id] = cap

        self._caps = caps_by_id
        logger.info(
            "[capabilities] loaded %d capabilities (builtins=%d, user=%d, project=%d)",
            len(self._caps),
            sum(1 for c in self._caps.values() if c.builtin),
            sum(1 for c in self._caps.values() if not c.builtin and not c.project_level),
            sum(1 for c in self._caps.values() if c.project_level),
        )

    # ── Queries ──────────────────────────────────────────────────

    def list_categories(self) -> List[str]:
        seen: List[str] = []
        for cap in self._caps.values():
            if cap.category not in seen:
                seen.append(cap.category)
        return seen

    def list_capabilities(self, category: Optional[str] = None) -> List[Capability]:
        caps = list(self._caps.values())
        if category:
            caps = [c for c in caps if c.category == category]
        # Sort by category then name so the UI grouping is stable
        caps.sort(key=lambda c: (c.category, c.name))
        return caps

    def get(self, cap_id: str) -> Optional[Capability]:
        return self._caps.get(cap_id)

    def list_as_dicts(
        self,
        category: Optional[str] = None,
        *,
        include_body: bool = False,
    ) -> List[Dict[str, Any]]:
        return [
            c.to_dict(include_body=include_body)
            for c in self.list_capabilities(category=category)
        ]

    # ── Template filling ─────────────────────────────────────────

    def fill_template(
        self,
        cap_id: str,
        values: Dict[str, str],
    ) -> Dict[str, Any]:
        """Substitute $placeholder$ values in the capability body.

        Returns:
            - {"ok": True, "prompt": str, "capability": dict, "missing": []}
            - {"ok": False, "error": str, "missing": [str, ...]}
        """
        cap = self.get(cap_id)
        if cap is None:
            return {"ok": False, "error": f"Unknown capability: {cap_id}"}

        # Validate required placeholders
        missing: List[str] = []
        for p in cap.placeholders:
            val = values.get(p.name) or p.default
            if not val and p.required:
                missing.append(p.name)

        if missing:
            return {
                "ok": False,
                "error": f"Missing required placeholders: {', '.join(missing)}",
                "missing": missing,
                "capability": cap.to_dict(),
            }

        # Substitute. Use a function so a value containing a $ doesn't
        # itself get re-matched (re.sub with a string repl does literal
        # substitution; with a callable it's safe).
        def _repl(m: "re.Match[str]") -> str:
            name = m.group(1)
            val = values.get(name) or next(
                (p.default for p in cap.placeholders if p.name == name), ""
            )
            return str(val)

        prompt = _PLACEHOLDER_USE_RE.sub(_repl, cap.body)
        return {
            "ok": True,
            "prompt": prompt,
            "capability": cap.to_dict(),
            "missing": [],
        }


# ── Module-level singleton ──────────────────────────────────────────

_catalog: Optional[CapabilityCatalog] = None
_catalog_lock = None

import threading as _threading
_catalog_lock = _threading.Lock()


def get_catalog() -> CapabilityCatalog:
    global _catalog
    if _catalog is None:
        with _catalog_lock:
            if _catalog is None:
                _catalog = CapabilityCatalog()
                _catalog.reload()
    return _catalog
