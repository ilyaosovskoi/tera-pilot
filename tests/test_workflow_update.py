"""Tests for the v2.5.0 workflow update.

Covers: permission rules, memory extraction, the new ToolEngine tools
(ask/todo/plan/worktree/repl/tasks/team/cron/sleep/symbols), prompt
gating per section, verbosity, the plugin marketplace helpers and the
new bridge methods. All home-directory state (~/.tera_pilot/...) is
redirected to tmp_path via HOME so tests are hermetic.
"""

import os
import shutil
from pathlib import Path

import pytest

from tera_pilot.agent_runtime.tool_engine import ToolEngine
from tera_pilot.agent_runtime.types import ToolCall, ToolName


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture()
def engine(tmp_path):
    eng = ToolEngine(str(tmp_path))
    eng.autonomy = "never_ask"
    eng.headless_confirm = "allow"
    return eng


def call(eng, name, args):
    return eng.execute(ToolCall(name=name, args=args))


# ── permission rules ────────────────────────────────────────────────

def test_rules_allow_deny_and_none(home):
    from tera_pilot import permission_rules as pr
    assert pr.check("execute_command", "Run: pytest -q") is None
    pr.add_rule("execute_command(git *)", "allow")
    pr.add_rule("execute_command(rm *)", "deny")
    assert pr.check("execute_command", "Run: git status") is True
    assert pr.check("execute_command", "Run: rm -rf /tmp/x") is False
    assert pr.check("execute_command", "Run: pytest -q") is None
    pr.remove_rule(1)
    assert pr.check("execute_command", "Run: rm -rf /tmp/x") is None


def test_rules_deny_wins_over_never_ask(home, engine):
    from tera_pilot import permission_rules as pr
    pr.add_rule("repl_run(*)", "deny")
    out = call(engine, ToolName.REPL_RUN,
               {"session": "s", "code": "print(1)", "language": "python"})
    assert "REJECTED" in out or "denied" in out.lower()


def test_permission_modes(home, engine):
    from tera_pilot import permission_rules as pr
    calls = []

    def fake_confirm(info):
        calls.append(info)
        engine._confirm_accepted = True
        engine._confirm_event.set()

    engine.autonomy = "always_ask"
    engine._confirm_callback = fake_confirm
    # default mode prompts every time
    assert engine._request_confirmation("todo_write", "x") is True
    assert engine._request_confirmation("todo_write", "x") is True
    assert len(calls) == 2
    # plan mode asks once per action kind
    pr.set_mode("plan")
    assert engine._request_confirmation("todo_write", "x") is True
    assert engine._request_confirmation("todo_write", "x") is True
    assert len(calls) == 3
    # bypass never prompts
    pr.set_mode("bypass")
    assert engine._request_confirmation("delete_file", "x") is True
    assert len(calls) == 3
    # auto allows tame actions, prompts for destructive ones
    pr.set_mode("auto")
    assert engine._request_confirmation("todo_write", "x") is True
    assert len(calls) == 3
    assert engine._request_confirmation("execute_command", "Run: git status") is True
    assert len(calls) == 4


def test_malformed_rules_file_fails_closed(home):
    from tera_pilot import permission_rules as pr
    pr.rules_path().parent.mkdir(parents=True, exist_ok=True)
    pr.rules_path().write_text("{not json", encoding="utf-8")
    assert pr.get_mode() == "default"
    assert pr.check("execute_command", "anything") is None


# ── ask_user / todos ────────────────────────────────────────────────

def test_ask_user_fallback_and_callback(engine):
    out = call(engine, ToolName.ASK_USER, {"question": "Which one?", "options": ["a", "b"]})
    assert "Which one?" in out and "best judgment" in out
    engine.set_ask_user_callback(lambda q, o: "b it is")
    out = call(engine, ToolName.ASK_USER, {"question": "Which one?"})
    assert out == "[USER ANSWER] b it is"
    assert "required" in call(engine, ToolName.ASK_USER, {"question": " "})


def test_todo_write_list_validation(engine):
    assert "empty" in call(engine, ToolName.TODO_LIST, {})
    out = call(engine, ToolName.TODO_WRITE, {"todos": [
        {"text": "first", "status": "in_progress"}, {"text": "second"}]})
    assert "2 item(s), 2 open" in out
    shown = call(engine, ToolName.TODO_LIST, {})
    assert "first" in shown and "second" in shown
    assert "ERROR" in call(engine, ToolName.TODO_WRITE, {"todos": "nope"})
    assert "ERROR" in call(engine, ToolName.TODO_WRITE, {"todos": [{"text": "x", "status": "bogus"}]})
    assert "ERROR" in call(engine, ToolName.TODO_WRITE, {"todos": [{"text": " "}]})


# ── plan mode gate ──────────────────────────────────────────────────

def test_plan_mode_blocks_writes_until_exit(engine):
    assert "PLAN MODE ON" in call(engine, ToolName.ENTER_PLAN_MODE, {"goal": "refactor"})
    blocked = call(engine, ToolName.WRITE_FILE, {"path": "a.txt", "content": "x"})
    assert "PLAN MODE" in blocked
    assert "disabled" in call(engine, ToolName.EXECUTE_COMMAND, {"command": "git status"})
    assert "not a file" in call(engine, ToolName.READ_FILE, {"path": "missing.txt"}) \
        or "PLAN MODE" not in call(engine, ToolName.READ_FILE, {"path": "missing.txt"})
    out = call(engine, ToolName.EXIT_PLAN_MODE, {"summary": "plan done"})
    assert "PLAN MODE OFF" in out
    assert "PLAN MODE" not in call(engine, ToolName.READ_FILE, {"path": "missing.txt"})


# ── section + role gates ────────────────────────────────────────────

def test_office_section_rejects_isolation_tools(engine):
    engine.section = "office"
    out = call(engine, ToolName.TASK_SPAWN, {"label": "t", "command": "git status"})
    assert "TOOL REJECTED" in out
    out = call(engine, ToolName.ASK_USER, {"question": "q"})
    assert "TOOL REJECTED" not in out


def test_explore_role_cannot_spawn_tasks(engine):
    engine.set_role_whitelist("explore")
    out = call(engine, ToolName.TASK_SPAWN, {"label": "t", "command": "git status"})
    assert "TOOL DENIED" in out
    engine.set_role_whitelist("parent")


# ── repl ────────────────────────────────────────────────────────────

def test_repl_persists_state(engine):
    assert "no output" in call(engine, ToolName.REPL_RUN,
                                {"session": "r", "code": "v = 21"})
    assert "42" in call(engine, ToolName.REPL_RUN,
                        {"session": "r", "code": "print(v * 2)"})
    assert "Traceback" in call(engine, ToolName.REPL_RUN,
                               {"session": "r", "code": "print(nope)"})
    assert "reset" in call(engine, ToolName.REPL_RESET, {"session": "r"})
    assert "ERROR" in call(engine, ToolName.REPL_RUN,
                           {"session": "r", "code": "print(1)", "language": "node"})


# ── background tasks ────────────────────────────────────────────────

def test_task_spawn_output_list(engine):
    import time
    out = call(engine, ToolName.TASK_SPAWN, {"label": "ver", "command": "git --version"})
    assert "started" in out
    deadline = time.time() + 15
    while time.time() < deadline:
        if "running" not in call(engine, ToolName.TASK_LIST, {}):
            break
        time.sleep(0.3)
    shown = call(engine, ToolName.TASK_OUTPUT, {"id": "1"})
    assert "git version" in shown
    assert "no task" in call(engine, ToolName.TASK_OUTPUT, {"id": "999"})


def test_task_spawn_blocked_command_stays_blocked(engine):
    out = call(engine, ToolName.TASK_SPAWN, {"label": "evil", "command": "rm -rf /"})
    assert "SECURITY" in out or "blocked" in out.lower()


# ── team bus / cron / sleep / symbols ───────────────────────────────

def test_team_send_list(home, engine):
    assert "queued" in call(engine, ToolName.TEAM_SEND,
                            {"target": "reviewer", "message": "look at auth.py"})
    assert "reviewer" in call(engine, ToolName.TEAM_LIST, {})
    assert "required" in call(engine, ToolName.TEAM_SEND, {"target": "", "message": ""})


def test_cron_add_list_remove(home, engine):
    assert "bad schedule" in call(engine, ToolName.CRON_ADD,
                                  {"schedule": "nope", "task": "x"})
    assert "stored" in call(engine, ToolName.CRON_ADD,
                            {"schedule": "0 9 * * 1", "task": "run tests"})
    assert "0 9 * * 1" in call(engine, ToolName.CRON_LIST, {})
    assert "no entry" in call(engine, ToolName.CRON_REMOVE, {"id": "42"})
    assert "removed" in call(engine, ToolName.CRON_REMOVE, {"id": "1"})
    assert "no scheduled" in call(engine, ToolName.CRON_LIST, {})


def test_sleep_bounds(engine):
    assert "waited 1s" in call(engine, ToolName.SLEEP, {"seconds": 1})
    assert "capped" in call(engine, ToolName.SLEEP, {"seconds": 9999})


def test_code_symbols(engine, tmp_path):
    target = tmp_path / "mod.py"
    target.write_text("class Api:\n    pass\n\ndef handle(x):\n    return x\n")
    out = call(engine, ToolName.CODE_SYMBOLS, {"path": "mod.py"})
    assert "class Api" in out and "def handle" in out
    other = tmp_path / "notes.txt"
    other.write_text("hello")
    assert "no symbol patterns" in call(engine, ToolName.CODE_SYMBOLS, {"path": "notes.txt"})
    assert "SECURITY" in call(engine, ToolName.CODE_SYMBOLS, {"path": "../escape.py"})


# ── worktrees ───────────────────────────────────────────────────────

@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_worktree_add_list_remove(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    os.system(f"git init -q {repo} && git -C {repo} config user.email t@t "
              f"&& git -C {repo} config user.name t "
              f"&& git -C {repo} commit -q --allow-empty -m init")
    eng = ToolEngine(str(repo))
    eng.autonomy = "never_ask"
    eng.headless_confirm = "allow"
    assert "created" in call(eng, ToolName.WORKTREE_ADD, {"name": "spike"})
    assert "spike" in call(eng, ToolName.WORKTREE_LIST, {})
    assert "removed" in call(eng, ToolName.WORKTREE_REMOVE, {"name": "spike"})
    assert "bad 'name'" in call(eng, ToolName.WORKTREE_ADD, {"name": "../evil"})


# ── prompts / parser / runtime ──────────────────────────────────────

def test_prompt_section_gating_and_verbosity():
    from tera_pilot.agent_runtime.prompts import (
        PromptBuilder, build_native_tools_schema)
    general = PromptBuilder.system(section="general")
    office = PromptBuilder.system(section="office")
    assert "enter_plan_mode" in general
    assert "enter_plan_mode" not in office
    assert "ask_user" in office  # all-section tools stay
    names = [t["function"]["name"] for t in build_native_tools_schema("office")]
    assert "task_spawn" not in names and "ask_user" in names
    assert len(PromptBuilder.system(verbosity="brief")) > len(PromptBuilder.system())
    assert PromptBuilder.system(verbosity="bogus") == PromptBuilder.system()


def test_parser_and_native_schema_cover_new_tools():
    from tera_pilot.agent_runtime.parser import OutputParser
    from tera_pilot.agent_runtime.prompts import build_native_tools_schema
    for tool in ("ask_user", "todo_write", "task_spawn", "cron_add", "code_symbols"):
        assert tool in OutputParser.TOOL_ARG_HINTS
    names = [t["function"]["name"] for t in build_native_tools_schema("general")]
    for tool in ("ask_user", "todo_write", "todo_list", "task_spawn",
                 "team_send", "cron_add", "sleep", "code_symbols"):
        assert tool in names


def test_runtime_verbosity_kwarg():
    from tera_pilot.agent_runtime.runtime import AgentRuntime
    from tera_pilot.providers import get_registry
    reg = get_registry()
    agent = AgentRuntime(registry=reg, verbosity="brief")
    assert agent.verbosity == "brief"
    assert "brief" in agent._system_prompt().lower()
    agent2 = AgentRuntime(registry=reg, verbosity="bogus")
    assert agent2.verbosity == "normal"


# ── memory ──────────────────────────────────────────────────────────

def test_memory_extract_and_store(home, tmp_path):
    from tera_pilot import memory_extract as mem
    facts = mem.extract_facts("Please remember that we use pytest. Never commit secrets.")
    assert any("pytest" in f["fact"] for f in facts)
    res = mem.remember_fact("we use pytest", str(tmp_path), "project")
    assert res["ok"] and res["path"].endswith("MEMORY.md")
    dup = mem.remember_fact("we use pytest", str(tmp_path), "project")
    assert dup["duplicate"] is True
    prompt = mem.facts_for_prompt(str(tmp_path))
    assert "pytest" in prompt
    assert mem.forget_fact(0, str(tmp_path), "project")["ok"] is True


# ── plugins marketplace ─────────────────────────────────────────────

def test_plugin_install_disable_remove(home, tmp_path):
    from tera_pilot import plugins as plug
    src = tmp_path / "demo_plug.py"
    src.write_text("def register():\n    return None\n")
    res = plug.install_plugin(str(src))
    assert res["ok"] and res["name"] == "demo_plug"
    names = [p["name"] for p in plug.list_marketplace()]
    assert "demo_plug" in names
    assert plug.set_plugin_enabled("demo_plug", False)["ok"] is True
    assert plug.is_plugin_enabled("demo_plug") is False
    assert plug.set_plugin_enabled("demo_plug", True)["ok"] is True
    assert plug.remove_plugin("demo_plug")["ok"] is True
    bad = tmp_path / "bad.py"
    bad.write_text("x = 1\n")
    assert plug.install_plugin(str(bad))["ok"] is False


def test_builtin_skills_include_new_set():
    from tera_pilot.skill_loader import load_all_skills_with_builtins
    ids = {s.id for s in load_all_skills_with_builtins()}
    for want in ("debug_detective", "code_simplifier", "stuck_unblocker",
                 "batch_operator", "change_remember", "config_updater", "skill_forge"):
        assert want in ids


# ── bridge ──────────────────────────────────────────────────────────

def test_bridge_verbosity_and_helpers(home, tmp_path):
    from tera_pilot_tui.bridge import TeraPilotBridge
    bridge = TeraPilotBridge(workspace=str(tmp_path))
    assert bridge.get_verbosity() == "normal"
    assert bridge.set_verbosity("brief")["ok"] is True
    assert bridge.get_verbosity() == "brief"
    assert bridge.set_verbosity("bogus")["ok"] is False
    assert bridge.set_verbosity("normal")["ok"] is True
    sched = bridge.get_schedule()
    assert sched["ok"] is True and sched["entries"] == []
    assert bridge.plugin_list()["ok"] is True
    assert bridge.get_permission_state()["ok"] is True
    mem = bridge.init_memory_file()
    assert mem["ok"] is True and mem["path"].endswith("MEMORY.md")
    assert bridge.remember_fact("we use pytest")["ok"] is True
    facts = bridge.list_facts()
    assert facts["ok"] is True and any("pytest" in f for f in facts["project"])


# ── v2.5.0 follow-ups ───────────────────────────────────────────────

def test_lsp_tools(engine, tmp_path):
    (tmp_path / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\nresult = add(2, 3)\n")
    syms = call(engine, ToolName.LSP_SYMBOLS, {"path": "calc.py"})
    assert "add" in syms
    definition = call(engine, ToolName.LSP_DEFINITION,
                      {"path": "calc.py", "line": 4, "character": 9})
    assert "calc.py:1:4" in definition
    refs = call(engine, ToolName.LSP_REFERENCES,
                {"path": "calc.py", "line": 1, "character": 4})
    assert "calc.py:1:4" in refs and "calc.py:4:9" in refs
    assert "out of range" in call(engine, ToolName.LSP_DEFINITION,
                                  {"path": "calc.py", "line": 99})
    assert "SECURITY" in call(engine, ToolName.LSP_DEFINITION,
                              {"path": "../x.py", "line": 1})


def test_lsp_missing_server(engine, monkeypatch, tmp_path):
    import tera_pilot.lsp_sync as sync
    monkeypatch.setattr(sync, "find_server_command", lambda: None)
    (tmp_path / "whatever.py").write_text("x = 1\n")
    engine._lsp = None
    out = call(engine, ToolName.LSP_SYMBOLS, {"path": "whatever.py"})
    assert "LSP ERROR" in out and "pip install" in out


def test_scheduler_matching_and_tick(home):
    from datetime import datetime
    from tera_pilot.scheduler import entry_due, _field_matches, ScheduleRunner
    assert _field_matches("*", 5, 0, 59) is True
    assert _field_matches("*/15", 30, 0, 59) is True
    assert _field_matches("*/15", 31, 0, 59) is False
    assert _field_matches("1-5", 3, 0, 59) is True
    assert _field_matches("1,3,5", 4, 0, 59) is False
    assert _field_matches("bogus", 4, 0, 59) is False
    now = datetime.now()
    assert entry_due("* * * * *", now) is True
    assert entry_due("0 0 30 2 *", now) is False
    assert entry_due("not a schedule", now) is False
    import json as _json
    sched = home / ".tera_pilot" / "schedule.json"
    sched.parent.mkdir(parents=True, exist_ok=True)
    sched.write_text(_json.dumps([{"id": "1", "schedule": "* * * * *",
                                   "task": "hello", "enabled": True}]),
                     encoding="utf-8")
    fired_all = []
    runner = ScheduleRunner(lambda prompt, ws="": fired_all.append(prompt))
    assert runner.tick(now) == ["1"]
    assert runner.tick(now) == []  # once per minute bucket
    assert fired_all == ["hello"]


def test_remote_approval_roundtrip():
    import threading
    import time
    from tera_pilot.remote_approval import RemoteApprovalBroker
    sent = []
    broker = RemoteApprovalBroker(sender=sent.append)
    assert broker.resolve("just a task") is None
    assert "No pending" in (broker.resolve("ALLOW 99") or "")
    result = []
    thread = threading.Thread(
        target=lambda: result.append(broker.request_approval("t1", "delete_file", "x")))
    thread.start()
    time.sleep(0.3)
    assert broker.pending_count() == 1
    assert "ALLOW" in (broker.resolve("allow 1") or "")
    thread.join(timeout=10)
    assert result == [True]
    assert sent and "ALLOW 1" in sent[0]
    # deny path
    result2 = []
    thread2 = threading.Thread(
        target=lambda: result2.append(broker.request_approval("t2", "execute_command", "y")))
    thread2.start()
    time.sleep(0.3)
    broker.resolve("DENY 2")
    thread2.join(timeout=10)
    assert result2 == [False]


def test_auto_checkpoint_before_delete(home, engine, tmp_path):
    from tera_pilot.checkpoint import get_checkpoint_manager, reset_checkpoint_manager
    reset_checkpoint_manager()
    target = tmp_path / "doomed.txt"
    target.write_text("precious")
    out = call(engine, ToolName.DELETE_FILE, {"path": "doomed.txt"})
    assert "DELETED" in out
    mgr = get_checkpoint_manager(session_id="default")
    assert len(mgr.list_checkpoints()) >= 1
    reset_checkpoint_manager()


def test_ask_modal_and_bridge_handler(home, tmp_path):
    from tera_pilot_tui.widgets.ask_modal import AskUserModal
    modal = AskUserModal("Which one?", ["a", "b"])
    assert modal is not None
    from tera_pilot_tui.bridge import TeraPilotBridge
    bridge = TeraPilotBridge(workspace=str(tmp_path))
    assert bridge._on_ask_user("q", []) is None
    bridge.set_ask_handler(lambda q, o: "picked-a")
    assert bridge._on_ask_user("q", ["a"]) == "picked-a"
    bridge.set_ask_handler(None)
    assert bridge._on_ask_user("q", []) is None


def test_bridge_bg_tasks_empty(home, tmp_path):
    from tera_pilot_tui.bridge import TeraPilotBridge
    bridge = TeraPilotBridge(workspace=str(tmp_path))
    assert bridge.get_bg_tasks() == {"ok": True, "tasks": []}


def test_share_signed_export(home, tmp_path):
    from tera_pilot_tui.bridge import TeraPilotBridge
    bridge = TeraPilotBridge(workspace=str(tmp_path))
    res = bridge.share_signed_conversation()
    assert res.get("ok") is True
    assert Path(res["path"]).exists()
    assert res["messages"] == 0
    try:
        from tera_pilot.audit_signing import verify_signed_file
        if res.get("signed"):
            report = verify_signed_file(res["path"])
            assert report.ok is True
    except ImportError:
        pass


# ── v2.5.0 second wave ──────────────────────────────────────────────

def test_tool_use_hooks_wired(engine):
    from tera_pilot.hook_system import (
        get_hook_manager, reset_hook_manager, HookResult, HookAction)
    reset_hook_manager()
    mgr = get_hook_manager()
    seen = []
    mgr.register("pre_tool_use",
                 lambda ev: seen.append(ev.tool_name) or HookResult(action=HookAction.ALLOW),
                 name="w-pre")
    mgr.register("post_tool_use",
                 lambda ev: seen.append("post:" + ev.tool_name) or HookResult(action=HookAction.ALLOW),
                 name="w-post")
    mgr.register("pre_tool_use",
                 lambda ev: HookResult(action=HookAction.BLOCK, message="stop")
                 if ev.tool_name == "delete_file"
                 else HookResult(action=HookAction.ALLOW),
                 name="w-block")
    assert "empty" in call(engine, ToolName.TODO_LIST, {})
    assert "HOOK BLOCK" in call(engine, ToolName.DELETE_FILE, {"path": "x"})
    assert seen == ["todo_list", "post:todo_list", "delete_file"]
    # MODIFY rewrites args in place.
    reset_hook_manager()
    get_hook_manager().register(
        "pre_tool_use",
        lambda ev: HookResult(action=HookAction.MODIFY,
                              modified_args={"question": "rewritten?", "options": []}),
        name="w-mod")
    out = call(engine, ToolName.ASK_USER, {"question": "original?"})
    assert "rewritten?" in out
    reset_hook_manager()


def test_add_dir_and_sandbox_bridge(home, tmp_path):
    from tera_pilot_tui.bridge import TeraPilotBridge
    bridge = TeraPilotBridge(workspace=str(tmp_path))
    other = tmp_path / "other"
    other.mkdir()
    assert bridge.add_extra_dir(str(tmp_path / "nope"))["ok"] is False
    res = bridge.add_extra_dir(str(other))
    assert res["ok"] is True and res["dir"] in res["dirs"]
    assert bridge.list_extra_dirs()["dirs"] == res["dirs"]
    assert bridge.remove_extra_dir(str(other))["ok"] is True
    assert bridge.get_sandbox_mode()["mode"] in ("off", "auto", "on")
    assert bridge.set_sandbox_mode("bogus")["ok"] is False
    assert bridge.set_sandbox_mode("on") == {"ok": True, "mode": "on"}
    assert bridge.get_sandbox_mode()["mode"] == "on"
    assert bridge.set_sandbox_mode("auto")["ok"] is True


def test_output_styles(home, tmp_path):
    from tera_pilot.output_styles import (
        load_all_styles, get_style_suffix, set_active_style)
    from tera_pilot.agent_runtime.prompts import PromptBuilder
    assert get_style_suffix("normal") == ""
    assert "terse" in get_style_suffix("brief").lower()
    assert set_active_style("bogus")["ok"] is False
    style_file = tmp_path / "report.md"
    style_file.write_text("---\nname: report\ndescription: Status reports.\n---\n## Report\nBe structured.\n")
    (tmp_path / "x").mkdir(exist_ok=True)
    import os as _os
    home_styles = Path(_os.path.expanduser("~/.tera_pilot")) / "styles"
    home_styles.mkdir(parents=True, exist_ok=True)
    (home_styles / "report.md").write_text(style_file.read_text())
    ids = {s.id for s in load_all_styles()}
    assert "report" in ids
    assert "structured" in get_style_suffix("report").lower()
    assert set_active_style("report")["ok"] is True
    from tera_pilot.output_styles import get_style_suffix as _suffix
    full = PromptBuilder.system(style_suffix=_suffix("report"))
    assert "Be structured." in full
    assert "Be structured." not in PromptBuilder.system()


def test_suggest_follow_ups_and_tips(home):
    from tera_pilot.suggest import follow_ups, rotating_tip, turn_footer
    from tera_pilot.agent_runtime.types import ToolCall, ToolName, TaskResult

    def _res(tools, ok=True):
        return TaskResult(success=ok, output="done", error=None if ok else "boom",
                          tool_calls=[ToolCall(name=t, args={}) for t in tools])

    assert any("test" in f for f in follow_ups(_res([ToolName.WRITE_FILE])))
    assert any("commit" in f for f in follow_ups(_res([ToolName.WRITE_FILE, ToolName.EXECUTE_COMMAND])))
    assert any("error" in f for f in follow_ups(_res([ToolName.READ_FILE], ok=False)))
    assert follow_ups(_res([])) == []
    first = rotating_tip()
    seen = {first}
    for _ in range(11):
        seen.add(rotating_tip())
    assert len(seen) == 12  # full rotation, no repeats
    assert turn_footer(_res([]), 1) == ""
    assert "Tip" in turn_footer(_res([ToolName.WRITE_FILE]), 5) or "→" in turn_footer(_res([ToolName.WRITE_FILE]), 5)


def test_resume_export_import(home, tmp_path):
    import json as _json
    from tera_pilot_tui.bridge import TeraPilotBridge
    chats = Path(_os_home_chats())
    chats.mkdir(parents=True, exist_ok=True)
    doc = {"id": "abc123", "title": "Old chat",
           "messages": [{"role": "user", "content": "hi"},
                        {"role": "assistant", "content": "hello"}]}
    (chats / "abc123.json").write_text(_json.dumps(doc))
    bridge = TeraPilotBridge(workspace=str(tmp_path))
    res = bridge.resume_chat("abc123")
    assert res["ok"] is True and len(res["messages"]) == 2
    assert bridge.resume_chat("missing")["ok"] is False
    exp = bridge.export_chat_file("abc123", str(tmp_path / "out.json"))
    assert exp["ok"] is True
    (chats / "abc123.json").unlink()
    imp = bridge.import_chat_file(str(tmp_path / "out.json"))
    assert imp["ok"] is True and imp["messages"] == 2
    assert bridge.import_chat_file(str(tmp_path / "out.json"))["ok"] is True  # id clash → new id
    assert bridge.import_chat_file(str(tmp_path / "nope.json"))["ok"] is False


def _os_home_chats():
    import os as _os
    return str(Path(_os.path.expanduser("~/.tera_pilot")) / "chats")


def test_sdk_run_with_fake(monkeypatch, tmp_path):
    import sys as _sys
    _sys.path.insert(0, "tests")
    from tera_pilot.sdk import TeraPilot, run as sdk_run
    from tera_pilot.providers import get_registry
    from tera_pilot.providers.base import ProviderConfig
    from fake_provider import FakeProvider
    reg = get_registry()
    try:
        reg.register(FakeProvider)
    except Exception:
        pass
    (tmp_path / "a.txt").write_text("hello")
    script = ["plan: read the file",
              '{"tool": "read_file", "args": {"path": "a.txt"}}',
              '{"final_answer": "file says hello"}']
    fp = FakeProvider(ProviderConfig(provider_id="fake", model="fake-1"), script=list(script))
    reg._instances["fake"] = fp
    reg.set_active("fake")
    with TeraPilot(workspace=str(tmp_path), registry=reg) as pilot:
        result = pilot.run("read a.txt")
    assert result.success is True
    assert "hello" in result.output
    assert any(c["tool"] == "read_file" for c in result.tool_calls)
    fp2 = FakeProvider(ProviderConfig(provider_id="fake", model="fake-1"),
                       script=["plan: say hi", '{"final_answer": "hi there"}'])
    reg._instances["fake"] = fp2
    one_shot = sdk_run("say hi", workspace=str(tmp_path), registry=reg)
    assert one_shot.success is True and "hi there" in one_shot.output
    with __import__("pytest").raises(ValueError):
        TeraPilot(workspace=str(tmp_path), autonomy="sometimes")
    with __import__("pytest").raises(ValueError):
        TeraPilot(workspace=str(tmp_path), registry=reg).run("  ")


def test_ide_bridge_protocol(home):
    import threading
    import time
    from tera_pilot.ide_bridge import IDEBridgeServer, IDEBridgeClient
    server = IDEBridgeServer()
    info = server.start()
    try:
        client = IDEBridgeClient(port=info["port"], token=server.token)
        assert client.call("ping")["server"] == "tera-pilot-ide-bridge"
        try:
            IDEBridgeClient(port=info["port"], token="wrong").call("ping")
            raise SystemExit("auth should have failed")
        except RuntimeError as exc:
            assert "unauthorized" in str(exc)
        client.call("provide_context", {"selection": {"path": "a.py", "text": "x"},
                                        "open_files": ["a.py"]})
        ctx = client.call("get_context")
        assert ctx["selection"]["path"] == "a.py"
        seen = {}
        thread = threading.Thread(
            target=lambda: seen.update(v=server.request_approval("rm", "x", timeout=10)))
        thread.start()
        time.sleep(0.3)
        events = client.call("poll_events", {"timeout": 5})["events"]
        assert events and events[0]["event"] == "request_approval"
        approval_id = events[0]["payload"]["id"]
        assert client.call("resolve_approval", {"id": approval_id, "decision": True}) == {"ok": True}
        thread.join(timeout=10)
        assert seen.get("v") is True
        client.close()
    finally:
        server.stop()
