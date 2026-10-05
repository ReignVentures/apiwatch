import json
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

import yaml

from apiwatch import site
from apiwatch.cli import main

ROOT = Path(__file__).parent.parent
RECORDS = ROOT / "feed" / "records"


REVIEWED = {"verified": True, "verified_by": "claude", "verified_on": "2026-09-24",
            "source_url": "https://v.example/changelog", "evidence": ["2026-10-01"]}


def rec(rid, vendor="Acme", summary="x", effective="2026-10-01", **kw):
    return {"id": rid, "vendor": vendor, "surface": "s", "kind": "rest", "severity": "breaking",
            "effective": effective, "summary": summary, **kw}


def fixture(tmp_path) -> Path:
    """A reviewed record due soon, a reviewed one in effect, a person-verified one, a draft, and an illustrative one."""
    d = tmp_path / "recs"
    d.mkdir()
    rows = [
        rec("acme-2026-10-01-soon", summary="Acme retires widget-1", signatures=["widget-1"], exclude=["widget-1.5"],
            context=[["acme", "billing"], "acme_sdk"], files=["*.py"], **REVIEWED),
        rec("acme-2026-08-01-past", summary="Acme dropped v1 paging", effective="2026-08-01", **REVIEWED),
        rec("beta-2026-07-01-human", vendor="Beta MCP", summary="Beta renamed a tool", effective="2026-07-01",
            **{**REVIEWED, "verified_by": "Alex Morgan"}),
        rec("acme-2026-11-01-draft", summary="Acme draft change", effective="2026-11-01"),
        rec("example-2026-09-01-demo", summary="Made-up demo", illustrative=True, **REVIEWED),
    ]
    for r in rows:
        (d / f"{r['id']}.yml").write_text(yaml.safe_dump(r))
    return d


def build(tmp_path, records=None, **kw):
    records = records or fixture(tmp_path)
    return site.build(records, tmp_path / "site", today=date(2026, 9, 23), **kw)


def test_only_reviewed_records_are_published(tmp_path):
    n, index = build(tmp_path)
    out = tmp_path / "site"
    assert n == 3
    assert {p.stem for p in (out / "records").glob("*.html")} == {
        "acme-2026-10-01-soon", "acme-2026-08-01-past", "beta-2026-07-01-human"}
    html = index.read_text()
    assert "Acme draft change" not in html and "Made-up demo" not in html
    assert (out / "vendors" / "beta-mcp.html").exists() and (out / "style.css").exists()


def test_review_labels(tmp_path):
    build(tmp_path)
    ai = (tmp_path / "site" / "records" / "acme-2026-10-01-soon.html").read_text()
    assert "Reviewed against the vendor source on Sep 24, 2026 (AI-assisted review)" in ai
    assert ">Verified<" not in ai and "Unverified" not in ai and "awaiting" not in ai
    assert "Not affected (matches inside these are ignored): <code>widget-1.5</code>" in ai
    assert ("Counts only in files matching <code>*.py</code>; files that mention <code>acme</code> + "
            "<code>billing</code> or <code>acme_sdk</code>.") in ai
    human = (tmp_path / "site" / "records" / "beta-2026-07-01-human.html").read_text()
    assert ">Verified<" in human and "by Alex Morgan on Sep 24, 2026" in human


def test_records_failing_the_source_check_are_left_out(tmp_path):
    n, _ = build(tmp_path, exclude={"acme-2026-08-01-past"})
    assert n == 2 and not (tmp_path / "site" / "records" / "acme-2026-08-01-past.html").exists()


def test_upcoming_and_in_effect_are_split_by_build_date(tmp_path):
    _, index = build(tmp_path)
    upcoming, past = index.read_text().split("<h2>In effect</h2>")
    assert "Acme retires widget-1" in upcoming.split("<h2>Coming up</h2>")[1]
    assert "Acme dropped v1 paging" in past


def test_feeds_are_valid(tmp_path):
    build(tmp_path, base_url="https://feed.example.com")
    out = tmp_path / "site"
    atom = ET.parse(out / "feed.xml").getroot()
    ns = {"a": "http://www.w3.org/2005/Atom"}
    titles = [e.find("a:title", ns).text for e in atom.findall("a:entry", ns)]
    assert "Acme: Acme retires widget-1" in titles and not any(t.startswith("[") for t in titles)
    jf = json.loads((out / "feed.json").read_text())
    assert jf["version"] == "https://jsonfeed.org/version/1.1" and len(jf["items"]) == 3
    item = next(i for i in jf["items"] if i["id"] == "urn:apiwatch:acme-2026-10-01-soon")
    assert item["url"] == "https://feed.example.com/records/acme-2026-10-01-soon"
    assert item["_apiwatch"]["signatures"] == ["widget-1"] and item["_apiwatch"]["review"] == "ai"


def test_record_text_is_html_escaped(tmp_path):
    recs = tmp_path / "recs"
    recs.mkdir()
    (recs / "x.yml").write_text(yaml.safe_dump(rec(
        "x-2026-01-01-evil", vendor="Evil <Co>", surface="<script>", summary="<img src=x onerror=alert(1)>",
        detail="a & b", signatures=["</code>"], **{**REVIEWED, "source_url": "javascript:alert(1)\" x=\""})))
    build(tmp_path, records=recs)
    page = (tmp_path / "site" / "records" / "x-2026-01-01-evil.html").read_text()
    assert "<img src=x" not in page and "<script>" not in page and "&lt;img" in page
    assert 'href="javascript' not in page
    ET.parse(tmp_path / "site" / "feed.xml")


def test_cli_site_with_check_results(tmp_path):
    recs = fixture(tmp_path)
    results = tmp_path / "check.json"
    results.write_text(json.dumps([{"id": "acme-2026-08-01-past", "status": "missing"},
                                   {"id": "acme-2026-10-01-soon", "status": "error"}]))
    assert main(["site", "--records", str(recs), "--out", str(tmp_path / "o"), "--check-results", str(results)]) == 0
    got = {p.stem for p in (tmp_path / "o" / "records").glob("*.html")}
    assert got == {"acme-2026-10-01-soon", "beta-2026-07-01-human"}   # fetch errors don't pull a record


def test_real_records_build(tmp_path):
    n, _ = site.build(RECORDS, tmp_path, today=date(2026, 9, 24))
    from apiwatch.models import load_records
    assert n == sum(r.published for r in load_records(RECORDS))


def test_pages_work_under_a_strict_csp(tmp_path):
    import re
    build(tmp_path)
    out = tmp_path / "site"
    headers = (out / "_headers").read_text()
    assert "script-src 'self'" in headers and "default-src 'none'" in headers
    assert (out / "site.js").exists()
    for page in out.rglob("*.html"):
        html = page.read_text()
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), page      # no inline scripts
        assert " style=" not in html and " on" + "click=" not in html, page    # no inline styles/handlers


def test_404_page_uses_root_absolute_links(tmp_path):
    build(tmp_path)
    html = (tmp_path / "site" / "404.html").read_text()
    assert 'href="/style.css"' in html and 'href="/index.html"' in html and "../" not in html


def test_canonical_urls_sitemap_and_robots(tmp_path):
    build(tmp_path, base_url="https://feed.example.com")
    out = tmp_path / "site"
    page = (out / "records" / "acme-2026-10-01-soon.html").read_text()
    assert '<link rel="canonical" href="https://feed.example.com/records/acme-2026-10-01-soon">' in page
    assert '<link rel="canonical" href="https://feed.example.com/">' in (out / "index.html").read_text()
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locs = [e.text for e in ET.parse(out / "sitemap.xml").getroot().findall("s:url/s:loc", ns)]
    assert "https://feed.example.com/records/acme-2026-10-01-soon" in locs and "https://feed.example.com/vendors/beta-mcp" in locs
    assert not any("draft" in u or "demo" in u for u in locs) and len(locs) == 1 + 2 + 3
    assert "Sitemap: https://feed.example.com/sitemap.xml" in (out / "robots.txt").read_text()


def test_no_canonical_or_sitemap_without_a_base_url(tmp_path):
    build(tmp_path)
    out = tmp_path / "site"
    assert "canonical" not in (out / "index.html").read_text() and not (out / "sitemap.xml").exists()
    assert (out / "robots.txt").read_text() == "User-agent: *\nAllow: /\n"
