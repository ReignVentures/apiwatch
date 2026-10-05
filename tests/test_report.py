import json
from pathlib import Path

from apiwatch.cli import main

ROOT = Path(__file__).parent.parent


def test_scan_writes_sarif_and_json(tmp_path):
    rc = main(["scan", str(ROOT / "samples/repo"), "--records", str(ROOT / "feed/records"),
               "--out", str(tmp_path / "briefs"), "--sarif", str(tmp_path / "r.sarif"),
               "--json", str(tmp_path / "r.json"), "--fail-on-breaking"])
    assert rc == 1
    doc = json.loads((tmp_path / "r.sarif").read_text())
    assert doc["version"] == "2.1.0"
    run = doc["runs"][0]
    rule_ids = {r["id"] for r in run["tool"]["driver"]["rules"]}
    assert "anthropic-2026-06-15-claude-4-retirement" in rule_ids
    results = run["results"]
    assert all(res["ruleId"] in rule_ids for res in results)
    agent = [r for r in results if r["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] == "src/agent.py"]
    assert {r["locations"][0]["physicalLocation"]["region"]["startLine"] for r in agent} >= {3, 9}
    assert {r["level"] for r in results} <= {"error", "warning", "note"}
    stripe = next(r for r in run["tool"]["driver"]["rules"] if r["id"].startswith("stripe"))
    assert "(reviewed against source 2026-09-24 (AI-assisted))" in stripe["help"]["text"]
    js = json.loads((tmp_path / "r.json").read_text())
    assert {r["id"] for r in js["records"]} == rule_ids


def test_markdown_report_orders_by_severity_and_links(tmp_path):
    md = tmp_path / "s.md"
    rc = main(["scan", str(ROOT / "samples/repo"), "--records", str(ROOT / "feed/records"), "--out", str(tmp_path / "b"),
               "--markdown", str(md), "--link-base", "https://github.com/o/r/blob/abc/"])
    assert rc == 0
    text = md.read_text()
    assert text.startswith("<!-- apiwatch -->\n<!-- apiwatch-hits: 3 -->\n"
                           "### apiwatch: 2 known changes touch this code (2 breaking or silent in non-test code)")
    assert text.index("🔴 breaking") < text.index("🟠 silent")
    assert '<a href="https://github.com/o/r/blob/abc/src/agent.py#L3"><code>src/agent.py:3</code></a>' in text
    assert "(in effect)" in text
    assert "ExampleDocs" not in text                       # made-up demo records are skipped by default
    from apiwatch.models import load_records
    n = sum(not r.illustrative for r in load_records(ROOT / "feed/records"))
    assert f"{n} change records checked, {n} of them reviewed" in text


def test_markdown_report_when_nothing_matches(tmp_path):
    from apiwatch import report
    assert "no known API or model change touches this code" in report.markdown([], 12)


def test_only_files_limits_hits_to_a_prs_changes(tmp_path):
    only = tmp_path / "changed.txt"
    only.write_text("./src/billing.py\nREADME.md\n.github/x.py\n")
    js = tmp_path / "r.json"
    main(["scan", str(ROOT / "samples/repo"), "--records", str(ROOT / "feed/records"), "--out", str(tmp_path / "b"),
          "--json", str(js), "--only-files", str(only)])
    files = {s["file"] for r in json.loads(js.read_text())["records"] for s in r["sites"]}
    assert files == {"src/billing.py"}



def _site(file, line=1, tier="use"):
    from apiwatch.models import CallSite
    return CallSite(file, line, "gpt-4", "x", tier=tier)


def _rec(**kw):
    from apiwatch.models import ChangeRecord
    base = dict(id="r", vendor="V", surface="s", kind="model", severity="breaking", effective="2026-01-01", summary="S",
                source_url="https://v.example")
    return ChangeRecord(**(base | kw))


def test_markdown_escapes_untrusted_paths():
    from apiwatch import report
    text = report.markdown([(_rec(), [_site("my dir/a (1)#b?.py"), _site("x`y\n![img](http://evil)<b>.py")])], 1,
                           link_base="https://github.com/o/r/blob/abc/")
    assert 'href="https://github.com/o/r/blob/abc/my%20dir/a%20%281%29%23b%3F.py#L1"' in text
    assert "<b>" not in text and "&lt;b&gt;" in text               # HTML in a file name is escaped
    assert "\n![img]" not in text                                # a newline in a file name can't start new markdown


def test_markdown_stays_under_githubs_comment_limit():
    from apiwatch import report
    results = [(_rec(id=f"r{i}", summary="S" * 150), [_site(f"services/very/long/path/number/{i}/{j}/module.py", j)
                                                       for j in range(40)]) for i in range(90)]
    text = report.markdown(results, 90, link_base="https://github.com/org/repo/blob/" + "a" * 40 + "/")
    assert len(text) <= report.MAX_CHARS and text.count("| 🔴 breaking") == 90


def test_markdown_orders_code_hits_before_test_only_changes():
    from apiwatch import report
    from apiwatch.models import CallSite
    test_only = (_rec(id="a", summary="test only", effective="2025-01-01"), [CallSite("tests/test_x.py", 1, "s", "x")])
    in_code = (_rec(id="b", summary="in code", effective="2026-01-01"), [_site("src/a.py")])
    text = report.markdown([test_only, in_code], 2)
    assert text.index("in code") < text.index("test only") and "(1 breaking or silent in non-test code)" in text


def test_sarif_keeps_hits_outside_only_files(tmp_path):
    only = tmp_path / "changed.txt"
    only.write_text("src/billing.py\n")
    rc = main(["scan", str(ROOT / "samples/repo"), "--records", str(ROOT / "feed/records"), "--out", str(tmp_path / "b"),
               "--sarif", str(tmp_path / "r.sarif"), "--only-files", str(only), "--fail-on-breaking"])
    uris = {r["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
            for r in json.loads((tmp_path / "r.sarif").read_text())["runs"][0]["results"]}
    assert uris == {"src/agent.py", "src/billing.py"}          # SARIF complete; the gate only sees the PR's files
    assert rc == 1                                            # billing.py's silent Stripe change
