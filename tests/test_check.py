import json
from pathlib import Path

import yaml

import urllib.error

import pytest

from apiwatch import check
from apiwatch.cli import main
from apiwatch.models import ChangeRecord

PAGE = """<html><body><h1>Deprecations</h1><table><tr><td><code>widget&#8209;1</code></td>
<td>Oct&nbsp;1,   2026</td></tr></table><p>Use **widget-2** instead.</p></body></html>"""
MD = "| `gadget-7` | 2026‑12‑01 |\n"


def rec(**kw):
    base = dict(id="r", vendor="V", surface="s", kind="model", severity="breaking", effective="2026-10-01",
                summary="x", source_url="https://v.example/deprecations", verified=True, verified_by="claude",
                verified_on="2026-09-24")
    return ChangeRecord(**{**base, **kw})


def fake(pages):
    """pages: url -> body, or an exception to raise. Unknown URLs are 404s."""
    def get(url):
        v = pages.get(url, check.PageGone("HTTP 404"))
        if isinstance(v, BaseException):
            raise v
        return v
    return get


NOSLEEP = lambda s: None   # noqa: E731


def test_normalize_handles_markup_entities_dashes_and_spacing():
    t = check.normalize(PAGE)
    assert "widget-1" in t and "oct 1, 2026" in t and "use widget-2 instead" in t


def test_normalize_keeps_text_held_in_attributes():
    t = check.normalize('<Update label="September 2, 2026" tags={["MCP"]}>\n## Search split\n</Update><img alt="diagram">')
    assert "september 2, 2026" in t and "search split" in t and "diagram" in t and "<" not in t
    assert "renamed to listx" in check.normalize("renamed to <code><style>.c{color:red}</style>listX</code>")
    assert "has been shut down on" in check.normalize("has been [shut down](/docs/deprecations#x) on")


def test_evidence_found_missing_and_other_page():
    pages = {"https://v.example/deprecations": PAGE, "https://v.example/notes": "Released <b>v2.0.0</b>"}
    r = check.check_record(rec(evidence=["widget-1", "Oct 1, 2026", {"url": "https://v.example/notes", "text": "v2.0.0"}]),
                           fake(pages))
    assert r.status == "ok"
    r = check.check_record(rec(evidence=["widget-1", "widget-9"]), fake(pages))
    assert r.status == "missing" and r.missing == ["widget-9"]


def test_fetch_errors_are_reported_not_missing():
    for exc in (TimeoutError("timed out"), __import__("http.client").client.IncompleteRead(b""), ValueError("unknown url type")):
        r = check.check_record(rec(evidence=["widget-1"]), fake({"https://v.example/deprecations": exc}), sleep=NOSLEEP)
        assert r.status == "error" and r.errors and not r.missing, exc


def test_a_deleted_source_page_is_missing():
    r = check.check_record(rec(evidence=["widget-1"]), fake({}), sleep=NOSLEEP)
    assert r.status == "missing" and "source page gone" in r.missing[0]


def test_twin_absent_is_fine_but_twin_failing_is_an_error():
    url = "https://platform.claude.com/docs/x"
    ok_html = {url: "<p>gadget-7 on 2026-12-01</p>"}
    assert check.check_record(rec(source_url=url, evidence=["gadget-7"]), fake(ok_html), sleep=NOSLEEP).status == "ok"
    assert check.check_record(rec(source_url=url, evidence=["gadget-9"]), fake(ok_html), sleep=NOSLEEP).status == "missing"
    flaky = {**ok_html, url + ".md": TimeoutError("timed out")}
    assert check.check_record(rec(source_url=url, evidence=["gadget-9"]), fake(flaky), sleep=NOSLEEP).status == "error"


def test_retries_before_giving_up():
    calls = []
    def get(u):
        calls.append(u)
        if len(calls) < 3:
            raise TimeoutError("slow")
        return PAGE
    assert check.check_record(rec(evidence=["widget-1"]), get, sleep=NOSLEEP).status == "ok" and len(calls) == 3


def test_inline_markup_and_invisible_characters_dont_split_ids():
    t = check.normalize("<code>gpt</code>-4 · claude-<wbr>opus · <span>2026</span><span>-10-23</span> · o\u200b1-pro · x\\-y")
    assert "gpt-4" in t and "claude-opus" in t and "2026-10-23" in t and "o1-pro" in t and "x-y" in t


def test_normalize_is_fast_on_hostile_input():
    import time
    t0 = time.time()
    check.normalize("[a " * 20000 + "<b " * 20000)
    assert time.time() - t0 < 1


@pytest.mark.parametrize("evidence", ["gpt-4-0613", ["**"], [" `` "], [{"url": "v.example/x", "text": "abcd"}], [{"url": "https://x", "text": 5}], [7]])
def test_bad_evidence_is_rejected(evidence):
    with pytest.raises(ValueError, match="evidence"):
        rec(evidence=evidence)


def test_only_claude_names_count_as_ai_review():
    assert rec(evidence=["abcd"], verified_by="claude-opus-5-5").human_verified is False
    assert rec(evidence=["abcd"], verified_by="Alex Morgan").human_verified is True


def test_docs_sites_use_the_markdown_twin():
    url = "https://platform.claude.com/docs/en/about-claude/model-deprecations"
    r = check.check_record(rec(source_url=url, evidence=["gadget-7", "2026-12-01"]), fake({url + ".md": MD}))
    assert r.status == "ok"
    assert check._candidates("https://ai.google.dev/gemini-api/docs/deprecations")[0].endswith(".md.txt")
    assert check._candidates("https://github.com/o/r/releases/tag/v1") == ["https://github.com/o/r/releases/tag/v1"]


def test_unreviewed_record_without_evidence():
    assert check.check_record(rec(verified=False), fake({}), sleep=NOSLEEP).status == "no_evidence"


def test_cli_check_writes_json_and_fails_on_missing(tmp_path, monkeypatch):
    d = tmp_path / "recs"
    d.mkdir()
    for rid, ev in [("a", ["widget-1"]), ("b", ["nope-3"])]:
        r = dict(id=rid, vendor="V", surface="s", kind="model", severity="breaking", effective="2026-10-01",
                 summary="x", source_url="https://v.example/deprecations", verified=True, verified_by="claude",
                 verified_on="2026-09-24", evidence=ev)
        (d / f"{rid}.yml").write_text(yaml.safe_dump(r))
    monkeypatch.setattr(check, "fetch", fake({"https://v.example/deprecations": PAGE}))
    out = tmp_path / "check.json"
    assert main(["check", "--records", str(d), "--json", str(out)]) == 1
    assert {r["id"]: r["status"] for r in json.loads(out.read_text())} == {"a": "ok", "b": "missing"}
    assert check.failed_ids(out) == {"b"}
    assert main(["check", "--records", str(d), "--id", "a"]) == 0


def test_site_can_require_check_results(tmp_path):
    d = tmp_path / "recs"
    d.mkdir()
    assert main(["site", "--records", str(d), "--out", str(tmp_path / "o"), "--check-results", str(tmp_path / "nope.json"),
                 "--require-check-results"]) == 2


def test_github_files_use_the_raw_twin():
    assert check._candidates("https://github.com/o/r/blob/main/docs/MIGRATION.md#removed") == [
        "https://raw.githubusercontent.com/o/r/main/docs/MIGRATION.md", "https://github.com/o/r/blob/main/docs/MIGRATION.md#removed"]
    url = "https://github.com/o/r/blob/main/MIGRATION.md"
    r = check.check_record(rec(source_url=url, evidence=["Removed: widget"]),
                           fake({url: TimeoutError("503"), "https://raw.githubusercontent.com/o/r/main/MIGRATION.md": "## Removed: widget"}),
                           sleep=NOSLEEP)
    assert r.status == "ok"
