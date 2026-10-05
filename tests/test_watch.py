import json
from datetime import date

from apiwatch import watch
from apiwatch.cli import main
from apiwatch.models import ChangeRecord

TODAY = date(2026, 9, 24)

MDX = """# Changelog
<Update label="September 17, 2026" tags={["MCP"]}>
## Search drops unavailable options instead of failing
These calls previously returned a validation_error.
</Update>
<Update label="September 9, 2026">
## New webhook events
Adds page.moved.
</Update>
"""

MONTH_DAY = """# Changelog
## September, 2026
### Sep 22
Feature · Model: widget-6
Released widget-6.
### Sep 3
Deprecation
widget-4 will be removed on December 1, 2026.
## August, 2026
### Aug 30
Fixed a typo.
"""

MONTH_ONLY = """# Release Notes
## September
### widget-image-q retirement on November 2
On November 2, 2026, `widget-image-q` is retired. Requests will be served by `widget-image-2`.
### Widget 4.7
Now available.
## August
### Image API updates
The default has moved from `medium` to `auto`.
"""

BULLETS = """This page documents updates.
## September 18, 2026
- **Widget 2.5 access update**: access is limited; these models are not deprecated.
- **Widget 3.8 GA**: released [Widget 3.8](https://v.example/models/w38).
## September 1, 2026
- Single change: widget-embed-1 will be shut down January 14, 2027.
"""

STRIPE = """# Changelog
## 2026-08-26.dahlia
| Change | Product | Type |
| --- | --- | --- |
| [Adds a deep link](https://docs.v.example/changelog/a.md) | Billing | Non-breaking |
| [Removes legacy field foo_bar](https://docs.v.example/changelog/b.md) | Payments | Breaking |
"""

HTML_DATED = """<html><body><style>.x{color:red}</style>
<h2>24 September 2026</h2><div><p>Renamed <code>findThing</code> to <code>listThing</code>.</p></div>
<h2>8 September 2026</h2><p>v2 is now Generally Available.</p></body></html>"""

HTML_LIST = """<html><body><h1>Product changelog and announcements</h1><p>Deep dive on recent changes.</p>
<div>
  <span>
    Sep 23, 2026
  </span>
  <span>
    Voice API
  </span>
  <h3>Conference list endpoint will default to in-progress conferences</h3>
  <p>Starting September 30, 2026 the endpoint changes. See <a href="/docs/voice/conference">the docs</a>.
  <a href="/en-us/changelog/conf-default">Learn more</a></p>
</div>
<div>
  <span>Sep 17, 2026</span>
  <span>Verify</span>
  <h3>New voices in public beta</h3>
  <p>Try them. <a href="/en-us/changelog/voices">Learn more</a></p>
</div></body></html>"""

GITHUB = json.dumps([
    {"name": "v0.31.0", "tag_name": "v0.31.0", "html_url": "https://github.com/o/r/releases/tag/v0.31.0",
     "published_at": "2026-02-19T10:00:00Z", "body": "Remove old non-consolidated toolsets"},
    {"name": "v0.30.1", "tag_name": "v0.30.1", "html_url": "https://github.com/o/r/releases/tag/v0.30.1",
     "published_at": "2026-02-01T10:00:00Z", "body": "Fix a crash", "draft": False},
    {"name": "v0.32.0-rc", "html_url": "x", "published_at": "2026-03-01T00:00:00Z", "body": "removed", "draft": True},
])


def parse(text, url="https://v.example/changelog"):
    return watch.parse_doc(text, "src", "V", url, TODAY)


def test_mdx_update_blocks():
    es = parse(MDX)
    assert [(e.date, e.title) for e in es] == [("2026-09-17", "Search drops unavailable options instead of failing"),
                                              ("2026-09-09", "New webhook events")]


def test_day_headings_take_the_year_from_the_month_heading():
    es = parse(MONTH_DAY)
    assert [e.date for e in es] == ["2026-09-22", "2026-09-03", "2026-08-30"]
    assert es[1].title.startswith("Deprecation") and "widget-4 will be removed" in es[1].text


def test_month_only_headings_infer_the_latest_year():
    es = parse(MONTH_ONLY)
    assert [(e.date, e.title) for e in es][:2] == [("2026-09", "widget-image-q retirement on November 2"), ("2026-09", "Widget 4.7")]
    assert es[2].date == "2026-08"
    assert watch.parse_doc(MONTH_ONLY.replace("## September", "## December"), "s", "V", "u", TODAY)[0].date == "2025-12"


def test_dated_sections_with_several_bullets_split_into_entries():
    es = parse(BULLETS)
    assert [e.date for e in es] == ["2026-09-18", "2026-09-18", "2026-09-01"]
    assert es[1].url == "https://v.example/models/w38" and es[0].url == "https://v.example/changelog"
    assert "widget-embed-1 will be shut down" in es[2].text


def test_tables_of_linked_changes_become_one_entry_per_row():
    es = parse(STRIPE)
    assert [(e.date, e.title, e.url) for e in es] == [
        ("2026-08-26", "Adds a deep link", "https://docs.v.example/changelog/a.md"),
        ("2026-08-26", "Removes legacy field foo_bar", "https://docs.v.example/changelog/b.md")]
    watch.annotate(es, [])
    assert es[0].flags == [] and "breaking" in es[1].flags      # "Non-breaking" isn't a flag


def test_html_with_date_headings_and_undated_link_lists():
    es = parse(HTML_DATED)
    assert [(e.date, e.text) for e in es] == [("2026-09-24", "Renamed findThing to listThing."),
                                             ("2026-09-08", "v2 is now Generally Available.")]
    es = parse("<html><h2>22 September 2026</h2><p>Fixed: &lt;findThing&gt; has been updated.</p></html>")
    assert es[0].text == "Fixed: <findThing> has been updated."          # escaped ids aren't stripped as tags
    es = parse(HTML_LIST, "https://www.v.example/en-us/changelog")       # Twilio: date line above, link in body
    assert [(e.date, e.title, e.url) for e in es] == [
        ("2026-09-23", "Conference list endpoint will default to in-progress conferences", "https://www.v.example/en-us/changelog/conf-default"),
        ("2026-09-17", "New voices in public beta", "https://www.v.example/en-us/changelog/voices")]


def test_github_releases_skip_drafts():
    es = watch.parse_github(GITHUB, "gh", "V")
    assert [(e.date, e.title) for e in es] == [("2026-02-19", "v0.31.0"), ("2026-02-01", "v0.30.1")]


def test_flags_and_related_records():
    rec = ChangeRecord(id="v-2026-09-17-search", vendor="V MCP", surface="s", kind="mcp", severity="silent",
                       effective="2026-09-17", summary="x", signatures=["findThing", "x"], source_url="https://v.example/changelog")
    es = watch.annotate(parse(HTML_DATED), [rec])
    es = watch.annotate(parse(HTML_DATED), [rec], page_url="https://v.example/changelog")
    assert es[0].flags == ["renamed"] and es[0].related == ["v-2026-09-17-search"]   # by signature
    assert es[1].flags == [] and es[1].related == []      # sharing the changelog page url alone isn't a link


def test_run_filters_by_since_and_seen(tmp_path):
    (tmp_path / "recs").mkdir()
    pages = {"https://developers.notion.com/page/changelog.md": MDX.replace("New webhook events", "Removed old webhook events")}
    get = lambda u: pages[u]   # noqa: E731
    seen = tmp_path / "seen.json"
    leads, errs = watch.run(tmp_path / "recs", ["notion"], seen, "2026-09-10", TODAY, get)
    assert [e.date for e in leads] == ["2026-09-17"] and not errs          # Sep 9 is before --since
    leads, _ = watch.run(tmp_path / "recs", ["notion"], seen, "2026-09-01", TODAY, get)
    assert [e.date for e in leads] == ["2026-09-17", "2026-09-09"]
    watch.mark_seen(leads, seen)
    assert watch.run(tmp_path / "recs", ["notion"], seen, "2026-09-01", TODAY, get)[0] == []
    leads, errs = watch.run(tmp_path / "recs", ["openai"], seen, "2026-09-01", TODAY, get)
    assert not leads and errs and errs[0].startswith("openai: KeyError")


def test_cli_rejects_unknown_sources(tmp_path):
    assert main(["watch", "--source", "nope", "--records", str(tmp_path)]) == 2


def test_flag_rules():
    def flags(text):
        return watch.annotate([watch.Entry("s", "V", "2026-09-01", "t", "", text)], [])[0].flags
    assert flags("Adds remove_issue_reaction and redirect_to_url tools") == []          # identifiers
    assert flags("See the migration guide for the new model") == []                     # one weak word
    assert flags("previously returned an error; now drops the filter instead of failing") != []
    assert "will default" in flags("The list endpoint will default to in-progress conferences")
    assert flags("Non-breaking change") == [] and flags("Breaking change") == ["breaking"]


def test_github_prereleases_are_skipped():
    data = json.dumps([{"name": "v2.0.0a1", "published_at": "2026-09-01T00:00:00Z", "body": "removed", "prerelease": True},
                       {"name": "v2.0.0", "published_at": "2026-09-02T00:00:00Z", "body": "removed"}])
    assert [e.title for e in watch.parse_github(data, "gh", "V")] == ["v2.0.0"]


def test_undated_intro_is_dropped_from_a_dated_changelog():
    es = parse("# Changelog\nUpcoming deprecations are listed elsewhere.\n" + MONTH_DAY)
    assert all(e.date for e in es) and len(es) == 3


def test_every_spec_release_is_a_lead():
    e = watch.Entry("mcp-spec", "MCP specification", "2026-07-28", "2026-07-28", "u", "stable release of the revision")
    assert watch.annotate([e], [])[0].flags == ["new release"]


def test_a_heading_that_only_mentions_a_date_keeps_its_words():
    es = parse("# Changelog\n## September 2026\n### Deprecating widget-3 on 2026-11-02\nSee the guide.\n"
               "## v2.0 (2026-07-01)\nNew things.\n")
    watch.annotate(es, [])
    assert (es[0].date, es[0].title) == ("2026-09", "Deprecating widget-3 on 2026-11-02") and es[0].flags == ["deprecating"]
    assert es[1].date == "2026-07-01" and "v2.0" in es[1].text


def test_one_row_tables_split_and_the_header_is_not_a_flag():
    es = watch.annotate(parse("## 2026-08-26.dahlia\n| Title | Product | Breaking change? |\n| --- | --- | --- |\n"
                              "| [Adds a thing](https://d.example/a.md) | Billing | Non-breaking |\n"), [])
    assert [(e.title, e.flags) for e in es] == [("Adds a thing", [])]


def test_keys_are_distinct_and_ignore_link_changes():
    es = parse("## September 1, 2026\n### Details\nFirst change.\n### Details\nSecond change.\n")
    assert len({e.key for e in es}) == 2
    a = watch.Entry("s", "V", "2026-09-01", "T", "https://x/1", "body")
    b = watch.Entry("s", "V", "2026-09-01", "T", "https://x/2", "body")
    assert a.key == b.key


def test_prose_beside_bullets_is_kept_and_bare_year_headings_date_days():
    es = parse("## September 18, 2026\nThe v1 API is removed today.\n- a thing\n- b thing\n")
    assert es[0].text == "The v1 API is removed today." and len(es) == 3
    es = parse("## 2026\n### Sep 22\nShipped.\n")
    assert [e.date for e in es] == ["2026-09-22"]


def test_not_deprecated_and_feature_verbs_are_not_flags():
    def flags(text):
        return watch.annotate([watch.Entry("s", "V", "2026-09-01", "t", "", text)], [])[0].flags
    assert flags("These models are not deprecated and will continue to be served") == []
    assert flags("Add or remove tools between turns") == []
    assert flags("We've removed fast mode") == ["removed"]


def test_parsing_is_fast_on_hostile_input():
    import time
    t0 = time.time()
    parse("## " + " " * 5000 + "x\n" + "<a href='x'>" * 3000 + "<h2>" * 3000)
    assert time.time() - t0 < 1


def test_run_reports_unparseable_sources_and_bad_since(tmp_path):
    import pytest
    (tmp_path / "recs").mkdir()
    leads, errs = watch.run(tmp_path / "recs", ["notion"], None, "2026-09-01", TODAY, lambda u: "<html>nothing</html>")
    assert errs == ["notion: parsed 0 entries (page layout changed?)"]
    with pytest.raises(ValueError):
        watch.run(tmp_path / "recs", ["notion"], None, "2026-9-1", TODAY, lambda u: "")
