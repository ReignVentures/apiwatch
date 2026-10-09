import json
import re
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
        # no inline scripts. JSON-LD data blocks are allowed: browsers never execute them, so the CSP doesn't apply
        assert not re.search(r'<script(?![^>]*\bsrc=)(?! type="application/ld\+json">)[^>]*>', html), page
        assert " style=" not in html and " on" + "click=" not in html, page    # no inline styles/handlers


def test_404_page_uses_root_absolute_links(tmp_path):
    build(tmp_path)
    html = (tmp_path / "site" / "404.html").read_text()
    assert 'href="/style.css"' in html and 'href="/index.html"' in html and "../" not in html
    assert 'href="/upcoming.html"' in html


def test_canonical_urls_sitemap_and_robots(tmp_path):
    build(tmp_path, base_url="https://feed.example.com")
    out = tmp_path / "site"
    page = (out / "records" / "acme-2026-10-01-soon.html").read_text()
    assert '<link rel="canonical" href="https://feed.example.com/records/acme-2026-10-01-soon">' in page
    assert '<link rel="canonical" href="https://feed.example.com/">' in (out / "index.html").read_text()
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locs = [e.text for e in ET.parse(out / "sitemap.xml").getroot().findall("s:url/s:loc", ns)]
    assert "https://feed.example.com/records/acme-2026-10-01-soon" in locs and "https://feed.example.com/vendors/beta-mcp" in locs
    assert not any("draft" in u or "demo" in u for u in locs) and len(locs) == 1 + 1 + 1 + 2 + 3   # index, upcoming, pro
    assert "https://feed.example.com/upcoming" in locs
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
        if page.name not in ("404.html", "terms.html", "privacy.html") and page.parent.name != "pro":   # legal, Pro: no CTA
            assert page.read_text().count('<aside class="cta"') == 1, page
    for page in pages:
        html = page.read_text()
        html = html[html.index('<aside class="cta"'):html.index("</aside>") + len("</aside>")]
        assert "<script" not in html and " style=" not in html, page
        assert "Catch this in CI" in html and "- uses: ReignVentures/apiwatch@v1</code></pre>" in html, page
        assert 'href="https://github.com/ReignVentures/apiwatch"' in html, page
        assert 'href="https://github.com/marketplace/actions/apiwatch-scan"' in html, page
        assert "opens an issue when a new change lands on your code. $19 per month" in html and '<a href="/pro/">How Pro works</a>' in html, page
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


def upcoming_fixture(tmp_path) -> Path:
    """Build date 2026-09-23, so the 180 day window runs through 2027-03-22."""
    d = tmp_path / "uprecs"
    d.mkdir()
    rows = [
        rec("acme-2026-09-22-past", summary="Acme dropped old paging", effective="2026-09-22", **REVIEWED),
        rec("acme-2026-09-23-today", summary="Acme retires today-1", effective="2026-09-23", kind="model", **REVIEWED),
        rec("beta-2026-10-15-dep", vendor="Beta", summary="Beta deprecates beta-2", effective="2026-10-15",
            severity="deprecation", fix_hint="Plan a move.", **REVIEWED),
        rec("acme-2026-10-15-silent", summary="Acme changes rounding", effective="2026-10-15", severity="silent", **REVIEWED),
        rec("acme-2027-03-22-edge", summary="Acme retires edge-1", effective="2027-03-22", **REVIEWED),
        rec("acme-2027-03-23-out", summary="Acme retires late-1", effective="2027-03-23", **REVIEWED),
        rec("acme-2026-11-01-draft", summary="Acme draft change", effective="2026-11-01"),
        rec("example-2026-12-01-demo", summary="Made-up demo", effective="2026-12-01", illustrative=True, **REVIEWED),
    ]
    for r in rows:
        (d / f"{r['id']}.yml").write_text(yaml.safe_dump(r))
    return d


def test_upcoming_page(tmp_path):
    build(tmp_path, records=upcoming_fixture(tmp_path), base_url="https://feed.example.com")
    out = tmp_path / "site"
    page = (out / "upcoming.html").read_text()
    assert "<title>Upcoming API and model retirements and changes · apiwatch</title>" in page
    assert '<link rel="canonical" href="https://feed.example.com/upcoming">' in page
    assert "from Sep 23, 2026 through Mar 22, 2027" in page and "4 changes in the next 180 days" in page
    # today and the last day of the window are in; yesterday, day 181, drafts and illustrative records are out
    ids = re.findall(r'href="records/([^"]+)\.html"', page)
    assert ids == ["acme-2026-09-23-today", "acme-2026-10-15-silent", "beta-2026-10-15-dep", "acme-2027-03-22-edge"]
    assert [m for m in re.findall(r"<h2[^>]*>([^<]+)</h2>", page) if m != "Catch this in CI"] == [
        "September 2026", "October 2026", "March 2027"]
    row = page[page.index('href="records/beta-2026-10-15-dep.html"') - 400:]
    assert '<time datetime="2026-10-15">Oct 15, 2026</time>' in row and ">Beta</a>" in row and ">Deprecation<" in row
    assert ">Beta deprecates beta-2</a>" in row
    assert page.count('<aside class="cta"') == 1 and page.rindex("</ul>") < page.index('<aside class="cta"')
    assert ('content="4 tracked changes dated Sep 23, 2026 to Mar 22, 2027: model retirement, breaking '
            'change, silent change and deprecation. By month, with what to do."') in page
    assert "—" not in page and "–" not in page
    index = (out / "index.html").read_text()
    assert 'href="upcoming.html"' in index.split("<main>")[1]          # linked from the index body, not only the nav


def test_upcoming_page_says_so_when_nothing_is_due(tmp_path):
    site.build(fixture(tmp_path), tmp_path / "later", today=date(2030, 1, 1))
    page = (tmp_path / "later" / "upcoming.html").read_text()
    assert "<p>No tracked changes are dated between Jan 1, 2030 and Jun 30, 2030.</p>" in page
    assert '<ul class="recs">' not in page and page.count('<aside class="cta"') == 1
    assert 'content="No tracked API, model or MCP changes are dated between Jan 1, 2030 and Jun 30, 2030."' in page


def _ld(html: str) -> dict:
    blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
    assert len(blocks) == 1
    return json.loads(blocks[0])


def test_structured_data_on_record_and_id_pages(tmp_path):
    build(tmp_path, records=id_fixture(tmp_path), base_url="https://feed.example.com")
    out = tmp_path / "site"
    ld = _ld((out / "records" / "acme-2026-12-01-model.html").read_text())
    assert ld == {"@context": "https://schema.org", "@type": "TechArticle", "headline": "Acme retires acme-pro-2.0",
                  "description": "Acme retires acme-pro-2.0. Takes effect Dec 1, 2026. Move to acme-pro-3.",
                  "about": {"@type": "Thing", "name": "s"}, "inLanguage": "en",
                  "publisher": {"@type": "Organization", "name": "Reign Ventures", "url": "https://reignventures.co"},
                  "url": "https://feed.example.com/records/acme-2026-12-01-model", "dateModified": "2026-09-24",
                  "isBasedOn": "https://v.example/changelog"}
    idl = _ld((out / "ids" / "acme-lite.html").read_text())
    assert idl["headline"] == "acme-lite retirement: Jun 1, 2026" and idl["about"] == {"@type": "Thing", "name": "acme-lite"}
    assert idl["url"] == "https://feed.example.com/ids/acme-lite" and idl["dateModified"] == "2026-09-24"
    assert _ld((out / "ids" / "gamma-old-1.html").read_text())["dateModified"] == "2026-09-10"   # as in the sitemap
    assert "datePublished" not in idl and "datePublished" not in ld
    # a passed date changes a record page's wording ("Took effect"), so it counts as a change there too
    past = (out / "records" / "gamma-2026-09-10-dep.html").read_text()
    assert _ld(past)["dateModified"] == "2026-09-10"
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    urls = {e.findtext("s:loc", namespaces=ns): e.findtext("s:lastmod", namespaces=ns)
            for e in ET.parse(out / "sitemap.xml").getroot().findall("s:url", ns)}
    assert urls["https://feed.example.com/records/gamma-2026-09-10-dep"] == "2026-09-10"
    assert urls["https://feed.example.com/records/acme-2026-12-01-model"] == "2026-09-24"
    # the data block sits in <head>
    record = (out / "records" / "acme-2026-12-01-model.html").read_text()
    assert record.index("application/ld+json") < record.index("</head>")
    # only record and id pages carry it
    for page in [out / "index.html", out / "upcoming.html", out / "vendors" / "acme.html", out / "404.html"]:
        assert "application/ld+json" not in page.read_text(), page


def test_structured_data_cannot_break_out_of_its_script_element(tmp_path):
    recs = tmp_path / "recs"
    recs.mkdir()
    evil = "</script><script>alert(1)</script> <!-- & -->"
    (recs / "x.yml").write_text(yaml.safe_dump(rec(
        "x-2026-01-01-evil", summary=evil, surface="</SCRIPT>", kind="model", signatures=["evil-model-1"],
        **{**REVIEWED, "source_url": "javascript:alert(1)"})))
    build(tmp_path, records=recs)
    for page in [tmp_path / "site" / "records" / "x-2026-01-01-evil.html", tmp_path / "site" / "ids" / "evil-model-1.html"]:
        html = page.read_text()
        start = html.index('<script type="application/ld+json">') + len('<script type="application/ld+json">')
        block = html[start:html.index("</script>", start)]
        assert not set("<>&") & set(block), page
        assert html.lower().count("</script>") == 1 and "<script>" not in html, page
        assert "isBasedOn" not in _ld(html)                     # never a non-http(s) source
    assert _ld((tmp_path / "site" / "records" / "x-2026-01-01-evil.html").read_text())["headline"] == evil


def test_readme_summary_counts_published_records_only(tmp_path):
    from apiwatch.models import load_records
    md = site.readme_summary(load_records(fixture(tmp_path)), date(2026, 9, 23), "https://feed.example.com/")
    assert md.startswith("## What it caught\n")
    assert "The feed holds 3 reviewed changes" in md
    assert "* **By vendor:** Acme 2, Beta MCP 1\n" in md and "* **By severity:** breaking 3\n" in md
    assert "[everything in the next 180 days](https://feed.example.com/upcoming)" in md
    assert ("* **Oct 1, 2026**, Acme, breaking: [Acme retires widget-1]"
            "(https://feed.example.com/records/acme-2026-10-01-soon)") in md
    assert "draft" not in md and "Made-up" not in md and "demo" not in md
    assert "—" not in md and "–" not in md


def test_readme_summary_lists_the_next_five_dates(tmp_path):
    from apiwatch.models import load_records
    recs = load_records(id_fixture(tmp_path))
    md = site.readme_summary(recs, date(2026, 9, 23), "https://feed.example.com")
    nxt = [line for line in md.splitlines() if line.startswith("* **") and not line.startswith("* **By")]
    assert [line.split("**")[1] for line in nxt] == ["Sep 30, 2026", "Oct 15, 2026", "Oct 20, 2026", "Nov 1, 2026",
                                                     "Dec 1, 2026"]
    assert "Apr 1, 2027" not in md
    assert "* No dated changes ahead in the feed right now." in site.readme_summary(recs, date(2030, 1, 1), "https://x.example")
    # a record dated today still counts as ahead; records that failed the source check are left out, as on the site
    md = site.readme_summary(recs, date(2026, 9, 30), "https://x.example", exclude={"beta-2026-10-15-silent"})
    assert "* **Sep 30, 2026**, Acme" in md and "beta-2026-10-15-silent" not in md and "Beta 2," in md


def test_readme_summary_escapes_markdown():
    from apiwatch.models import ChangeRecord
    r = ChangeRecord(**rec("x-2026-10-01-md", vendor="X_Co", summary="Use [this](http://evil) <b>*now*</b> a ~~b~~ & c", **REVIEWED))
    md = site.readme_summary([r], date(2026, 9, 1), "https://feed.example.com")
    assert "X\\_Co" in md and "a \\~\\~b\\~\\~ \\& c" in md
    assert "\\[this\\](http://evil) \\<b\\>\\*now\\*\\</b\\>" in md


CHECKOUT = "https://buy.stripe.com/test_abc123"


def test_pro_pages_say_email_until_checkout_and_app_are_both_set(tmp_path):
    records = fixture(tmp_path)
    for kw in ({}, {"pro_checkout_url": CHECKOUT}, {"pro_app_slug": "apiwatch-pro"},
               {"pro_checkout_url": "https://evil.example/pay", "pro_app_slug": "apiwatch-pro"}):
        build(tmp_path, records, **kw)
        pro = (tmp_path / "site" / "pro" / "index.html").read_text()
        assert "early access" in pro and "mailto:hello@reignventures.co" in pro, kw
        assert "buy.stripe.com" not in pro and "evil.example" not in pro and "installations/new" not in pro, kw


def test_pro_pages_link_checkout_and_install_when_set(tmp_path):
    build(tmp_path, pro_checkout_url=CHECKOUT, pro_app_slug="apiwatch-pro")
    out = tmp_path / "site" / "pro"
    pro = (out / "index.html").read_text()
    install = "https://github.com/apps/apiwatch-pro/installations/new"
    assert f'href="{CHECKOUT}"' in pro and f'href="{install}"' in pro and "early access" not in pro
    assert "$19 per month" in pro and "<script" not in pro and " style=" not in pro
    assert "(3 today)" in pro                       # the fixture's published breaking records
    welcome, installed = (out / "welcome.html").read_text(), (out / "installed.html").read_text()
    assert f'href="{install}"' in welcome and f'href="{CHECKOUT}"' in installed
    for page in (welcome, installed):
        assert '<meta name="robots" content="noindex">' in page
    assert '<meta name="robots"' not in pro
    assert "—" not in pro + welcome + installed and "–" not in pro + welcome + installed


def test_ci_block_links_the_pro_page(tmp_path):
    build(tmp_path)
    assert '<a href="/pro/">How Pro works</a>' in (tmp_path / "site" / "index.html").read_text()


def test_cli_passes_pro_settings(tmp_path):
    out = tmp_path / "s"
    assert main(["site", "--records", str(fixture(tmp_path)), "--out", str(out),
                 "--pro-checkout-url", CHECKOUT, "--pro-app-slug", "apiwatch-pro"]) == 0
    assert CHECKOUT in (out / "pro" / "index.html").read_text()


def test_terms_and_privacy_pages_are_linked_everywhere(tmp_path):
    build(tmp_path, pro_checkout_url=CHECKOUT, pro_app_slug="apiwatch-pro")
    out = tmp_path / "site"
    for name, heading in (("terms.html", "Terms of Service"), ("privacy.html", "Privacy Policy")):
        html = (out / name).read_text()
        assert f"<h1>{heading}</h1>" in html and "Reign Ventures LLC" in html and "<script" not in html
        assert "—" not in html and "–" not in html
    for page in out.rglob("*.html"):
        html = page.read_text()
        assert 'terms.html">Terms</a>' in html and 'privacy.html">Privacy</a>' in html, page
    pro = (out / "pro" / "index.html").read_text()
    assert 'href="../terms.html"' in pro and 'href="../privacy.html"' in pro
