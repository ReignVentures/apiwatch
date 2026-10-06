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


def test_every_page_has_the_ci_next_step(tmp_path):
    build(tmp_path)
    out = tmp_path / "site"
    pages = [out / "index.html", out / "vendors" / "acme.html", out / "records" / "acme-2026-10-01-soon.html"]
    for page in out.rglob("*.html"):
        if page.name != "404.html":
            assert page.read_text().count('<aside class="cta"') == 1, page
    for page in pages:
        html = page.read_text()
        html = html[html.index('<aside class="cta"'):html.index("</aside>") + len("</aside>")]
        assert "<script" not in html and " style=" not in html, page
        assert "Catch this in CI" in html and "- uses: ReignVentures/apiwatch@v1</code></pre>" in html, page
        assert 'href="https://github.com/ReignVentures/apiwatch"' in html, page
        assert 'href="https://github.com/marketplace/actions/apiwatch-scan"' in html, page
        assert "opens an issue when a new change lands on your code. $19 per month" in html and 'href="mailto:hello@reignventures.co"' in html, page
        assert "—" not in html and "–" not in html, page
    assert "Catch this in CI" not in (out / "404.html").read_text()


def id_fixture(tmp_path) -> Path:
    """Model ids, a code fragment, scoped records (context, sdk with repo_context, files), a non-model change,
    spellings that slug the same, a deprecation, an id with a past and an upcoming record, and "index"."""
    d = tmp_path / "idrecs"
    d.mkdir()
    rows = [
        rec("acme-2026-12-01-model", summary="Acme retires acme-pro-2.0", effective="2026-12-01", kind="model",
            signatures=["acme-pro-2.0", "output_format={", "acme-shared"], fix_hint="Move to acme-pro-3.", **REVIEWED),
        rec("acme-2026-06-01-old", summary="Acme retired acme-lite", effective="2026-06-01", kind="model",
            signatures=["acme-lite", "acme.lite", "ACME-LITE", "acme-shared"], fix_hint="Use acme-pro-3.", **REVIEWED),
        rec("acme-2026-11-01-scoped", summary="Acme renames limit", effective="2026-11-01",
            signatures=["limit_rows"], context=["acme"], **REVIEWED),
        rec("acme-2026-09-30-sdk", summary="Acme SDK 16 renames Reversal", effective="2026-09-30", kind="sdk",
            signatures=["acme.Reversal"], repo_context=["dep:pypi:acme@16."], **REVIEWED),
        rec("acme-2026-08-30-files", summary="Acme Go client drops Foo", effective="2026-08-30",
            signatures=["FooClient"], files=["*.go"], **REVIEWED),
        rec("beta-2026-10-15-silent", vendor="Beta", summary="Beta serves beta-fast-1 from a new model", effective="2026-10-15",
            kind="model", severity="silent", signatures=["beta-fast-1", "Index"], fix_hint="Re-check output.", **REVIEWED),
        rec("beta-2026-10-20-rest", vendor="Beta", summary="Beta preview API drops /v2/search", effective="2026-10-20",
            signatures=["/v2/search"], **REVIEWED),
        rec("gamma-2026-09-10-dep", vendor="Gamma", summary="Gamma deprecated gamma-old-1", effective="2026-09-10",
            kind="model", severity="deprecation", signatures=["gamma-old-1"],
            **{**REVIEWED, "verified_on": "2026-09-01"}),
        rec("delta-2026-09-15-info", vendor="Delta", summary="Delta notes a change", effective="2026-09-15",
            severity="info", **REVIEWED),
        rec("beta-2027-04-01-dep", vendor="Beta", summary="Beta will remove beta-model-5 on Apr 1, 2027",
            effective="2027-04-01", kind="model", severity="deprecation", signatures=["beta-model-5"],
            fix_hint="Plan a move to beta-model-6.", **REVIEWED),
    ]
    for r in rows:
        (d / f"{r['id']}.yml").write_text(yaml.safe_dump(r))
    return d


def test_identifier_pages(tmp_path):
    build(tmp_path, records=id_fixture(tmp_path), base_url="https://feed.example.com")
    out = tmp_path / "site"
    slugs = {p.stem for p in (out / "ids").glob("*.html")}
    # no pages for code fragments or scoped records; spellings that slug the same share one page; "index" is reserved
    # (model ids only: the non-model /v2/search change keeps to its record page)
    assert slugs == {"acme-pro-2-0", "acme-lite", "acme-shared", "beta-fast-1", "index-id", "beta-model-5", "gamma-old-1"}
    page = (out / "ids" / "acme-pro-2-0.html").read_text()
    assert "<title>acme-pro-2.0 retirement: Dec 1, 2026 · apiwatch</title>" in page
    assert '<p class="lede">acme-pro-2.0 stops working on Dec 1, 2026.</p>' in page
    assert 'content="acme-pro-2.0 stops working on Dec 1, 2026. Move to acme-pro-3."' in page
    assert 'href="../records/acme-2026-12-01-model.html"' in page and page.count('<aside class="cta"') == 1
    assert '<link rel="canonical" href="https://feed.example.com/ids/acme-pro-2-0">' in page
    lite = (out / "ids" / "acme-lite.html").read_text()
    assert "<code>acme-lite</code> retirement" in lite and "stopped working on Jun 1, 2026" in lite
    assert "Also written as <code>ACME-LITE</code>, <code>acme.lite</code>." in lite
    # an id with a past and an upcoming record leads with the upcoming date and lists both
    shared = (out / "ids" / "acme-shared.html").read_text()
    assert '<p class="lede">acme-shared stops working on Dec 1, 2026.</p>' in shared
    assert "acme-2026-06-01-old.html" in shared and "acme-2026-12-01-model.html" in shared
    silent = (out / "ids" / "beta-fast-1.html").read_text()
    assert "<title>beta-fast-1 behavior change: Oct 15, 2026 · apiwatch</title>" in silent
    assert "changes behavior on Oct 15, 2026" in silent
    # a deprecation never claims the date is when it stops working or became deprecated
    dep = (out / "ids" / "beta-model-5.html").read_text()
    assert ("beta-model-5 is deprecated: it still works, and removal is announced. Key date: Apr 1, 2027.") in dep
    assert "stops working" not in dep and "deprecated from" not in dep
    gone = (out / "ids" / "gamma-old-1.html").read_text()     # past key date: no claim that it still works
    assert "gamma-old-1 is deprecated, and removal was announced. Key date: Sep 10, 2026." in gone and "still works" not in gone
    # record pages link their signatures to id pages, but not the ones without a page
    record = (out / "records" / "acme-2026-12-01-model.html").read_text()
    assert '<a href="../ids/acme-pro-2-0.html"><code>acme-pro-2.0</code></a>' in record
    assert "<code>output_format={</code>" in record and "ids/output" not in record
    assert "ids/" not in (out / "records" / "acme-2026-09-30-sdk.html").read_text().split('<aside class="cta"')[0]
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    urls = {e.findtext("s:loc", namespaces=ns): e.findtext("s:lastmod", namespaces=ns)
            for e in ET.parse(out / "sitemap.xml").getroot().findall("s:url", ns)}
    assert "https://feed.example.com/ids/acme-pro-2-0" in urls and "https://feed.example.com/ids/beta-fast-1" in urls
    # a page whose date has passed counts that date as a change (its wording flipped to the past tense)
    assert urls["https://feed.example.com/ids/acme-lite"] == "2026-09-24"
    assert urls["https://feed.example.com/ids/gamma-old-1"] == "2026-09-10"   # passed date is later than the review


def test_page_descriptions(tmp_path):
    build(tmp_path, records=id_fixture(tmp_path))
    out = tmp_path / "site"
    record = (out / "records" / "acme-2026-12-01-model.html").read_text()
    assert '<meta name="description" content="Acme retires acme-pro-2.0. Takes effect Dec 1, 2026. Move to acme-pro-3.">' in record
    vendor = (out / "vendors" / "acme.html").read_text()
    assert "<title>Acme API deprecations and breaking changes · apiwatch</title>" in vendor
    assert "5 tracked Acme changes: model retirements and breaking changes, each with" in vendor
    beta = (out / "vendors" / "beta.html").read_text()
    assert "3 tracked Beta changes: breaking change, silent change and deprecation, each with" in beta
    assert "retirement" not in beta.split("<main>")[0]
    assert 'content="1 tracked Delta change, with the date and what to do."' in (out / "vendors" / "delta.html").read_text()
    assert f'content="{site.TAGLINE}"' in (out / "index.html").read_text()
    long = site._clip("word " * 60)
    assert len(long) <= 158 and long.endswith("...") and not long.endswith("....")
    assert site._clip("x" * 200).endswith("...") and len(site._clip("x" * 200)) <= 158
