"""Source check: confirm each record's evidence still appears on the vendor's primary source.

Every published record carries `evidence`, short verbatim facts (model ids, dates, tool names) that
support it. This fetches each source page and looks for them. It's the automated half of the review in
docs/REVIEW.md; the other half is an independent read of the source. Run daily in CI: when a vendor
edits or removes a page, the record stops passing and comes off the site until it's reviewed again.

Network only; no LLM.
"""
from __future__ import annotations

import html
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from .models import ChangeRecord, load_records

# docs sites that render in the browser; their Markdown twins carry the same text
_MD_TWINS = (
    ("https://platform.claude.com/docs/", ".md"),
    ("https://docs.claude.com/", ".md"),
    ("https://developers.openai.com/", ".md"),
    ("https://ai.google.dev/", ".md.txt"),
)
MAX_BYTES = 10_000_000


@dataclass
class CheckResult:
    id: str
    status: str                                   # ok | missing | error | no_evidence
    missing: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


_TEXT_ATTRS = re.compile(r'\b(?:label|title|alt|aria-label|datetime)\s*=\s*"([^"]{0,500})"')
_INLINE = {"a", "abbr", "b", "code", "em", "i", "kbd", "mark", "s", "small", "span", "strong", "sub", "sup", "u", "var", "wbr"}
_TAG = re.compile(r"<(/?)([A-Za-z][\w:-]{0,40})?[^<>]{0,2000}>")       # bounded: no backtracking blowup on stray '<'
_MD_LINK = re.compile(r'!?\[([^\[\]]{0,500})\]\([^()\s]{0,2000}(?:\s+"[^"]{0,300}")?\)')
_MD_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|<>])")
_ZERO_WIDTH = re.compile("[​‌‍⁠﻿­]")
_SPACES = re.compile("[    ]")


def _tag_text(m: re.Match) -> str:
    # docs written in MDX keep visible text in attributes, e.g. <Update label="September 2, 2026">
    attrs = " ".join(_TEXT_ATTRS.findall(m.group(0)))
    if (m.group(2) or "").lower() in _INLINE:
        return f" {attrs} " if attrs else ""     # inline markup inside a word: <code>gpt</code>-4 → gpt-4
    return f" {attrs} "


def normalize(text: str) -> str:
    t = re.sub(r"<style\b.*?</style>", " ", text, flags=re.S | re.I)   # inline CSS isn't page text
    t = html.unescape(_TAG.sub(_tag_text, t))
    t = _MD_LINK.sub(r"\1", t)                                          # Markdown links and images keep their text
    t = _MD_ESCAPE.sub(r"\1", t)
    t = _ZERO_WIDTH.sub("", t)
    t = _SPACES.sub(" ", t)
    t = re.sub("[‐‑‒–—−]", "-", t)
    t = (t.replace("`", "").replace("*", "").replace("’", "'")
          .replace("“", '"').replace("”", '"'))
    return re.sub(r"\s+", " ", t).strip().lower()


class PageGone(OSError):
    """404 or 410."""


def fetch(url: str, timeout: float = 30) -> str:
    headers = {"User-Agent": "apiwatch-check/0.1 (+https://apiwatch.reignventures.co)",
               "Accept": "text/markdown, text/html;q=0.9, */*;q=0.8"}
    if url.startswith("https://api.github.com/") and os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"   # shared runners hit the anonymous limit
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
            body = r.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            raise PageGone(f"HTTP {e.code}") from e
        raise
    if len(body) > MAX_BYTES:
        raise OSError(f"page larger than {MAX_BYTES} bytes")
    return body.decode("utf-8", errors="replace")


_GITHUB_BLOB = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/blob/(.+?)(?:[?#].*)?$")


def _candidates(url: str) -> list[str]:
    # a file on github.com: the raw copy has the same text and doesn't rate-limit or 503 like the HTML view
    if m := _GITHUB_BLOB.match(url):
        return [f"https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}/{m.group(3)}", url]
    for prefix, suffix in _MD_TWINS:
        if url.startswith(prefix) and not url.endswith((".md", ".txt")):
            return [url.split("#")[0] + suffix, url]
    return [url]


@dataclass
class Page:
    text: str = ""
    errors: list[str] = field(default_factory=list)   # failures other than 404/410 (timeouts, 5xx, 403, bad data)
    gone: bool = False                                  # every candidate URL answered 404/410


def page(url: str, get: Callable[[str], str], cache: dict, sleep: Callable[[float], None] = time.sleep) -> Page:
    """Normalized text of the page and its Markdown twin, if any. Never raises."""
    if url in cache:
        return cache[url]
    texts, errors, gone = [], [], 0
    cands = _candidates(url)
    for u in cands:
        for attempt in range(3):
            try:
                texts.append(normalize(get(u)))
                break
            except PageGone:
                gone += 1
                break
            except Exception as e:            # a bad page or URL is a result for this record, never a crash for the run
                if attempt == 2:
                    errors.append(f"{u}: {type(e).__name__}: {e}")
                else:
                    sleep(2 * (attempt + 1))
    cache[url] = Page(" \n ".join(texts), errors, gone == len(cands))
    return cache[url]


def check_record(rec: ChangeRecord, get: Callable[[str], str] | None = None, cache: dict | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> CheckResult:
    get = get or fetch
    cache = {} if cache is None else cache
    items = rec.evidence_items()
    if not items:
        return CheckResult(rec.id, "no_evidence")
    missing, errors = [], []
    for url, text in items:
        p = page(url, get, cache, sleep)
        label = text if url == rec.source_url else f"{text} ({url})"
        if p.text and normalize(text) in p.text:
            continue
        if p.gone:
            missing.append(f"{label}: source page gone")
        elif p.errors:
            errors.extend(e for e in p.errors if e not in errors)   # can't tell: part of the page didn't load
        else:
            missing.append(label)
    status = "missing" if missing else ("error" if errors else "ok")
    return CheckResult(rec.id, status, missing, errors)


def run(records_dir: Path, get: Callable[[str], str] | None = None, only: list[str] | None = None,
        sleep: Callable[[float], None] = time.sleep) -> list[CheckResult]:
    cache: dict = {}
    return [check_record(r, get, cache, sleep) for r in load_records(records_dir)
            if not r.illustrative and (not only or r.id in only)]


def write_json(results: list[CheckResult], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(r) for r in results], indent=2))


def failed_ids(path: Path) -> set[str]:
    """Records whose evidence is gone from a page that loaded, or whose source page is gone (404/410).
    Other fetch errors don't count: a flaky vendor site shouldn't pull a record for a day."""
    return {r["id"] for r in json.loads(path.read_text()) if r["status"] == "missing"}
