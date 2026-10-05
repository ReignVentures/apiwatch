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
CTA = ("Does this touch your code? Run <code>apiwatch scan &lt;repo&gt;</code>. It finds the lines "
       "this change lands on and writes a fix brief.")


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
    (out / "style.css").write_text(CSS)
    (out / "site.js").write_text(JS)
    (out / "_headers").write_text(HEADERS)   # Cloudflare Pages response headers

    by_vendor: dict[str, list[ChangeRecord]] = defaultdict(list)
    for r in recs:
        by_vendor[r.vendor].append(r)

    # canonical URLs are the clean paths Cloudflare Pages serves (it 308-redirects "x.html" to "x"), on base_url
    (out / "index.html").write_text(_canonical(_index(recs, by_vendor, today), base, ""))
    for r in recs:
        (out / "records" / f"{r.id}.html").write_text(_canonical(_record_page(r, today), base, f"records/{r.id}"))
    for vendor, rs in by_vendor.items():
        (out / "vendors" / f"{vendor_slug(vendor)}.html").write_text(_canonical(
            _page(f"{vendor} changes", _vendor_body(vendor, rs, today), depth=1), base, f"vendors/{vendor_slug(vendor)}"))
    # served for any unknown path at any depth, so links are root-absolute. Without it Cloudflare Pages
    # treats the site as a SPA and answers every unknown path with index.html and a 200
    (out / "404.html").write_text(_page("Not found", '<h1>Not found</h1><p>No page here. '
                                        '<a href="/index.html">See all changes</a>.</p>', up="/"))
    (out / "feed.json").write_text(_json_feed(recs, base))
    (out / "feed.xml").write_text(_atom(recs, base, today))
    (out / "robots.txt").write_text("User-agent: *\nAllow: /\n" + (f"Sitemap: {base}sitemap.xml\n" if base else ""))
    if base:
        (out / "sitemap.xml").write_text(_sitemap(recs, by_vendor, base, today))
    return len(recs), out / "index.html"


def _canonical(html: str, base: str, path: str) -> str:
    if not base:
        return html
    return html.replace("</head>", f'<link rel="canonical" href="{escape(base + path)}">\n</head>', 1)


def _sitemap(recs, by_vendor, base, today) -> str:
    urls = [(base, today.isoformat())]
    urls += [(f"{base}vendors/{vendor_slug(v)}", max(r.verified_on or r.effective for r in rs)) for v, rs in by_vendor.items()]
    urls += [(f"{base}records/{r.id}", r.verified_on or r.effective) for r in recs]
    body = "".join(f"<url><loc>{xesc(u)}</loc><lastmod>{d}</lastmod></url>" for u, d in urls)
    return f'<?xml version="1.0" encoding="utf-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</urlset>\n'


# ---------- pages ----------

def _page(title: str, body: str, depth: int = 0, up: str | None = None) -> str:
    up = "../" * depth if up is None else up
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)} · {SITE_TITLE}</title>
<meta name="description" content="{escape(TAGLINE)}">
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
<aside class="cta">{CTA}</aside>
<script src="site.js" defer></script>"""
    return _page("API & MCP change feed", body)


def _vendor_body(vendor, rs, today) -> str:
    return (f'<p class="crumb"><a href="../index.html">All changes</a></p><h1>{escape(vendor)}</h1>'
            f'<p class="meta">{len(rs)} change{"s" if len(rs) != 1 else ""}</p>'
            + _split(rs, today, 1) + f'<aside class="cta">{CTA}</aside>')


def _record_page(r: ChangeRecord, today: date) -> str:
    when = "Takes effect" if r.effective > today.isoformat() else "Took effect"
    if r.human_verified:
        flag = f'<p class="meta">Verified against the source by {escape(r.verified_by)} on {_fmt(r.verified_on)}.</p>'
    else:
        flag = (f'<p class="meta checked">Reviewed against the vendor source on {_fmt(r.verified_on)} (AI-assisted review). '
                'Key facts are re-checked against the source daily.</p>')
    sigs = "".join(f"<code>{escape(s)}</code>" for s in r.signatures) or "<span>None recorded</span>"
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
<aside class="cta">{CTA}</aside>"""
    return _page(r.summary, body, depth=1)


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
.cta { margin: 40px 0; padding: 16px; border: 1px solid var(--line); border-radius: 10px; background: var(--card); }
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
