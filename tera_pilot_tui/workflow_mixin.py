"""workflow_mixin.py — built-in slash commands of the v2.5.0 workflow update.

Mixed into ``TeraPilotTUIApp`` (see app.py). Two families:

* Agent-turn commands (/review, /security-review, /advisor, /bughunter,
  /commit, /commit-push-pr, /pr-comments) — compose a focused prompt
  and run it through the normal ``_run_turn`` path, so approvals,
  sandbox and audit apply unchanged.
* Local commands (/compact, /export, /share, /rename, /tag, /stats,
  /effort, /fast, /brief, /output-style, /permissions, /init,
  /onboarding, /remember, /plugin, /schedule) — answered in-process
  via new ``TeraPilotBridge`` helpers, no agent turn spent.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Optional


class WorkflowCommandsMixin:
    """Assumes the host is a Textual App with ``bridge``, ``query_one``,
    ``_run_turn``, ``_turn_running``, ``_refresh_status`` and ``_last_prompt``."""

    # — helpers —

    def _wf_busy(self) -> bool:
        from .widgets.chat_log import ChatLog
        from .widgets.input_box import InputBox
        if getattr(self, "_turn_running", False):
            self.query_one(ChatLog).add_system(
                "Wait for the current turn to finish first.")
            self.query_one(InputBox).focus()
            return True
        return False

    def _wf_launch(self, display: str, prompt: str) -> None:
        """Run an agent turn, showing ``display`` as the user message."""
        from .widgets.chat_log import ChatLog
        from .widgets.input_box import InputBox
        box = self.query_one(InputBox)
        box.remember(display)
        box.value = ""
        self.query_one(ChatLog).add_user(display)
        self._last_prompt = prompt  # type: ignore[attr-defined]
        self._turn_running = True  # type: ignore[attr-defined]
        self._refresh_status("thinking")  # type: ignore[attr-defined]
        self._run_turn(prompt)  # type: ignore[attr-defined]

    def _wf_say(self, text: str, error: bool = False) -> None:
        from .widgets.chat_log import ChatLog
        from .widgets.input_box import InputBox
        chat = self.query_one(ChatLog)
        if error:
            chat.add_error(text)
        else:
            chat.add_system(text)
        self.query_one(InputBox).focus()

    # — review & advice (agent turns) —

    def _exec_review(self, arg: str) -> None:
        """/review [path] — review staged+unstaged changes (or a path)."""
        if self._wf_busy():
            return
        target = arg.strip() or "the current staged and unstaged git changes"
        self._wf_launch(
            f"/review {arg.strip()}".rstrip(),
            "Code review task. Scope: " + target + ". Steps: "
            "1) git_status + git_diff (staged and unstaged) — or read the given path. "
            "2) Read every changed file fully. "
            "3) Report: correctness bugs, edge cases, security issues, test gaps — "
            "each with file:line and a concrete fix. "
            "Be direct; skip praise. Do NOT write code or commit — review only. "
            "If there are no changes, say so in one line.",
        )

    def _exec_security_review(self, arg: str) -> None:
        """/security-review [path] — adversarial review: injection, SSRF, traversal, secrets."""
        if self._wf_busy():
            return
        target = arg.strip() or "the current staged and unstaged git changes"
        self._wf_launch(
            f"/security-review {arg.strip()}".rstrip(),
            "Security review task. Scope: " + target + ". Steps: "
            "1) git_status + git_diff (staged and unstaged) — or read the given path. "
            "2) Hunt for: command injection, path traversal, SSRF, hardcoded secrets, "
            "unsafe deserialization, weak auth checks, prompt-injection sinks. "
            "3) For each finding: severity (high/medium/low), file:line, exploit sketch "
            "in ONE sentence, and the minimal fix. "
            "Do NOT write code or commit. If clean, say what you checked in 3 lines.",
        )

    def _exec_advisor(self, arg: str) -> None:
        """/advisor <question> — architecture / design advice, no code changes."""
        if self._wf_busy():
            return
        q = arg.strip()
        if not q:
            self._wf_say("Usage: /advisor <design question>", error=True)
            return
        self._wf_launch(
            f"/advisor {q}",
            "Architecture advice (read-only — do NOT write, edit or execute anything). "
            "Question: " + q + " Explore the repo first (structure, relevant files), "
            "then answer with: recommendation, 1-2 alternatives with trade-offs, "
            "and concrete first steps. Keep it under 30 lines.",
        )

    def _exec_bughunter(self, arg: str) -> None:
        """/bughunter [path] — hunt for latent bugs, report only."""
        if self._wf_busy():
            return
        target = arg.strip() or "the whole workspace"
        self._wf_launch(
            f"/bughunter {arg.strip()}".rstrip(),
            "Bug-hunt task (read-only — do NOT fix anything yet). Scope: " + target + ". "
            "Search for: off-by-one errors, unchecked None/empty cases, race conditions, "
            "resource leaks, wrong error handling, dead code paths. "
            "Rank findings by likelihood, each with file:line and why it triggers. "
            "End with the single most valuable fix to make first.",
        )

    # — git flow (agent turns) —

    def _exec_commit(self, arg: str) -> None:
        """/commit [hint] — stage sensible files and commit with a good message."""
        if self._wf_busy():
            return
        hint = f" User hint: {arg.strip()}." if arg.strip() else ""
        self._wf_launch(
            f"/commit {arg.strip()}".rstrip() or "/commit",
            "Commit task." + hint + " Steps: 1) git_status + git_diff to see the work. "
            "2) Stage exactly the files that belong to one logical change "
            "(never stage secrets, .env, or unrelated files — ask if unsure). "
            "3) Write an imperative, specific commit message (≤72-char subject, "
            "short body listing what/why) and git_commit it. "
            "4) Report the commit hash + subject. NEVER push.",
        )

    def _exec_commit_push_pr(self, arg: str) -> None:
        """/commit-push-pr [hint] — commit, push, open a PR."""
        if self._wf_busy():
            return
        hint = f" User hint: {arg.strip()}." if arg.strip() else ""
        self._wf_launch(
            f"/commit-push-pr {arg.strip()}".rstrip() or "/commit-push-pr",
            "Ship task." + hint + " Steps: 1) git_status + git_diff; stage one logical "
            "change; commit with an imperative message. "
            "2) Push the branch (execute_command with the git push for the current "
            "branch; if push needs a new upstream, set it). "
            "3) Open a pull request with a summary + test plan. Prefer the `gh` CLI "
            "when available; otherwise print exact manual steps. "
            "4) Report the PR URL. Never force-push without asking first.",
        )

    def _exec_pr_comments(self, arg: str) -> None:
        """/pr-comments [pr#] — fetch review comments and address each."""
        if self._wf_busy():
            return
        which = f"PR #{arg.strip()}" if arg.strip() else "the PR for the current branch"
        self._wf_launch(
            f"/pr-comments {arg.strip()}".rstrip() or "/pr-comments",
            "PR-comments task for " + which + ". Steps: 1) Find the PR (prefer `gh pr "
            "view --comments`, fall back to git log + remote info). "
            "2) List every unresolved review comment. 3) For each: locate the code, "
            "decide fix vs. push-back-with-reason, implement agreed fixes, verify "
            "with the relevant tests. 4) Summarise per-comment outcomes. "
            "Do NOT push unless the user asked in the same breath.",
        )

    # — local commands —

    def _exec_compact(self, arg: str) -> None:
        """/compact — summarise old conversation, keep recent context."""
        if self._wf_busy():
            return
        res = self.bridge.run_compact()
        if res.get("ok"):
            self._wf_say(f"Compacted: {res.get('message', 'done')}")
        else:
            self._wf_say(f"Compact failed: {res.get('error', 'unknown')}", error=True)

    def _wf_export_path(self, ext: str) -> Path:
        outdir = Path.home() / ".tera_pilot" / "exports"
        outdir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        return outdir / f"chat-{stamp}.{ext}"

    def _exec_export(self, arg: str) -> None:
        """/export [path] — save the current run as JSON (backup / transfer)."""
        res = self.bridge.export_conversation()
        if not res.get("ok"):
            self._wf_say(f"Export failed: {res.get('error', 'unknown')}", error=True)
            return
        import json as _json
        dest = Path(arg.strip()).expanduser() if arg.strip() else self._wf_export_path("json")
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(_json.dumps(
                {"exported_at": datetime.datetime.now().isoformat(),
                 "workspace": self.bridge.workspace,
                 "messages": res["messages"]}, indent=2), encoding="utf-8")
        except OSError as exc:
            self._wf_say(f"Export failed: {exc}", error=True)
            return
        self._wf_say(f"Exported {len(res['messages'])} messages → {dest}")

    def _exec_share(self, arg: str) -> None:
        """/share [path] — save the current run as readable Markdown."""
        res = self.bridge.export_conversation()
        if not res.get("ok"):
            self._wf_say(f"Share failed: {res.get('error', 'unknown')}", error=True)
            return
        dest = Path(arg.strip()).expanduser() if arg.strip() else self._wf_export_path("md")
        try:
            lines = ["# Shared conversation",
                     f"_Workspace: `{self.bridge.workspace}`_",
                     f"_Exported: {datetime.datetime.now().isoformat()}_", ""]
            for m in res["messages"]:
                role = str(m.get("role", "?"))
                body = str(m.get("content", ""))[:6000]
                lines += [f"## {role}", "", body, ""]
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text("\n".join(lines), encoding="utf-8")
        except OSError as exc:
            self._wf_say(f"Share failed: {exc}", error=True)
            return
        self._wf_say(f"Shared {len(res['messages'])} messages → {dest}\nSend this file to share the session.")

    def _exec_share_signed(self, arg: str) -> None:
        """/share-signed — export the run as signed, tamper-evident JSON."""
        res = self.bridge.share_signed_conversation()
        if not res.get("ok"):
            self._wf_say(f"Share failed: {res.get('error', 'unknown')}", error=True)
            return
        signed = "signed ✓ (verify: tera-pilot audit verify)" if res.get("signed") \
            else "UNSIGNED fallback (no crypto backend)"
        self._wf_say(f"Shared {res['messages']} messages → {res['path']}\n{signed}")

    def _wf_recent_chat_id(self) -> Optional[str]:
        try:
            chats = self.bridge.list_chats() or []
        except Exception:
            return None
        return chats[0]["id"] if chats else None

    def _exec_rename(self, arg: str) -> None:
        """/rename <title> | /rename <id> <title> — rename a saved chat."""
        parts = arg.strip().split(None, 1)
        if not parts:
            self._wf_say("Usage: /rename <title>  (or /rename <id> <title>)", error=True)
            return
        chats = []
        try:
            chats = self.bridge.list_chats() or []
        except Exception:
            pass
        ids = {c["id"] for c in chats}
        if len(parts) == 2 and parts[0] in ids:
            sid, title = parts
        else:
            sid = self._wf_recent_chat_id()
            title = arg.strip()
            if not sid:
                self._wf_say("No saved chats yet — nothing to rename.", error=True)
                return
        res = self.bridge.rename_chat(sid, title)
        if res.get("ok"):
            self._wf_say(f"Renamed → {res['title']}")
        else:
            self._wf_say(f"Rename failed: {res.get('error', 'unknown')}", error=True)

    def _exec_tag(self, arg: str) -> None:
        """/tag show [id] | /tag <t1,t2> [id] — tag saved chats."""
        text = arg.strip()
        if not text:
            self._wf_say("Usage: /tag show [id]  |  /tag <tag1,tag2> [id]", error=True)
            return
        parts = text.split()
        chats = []
        try:
            chats = self.bridge.list_chats() or []
        except Exception:
            pass
        ids = {c["id"] for c in chats}
        if parts[0] == "show":
            sid = parts[1] if len(parts) > 1 and parts[1] in ids else self._wf_recent_chat_id()
            if not sid:
                self._wf_say("No saved chats yet.", error=True)
                return
            tags = self.bridge.get_chat_tags(sid)
            self._wf_say(f"Tags for {sid}: {', '.join(tags) or '(none)'}")
            return
        if parts[-1] in ids and len(parts) > 1:
            sid, raw = parts[-1], " ".join(parts[:-1])
        else:
            sid, raw = self._wf_recent_chat_id(), text
            if not sid:
                self._wf_say("No saved chats yet — nothing to tag.", error=True)
                return
        tags = [t for chunk in raw.replace(",", " ").split() for t in [chunk.strip()] if t]
        res = self.bridge.set_chat_tags(sid, tags)
        if res.get("ok"):
            self._wf_say(f"Tags for {sid}: {', '.join(res['tags']) or '(cleared)'}")
        else:
            self._wf_say(f"Tag failed: {res.get('error', 'unknown')}", error=True)

    def _exec_stats(self, arg: str) -> None:
        """/stats — session numbers: usage, compaction, tools, schedule."""
        bits = []
        try:
            usage = self.bridge.get_usage() or {}
            if isinstance(usage, dict) and usage:
                flat = ", ".join(f"{k}={v}" for k, v in list(usage.items())[:8])
                bits.append(f"Usage: {flat}")
        except Exception:
            pass
        try:
            comp = self.bridge.get_compaction_stats()
            if comp:
                bits.append(f"Compaction: {comp}")
        except Exception:
            pass
        try:
            sched = self.bridge.get_schedule()
            entries = (sched or {}).get("entries", [])
            bits.append(f"Scheduled tasks: {len(entries)}")
        except Exception:
            pass
        try:
            chats = self.bridge.list_chats() or []
            bits.append(f"Saved chats: {len(chats)}")
        except Exception:
            pass
        self._wf_say("\n".join(bits) if bits else "No stats available yet.")

    def _exec_effort(self, arg: str) -> None:
        """/effort <normal|brief|detailed> — how much the agent explains."""
        level = arg.strip().lower()
        if level not in ("normal", "brief", "detailed"):
            self._wf_say(f"Effort: {self.bridge.get_verbosity()}  (usage: /effort normal|brief|detailed)")
            return
        res = self.bridge.set_verbosity(level)
        self._wf_say(f"Effort → {level}" if res.get("ok")
                     else f"Failed: {res.get('error')}", error=not res.get("ok"))

    def _exec_fast(self, arg: str) -> None:
        """/fast [on|off] — minimal chatter, minimal tool calls."""
        arg = arg.strip().lower()
        if arg in ("on", "fast"):
            res = self.bridge.set_verbosity("fast")
        elif arg in ("off", "normal"):
            res = self.bridge.set_verbosity("normal")
        else:
            self._wf_say(f"Fast mode is {'ON' if self.bridge.get_verbosity() == 'fast' else 'OFF'}  (usage: /fast on|off)")
            return
        self._wf_say("Fast mode ON" if res.get("verbosity") == "fast" else "Fast mode OFF",
                     error=not res.get("ok"))

    def _exec_brief(self, arg: str) -> None:
        """/brief [on|off] — terse output mode."""
        arg = arg.strip().lower()
        if arg in ("on", "brief"):
            res = self.bridge.set_verbosity("brief")
        elif arg in ("off", "normal"):
            res = self.bridge.set_verbosity("normal")
        else:
            self._wf_say(f"Brief mode is {'ON' if self.bridge.get_verbosity() == 'brief' else 'OFF'}  (usage: /brief on|off)")
            return
        self._wf_say("Brief mode ON" if res.get("verbosity") == "brief" else "Brief mode OFF",
                     error=not res.get("ok"))

    def _exec_output_style(self, arg: str) -> None:
        """/output-style [normal|brief|detailed|fast] — output verbosity."""
        level = arg.strip().lower()
        if not level:
            self._wf_say(f"Output style: {self.bridge.get_verbosity()}  (normal/brief/detailed/fast)")
            return
        res = self.bridge.set_verbosity(level)
        self._wf_say(f"Output style → {level}" if res.get("ok")
                     else f"Failed: {res.get('error')}", error=not res.get("ok"))

    def _exec_permissions(self, arg: str) -> None:
        """/permissions show | mode <m> | allow <pat> | deny <pat> | rm <#>."""
        parts = arg.strip().split(None, 1)
        sub = parts[0].lower() if parts else "show"
        rest = parts[1] if len(parts) > 1 else ""
        if sub == "show":
            st = self.bridge.get_permission_state()
            if not st.get("ok"):
                self._wf_say(f"Failed: {st.get('error')}", error=True)
                return
            lines = [f"Mode: {st['mode']}  (default/plan/auto/bypass)"]
            rules = st.get("rules", [])
            if not rules:
                lines.append("Rules: (none) — everything prompts per autonomy.")
            for i, r in enumerate(rules):
                lines.append(f"  [{i}] {r.get('effect')}: {r.get('pattern')}")
            lines.append("Examples: /permissions allow \"execute_command(git *)\" · /permissions mode plan")
            self._wf_say("\n".join(lines))
        elif sub == "mode":
            res = self.bridge.set_permission_mode(rest)
            self._wf_say(f"Permission mode → {rest}" if res.get("ok")
                         else f"Failed: {res.get('error')}", error=not res.get("ok"))
        elif sub in ("allow", "deny"):
            if not rest:
                self._wf_say(f"Usage: /permissions {sub} \"<pattern>\"", error=True)
                return
            res = self.bridge.add_permission_rule(rest.strip("\"'"), sub)
            self._wf_say(f"Rule added: {sub} {rest}" if res.get("ok")
                         else f"Failed: {res.get('error')}", error=not res.get("ok"))
        elif sub in ("rm", "remove", "del"):
            try:
                res = self.bridge.remove_permission_rule(int(rest.strip()))
            except ValueError:
                self._wf_say("Usage: /permissions rm <#>", error=True)
                return
            self._wf_say("Rule removed." if res.get("ok")
                         else f"Failed: {res.get('error')}", error=not res.get("ok"))
        else:
            self._wf_say("Usage: /permissions show|mode|allow|deny|rm", error=True)

    def _exec_init(self, arg: str) -> None:
        """/init — scaffold the project memory file."""
        res = self.bridge.init_memory_file()
        if not res.get("ok"):
            self._wf_say(f"Init failed: {res.get('error')}", error=True)
            return
        if res.get("exists"):
            self._wf_say(f"Memory file already exists: {res['path']}\nEdit it or /remember <fact> to append.")
        else:
            self._wf_say(f"Created {res['path']}\nFill in stack, conventions, gotchas — the agent reads it every run. "
                          "(/remember <fact> appends a line.)")

    def _exec_onboarding(self, arg: str) -> None:
        """/onboarding — first-run checklist with live status."""
        try:
            providers = self.bridge.list_providers() or []
            active = self.bridge.get_active_provider_id()
        except Exception:
            providers, active = [], "?"
        try:
            chats = len(self.bridge.list_chats() or [])
        except Exception:
            chats = 0
        self._wf_say(
            "Onboarding — 4 steps to a working pilot:\n"
            f"1) Provider: active = {active} ({len(providers)} known). "
            "Switch: /model · key: /key\n"
            f"2) Workspace: {self.bridge.workspace} — move: /cd\n"
            "3) Memory: /init creates the project memory file; /remember stores facts.\n"
            "4) First task: describe it in plain words, e.g. “find the riskiest file "
            "and explain why”.\n"
            f"Saved chats so far: {chats}. Full help: /help.")

    def _exec_remember(self, arg: str) -> None:
        """/remember <fact> | global <fact> | list | forget <#> | scan."""
        text = arg.strip()
        if not text:
            self._wf_say("Usage: /remember <fact> | /remember global <fact> | /remember list | /remember forget <#> | /remember scan",
                         error=True)
            return
        if text == "list":
            res = self.bridge.list_facts()
            if not res.get("ok"):
                self._wf_say(f"Failed: {res.get('error')}", error=True)
                return
            lines = []
            for scope in ("project", "global"):
                facts = res.get(scope, [])
                lines.append(f"{scope} ({res.get(scope + '_path', '')}):")
                lines += [f"  [{i}] {f}" for i, f in enumerate(facts)] or ["  (none)"]
            self._wf_say("\n".join(lines))
            return
        if text == "scan":
            conv = self.bridge.export_conversation(max_messages=60)
            if not conv.get("ok"):
                self._wf_say("Nothing to scan yet.", error=True)
                return
            transcript = "\n".join(str(m.get("content", "")) for m in conv["messages"][-60:])
            sug = self.bridge.suggest_facts(transcript)
            facts = (sug or {}).get("facts", [])
            if not facts:
                self._wf_say("No memorable statements found in recent chat.")
                return
            lines = ["Candidates — /remember the ones you want to keep:"]
            lines += [f"  · {f['fact']} ({f['confidence']:.1f})" for f in facts]
            self._wf_say("\n".join(lines))
            return
        if text.startswith("forget "):
            try:
                idx = int(text.split(None, 1)[1])
            except ValueError:
                self._wf_say("Usage: /remember forget <#>", error=True)
                return
            res = self.bridge.forget_fact(idx)
            self._wf_say("Forgotten." if res.get("ok") else f"Failed: {res.get('error')}",
                         error=not res.get("ok"))
            return
        scope = "project"
        fact = text
        if text.startswith("global "):
            scope, fact = "global", text[len("global "):]
        res = self.bridge.remember_fact(fact, scope)
        if res.get("ok"):
            dup = " (already stored)" if res.get("duplicate") else ""
            self._wf_say(f"Remembered [{scope}]{dup}: {fact[:120]}")
        else:
            self._wf_say(f"Failed: {res.get('error')}", error=True)

    def _exec_plugin(self, arg: str) -> None:
        """/plugin list | install <path|url> | remove <n> | enable|disable <n>."""
        parts = arg.strip().split(None, 1)
        sub = parts[0].lower() if parts else "list"
        rest = parts[1].strip() if len(parts) > 1 else ""
        if sub == "list":
            res = self.bridge.plugin_list()
            if not res.get("ok"):
                self._wf_say(f"Failed: {res.get('error')}", error=True)
                return
            plugins = res.get("plugins", [])
            if not plugins:
                lines = ["No plugins installed.",
                         "Install: /plugin install <path-to-.py|https-url>"]
            else:
                lines = [f"  {'✓' if p['enabled'] else '✗'} {p['name']}"
                         f"{(' — ' + p['description'][:80]) if p.get('description') else ''}"
                         for p in plugins]
            self._wf_say("\n".join(lines))
        elif sub == "install":
            if not rest:
                self._wf_say("Usage: /plugin install <path|https-url>", error=True)
                return
            res = self.bridge.plugin_install(rest)
            self._wf_say(f"Installed {res.get('name')} — restart the TUI to load it."
                         if res.get("ok") else f"Failed: {res.get('error')}",
                         error=not res.get("ok"))
        elif sub == "remove":
            res = self.bridge.plugin_remove(rest)
            self._wf_say(f"Removed {rest}." if res.get("ok") else f"Failed: {res.get('error')}",
                         error=not res.get("ok"))
        elif sub in ("enable", "disable"):
            res = self.bridge.plugin_enable(rest, sub == "enable")
            self._wf_say(f"{rest} {sub}d — takes effect on restart."
                         if res.get("ok") else f"Failed: {res.get('error')}",
                         error=not res.get("ok"))
        else:
            self._wf_say("Usage: /plugin list|install|remove|enable|disable", error=True)

    def _exec_schedule(self, arg: str) -> None:
        """/schedule [rm <id>] — show scheduled tasks (added by the agent)."""
        parts = arg.strip().split()
        if len(parts) == 2 and parts[0] == "rm":
            try:
                from tera_pilot.agent_runtime.tool_engine import ToolEngine
                eng = ToolEngine(self.bridge.workspace)
                out = eng._cron_remove(parts[1])
            except Exception as exc:
                out = f"[CRON ERROR] {exc}"
            self._wf_say(out, error=out.startswith("[CRON ERROR]"))
            return
        res = self.bridge.get_schedule()
        if not res.get("ok"):
            self._wf_say(f"Failed: {res.get('error')}", error=True)
            return
        entries = res.get("entries", [])
        if not entries:
            self._wf_say("No scheduled tasks. The agent can add them (cron_add).")
            return
        self._wf_say("\n".join(
            f"  [{e.get('id')}] {'on' if e.get('enabled') else 'off'} "
            f"{e.get('schedule')} — {str(e.get('task'))[:100]}" for e in entries))

    def _exec_tasks(self, arg: str) -> None:
        """/tasks [id] — live background tasks of this run (completions also stream to Activity)."""
        parts = arg.strip().split()
        if len(parts) == 1:
            res = self.bridge.get_bg_task_output(parts[0])
            self._wf_say(res.get("output", "") if res.get("ok")
                         else f"Failed: {res.get('error')}", error=not res.get("ok"))
            return
        if parts:
            self._wf_say("Usage: /tasks [id]", error=True)
            return
        res = self.bridge.get_bg_tasks()
        if not res.get("ok"):
            self._wf_say(f"Failed: {res.get('error')}", error=True)
            return
        tasks = res.get("tasks", [])
        if not tasks:
            self._wf_say("No background tasks this run. The agent starts them with task_spawn.")
            return
        self._wf_say("\n".join(
            f"  [{t['id']}] ({t['status']}) {t['label']}: {t['command']}" for t in tasks)
            + "\n/tasks <id> shows the output.")
