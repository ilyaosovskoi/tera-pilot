"""Regression tests for learning_loop template formatting (v2.3.4-fix).

Previously ``create_learning_entry`` called ``template.format(**kwargs)``
outside any try/except. A project's custom ``Learnings.md`` template with
an unknown placeholder (or a literal brace) raised ``KeyError``/``ValueError``
and crashed the whole ``/learnings scan`` (and the auto learning loop that
calls it). It must degrade to the built-in fallback template instead.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tera_pilot.learning_loop import create_learning_entry  # noqa: E402

BASE_KWARGS = {
    "project_path": None,  # set per-test
    "title": "Test entry",
    "source": "test",
    "severity": "medium",
    "context": "ctx",
    "what_happened": "what",
    "root_cause": "why",
    "evidence": "proof",
    "do_rule": "do it",
    "dont_rule": "don't",
    "how_to_apply": "apply",
}


def _write_learnings_md(tmp: Path, body: str) -> None:
    (tmp / "Learnings.md").write_text(body, encoding="utf-8")


@pytest.mark.parametrize(
    "template_body",
    [
        # Unknown placeholder the entry formatter does not provide
        "## Entry Template\n\n```\n## {title}\n- Bogus: {no_such_placeholder}\n```\n",
        # Literal JSON braces inside the template
        '## Entry Template\n\n```\n## {title}\n- Config: {"a": 1}\n```\n',
        # Placeholder with a format spec we don't support
        "## Entry Template\n\n```\n## {title}\n- Weird: {date:%Y}\n```\n",
    ],
)
def test_broken_template_falls_back_without_crash(tmp_path: Path, template_body: str):
    _write_learnings_md(tmp_path, template_body)
    kwargs = dict(BASE_KWARGS, project_path=str(tmp_path))
    res = create_learning_entry(**kwargs)
    assert res["ok"] is True, f"expected graceful fallback, got: {res}"
    body = Path(res["path"]).read_text(encoding="utf-8")
    # Fallback template output should still be a valid entry
    assert "id:" in body
    assert "# Title: Test entry" in body


def test_valid_template_is_used_verbatim(tmp_path: Path):
    _write_learnings_md(
        tmp_path,
        "## Entry Template\n\n```\n# {title} [id:{id}]\n- {date} @ {tags}\n```\n",
    )
    kwargs = dict(BASE_KWARGS, project_path=str(tmp_path))
    res = create_learning_entry(**kwargs)
    assert res["ok"] is True, res
    body = Path(res["path"]).read_text(encoding="utf-8")
    assert body.startswith("# Test entry [id:")
    assert "@" in body


def test_no_learnings_md_uses_fallback(tmp_path: Path):
    kwargs = dict(BASE_KWARGS, project_path=str(tmp_path))
    res = create_learning_entry(**kwargs)
    assert res["ok"] is True, res
    body = Path(res["path"]).read_text(encoding="utf-8")
    assert "id:" in body
