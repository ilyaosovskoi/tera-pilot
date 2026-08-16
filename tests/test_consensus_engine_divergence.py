"""Regression tests for consensus_engine divergence analysis.

Covers the fixed bug where file-set divergence was NOT reported when one
provider's file set was a strict superset of another's (e.g. {main.py}
vs {main.py, utils.py}) — the old `if others:` guard skipped exactly the
provider with the largest file set.
"""

from tera_pilot.consensus_engine import ProviderResponse, _analyze_divergences


def _resp(pid, files):
    return ProviderResponse(
        provider_id=pid,
        model="m",
        text="x",
        elapsed_ms=1.0,
        files_touched=tuple(files),
        code_blocks=0,
        code_chars=0,
        text_chars=1,
    )


def test_superset_file_divergence_detected():
    """A={main.py}, B={main.py, utils.py} must produce a files_touched
    divergence — B created a file A didn't."""
    rs = [_resp("openai", ["main.py"]), _resp("anthropic", ["main.py", "utils.py"])]
    d = _analyze_divergences(rs)
    dims = [x.dimension for x in d]
    assert "files_touched" in dims, f"expected files_touched divergence, got {dims}"
    div = next(x for x in d if x.dimension == "files_touched")
    assert "anthropic" in div.description


def test_disjoint_file_divergence_detected():
    rs = [_resp("openai", ["a.py"]), _resp("anthropic", ["b.py"])]
    d = _analyze_divergences(rs)
    dims = [x.dimension for x in d]
    assert "files_touched" in dims
    div = next(x for x in d if x.dimension == "files_touched")
    assert "openai" in div.description and "anthropic" in div.description


def test_identical_file_sets_no_divergence():
    rs = [_resp("openai", ["main.py"]), _resp("anthropic", ["main.py"])]
    d = _analyze_divergences(rs)
    assert not [x for x in d if x.dimension == "files_touched"]


def test_three_way_partial_overlap():
    """Only groq touches c.py — the divergence must name groq (the
    provider with files nobody else has), not just the first provider."""
    rs = [
        _resp("openai", ["a.py", "b.py"]),
        _resp("anthropic", ["a.py"]),
        _resp("groq", ["a.py", "b.py", "c.py"]),
    ]
    d = _analyze_divergences(rs)
    dims = [x.dimension for x in d]
    assert "files_touched" in dims
    div = next(x for x in d if x.dimension == "files_touched")
    assert "groq" in div.description
    assert "c.py" in div.description


def test_every_unique_file_listed_within_cap():
    """More than 3 providers with unique files — description lists the
    first 3, but each unique file set is computed correctly."""
    rs = [
        _resp("p1", ["a.py"]),
        _resp("p2", ["b.py"]),
        _resp("p3", ["c.py"]),
        _resp("p4", ["d.py"]),
    ]
    d = _analyze_divergences(rs)
    dims = [x.dimension for x in d]
    assert "files_touched" in dims
    div = next(x for x in d if x.dimension == "files_touched")
    for pid in ("p1", "p2", "p3"):
        assert pid in div.description
