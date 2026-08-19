"""
Command Policy — v1.2.1-fix (review §4.6).

Makes the ``ALLOWED_COMMANDS`` whitelist and the ``_DANGEROUS_FLAGS``
map user/project-extensible, instead of the hardcoded constants the
review flagged as too rigid.

The original v1.2.0 implementation hardwired ~20 binaries (python3,
node, npm, git, pytest, ruff, …) and a small set of dangerous-flag
overrides (``python3 -c``, ``pip install``, ``git clone``, …) at
module load time. Extending the whitelist required editing source
code — which is fine for the Tera Pilot maintainers but useless for users
who legitimately need ``docker``, ``curl``, ``go``, ``cargo``,
``make``, ``yarn``, ``kubectl``, etc. The review suggested either
making the whitelist configurable OR moving to OS-level sandboxing
(bubblewrap / firejail). We do BOTH — the configurable whitelist
ships now (low-risk, additive), and a hook is left for future
sandbox-based enforcement.

Layers (later layers ADD to earlier ones; later ``dangerous_flags``
entries MERGE with earlier ones):

  1. ``BASE_ALLOWED_COMMANDS`` (this module) — the same ~20 binaries
     the v1.2.0 ``ALLOWED_COMMANDS`` set contained, kept as a stable
     floor so out-of-the-box behaviour is unchanged.
  2. ``~/.tera_pilot/commands.json`` — user-global extra allow + deny +
     trusted_flags overrides. Format:
       {
         "extra_allowed": ["docker", "go", "cargo", "make"],
         "extra_dangerous_flags": {"go": ["install"]},
         "extra_trusted_flags":  {"npm": ["test"]}
       }
  3. ``<project>/.tera_pilot/commands.json`` — same format, project-scoped.
     Wins on conflicts (so a project can DENY a command the user
     globally allowed — useful for tighter policy on production code).
  4. Per-session programmatic overrides via ``set_allowed_commands()``
     (used by the headless CLI to apply ``--allow-all`` / ``--deny X``
     command-line flags without touching disk).

Files are read with the same atomic-read pattern as ``utils.load_config``
and the same warning-on-malformed behaviour — a bad file is logged
and skipped, never crashes the agent.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ── Floor: the original v1.2.0 hardcoded set ────────────────────────────
# Kept verbatim so out-of-the-box behaviour matches v1.2.0 exactly.
# Users can EXTEND this via commands.json; they cannot SHRINK it
# (deny list is separate, see below).
BASE_ALLOWED_COMMANDS: FrozenSet[str] = frozenset({
    "python3", "python", "node", "npm", "pip", "git", "ls", "cat", "head",
    "tail", "find", "grep", "wc", "mkdir", "touch", "cp", "mv", "rm",
    "pytest", "black", "isort", "flake8", "mypy", "ruff",
})

# Floor for dangerous-flag overrides. Same content as the original
# _DANGEROUS_FLAGS dict in agent_runtime.py.
BASE_DANGEROUS_FLAGS: Dict[str, FrozenSet[str]] = {
    "python3": frozenset({"-c", "-m"}),
    "python":  frozenset({"-c", "-m"}),
    "node":    frozenset({"-e", "--eval"}),
    "pip":     frozenset({"install", "uninstall"}),
    # v2.3.4-security: `npm run` was blocked, but `npm test`/`npm start`/
    # `npm exec` are ALIASES that execute the same arbitrary
    # package.json scripts — a malicious repo can ship
    # `{"scripts":{"test":"rm -rf ~"}}` and `npm test` runs it. All
    # script-executing npm subcommands are now blocked (fail-closed); a
    # user can re-allow them for a trusted project via
    # commands.json extra_trusted_flags.
    "npm":     frozenset({"install", "uninstall", "run", "run-script",
                           "test", "t", "start", "restart", "exec", "ci",
                           "install-test", "install-ci-test", "link",
                           "rebuild", "publish"}),
    "git":     frozenset({"clone", "push", "pull", "fetch", "remote"}),
}


@dataclass
class CommandPolicy:
    """Resolved command-execution policy.

    Built by ``resolve()`` from BASE + user config + project config +
    programmatic overrides. The agent runtime queries the resulting
    instance via ``is_allowed(cmd)`` and ``is_dangerous_flag(cmd, flag)``
    instead of consulting module-level constants directly.

    The structure is intentionally simple — a frozen set of allowed
    binaries, a dict of per-binary dangerous-flag sets, and a deny
    list (always wins over allow, even if the binary appears in
    BASE_ALLOWED_COMMANDS).
    """
    allowed: FrozenSet[str] = BASE_ALLOWED_COMMANDS
    dangerous_flags: Dict[str, FrozenSet[str]] = field(
        default_factory=lambda: dict(BASE_DANGEROUS_FLAGS)
    )
    # Deny list: binaries that are NEVER allowed, even if they appear
    # in BASE_ALLOWED_COMMANDS or extra_allowed. Empty by default.
    # Useful for project-scoped policy ("don't allow git push even
    # though git is in the base whitelist").
    denied: FrozenSet[str] = frozenset()
    # Source attribution (for the /policy command in the UI).
    sources: Dict[str, Any] = field(default_factory=dict)
    # v1.2.2-fix (found while validating review §4.6): capability
    # EXPANSIONS requested by a *project*-scoped commands.json that the
    # user has not yet approved for this project. These are NOT merged
    # into ``allowed`` / ``dangerous_flags`` — a project dropped into a
    # workspace (e.g. a freshly cloned repo) must not be able to widen
    # its own sandbox just by shipping a config file; only the user
    # (via ~/.tera_pilot/commands.json) or an explicit approval can do that.
    # Restrictions (``extra_denied``) from a project ARE applied
    # automatically, since a project can only make itself stricter this
    # way, never looser. Structure:
    #   {"project_root": str, "extra_allowed": [...], "extra_trusted_flags": {...}}
    # Empty dict if there's nothing pending (no project config, or it
    # was already approved, or it requests nothing beyond what base/
    # user config already grants).
    pending_grants: Dict[str, Any] = field(default_factory=dict)

    def has_pending_grants(self) -> bool:
        return bool(self.pending_grants.get("extra_allowed") or
                    self.pending_grants.get("extra_trusted_flags"))

    def is_allowed(self, binary: str) -> bool:
        """True iff *binary* is in the allowed set AND NOT in the
        deny list. ``binary`` should be the basename (no path)."""
        if not binary:
            return False
        if binary in self.denied:
            return False
        return binary in self.allowed

    def is_dangerous_flag(self, binary: str, flag: str) -> bool:
        """True iff *flag* is in the dangerous-flags set for *binary*."""
        if not binary or not flag:
            return False
        flags = self.dangerous_flags.get(binary)
        return bool(flags) and flag in flags

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed": sorted(self.allowed),
            "denied": sorted(self.denied),
            "dangerous_flags": {
                k: sorted(v) for k, v in self.dangerous_flags.items()
            },
            "sources": dict(self.sources),
        }


# ── Config file paths ───────────────────────────────────────────────────

def _tera_pilot_home() -> Path:
    return Path.home() / ".tera_pilot"


def _user_commands_path() -> Path:
    return _tera_pilot_home() / "commands.json"


def _project_commands_path(project_root: Optional[str]) -> Optional[Path]:
    if not project_root:
        return None
    return Path(project_root) / ".tera_pilot" / "commands.json"


# ── Project trust store ──────────────────────────────────────────────────
#
# v1.2.2-fix (found while validating review §4.6): the original §4.6
# patch read <project>/.tera_pilot/commands.json automatically on
# set_workspace() and merged its extra_allowed / extra_trusted_flags
# straight into the effective policy — no confirmation. That means a
# cloned/untrusted repository could silently widen its own sandbox
# (add ``curl``/``docker``/``bash``, or strip the ``git push`` /
# ``pip install`` protections) the moment it was opened as a
# workspace, and — combined with the headless CLI's default
# ``--autonomy never_ask`` — could do so with zero human in the loop.
#
# The fix: project-requested *expansions* (extra_allowed,
# extra_trusted_flags) are pinned to a content hash and only take
# effect once ``approve_project_policy()`` has been called for that
# exact file content. Until then they sit in ``CommandPolicy.
# pending_grants`` — visible to the agent/CLI/UI, but NOT enforced.
# Project-requested *restrictions* (extra_denied) are always applied
# immediately, since a project narrowing its own permissions can't be
# used to escape the sandbox.

def _trust_store_path() -> Path:
    return _tera_pilot_home() / "project_trust.json"


def _load_trust_store() -> Dict[str, Any]:
    path = _trust_store_path()
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("[command-policy] %s: failed to read trust store: %s", path, e)
        return {}


def _save_trust_store(store: Dict[str, Any]) -> None:
    path = _trust_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)  # atomic on POSIX and Windows


def _grant_relevant_subset(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Extract only the capability-EXPANDING parts of a commands.json
    payload (``extra_allowed`` / ``extra_trusted_flags``). This is
    what gets hashed for approval — ``extra_denied`` is excluded
    because changing a restriction should never require re-approval.
    """
    out: Dict[str, Any] = {}
    if cfg.get("extra_allowed"):
        out["extra_allowed"] = sorted(
            {str(b) for b in cfg["extra_allowed"] if isinstance(b, str) and b}
        )
    if cfg.get("extra_trusted_flags"):
        tf = cfg["extra_trusted_flags"]
        if isinstance(tf, dict):
            out["extra_trusted_flags"] = {
                str(k): sorted({str(f) for f in v if isinstance(f, str)})
                for k, v in tf.items() if isinstance(v, list) and v
            }
    return out


def _hash_grant_subset(cfg: Dict[str, Any]) -> str:
    subset = _grant_relevant_subset(cfg)
    blob = json.dumps(subset, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def is_project_policy_approved(project_root: str, cfg: Dict[str, Any]) -> bool:
    """True iff the CURRENT grant-relevant content of *cfg* (the
    project's commands.json) exactly matches what was approved last
    time for this project. Any edit to extra_allowed / extra_trusted_flags
    invalidates a prior approval — approval is pinned to content, not
    just to the project path, so a malicious later edit can't ride on
    an earlier legitimate approval.
    """
    subset = _grant_relevant_subset(cfg)
    if not subset:
        return True  # nothing being requested — nothing to approve
    store = _load_trust_store()
    key = str(Path(project_root).resolve())
    entry = store.get(key)
    if not entry:
        return False
    return entry.get("content_hash") == _hash_grant_subset(cfg)


def approve_project_policy(project_root: str) -> bool:
    """Explicitly approve the CURRENT contents of
    ``<project_root>/.tera_pilot/commands.json`` for this project. Pins the
    approval to a content hash of the grant-relevant fields, so a
    later edit requires re-approval. Returns False if there's no
    project config file, or it requests nothing beyond base/user
    config (nothing to approve).
    """
    proj_path = _project_commands_path(project_root)
    if proj_path is None:
        return False
    cfg = _read_commands_json(proj_path)
    subset = _grant_relevant_subset(cfg)
    if not subset:
        return False
    store = _load_trust_store()
    key = str(Path(project_root).resolve())
    store[key] = {
        "content_hash": _hash_grant_subset(cfg),
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "granted": subset,
    }
    _save_trust_store(store)
    invalidate_global_policy()
    logger.info("[command-policy] project policy approved for %s", key)
    return True


def revoke_project_policy_approval(project_root: str) -> bool:
    """Remove a previously granted approval for *project_root*, if any.
    The project's requested expansions go back to ``pending_grants``
    on the next resolve."""
    store = _load_trust_store()
    key = str(Path(project_root).resolve())
    if key not in store:
        return False
    del store[key]
    _save_trust_store(store)
    invalidate_global_policy()
    return True


def describe_pending_grants(policy: "CommandPolicy") -> str:
    """Human-readable summary of ``policy.pending_grants``, suitable
    for surfacing to the agent (as a tool-rejection message), the CLI
    (as a startup warning), or the GUI (as a one-time approval
    prompt)."""
    pg = policy.pending_grants
    if not pg or not policy.has_pending_grants():
        return ""
    parts = [
        f"This project's .tera_pilot/commands.json requests capabilities "
        f"that have NOT been approved yet (project: {pg.get('project_root', '?')}):"
    ]
    if pg.get("extra_allowed"):
        parts.append(f"  - allow additional commands: {', '.join(pg['extra_allowed'])}")
    if pg.get("extra_trusted_flags"):
        for binary, flags in pg["extra_trusted_flags"].items():
            parts.append(f"  - trust flags for {binary!r}: {', '.join(flags)}")
    parts.append(
        "These stay BLOCKED until approved in the TUI Project Policy safety flow "
        "or the Web UI Settings → Project Policy panel."
    )
    return "\n".join(parts)


def _read_commands_json(path: Path) -> Dict[str, Any]:
    """Read and validate a commands.json file.

    Returns an empty dict on missing file, malformed JSON, or wrong
    shape — never raises. All errors are logged at WARNING level so
    the user sees them but the agent doesn't crash.
    """
    if not path.exists() or not path.is_file():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("[command-policy] %s: failed to read: %s", path, e)
        return {}
    if not isinstance(data, dict):
        logger.warning("[command-policy] %s: top-level value must be an object", path)
        return {}
    return data


# ── Resolver ────────────────────────────────────────────────────────────

# Module-level singleton — most callers (the agent runtime) want the
# same resolved policy for a given process. The headless CLI may swap
# it via set_global_policy() to apply --allow-all / --deny flags.
_global_policy: Optional[CommandPolicy] = None
_global_policy_project_root: Optional[str] = None
# v1.2.2-fix: once set_global_policy() installs an explicit manual
# override (used by the CLI's --allow/--deny flags), it stays in
# effect regardless of what project_root subsequent get_global_policy()
# calls pass, until invalidate_global_policy() is called. Without this,
# the project_root-mismatch check added below would immediately discard
# a manual override the next time get_global_policy() is called with a
# different (or no) project_root.
_global_policy_pinned: bool = False
_global_policy_lock = threading.Lock()


def resolve(
    project_root: Optional[str] = None,
    *,
    extra_allowed: Optional[Set[str]] = None,
    extra_denied: Optional[Set[str]] = None,
    extra_dangerous_flags: Optional[Dict[str, Set[str]]] = None,
) -> CommandPolicy:
    """Build a CommandPolicy from BASE + user config + project config +
    programmatic overrides.

    Layers (later layers EXTEND allow / dangerous_flags; DENY always wins):
      1. BASE_ALLOWED_COMMANDS + BASE_DANGEROUS_FLAGS
      2. ~/.tera_pilot/commands.json (user-global) — TRUSTED: applies in full.
      3. <project>/.tera_pilot/commands.json (project-scoped) — RESTRICTIONS
         (extra_denied, extra_dangerous_flags) apply immediately;
         EXPANSIONS (extra_allowed, extra_trusted_flags) apply only if
         previously approved via ``approve_project_policy()`` for the
         exact current file content — otherwise they land in
         ``CommandPolicy.pending_grants`` and are NOT enforced. See
         the "Project trust store" section above for why.
      4. ``extra_allowed`` / ``extra_denied`` / ``extra_dangerous_flags``
         kwargs (programmatic — used by headless CLI ``--allow``/``--deny``
         flags, which are explicit user action, so always trusted).

    Deny always wins over allow, at every layer.
    """
    allowed: Set[str] = set(BASE_ALLOWED_COMMANDS)
    dangerous_flags: Dict[str, Set[str]] = {
        k: set(v) for k, v in BASE_DANGEROUS_FLAGS.items()
    }
    denied: Set[str] = set()
    sources: Dict[str, Any] = {"base": {"allowed_count": len(allowed)}}
    pending: Dict[str, Any] = {}

    # Layer 2: user-global — trusted (it's the local user's own file).
    user_path = _user_commands_path()
    user_cfg = _read_commands_json(user_path)
    if user_cfg:
        _merge_layer(
            "user", user_cfg, allowed, dangerous_flags, denied, sources,
            gate_expansions=False, pending_out=None,
        )

    # Layer 3: project-scoped. Restrictions apply immediately;
    # expansions are gated behind approve_project_policy().
    proj_path = _project_commands_path(project_root)
    if proj_path is not None:
        proj_cfg = _read_commands_json(proj_path)
        if proj_cfg:
            approved = False
            if project_root:
                try:
                    approved = is_project_policy_approved(project_root, proj_cfg)
                except Exception as e:
                    logger.warning(
                        "[command-policy] trust-store check failed for %s: "
                        "%s — treating as NOT approved", project_root, e,
                    )
                    approved = False
            _merge_layer(
                "project", proj_cfg, allowed, dangerous_flags, denied, sources,
                gate_expansions=not approved, pending_out=pending,
            )
            if pending and project_root:
                pending["project_root"] = str(project_root)

    # Layer 4: programmatic (headless CLI flags) — explicit user action
    # for this run, always trusted.
    if extra_allowed:
        allowed |= {b for b in extra_allowed if isinstance(b, str) and b}
        sources["programmatic_extra_allowed"] = sorted(extra_allowed)
    if extra_denied:
        denied |= {b for b in extra_denied if isinstance(b, str) and b}
        sources["programmatic_extra_denied"] = sorted(extra_denied)
    if extra_dangerous_flags:
        for bin_name, flags in extra_dangerous_flags.items():
            if not isinstance(flags, (set, list, tuple)):
                continue
            dangerous_flags.setdefault(bin_name, set()).update(flags)
        sources["programmatic_extra_dangerous_flags"] = {
            k: sorted(v) for k, v in extra_dangerous_flags.items()
        }

    # Apply deny list — even BASE binaries can be denied by the user
    # or project policy (e.g. forbid ``rm`` entirely in a project).
    allowed -= denied

    return CommandPolicy(
        allowed=frozenset(allowed),
        dangerous_flags={k: frozenset(v) for k, v in dangerous_flags.items()},
        denied=frozenset(denied),
        sources=sources,
        pending_grants=pending,
    )


def _merge_layer(
    layer_name: str,
    cfg: Dict[str, Any],
    allowed: Set[str],
    dangerous_flags: Dict[str, Set[str]],
    denied: Set[str],
    sources: Dict[str, Any],
    *,
    gate_expansions: bool = False,
    pending_out: Optional[Dict[str, Any]] = None,
) -> None:
    """Merge one commands.json layer into the accumulating sets.

    ``gate_expansions``: if True, ``extra_allowed`` and
    ``extra_trusted_flags`` (the two fields that WIDEN what the agent
    can do) are NOT merged into ``allowed`` / ``dangerous_flags`` —
    instead they're written into ``pending_out`` for the caller to
    surface as an approval request. ``extra_denied`` and
    ``extra_dangerous_flags`` (both RESTRICTIONS) are always merged
    regardless of ``gate_expansions``, since narrowing permissions
    can't be used to escape the sandbox.
    """
    # extra_allowed: list of binaries to ADD to the allow set.
    extra_allowed = cfg.get("extra_allowed") or []
    if isinstance(extra_allowed, list):
        valid = {str(b) for b in extra_allowed if isinstance(b, str) and b}
        new_grants = valid - allowed  # only what isn't already effectively allowed
        if gate_expansions:
            if new_grants and pending_out is not None:
                pending_out["extra_allowed"] = sorted(new_grants)
            sources[layer_name] = {"extra_allowed_pending": sorted(new_grants)}
        else:
            allowed |= valid
            sources[layer_name] = {"extra_allowed": sorted(valid)}
    # extra_denied: list of binaries to ADD to the deny list. A
    # restriction — always applied, never gated.
    extra_denied = cfg.get("extra_denied") or []
    if isinstance(extra_denied, list):
        valid = {str(b) for b in extra_denied if isinstance(b, str) and b}
        denied |= valid
        sources.setdefault(layer_name, {})["extra_denied"] = sorted(valid)
    # extra_dangerous_flags: per-binary dangerous-flag overrides to ADD.
    # A restriction (it BLOCKS previously-allowed flag combinations) —
    # always applied, never gated.
    extra_df = cfg.get("extra_dangerous_flags") or {}
    if isinstance(extra_df, dict):
        for bin_name, flags in extra_df.items():
            if not isinstance(flags, list):
                continue
            valid_flags = {str(f) for f in flags if isinstance(f, str) and f}
            dangerous_flags.setdefault(str(bin_name), set()).update(valid_flags)
        sources.setdefault(layer_name, {})["extra_dangerous_flags"] = {
            k: sorted(v) for k, v in extra_df.items() if isinstance(v, list)
        }
    # extra_trusted_flags: REMOVE flags from the dangerous set for this
    # binary — e.g. un-blocking ``git push`` for a project that wants
    # the agent to be able to push. This WIDENS what the agent can do,
    # so it's gated exactly like extra_allowed.
    extra_tf = cfg.get("extra_trusted_flags") or {}
    if isinstance(extra_tf, dict):
        if gate_expansions:
            pending_tf: Dict[str, List[str]] = {}
            for bin_name, flags in extra_tf.items():
                if not isinstance(flags, list):
                    continue
                valid_flags = {str(f) for f in flags if isinstance(f, str) and f}
                existing = dangerous_flags.get(str(bin_name))
                still_blocked = (existing & valid_flags) if existing else set()
                if still_blocked:
                    pending_tf[str(bin_name)] = sorted(still_blocked)
            if pending_tf and pending_out is not None:
                pending_out["extra_trusted_flags"] = pending_tf
            sources.setdefault(layer_name, {})["extra_trusted_flags_pending"] = pending_tf
        else:
            for bin_name, flags in extra_tf.items():
                if not isinstance(flags, list):
                    continue
                valid_flags = {str(f) for f in flags if isinstance(f, str) and f}
                existing = dangerous_flags.get(str(bin_name))
                if existing:
                    existing -= valid_flags
            sources.setdefault(layer_name, {})["extra_trusted_flags"] = {
                k: sorted(v) for k, v in extra_tf.items() if isinstance(v, list)
            }


# ── Global policy accessor ──────────────────────────────────────────────

def get_global_policy(project_root: Optional[str] = None) -> CommandPolicy:
    """Return the process-wide CommandPolicy singleton.

    If no policy has been set yet (or if ``project_root`` changed since
    the last resolve), we re-resolve from disk. The result is cached
    so repeated calls are cheap. Call ``invalidate_global_policy()``
    to force a re-resolve on the next call (e.g. after the user edits
    ~/.tera_pilot/commands.json).
    """
    global _global_policy, _global_policy_project_root
    with _global_policy_lock:
        # v1.2.2-fix: also re-resolve if project_root changed since the
        # cached policy was built — not just when the cache is empty.
        # Without this, a caller that passes a different project_root
        # than whatever the FIRST call happened to use would silently
        # keep getting the first project's (or no project's) policy,
        # even without an explicit invalidate_global_policy() call.
        # Skipped entirely while a manual override is pinned (see
        # set_global_policy) — that override is explicit and should
        # stick until invalidate_global_policy() is called.
        if not _global_policy_pinned and (
            _global_policy is None or _global_policy_project_root != project_root
        ):
            _global_policy = resolve(project_root=project_root)
            _global_policy_project_root = project_root
        return _global_policy


def set_global_policy(policy: CommandPolicy) -> None:
    """Replace the global policy (used by the headless CLI to apply
    --allow-all / --deny X flags without touching disk).

    v1.2.2-fix: this now PINS the override — subsequent
    ``get_global_policy(project_root)`` calls with a different (or no)
    ``project_root`` will keep returning this exact policy instead of
    silently re-resolving it away. Call ``invalidate_global_policy()``
    to release the pin.
    """
    global _global_policy, _global_policy_pinned
    with _global_policy_lock:
        _global_policy = policy
        _global_policy_pinned = True


def invalidate_global_policy() -> None:
    """Force the next ``get_global_policy()`` call to re-resolve from
    disk. Called by the UI's Settings panel after the user saves a
    new commands.json."""
    global _global_policy, _global_policy_project_root, _global_policy_pinned
    with _global_policy_lock:
        _global_policy_project_root = None
        _global_policy_pinned = False
        _global_policy = None


# ── Sample config (for documentation / first-run scaffolding) ───────────

SAMPLE_COMMANDS_JSON = {
    "extra_allowed": ["docker", "go", "cargo", "make", "yarn", "kubectl"],
    "extra_dangerous_flags": {
        "docker": ["rm", "rmi"],
        "go": ["install"],
        "cargo": ["publish"],
    },
    "extra_trusted_flags": {
        # Allow `npm test` to run via `npm run test` if you trust the project.
        "npm": ["run"],
    },
}


def write_sample_config(path: Optional[Path] = None) -> Path:
    """Write a sample commands.json to *path* (default: user-global).
    Useful for first-run scaffolding — the user edits the file to
    customise. Never overwrites an existing file."""
    target = path or _user_commands_path()
    if target.exists():
        logger.info("[command-policy] %s already exists — not overwriting", target)
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        json.dump(SAMPLE_COMMANDS_JSON, f, indent=2, ensure_ascii=False)
        f.write("\n")
    logger.info("[command-policy] wrote sample config to %s", target)
    return target
