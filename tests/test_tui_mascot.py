"""Mascot animation + dead-model-hint removal tests (v2.5.0)."""

import asyncio


def _run(coro):
    return asyncio.run(coro)


def test_mascot_frames_differ():
    from tera_pilot_tui.widgets.mascot import render_mascot, BLINK_CYCLE
    open_t = render_mascot(dark=True, frame=0).plain
    blink_t = render_mascot(dark=True, frame=1).plain
    assert open_t != blink_t
    assert len(open_t.splitlines()) == 9
    assert set(BLINK_CYCLE) <= {0, 1}


def test_mascot_animates_in_place():
    from tera_pilot_tui.app import TeraPilotTUIApp
    from tera_pilot_tui.widgets.chat_log import ChatLog

    async def _go():
        app = TeraPilotTUIApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            chat = app.query_one(ChatLog)
            before = len(chat.lines)
            app._exec_mascot("")
            await pilot.pause(1.6)  # blink cycle + quip
            growth = len(chat.lines) - before
            # One mascot block (~9 rows) + quip — not N stacked mascots.
            assert 9 <= growth <= 14, f"unexpected growth: {growth}"
            assert app._exception is None

    _run(_go())


def test_replace_tail_aborts_on_new_content():
    from tera_pilot_tui.app import TeraPilotTUIApp
    from tera_pilot_tui.widgets.chat_log import ChatLog
    from rich.text import Text

    async def _go():
        app = TeraPilotTUIApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            chat = app.query_one(ChatLog)
            base = chat.tail_mark()
            chat.write(Text("mascot-frame-0"))
            chat._tail_expected = len(chat.lines)
            # New content lands mid-animation…
            chat.write(Text("user message arrived"))
            # …so the rewrite must refuse instead of eating it.
            assert chat.replace_tail(base, Text("mascot-frame-1")) is False
            plains = [str(line.text) for line in chat.lines[-3:]]
            assert any("user message arrived" in p for p in plains)
            assert app._exception is None

    _run(_go())


def test_replace_tail_redraws_when_clean():
    from tera_pilot_tui.app import TeraPilotTUIApp
    from tera_pilot_tui.widgets.chat_log import ChatLog
    from rich.text import Text

    async def _go():
        app = TeraPilotTUIApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            chat = app.query_one(ChatLog)
            base = chat.tail_mark()
            chat.write(Text("frame-0"))
            chat._tail_expected = len(chat.lines)
            assert chat.replace_tail(base, Text("frame-1")) is True
            assert "frame-1" in str(chat.lines[-1].text)
            assert not any("frame-0" in str(line.text)
                           for line in chat.lines[-2:])
            assert app._exception is None

    _run(_go())


def test_no_ox_alpha_in_product_sources():
    import re
    from pathlib import Path
    root = Path(".")
    offenders = []
    for path in list(root.rglob("*.py")) + list(root.rglob("*.tcss")):
        if ".git" in path.parts or "eval" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if re.search("ox" + "-alpha", text):
            offenders.append(str(path))
    assert offenders == [], f"dead model hint still referenced: {offenders}"
