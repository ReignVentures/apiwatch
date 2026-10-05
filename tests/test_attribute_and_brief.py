from pathlib import Path

from apiwatch import attribute, brief
from apiwatch.models import load_records

ROOT = Path(__file__).parent.parent
REPO = ROOT / "samples" / "repo"


def recs_by_id(recs):
    return {r.id: r for r in recs}


def test_feed_records_load_and_validate():
    recs = load_records(ROOT / "feed" / "records")
    assert len(recs) >= 7
    # feed policy: a record is only verified with a primary source linked
    assert all(r.source_url and r.verified_by and r.verified_on for r in recs if r.verified)
    assert not any(r.human_verified for r in recs)  # nothing human-verified yet; flip this when a person reviews one
    assert not recs_by_id(recs)["example-mcp-2026-09-01-tool-rename"].verified  # illustrative, never verified


def test_attribution_finds_the_three_planted_hits():
    recs = {r.id: r for r in load_records(ROOT / "feed" / "records")}
    hits = {rid: attribute.attribute_record(REPO, r) for rid, r in recs.items()}
    assert any(s.file.endswith("agent.py") for s in hits["anthropic-2026-06-15-claude-4-retirement"])
    assert any(s.file.endswith("agent.py") for s in hits["example-mcp-2026-09-01-tool-rename"])
    assert any(s.file.endswith("billing.py") for s in hits["stripe-2026-03-26-sdk-decimal-type"])
    assert hits["greenhouse-2026-08-31-harvest-v1v2-retired"] == []  # not used by the sample repo


def test_brief_contains_sites_and_acceptance():
    recs = {r.id: r for r in load_records(ROOT / "feed" / "records")}
    r = recs["stripe-2026-03-26-sdk-decimal-type"]
    sites = attribute.attribute_record(REPO, r)
    b = brief.brief_for_record(r, sites)
    assert "billing.py" in b and "## Acceptance" in b and "reviewed against source 2026-09-24 (AI-assisted)" in b


def test_brief_flags_unverified_records():
    recs = {r.id: r for r in load_records(ROOT / "feed" / "records")}
    r = recs["example-mcp-2026-09-01-tool-rename"]
    b = brief.brief_for_record(r, attribute.attribute_record(REPO, r))
    assert "confirm before acting" in b


def test_draft_patch_is_none_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert brief.draft_patch("x", "y") is None


def test_verified_without_who_or_when_is_rejected():
    import pytest
    from apiwatch.models import ChangeRecord
    base = dict(id="x", vendor="v", surface="s", kind="rest", severity="breaking", effective="2026-01-01",
                summary="x", source_url="https://v.example")
    with pytest.raises(ValueError, match="verified_by"):
        ChangeRecord(**base, verified=True)
    with pytest.raises(ValueError, match="evidence"):
        ChangeRecord(**base, verified=True, verified_by="claude", verified_on="2026-09-23")
    ok = dict(base, verified=True, verified_on="2026-09-24", evidence=["2026-01-01"])
    assert ChangeRecord(**ok, verified_by="claude").human_verified is False
    assert ChangeRecord(**ok, verified_by="Alex Morgan").human_verified is True
    with pytest.raises(ValueError, match="evidence items"):
        ChangeRecord(**dict(ok, evidence=[{"url": "https://x"}]), verified_by="claude")


class _FakeStream:
    def __init__(self, msg, seen, kwargs):
        self.msg, self.seen, self.kwargs = msg, seen, kwargs

    def __enter__(self):
        self.seen.append(self.kwargs)
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.msg


def _fake_client(stop_reason, text="--- a/x\n+++ b/x\n"):
    from types import SimpleNamespace as NS
    seen = []
    msg = NS(stop_reason=stop_reason, content=[NS(type="thinking", thinking=""), NS(type="text", text=text)])
    client = NS(beta=NS(messages=NS(stream=lambda **kw: _FakeStream(msg, seen, kw))))
    return client, seen


def test_draft_patch_request_shape_and_model_override(monkeypatch):
    monkeypatch.delenv("APIWATCH_PATCH_MODEL", raising=False)
    client, seen = _fake_client("end_turn")
    assert brief.draft_patch("b", "f", client=client).startswith("--- a/x")
    req = seen[0]
    assert req["model"] == "claude-opus-5" and req["thinking"] == {"type": "adaptive"}
    assert req["fallbacks"] == "default" and req["betas"] == ["server-side-fallback-2026-07-01"]
    assert req["max_tokens"] >= 32000
    monkeypatch.setenv("APIWATCH_PATCH_MODEL", "claude-sonnet-5")
    client, seen = _fake_client("end_turn")
    brief.draft_patch("b", "f", client=client)
    assert seen[0]["model"] == "claude-sonnet-5" and "fallbacks" not in seen[0]


def test_draft_patch_never_returns_a_partial_diff():
    for reason in ("refusal", "max_tokens"):
        client, _ = _fake_client(reason)
        assert brief.draft_patch("b", "f", client=client) is None
