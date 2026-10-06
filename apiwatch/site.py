"""Build the public feed as a static site: index, per-record and per-vendor pages, JSON Feed, Atom.

No dependencies, no JavaScript required (a small script adds severity filtering).
Only reviewed records are published (ChangeRecord.published); drafts and `illustrative` records are
left out, and so is any record whose evidence failed the latest source check (see check.py).
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date
from html import escape
from pathlib import Path
from xml.sax.saxutils import escape as xesc

from .feed import ORDER
from .models import ChangeRecord, load_records

SITE_TITLE = "apiwatch"
TAGLINE = "Breaking, silent, and scheduled changes in the APIs, models, and MCP servers your code depends on."
SEV_LABEL = {"breaking": "Breaking", "silent": "Silent", "deprecation": "Deprecation", "additive": "Additive", "info": "Info"}
SEV_NOTE = {
    "breaking": "Calls fail after this date.",
    "silent": "No error. Behavior, results, or cost change quietly.",
    "deprecation": "Still works; removal is announced.",
    "additive": "New capability; nothing existing breaks.",
    "info": "Worth knowing; no action required.",
}
REPO_URL = "https://github.com/ReignVentures/apiwatch"
MARKETPLACE_URL = "https://github.com/marketplace/actions/apiwatch-scan"
PRO_EMAIL = "hello@reignventures.co"
# the next step on every index, vendor and record page: add the free Action, or ask about Pro. Plain HTML, no script
CTA = f"""<aside class="cta" aria-labelledby="ci">
<h2 id="ci">Catch this in CI</h2>
<p>Add two steps to a GitHub Actions job. Each run checks your code against the changes in this feed, and the
build fails when a breaking or silent change lands on your code.</p>
<pre><code>- uses: actions/checkout@v4
- uses: ReignVentures/apiwatch@v1</code></pre>
<p><a href="{REPO_URL}">Setup and options on GitHub</a> · <a href="{MARKETPLACE_URL}">GitHub Marketplace</a></p>
<p class="meta">apiwatch Pro watches every repository in your GitHub organization each night, with nothing to add
to your workflows, and opens an issue when a new change lands on your code. $19 per month. Email <a href="mailto:{PRO_EMAIL}">{PRO_EMAIL}</a> for early access.</p>
</aside>"""


def vendor_slug(vendor: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in vendor.lower()).strip("-")


def build(records_dir: Path, out: Path, today: date | None = None, base_url: str = "",
          exclude: set[str] = frozenset()) -> tuple[int, Path]:
    today = today or date.today()
    base = base_url.rstrip("/") + "/" if base_url else ""
    recs = [r for r in load_records(records_dir) if r.published and r.id not in exclude]
    recs.sort(key=lambda r: (r.effective, -ORDER[r.severity]), reverse=True)
    out.mkdir(parents=True, exist_ok=True)
    (out / "records").mkdir(exist_ok=True)
    (out / "vendors").mkdir(exist_ok=True)
    (out / "ids").mkdir(exist_ok=True)
    (out / "style.css").write_text(CSS)
    (out / "site.js").write_text(JS)
    (out / "_headers").write_text(HEADERS)   # Cloudflare Pages response headers

    by_vendor: dict[str, list[ChangeRecord]] = defaultdict(list)
    for r in recs:
        by_vendor[r.vendor].append(r)

    # canonical URLs are the clean paths Cloudflare Pages serves (it 308-redirects "x.html" to "x"), on base_url
    (out / "index.html").write_text(_canonical(_index(recs, by_vendor, today), base, ""))
    ids = id_pages(recs)
    id_links = {sig: slug for slug, (sigs, _) in ids.items() for sig in sigs}
    for r in recs:
        (out / "records" / f"{r.id}.html").write_text(_canonical(_record_page(r, today, id_links), base, f"records/{r.id}"))
    for vendor, rs in by_vendor.items():
        (out / "vendors" / f"{vendor_slug(vendor)}.html").write_text(_canonical(
            _page(f"{vendor} API deprecations and breaking changes", _vendor_body(vendor, rs, today), depth=1,
                  description=_vendor_description(vendor, rs, today)), base, f"vendors/{vendor_slug(vendor)}"))
    for slug, (sigs, rs) in ids.items():
        (out / "ids" / f"{slug}.html").write_text(_canonical(_id_page(sigs, rs, today), base, f"ids/{slug}"))
    # served for any unknown path at any depth, so links are root-absolute. Without it Cloudflare Pages
    # treats the site as a SPA and answers every unknown path with index.html and a 200
    (out / "404.html").write_text(_page("Not found", '<h1>Not found</h1><p>No page here. '
                                        '<a href="/index.html">See all changes</a>.</p>', up="/"))
    (out / "feed.json").write_text(_json_feed(recs, base))
    (out / "feed.xml").write_text(_atom(recs, base, today))
    (out / "robots.txt").write_text("User-agent: *\nAllow: /\n" + (f"Sitemap: {base}sitemap.xml\n" if base else ""))
    if base:
        (out / "sitemap.xml").write_text(_sitemap(recs, by_vendor, ids, base, today))
    return len(recs), out / "index.html"


def _canonical(html: str, base: str, path: str) -> str:
    if not base:
        return html
    return html.replace("</head>", f'<link rel="canonical" href="{escape(base + path)}">\n</head>', 1)


def _sitemap(recs, by_vendor, ids, base, today) -> str:
    urls = [(base, today.isoformat())]
    urls += [(f"{base}vendors/{vendor_slug(v)}", max(r.verified_on or r.effective for r in rs)) for v, rs in by_vendor.items()]
    urls += [(f"{base}records/{r.id}", r.verified_on or r.effective) for r in recs]
    # an id page's wording turns from "stops" to "stopped" once a date passes, so that date counts as a change
    urls += [(f"{base}ids/{slug}", max(max(r.verified_on or r.effective, r.effective if r.effective <= today.isoformat() else "")
                                       for r in rs)) for slug, (_, rs) in ids.items()]
    body = "".join(f"<url><loc>{xesc(u)}</loc><lastmod>{d}</lastmod></url>" for u, d in urls)
    return f'<?xml version="1.0" encoding="utf-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</urlset>\n'


# ---------- pages ----------

def _page(title: str, body: str, depth: int = 0, up: str | None = None, description: str = TAGLINE) -> str:
    up = "../" * depth if up is None else up
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)} · {SITE_TITLE}</title>
<meta name="description" content="{escape(description)}">
<link rel="stylesheet" href="{up}style.css">
<link rel="alternate" type="application/atom+xml" title="{SITE_TITLE}" href="{up}feed.xml">
<link rel="alternate" type="application/feed+json" title="{SITE_TITLE}" href="{up}feed.json">
</head>
<body>
<header class="site"><a class="brand" href="{up}index.html">{SITE_TITLE}</a>
<nav><a href="{up}feed.xml">Atom</a><a href="{up}feed.json">JSON</a></nav></header>
<main>
{body}
</main>
<footer class="site"><p>Records summarize vendor changes in our own words and link the vendor's primary source.
Each one is reviewed against that source before it's published, and its key facts are re-checked against the source daily;
a record whose source no longer supports it comes off the site. Reviews are AI-assisted: confirm details that matter before acting.</p></footer>
</body>
</html>
"""


def _badges(r: ChangeRecord) -> str:
    if r.human_verified:
        v = f'<span class="badge verified" title="Checked against the source by {escape(r.verified_by)} on {r.verified_on}">Verified</span>'
    else:
        v = ''
    return f'<span class="badge sev-{r.severity}">{SEV_LABEL[r.severity]}</span> <span class="badge kind">{escape(r.kind)}</span> {v}'


def _row(r: ChangeRecord, depth: int) -> str:
    up = "../" * depth
    return (f'<li class="rec" data-sev="{r.severity}">'
            f'<time datetime="{r.effective}">{_fmt(r.effective)}</time>'
            f'<div><a class="vendor" href="{up}vendors/{vendor_slug(r.vendor)}.html">{escape(r.vendor)}</a> '
            f'{_badges(r)}'
            f'<a class="summary" href="{up}records/{r.id}.html">{escape(r.summary)}</a></div></li>')


def _split(recs: list[ChangeRecord], today: date, depth: int) -> str:
    upcoming = sorted((r for r in recs if r.effective > today.isoformat()), key=lambda r: r.effective)
    past = [r for r in recs if r.effective <= today.isoformat()]
    parts = []
    if upcoming:
        parts.append('<h2>Coming up</h2><ul class="recs">' + "".join(_row(r, depth) for r in upcoming) + "</ul>")
    if past:
        parts.append('<h2>In effect</h2><ul class="recs">' + "".join(_row(r, depth) for r in past) + "</ul>")
    return "\n".join(parts)


def _index(recs, by_vendor, today) -> str:
    counts = defaultdict(int)
    for r in recs:
        counts[r.severity] += 1
    filters = "".join(
        f'<button type="button" data-sev="{s}" aria-pressed="false">{SEV_LABEL[s]} <span>{counts[s]}</span></button>'
        for s in ORDER if counts[s])
    vendors = "".join(f'<a href="vendors/{vendor_slug(v)}.html">{escape(v)} <span>{len(rs)}</span></a>'
                      for v, rs in sorted(by_vendor.items(), key=lambda kv: kv[0].lower()))
    body = f"""<section class="intro">
<h1>API &amp; MCP change feed</h1>
<p class="lede">{escape(TAGLINE)}</p>
<p class="meta">{len(recs)} changes · {len(by_vendor)} vendors · updated {_fmt(today.isoformat())}</p>
</section>
<div class="filters" role="group" aria-label="Filter by severity">{filters}</div>
{_split(recs, today, 0)}
<h2>Vendors</h2>
<div class="vendors">{vendors}</div>
{CTA}
<script src="site.js" defer></script>"""
    return _page("API & MCP change feed", body)


def _vendor_body(vendor, rs, today) -> str:
    return (f'<p class="crumb"><a href="../index.html">All changes</a></p><h1>{escape(vendor)}</h1>'
            f'<p class="meta">{len(rs)} change{"s" if len(rs) != 1 else ""}</p>'
            + _split(rs, today, 1) + CTA)


def _record_page(r: ChangeRecord, today: date, id_links: dict[str, str] | None = None) -> str:
    id_links = id_links or {}
    when = "Takes effect" if r.effective > today.isoformat() else "Took effect"
    if r.human_verified:
        flag = f'<p class="meta">Verified against the source by {escape(r.verified_by)} on {_fmt(r.verified_on)}.</p>'
    else:
        flag = (f'<p class="meta checked">Reviewed against the vendor source on {_fmt(r.verified_on)} (AI-assisted review). '
                'Key facts are re-checked against the source daily.</p>')
    sigs = "".join(f'<a href="../ids/{id_links[s]}.html"><code>{escape(s)}</code></a>' if s in id_links
                   else f"<code>{escape(s)}</code>" for s in r.signatures) or "<span>None recorded</span>"
    def _words(items):
        return " or ".join(" + ".join(f"<code>{escape(w)}</code>" for w in ([c] if isinstance(c, str) else c)) for c in items)
    scope = []
    if r.files:
        scope.append("files matching " + ", ".join(f"<code>{escape(g)}</code>" for g in r.files))
    if r.context:
        scope.append("files that mention " + _words(r.context))
    if r.repo_context:
        pinned = [c for c in r.repo_context if isinstance(c, str) and c.startswith("dep:")]
        rest = [c for c in r.repo_context if c not in pinned]
        phrases = []
        for c in pinned:                       # dep:pypi:stripe@16. → stripe 16.x (pypi)
            m = re.match(r"dep:(?:(\w+):)?(.+)@(.+)", c)
            if m:
                phrases.append(f"<code>{escape(m.group(2))} {escape(m.group(3).rstrip('.'))}.x</code>"
                               + (f" ({escape(m.group(1))})" if m.group(1) else ""))
        if phrases:
            scope.append("repos that depend on " + " or ".join(phrases))
        if rest:
            scope.append(("or " if phrases else "") + "repos that mention " + _words(rest) + " (including dependency manifests)")
    excl = ('<p class="meta">Not affected (matches inside these are ignored): '
            + ", ".join(f"<code>{escape(e)}</code>" for e in r.exclude) + "</p>") if r.exclude else ""
    if scope:
        excl += '<p class="meta">Counts only in ' + "; ".join(scope) + ".</p>"
    if r.source_url.startswith(("https://", "http://")):
        src = f'<a href="{escape(r.source_url)}" rel="noopener">{escape(r.source_url)}</a>'
    else:
        src = escape(r.source_url) or "No source recorded"   # never link a non-http(s) URL
    body = f"""<p class="crumb"><a href="../index.html">All changes</a> / <a href="../vendors/{vendor_slug(r.vendor)}.html">{escape(r.vendor)}</a></p>
<article class="record">
<p class="badges">{_badges(r)}</p>
<h1>{escape(r.summary)}</h1>
<p class="meta">{when} <time datetime="{r.effective}">{_fmt(r.effective)}</time> · <code>{escape(r.surface)}</code></p>
{flag}
<h2>What changes</h2>
<p>{escape(r.detail) or "No detail recorded."}</p>
<p class="sevnote"><strong>{SEV_LABEL[r.severity]}:</strong> {SEV_NOTE[r.severity]}</p>
<h2>What to do</h2>
<p>{escape(r.fix_hint) or "No fix recorded."}</p>
<h2>Strings to search for</h2>
<p class="sigs">{sigs}</p>{excl}
<h2>Source</h2>
<p>{src}</p>
</article>
{CTA}"""
    return _page(r.summary, body, depth=1, description=_clip(f"{_sentence(r.summary)} {when} {_fmt(r.effective)}. {r.fix_hint}"))


# ---------- identifier pages ----------

# model ids only, from records with no `context`, `files` or `repo_context` scope: a model retirement applies to
# every caller, so a one line answer ("x stops working on <date>") is true as stated. API, SDK and MCP changes are
# often tied to an API version, SDK major or protocol revision that a one line answer can't carry, so they stay
# on their record pages. Code fragments like `output_format={` never get a page
_IDENT = re.compile(r"^/?[A-Za-z0-9][A-Za-z0-9._/-]*[A-Za-z0-9]$")


def id_slug(sig: str) -> str:
    # dots become dashes too: Cloudflare Pages serves x.html at /x, and a dotted path could be taken for a file.
    # "index" is reserved: ids/index.html would be served at /ids/
    slug = re.sub(r"-+", "-", re.sub(r"[^a-z0-9_-]", "-", sig.lower())).strip("-_")
    return "index-id" if slug == "index" else slug


def id_pages(recs: list[ChangeRecord]) -> dict[str, tuple[list[str], list[ChangeRecord]]]:
    """slug -> (every spelling that maps to it, sorted; the records). Spellings that differ only in case or
    punctuation share one page, so a slug never depends on the order records or signatures were written in."""
    pages: dict[str, tuple[list[str], list[ChangeRecord]]] = {}
    for r in recs:
        if r.kind != "model" or r.context or r.files or r.repo_context:
            continue
        for sig in r.signatures:
            if len(sig) < 4 or not _IDENT.match(sig) or not re.search(r"[A-Za-z]", sig):
                continue
            sigs, rs = pages.setdefault(id_slug(sig), ([], []))
            if sig not in sigs:
                sigs.append(sig)
            if r not in rs:
                rs.append(r)
    return {slug: (sorted(sigs, key=lambda x: (x.lower(), x != x.lower(), x)), sorted(rs, key=lambda r: (r.effective, r.id)))
            for slug, (sigs, rs) in sorted(pages.items())}


def _what_happens(r: ChangeRecord, today: date) -> tuple[str, str]:
    """(what happens, title noun) for an identifier under this record. Each phrase claims only what the
    severity means: breaking = calls fail after the date, silent = behavior changes on it, deprecation = still
    works with removal announced (its date may be the removal date, so no verb is tied to it)."""
    future = r.effective > today.isoformat()
    when = _fmt(r.effective)
    if r.severity == "breaking":
        return (f"stops working on {when}" if future else f"stopped working on {when}",
                "retirement" if r.kind == "model" else "breaking change")
    if r.severity == "silent":
        return (f"changes behavior on {when}" if future else f"changed behavior on {when}", "behavior change")
    if r.severity == "deprecation":
        # once the key date passes it may have been the shutdown, so stop saying it still works
        return (f"is deprecated: it still works, and removal is announced. Key date: {when}" if future
                else f"is deprecated, and removal was announced. Key date: {when}", "deprecation")
    return (f"changes on {when}" if future else f"changed on {when}", "change")


def _id_page(sigs: list[str], rs: list[ChangeRecord], today: date) -> str:
    sig = sigs[0]
    upcoming = [r for r in rs if r.effective > today.isoformat()]
    lead = upcoming[0] if upcoming else rs[-1]          # the next date to act on, else the most recent one
    what, noun = _what_happens(lead, today)
    answer = f"{sig} {what}."
    also = ("<p class=\"meta\">Also written as " + ", ".join(f"<code>{escape(x)}</code>" for x in sigs[1:]) + ".</p>"
            if len(sigs) > 1 else "")
    items = "".join(
        f'<li><p><strong>{escape(r.vendor)}</strong> {_badges(r)}</p>'
        f'<p><code>{escape(sig)}</code> {escape(_what_happens(r, today)[0])}. '
        f'<a href="../records/{r.id}.html">{escape(r.summary)}</a></p>'
        f'<p><strong>What to do:</strong> {escape(r.fix_hint) or "See the record."}</p></li>' for r in rs)
    body = f"""<p class="crumb"><a href="../index.html">All changes</a> / <a href="../vendors/{vendor_slug(lead.vendor)}.html">{escape(lead.vendor)}</a></p>
<article class="record">
<h1><code>{escape(sig)}</code> {noun}</h1>
<p class="lede">{escape(answer)}</p>
{also}
<ul class="idrecs">{items}</ul>
</article>
{CTA}"""
    title = f"{sig} {noun}: {_fmt(lead.effective)}"
    return _page(title, body, depth=1, description=_clip(f"{answer} {lead.fix_hint}"))


_KINDS = [("retirement", "model retirement"), ("breaking", "breaking change"), ("silent", "silent change"),
          ("deprecation", "deprecation")]


def _vendor_description(vendor: str, rs: list[ChangeRecord], today: date) -> str:
    counts: dict[str, int] = defaultdict(int)
    for r in rs:
        counts["retirement" if (r.severity == "breaking" and r.kind == "model") else r.severity] += 1
    names = [label + ("s" if counts[key] > 1 else "") for key, label in _KINDS if counts[key]]
    listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1] if names else ""
    nxt = sorted((r for r in rs if r.effective > today.isoformat()), key=lambda r: r.effective)
    tail = f" Next: {_sentence(nxt[0].summary)}" if nxt else ""
    each = "each with" if len(rs) != 1 else "with"
    listed = f": {listed}," if listed else ","
    return _clip(f"{len(rs)} tracked {vendor} change{'s' if len(rs) != 1 else ''}{listed} {each} the date "
                 f"and what to do.{tail}")


def _sentence(text: str) -> str:
    text = text.strip()
    return text if not text or text[-1] in ".!?" else text + "."


def _clip(text: str, n: int = 158) -> str:
    text = " ".join(text.split())
    if len(text) <= n:
        return text
    cut = text[:n - 3]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(",;:.") + "..."


# ---------- feeds ----------

def _json_feed(recs, base) -> str:
    items = []
    for r in recs:
        items.append({
            "id": f"urn:apiwatch:{r.id}",
            **({"url": f"{base}records/{r.id}"} if base else {}),
            "title": f"{r.vendor}: {r.summary}",
            "content_text": "\n\n".join(x for x in [r.detail, f"What to do: {r.fix_hint}" if r.fix_hint else "",
                                                   f"Source: {r.source_url}" if r.source_url else ""] if x),
            "date_published": f"{r.effective}T00:00:00Z",
            "tags": [r.vendor, r.kind, r.severity],
            **({"external_url": r.source_url} if r.source_url.startswith(("https://", "http://")) else {}),
            "_apiwatch": {"severity": r.severity, "kind": r.kind, "surface": r.surface, "effective": r.effective,
                          "signatures": r.signatures, "exclude": r.exclude, "context": r.context,
                          "files": r.files, "repo_context": r.repo_context, "verified": r.verified,
                          "verified_by": r.verified_by, "verified_on": r.verified_on,
                          "review": "human" if r.human_verified else "ai"},
        })
    feed = {"version": "https://jsonfeed.org/version/1.1", "title": f"{SITE_TITLE}: API & MCP change feed",
            "description": TAGLINE, **({"home_page_url": base, "feed_url": f"{base}feed.json"} if base else {}),
            "items": items}
    return json.dumps(feed, indent=2)


def _atom(recs, base, today) -> str:
    entries = []
    for r in recs:
        link = f'<link href="{xesc(base)}records/{xesc(r.id)}"/>' if base else ""
        entries.append(f"""<entry>
<id>urn:apiwatch:{xesc(r.id)}</id>
<title>{xesc(r.vendor + ": " + r.summary)}</title>
{link}
<updated>{r.effective}T00:00:00Z</updated>
<category term="{xesc(r.severity)}"/><category term="{xesc(r.kind)}"/>
<summary>{xesc(r.detail)}</summary>
</entry>""")
    self_link = f'<link rel="self" href="{xesc(base)}feed.xml"/>' if base else ""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<id>urn:apiwatch:feed</id>
<title>{SITE_TITLE}: API &amp; MCP change feed</title>
<subtitle>{xesc(TAGLINE)}</subtitle>
{self_link}
<updated>{today.isoformat()}T00:00:00Z</updated>
<author><name>{SITE_TITLE}</name></author>
{"".join(entries)}
</feed>
"""


def _fmt(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d:%b} {d.day}, {d.year}"


CSS = """:root {
  --bg: #fbfbf9; --fg: #1d1d1b; --muted: #6b6a66; --line: #e6e4de; --card: #ffffff; --link: #1f4fd1;
  --breaking: #b42318; --silent: #b54708; --deprecation: #8a6d00; --additive: #067647; --info: #555;
  --flag-bg: #fff6e5; --flag-line: #f0c36d;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #141413; --fg: #ecebe6; --muted: #a09e97; --line: #2c2b28; --card: #1b1b19; --link: #8fb0ff;
    --breaking: #ff8a80; --silent: #ffb366; --deprecation: #e8cc6a; --additive: #6fd6a0; --info: #bbb;
    --flag-bg: #2a2214; --flag-line: #7a5a1c;
  }
}
:root[data-theme="dark"] {
  --bg: #141413; --fg: #ecebe6; --muted: #a09e97; --line: #2c2b28; --card: #1b1b19; --link: #8fb0ff;
  --breaking: #ff8a80; --silent: #ffb366; --deprecation: #e8cc6a; --additive: #6fd6a0; --info: #bbb;
  --flag-bg: #2a2214; --flag-line: #7a5a1c;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 16px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; }
a { color: var(--link); text-decoration: none; }
a:hover { text-decoration: underline; }
code { font: 0.9em ui-monospace, SFMono-Regular, Menlo, monospace; background: var(--card);
  border: 1px solid var(--line); border-radius: 4px; padding: 0 4px; overflow-wrap: anywhere; }
header.site, main, footer.site { max-width: 880px; margin: 0 auto; padding: 0 16px; }
header.site { display: flex; justify-content: space-between; align-items: center; padding-top: 20px; padding-bottom: 12px; }
.brand { font-weight: 700; color: var(--fg); letter-spacing: -0.01em; }
header nav a { margin-left: 16px; color: var(--muted); font-size: 14px; }
h1 { font-size: 30px; line-height: 1.2; letter-spacing: -0.02em; margin: 16px 0 8px; }
h2 { font-size: 15px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); margin: 36px 0 8px; }
.record h2 { text-transform: none; letter-spacing: 0; font-size: 18px; color: var(--fg); margin-top: 28px; }
.lede { font-size: 18px; margin: 0 0 6px; max-width: 60ch; }
.meta, .crumb { color: var(--muted); font-size: 14px; }
.recs { list-style: none; padding: 0; margin: 0; border-top: 1px solid var(--line); }
.rec { display: grid; grid-template-columns: 110px 1fr; gap: 12px; padding: 12px 0; border-bottom: 1px solid var(--line); }
.rec time { color: var(--muted); font-size: 14px; font-variant-numeric: tabular-nums; padding-top: 2px; }
.rec .vendor { font-weight: 600; color: var(--fg); margin-right: 4px; }
.rec .summary { display: block; margin-top: 4px; color: var(--fg); }
.badge { display: inline-block; font-size: 12px; line-height: 18px; padding: 0 7px; border-radius: 999px;
  border: 1px solid var(--line); color: var(--muted); vertical-align: 1px; }
.sev-breaking { color: var(--breaking); border-color: currentColor; }
.sev-silent { color: var(--silent); border-color: currentColor; }
.sev-deprecation { color: var(--deprecation); border-color: currentColor; }
.sev-additive { color: var(--additive); border-color: currentColor; }
.sev-info { color: var(--info); }
.verified { color: var(--additive); }
.filters { display: flex; flex-wrap: wrap; gap: 8px; margin: 20px 0 0; }
.filters button { font: inherit; font-size: 14px; background: var(--card); color: var(--fg);
  border: 1px solid var(--line); border-radius: 999px; padding: 4px 12px; cursor: pointer; }
.filters button span { color: var(--muted); margin-left: 4px; }
.filters button[aria-pressed="true"] { border-color: var(--fg); }
.vendors { display: flex; flex-wrap: wrap; gap: 8px; }
.vendors a { background: var(--card); border: 1px solid var(--line); border-radius: 8px; padding: 6px 12px; color: var(--fg); }
.vendors a span { color: var(--muted); margin-left: 4px; }
.flag { background: var(--flag-bg); border: 1px solid var(--flag-line); border-radius: 8px; padding: 12px 14px; margin: 16px 0; }
.sevnote { color: var(--muted); }
.sigs code { display: inline-block; margin: 0 6px 6px 0; }
.idrecs { list-style: none; padding: 0; margin: 16px 0 0; }
.idrecs li { border-top: 1px solid var(--line); padding: 8px 0; }
.idrecs p { margin: 4px 0; }
.cta { margin: 40px 0; padding: 16px; border: 1px solid var(--line); border-radius: 10px; background: var(--card); }
.cta h2 { margin-top: 0; }
.cta pre { margin: 12px 0; padding: 10px 12px; overflow-x: auto; background: var(--bg); border: 1px solid var(--line); border-radius: 6px; }
.cta pre code { border: 0; padding: 0; background: none; overflow-wrap: normal; }
footer.site { color: var(--muted); font-size: 13px; padding-bottom: 40px; }
@media (max-width: 560px) {
  .rec { grid-template-columns: 1fr; gap: 2px; }
  h1 { font-size: 24px; }
}
"""


JS = """(function () {
  var active = null;
  document.querySelectorAll('.filters button').forEach(function (b) {
    b.addEventListener('click', function () {
      active = active === b.dataset.sev ? null : b.dataset.sev;
      document.querySelectorAll('.filters button').forEach(function (x) {
        x.setAttribute('aria-pressed', String(x.dataset.sev === active));
      });
      document.querySelectorAll('li.rec').forEach(function (li) {
        li.hidden = active !== null && li.dataset.sev !== active;
      });
    });
  });
})();
"""

HEADERS = """/*
  Content-Security-Policy: default-src 'none'; style-src 'self'; script-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'
  X-Content-Type-Options: nosniff
  Referrer-Policy: strict-origin-when-cross-origin
  Permissions-Policy: interest-cohort=(), camera=(), microphone=(), geolocation=()
  Strict-Transport-Security: max-age=31536000; includeSubDomains

/feed.xml
  Content-Type: application/atom+xml; charset=utf-8

/feed.json
  Content-Type: application/feed+json; charset=utf-8
"""
