"""Watch vendor changelogs for changes that might need a feed record.

Fetches each changelog (Markdown, MDX, HTML, or the GitHub releases API), splits it into entries with a
date, title and link, and flags entries whose text mentions a breaking change, removal, rename,
retirement, or migration. Output is *leads*: prompts to research, never records. A lead becomes a
record only by being written up from the primary source and passing docs/REVIEW.md.

Entries already shown are remembered in a seen file, so each run lists what's new. Network only; no LLM.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin

from .attribute import context_matches, find_in_line
from .ingest import parse_date
from .models import ChangeRecord, load_records

# key -> (vendor as used in records, url, kind). kind: "doc" (Markdown/MDX/HTML) or "github" (releases API)
SOURCES: dict[str, tuple[str, str, str]] = {
    "anthropic": ("Anthropic", "https://platform.claude.com/docs/en/release-notes/overview.md", "doc"),
    "openai": ("OpenAI", "https://developers.openai.com/api/docs/changelog.md", "doc"),
    "google": ("Google", "https://ai.google.dev/gemini-api/docs/changelog.md.txt", "doc"),
    "xai": ("xAI", "https://docs.x.ai/developers/release-notes", "doc"),
    "notion": ("Notion", "https://developers.notion.com/page/changelog.md", "doc"),
    "atlassian-rovo-mcp": ("Atlassian Rovo MCP", "https://developer.atlassian.com/cloud/rovo-mcp/changelog/", "doc"),
    "stripe": ("Stripe", "https://docs.stripe.com/changelog", "doc"),
    "twilio": ("Twilio", "https://www.twilio.com/en-us/changelog", "doc"),
    "github-mcp-server": ("GitHub MCP Server", "github/github-mcp-server", "github"),
    "mcp-spec": ("MCP specification", "modelcontextprotocol/modelcontextprotocol", "github"),
    "stripe-python": ("Stripe", "stripe/stripe-python", "github"),
    "stripe-node": ("Stripe", "stripe/stripe-node", "github"),
}

# sources where every entry is a lead: a new MCP spec revision always matters, whatever its notes say
ALWAYS = {"mcp-spec"}

# Strong words make a lead on their own; weak ones need a second, different weak word. Letters only, so
# identifiers like remove_issue_reaction or redirect_to_url don't count.
STRONG = re.compile(r"(?i)\b(?:(?<!non-)breaking|(?<!not )deprecat[a-z]*|remov(?:ed|es|al|ing)|retir[a-z]*|"
                    r"renam[a-z]*|sunset[a-z]*|no longer|end[- ]of[- ]life|shut(?:ting)? ?down|discontinu[a-z]*|"
                    r"will default|default[a-z]* (?:will )?change[a-z]*|will be served by)\b(?!_)")
WEAK = re.compile(r"(?i)\b(?:remove|migrat[a-z]*|redirect[a-z]*|replaced by|now requires?|defaults? to|"
                  # silent changes read like "previously returned X; now drops Y instead of failing"
                  r"drop(?:s|ped)?|instead of|previously|now returns?)\b(?!_)")


_MONTHS = "january february march april may june july august september october november december".split()
_MON = "|".join(m[:3] for m in _MONTHS)
# the date part of a heading, in the forms changelogs use; whatever is left decides if it's a date heading
_DATE_TOKEN = re.compile(r"(?i)\b(?:\d{4}-\d{2}-\d{2}|(?:%s)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}|"
                         r"\d{1,2}\s+(?:%s)[a-z]*\.?,?\s+\d{4}|(?:%s)[a-z]*\.?,?\s+\d{4}|(?:%s)[a-z]*\.?\s+\d{1,2}|"
                         r"(?:%s)[a-z]*|\d{4})\b" % (_MON, _MON, _MON, _MON, _MON))


@dataclass
class Entry:
    source: str
    vendor: str
    date: str            # ISO date, "YYYY-MM" when the changelog only gives a month, or "" if undated
    title: str
    url: str
    text: str
    flags: list[str] = field(default_factory=list)
    related: list[str] = field(default_factory=list)   # records this entry may already be about

    @property
    def key(self) -> str:
        # ignores url (a changed doc link shouldn't resurface an entry); title + opening text tell entries apart
        basis = re.sub(r"\W+", " ", f"{self.title} | {self.text[:200]}").lower().strip()
        return f"{self.source}|{self.date}|{hashlib.sha1(basis.encode()).hexdigest()[:16]}"


# ---------- document → markdown-ish lines ----------

def _tags_out(s: str) -> str:
    """Drop tags, keep entities escaped (the one unescape happens at the end, so &lt;id&gt; survives)."""
    return re.sub(r"\s+", " ", re.sub(r"<[^<>]{0,2000}>", " ", s)).strip()


def _html_to_md(text: str) -> str:
    t = re.sub(r"<(script|style|svg|noscript)\b.{0,200000}?</\1>", " ", text, flags=re.S | re.I)
    t = re.sub(r"<a\b[^>]{0,2000}?href=\"([^\"]+)\"[^>]{0,2000}>(.{0,20000}?)</a>",
               lambda m: f"[{_tags_out(m.group(2))}]({m.group(1)})", t, flags=re.S | re.I)
    t = re.sub(r"<h([1-4])\b[^>]{0,2000}>(.{0,20000}?)</h\1>",
               lambda m: f"\n{'#' * int(m.group(1))} {_tags_out(m.group(2))}\n", t, flags=re.S | re.I)
    t = re.sub(r"<li\b[^>]{0,2000}>", "\n- ", t, flags=re.I)
    t = re.sub(r"<(?:br|/p|/div|/li|/tr)\b[^>]{0,2000}>", "\n", t, flags=re.I)
    t = re.sub(r"<[^<>]{0,2000}>", "", t)
    return html.unescape(t)


def to_markdown(text: str) -> str:
    if re.search(r"<(?:html|body|h[1-4])\b", text[:200000], re.I):
        text = _html_to_md(text)
    # MDX changelogs: <Update label="September 2, 2026"> … </Update>
    text = re.sub(r'<Update\b[^>]*\blabel="([^"]+)"[^>]*>', r"\n# \1\n", text)   # a level above the entry's own headings
    return re.sub(r"</?Update\b[^>]*>", "", text)


# ---------- dates ----------

def _heading_date(h: str, ctx_year: int | None, today: date) -> tuple[str, int | None, int | None, bool]:
    """(date, year, month, is_date_heading). A date heading is one that is (nearly) only a date:
    "September 2, 2026", "2026-08-26.dahlia", "Sep 22" under a year, "September", "v2.0 (2026-07-01)".
    "Deprecating widget-3 on 2026-11-02" is not: it keeps its words and inherits the section's date."""
    clean = re.sub(r"[*`#\[\]]", "", h).strip()
    rest = _DATE_TOKEN.sub(" ", clean)
    if len(re.findall(r"[A-Za-z0-9][\w.]*", rest)) > 1 or clean == rest.strip():
        return "", None, None, False
    if re.fullmatch(r"\d{4}", clean):                             # "## 2026": a year for the day headings below
        return "", int(clean), None, False
    full = parse_date(re.sub(r"(?i)\b([a-z]{3,9})\.", r"\1", _DATE_TOKEN.search(clean).group(0)))
    if full:
        d = date.fromisoformat(full)
        return full, d.year, d.month, True
    months = [m[:3] for m in _MONTHS]
    if m := re.search(r"(?i)\b(%s)[a-z]*\.?,?\s+(\d{4})\b" % _MON, clean):          # "September, 2026"
        mo = months.index(m.group(1).lower()[:3]) + 1
        return f"{m.group(2)}-{mo:02d}", int(m.group(2)), mo, True
    if (m := re.search(r"(?i)\b(%s)[a-z]*\.?\s+(\d{1,2})\b" % _MON, clean)) and ctx_year:   # "Sep 22" under a year
        mo = months.index(m.group(1).lower()[:3]) + 1
        return f"{ctx_year}-{mo:02d}-{int(m.group(2)):02d}", ctx_year, mo, True
    if m := re.search(r"(?i)\b(%s)\b" % "|".join(_MONTHS), clean):   # "September": the latest one not after today
        mo = _MONTHS.index(m.group(1).lower()) + 1
        yr = ctx_year or (today.year if mo <= today.month else today.year - 1)
        return f"{yr}-{mo:02d}", yr, mo, True
    return "", None, None, False


def _line_date(line: str) -> str:
    """A line that is only a date, e.g. Twilio's "Sep 23, 2026" printed above each entry."""
    s = line.strip()
    if not s or len(s) > 30:
        return ""
    return parse_date(re.sub(r"(?i)\b([a-z]{3,9})\.", r"\1", s)) or ""


# ---------- split into entries ----------

_LINK = re.compile(r"\[([^\]]{1,300})\]\(([^)\s]{1,2000})\)")
_HEAD = re.compile(r"^(#{1,4})\s+(.*\S)")


def _plain(s: str) -> str:
    s = _LINK.sub(r"\1", s)
    s = re.sub(r"(?m)^\s*(?:>\s?)+", "", s)                      # blockquote markers, not a ">" in "<id>"
    s = re.sub(r"^\s*[-*]\s+", "", s)
    return re.sub(r"\s+", " ", re.sub(r"[*`#|]", " ", s)).strip()      # keep "_": it's part of identifiers


def _abs(url: str, base: str) -> str:
    return urljoin(base, url)


def parse_doc(text: str, source: str, vendor: str, url: str, today: date) -> list[Entry]:
    lines = to_markdown(text).splitlines()
    heads = []
    for i, l in enumerate(lines):
        if m := _HEAD.match(l):
            heads.append((i, len(m.group(1)), m.group(2).rstrip("#").strip()))
    out: list[Entry] = []
    ctx: dict[int, tuple[str, int | None]] = {}      # level -> (date, year) of the open heading
    for n, (i, level, h) in enumerate(heads):
        ctx = {k: v for k, v in ctx.items() if k < level}
        parent = max(ctx) if ctx else None
        pdate, pyear = ctx[parent] if parent else ("", None)
        d, y, _, is_date = _heading_date(h, pyear, today)
        if not d:
            d = pdate
        if not d:                                   # a date line just above the heading (Twilio's index)
            prev = [l for l in lines[max(0, i - 30):i] if l.strip()][-3:]
            d = next((x for x in map(_line_date, reversed(prev)) if x), "")
        ctx[level] = (d, y or pyear)
        end = heads[n + 1][0] if n + 1 < len(heads) else len(lines)
        body = lines[i + 1:end]
        if not "".join(body).strip():
            continue
        head_link = _LINK.search(h)
        links = [m for l in body for m in _LINK.finditer(l)]
        # an entry's own page: a "Learn more" / changelog link beats the first doc link in the text
        body_link = next((m for m in links if re.search(r"(?i)learn more|read more|changelog", m.group(0))),
                         links[0] if links else None)
        link = _abs((head_link or body_link).group(2), url) if (head_link or body_link) else url
        out += _entries_in_section(body, source, vendor, d, None if is_date else _plain(h), h, link, url)
    if any(e.date for e in out):          # a dated changelog: undated bits are page chrome, not changes
        out = [e for e in out if e.date]
    return out


def _entries_in_section(body, source, vendor, d, title, heading, link, base) -> list[Entry]:
    rows = [l for l in body if l.lstrip().startswith("|") and _LINK.search(l)]
    if rows:                                                     # a table of linked changes (Stripe)
        out = []
        for r in rows:
            m = _LINK.search(r)
            out.append(Entry(source, vendor, d, _plain(m.group(1)), _abs(m.group(2), base), _plain(r)))
        return out
    bullets, loose, cur = [], [], None
    for l in body:
        if re.match(r"^[-*] ", l):
            cur = [l[2:]]
            bullets.append(cur)
        elif cur is not None and (l.startswith((" ", "\t")) or not l.strip()):
            cur.append(l)
        else:
            cur = None
            loose.append(l)
    if title is None and len(bullets) >= 2:                      # a dated section holding several changes
        out = []
        for b in bullets:
            bt = "\n".join(b)
            m = _LINK.search(bt)
            out.append(Entry(source, vendor, d, _plain(bt)[:140], _abs(m.group(2), base) if m else base, _plain(bt)))
        if _plain("\n".join(loose)):                             # prose next to the bullets is a change too
            prose = _plain("\n".join(loose))
            out.insert(0, Entry(source, vendor, d, prose[:140], link, prose))
        return out
    plain = _plain("\n".join(body))
    if not plain:
        return []
    if title is None:
        # the heading was (nearly) just a date; keep any leftover words ("v2.0") in the text that gets flagged
        extra = _plain(_DATE_TOKEN.sub(" ", heading))
        return [Entry(source, vendor, d, plain[:140], link, f"{extra} {plain}".strip())]
    return [Entry(source, vendor, d, title, link, f"{title}. {plain}")]


def parse_github(data: str, source: str, vendor: str) -> list[Entry]:
    return [Entry(source, vendor, (r.get("published_at") or "")[:10], r.get("name") or r.get("tag_name") or "",
                  r.get("html_url") or "", _plain(r.get("body") or ""))
            for r in json.loads(data) if not r.get("draft") and not r.get("prerelease")]


# ---------- flagging and coverage ----------

def annotate(entries: list[Entry], records: list[ChangeRecord], page_url: str = "") -> list[Entry]:
    page = page_url.split("#")[0]
    cited: dict[str, int] = {}
    for r in records:
        for u in {u.split("#")[0] for u in (r.source_url, *(u for u, _ in r.evidence_items()))}:
            cited[u] = cited.get(u, 0) + 1
    for e in entries:
        blob = f"{e.title}\n{e.text}"
        strong = {m.group(0).lower() for m in STRONG.finditer(blob)}
        weak = {m.group(0).lower() for m in WEAK.finditer(blob)}
        e.flags = sorted(strong | weak) if (strong or len(weak) >= 2) else []
        if e.source in ALWAYS and not e.flags:
            e.flags = ["new release"]
        own = e.url.split("#")[0]
        # the changelog page, or an index page many records cite (a deprecations table), says nothing about which entry
        own = "" if own == page or cited.get(own, 0) > 2 else own
        low = blob.lower()
        for r in records:
            if not (r.vendor == e.vendor or r.vendor.startswith(e.vendor) or e.vendor.startswith(r.vendor)):
                continue
            rurls = {u.split("#")[0] for u in (r.source_url, *(u for u, _ in r.evidence_items()))}
            ctx_ok = not r.context or context_matches(low, r.context)
            by_sig = ctx_ok and any(len(s) >= 6 and find_in_line(blob, s) >= 0
                                    and not any(find_in_line(blob, x) >= 0 and s in x for x in r.exclude)
                                    for s in r.signatures)
            if (own and own in rurls) or by_sig:
                e.related.append(r.id)
    return entries


# ---------- run ----------

def fetch_source(key: str, get: Callable[[str], str], today: date) -> list[Entry]:
    vendor, where, kind = SOURCES[key]
    if kind == "github":
        return parse_github(get(f"https://api.github.com/repos/{where}/releases?per_page=30"), key, vendor)
    return parse_doc(get(where), key, vendor, where, today)


def run(records_dir: Path, sources: list[str], seen_path: Path | None, since: str, today: date,
        get: Callable[[str], str] | None = None, include_all: bool = False) -> tuple[list[Entry], list[str]]:
    date.fromisoformat(since)                      # "2026-7-1" would compare wrongly as a string
    """Returns (leads, errors). A lead is a flagged entry (or any entry with include_all) that is dated on
    or after `since` (undated entries always count) and not in the seen file."""
    from .check import fetch
    get = get or fetch
    records = load_records(records_dir)
    seen = set(json.loads(seen_path.read_text())) if seen_path and seen_path.exists() else set()
    leads, errors = [], []
    for key in sources:
        try:
            entries = annotate(fetch_source(key, get, today), records, SOURCES[key][1])
        except Exception as e:                    # one broken changelog shouldn't stop the others
            errors.append(f"{key}: {type(e).__name__}: {e}")
            continue
        if not entries:                           # a redesigned page parses to nothing: say so, don't go quiet
            errors.append(f"{key}: parsed 0 entries (page layout changed?)")
            continue
        for e in entries:
            if e.key in seen or (e.date and e.date < since[:len(e.date)]):
                continue
            if include_all or e.flags:
                leads.append(e)
    return leads, errors


def mark_seen(leads: list[Entry], seen_path: Path) -> None:
    seen = set(json.loads(seen_path.read_text())) if seen_path.exists() else set()
    seen |= {e.key for e in leads}
    seen_path.parent.mkdir(parents=True, exist_ok=True)
    seen_path.write_text(json.dumps(sorted(seen), indent=0) + "\n")


def write_json(leads: list[Entry], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(e) | {"key": e.key} for e in leads], indent=2))
