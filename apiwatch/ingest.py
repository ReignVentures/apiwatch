"""Draft ChangeRecords from vendors' model-deprecation pages, for a human to verify.

Reads the Markdown versions of the Anthropic, OpenAI, and Google (Gemini API) deprecation pages,
extracts (model, shutdown date, replacement) rows from their tables, and writes a draft record for
every group of retirements the feed doesn't already cover. Drafts go to feed/drafts/, always
`verified: false`; nothing is published until a person reviews and moves them into feed/records/.

Network is used only to fetch the pages (or pass --from-dir with saved copies). No LLM involved.
"""
from __future__ import annotations

import re
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import yaml

from .attribute import find_in_line
from .models import ChangeRecord

SOURCES = {
    "anthropic": ("Anthropic", "https://platform.claude.com/docs/en/about-claude/model-deprecations",
                  "https://platform.claude.com/docs/en/about-claude/model-deprecations.md"),
    "openai": ("OpenAI", "https://developers.openai.com/api/docs/deprecations",
               "https://developers.openai.com/api/docs/deprecations.md"),
    "google": ("Google", "https://ai.google.dev/gemini-api/docs/deprecations",
               "https://ai.google.dev/gemini-api/docs/deprecations.md.txt"),
}

BREAKING_WITHIN_DAYS = 180


@dataclass
class Retirement:
    vendor: str
    model: str
    shutdown: str                     # ISO date
    replacements: list[str] = field(default_factory=list)
    section: str = ""                 # heading the table sat under, e.g. "2026-04-22: Legacy GPT model snapshots"
    aliases: list[str] = field(default_factory=list)


# ---------- parsing ----------

_DATE_FORMATS = ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d", "%B %d %Y", "%b %d %Y", "%d %B %Y", "%d %b %Y")


def parse_date(text: str) -> str | None:
    t = text.replace("‑", "-").replace("‐", "-").replace("*", "").strip()
    t = re.sub(r"\bSept\b", "Sep", t)
    t = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", t)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(t, fmt).date().isoformat()
        except ValueError:
            pass
    m = re.search(r"\d{4}-\d{2}-\d{2}", t)
    return m.group(0) if m else None


def _cells(row: str) -> list[str]:
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|"):
        row = row[:-1]
    # split on unescaped pipes only; vendors write "`id` \\| `alias`" inside a cell
    return [c.replace("\\|", "|").strip() for c in re.split(r"(?<!\\)\|", row)]


def _ids(cell: str) -> list[str]:
    ticked = re.findall(r"`([^`]+)`", cell)
    if ticked:
        return [t.strip().rstrip("*") for t in ticked if t.strip().rstrip("*")]   # "gpt-4.1*" footnotes
    cell = re.sub(r"<[^>]+>", ",", cell).replace("*", "")
    return [p.strip() for p in re.split(r",| or ", cell) if re.fullmatch(r"[A-Za-z0-9][\w.:\-/]*", p.strip())]


def _is_sep(row: str) -> bool:
    return bool(re.fullmatch(r"\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?", row.strip()))


def parse_markdown(md: str, vendor: str) -> list[Retirement]:
    """Pull retirements out of every table whose header names a model column and a shutdown/retirement date."""
    out: list[Retirement] = []
    lines = md.splitlines()
    section = ""
    i = 0
    while i < len(lines):
        line = lines[i]
        h = re.match(r"^#{2,4}\s+(.*)", line)
        if h:
            section = h.group(1).strip()
        if line.lstrip().startswith("|") and i + 1 < len(lines) and _is_sep(lines[i + 1]):
            header = [c.replace("*", "").lower() for c in _cells(line)]
            model_col = next((k for k, c in enumerate(header) if "model" in c
                              and not any(w in c for w in ("replacement", "substitute", "recommended"))), None)
            date_col = next((k for k, c in enumerate(header) if ("shutdown" in c or "retirement" in c) and "date" in c), None)
            repl_col = next((k for k, c in enumerate(header) if any(w in c for w in ("replacement", "substitute"))), None)
            i += 2
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                if model_col is not None and date_col is not None:
                    cells = _cells(lines[i])
                    if len(cells) > max(model_col, date_col):
                        shutdown = parse_date(cells[date_col])
                        ids = _ids(cells[model_col])
                        if shutdown and ids:
                            repl = _ids(cells[repl_col]) if repl_col is not None and repl_col < len(cells) else []
                            out.append(Retirement(vendor, ids[0], shutdown, repl, section, ids[1:]))
                i += 1
            continue
        i += 1
    return _dedupe(out)


def _dedupe(rs: list[Retirement]) -> list[Retirement]:
    """The same model can appear in a status table and a history table; keep the row with a dated section."""
    best: dict[tuple[str, str], Retirement] = {}
    for r in rs:
        k = (r.model, r.shutdown)
        if k not in best or (re.match(r"\d{4}-\d{2}-\d{2}", r.section) and not re.match(r"\d{4}-\d{2}-\d{2}", best[k].section)):
            if k in best and not r.replacements:
                r.replacements = best[k].replacements
            best[k] = r
    return list(best.values())


# ---------- coverage + drafting ----------

def code_id(model: str) -> str:
    """How an id is written in code. OpenAI's page spells fine-tunes "ft-babbage-002"; the API id is "ft:babbage-002…"."""
    return "ft:" + model[3:] if model.startswith("ft-") else model


def covered(r: Retirement, records: list[ChangeRecord]) -> bool:
    for rec in records:
        if rec.vendor != r.vendor or rec.effective != r.shutdown:
            continue
        if any(find_in_line(code_id(m), s) >= 0 for m in [r.model, *r.aliases] for s in rec.signatures):
            return True
    return False


def _slug(text: str, limit: int = 48) -> str:
    s = re.sub(r"^\d{4}-\d{2}-\d{2}:\s*", "", text).lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:limit].rstrip("-") or "model-retirements"


def _announced(section: str) -> str | None:
    m = re.match(r"(\d{4}-\d{2}-\d{2})", section)
    return m.group(1) if m else None


def draft_records(retirements: list[Retirement], records: list[ChangeRecord], since: str,
                  source_url: str, today: date) -> list[dict]:
    groups: dict[tuple, list[Retirement]] = defaultdict(list)
    for r in retirements:
        if r.shutdown < since or covered(r, records):
            continue
        groups[(r.vendor, r.shutdown, r.section)].append(r)
    drafts = []
    for (vendor, shutdown, section), rs in sorted(groups.items(), key=lambda kv: kv[0][1]):
        models = [r.model for r in rs]
        names = ", ".join(models[:4]) + (f" and {len(models) - 4} more" if len(models) > 4 else "")
        past = shutdown <= today.isoformat()
        ann = _announced(section)
        repl_lines = [f"{r.model} → {' or '.join(r.replacements)}" if r.replacements else f"{r.model} → (no replacement listed)"
                      for r in rs]
        aliases = sorted({a for r in rs for a in r.aliases})
        detail = (f"{'Announced ' + ann + '. ' if ann else ''}{vendor}'s deprecations page lists {len(rs)} "
                  f"model{'s' if len(rs) != 1 else ''} with a shutdown date of {shutdown}. "
                  + ("Also covers aliases: " + ", ".join(aliases) + ". " if aliases else "")
                  + "Replacements: " + "; ".join(repl_lines) + ".")
        vslug = re.sub(r"[^a-z0-9]+", "-", vendor.lower()).strip("-")
        drafts.append({
            "id": f"{vslug}-{shutdown}-{_slug(section) if section else 'model-retirements'}",
            "vendor": vendor,
            "surface": ("model " if len(models) == 1 else "models ") + ", ".join(models),
            "kind": "model",
            # breaking once it's close enough to act on; further out it's a scheduled deprecation
            "severity": "breaking" if (date.fromisoformat(shutdown) - today).days <= BREAKING_WITHIN_DAYS else "deprecation",
            "effective": shutdown,
            "summary": f"{vendor} {'retired' if past else 'retires'} {names} on {shutdown}",
            "detail": detail,
            "signatures": sorted({code_id(m) for m in [*models, *aliases]}, key=lambda s: (len(s), s)),
            "source_url": source_url,
            "verified": False,
            "fix_hint": "Switch to the listed replacement and re-run your evals; move model ids to config so the next retirement is a one-line change.",
        })
    return drafts


def fetch(url: str, timeout: float = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "apiwatch-ingest/0.1 (+https://github.com/ReignVentures/APIWatch)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def run(records_dir: Path, out_dir: Path, sources: list[str], since: str, today: date | None = None,
        from_dir: Path | None = None, force: bool = False) -> list[tuple[str, dict, bool]]:
    """Returns (source, draft, written) for each draft. Existing draft files are kept unless force."""
    from .models import load_records
    today = today or date.today()
    records = load_records(records_dir)
    results = []
    out_dir.mkdir(parents=True, exist_ok=True)
    for key in sources:
        vendor, page_url, md_url = SOURCES[key]
        md = (from_dir / f"{key}.md").read_text() if from_dir else fetch(md_url)
        for d in draft_records(parse_markdown(md, vendor), records, since, page_url, today):
            path = out_dir / f"{d['id']}.yml"
            ChangeRecord(**d)   # validate before writing
            written = force or not path.exists()
            if written:
                header = (f"# DRAFT generated by `apiwatch ingest` from {page_url} on {today.isoformat()}.\n"
                          f"# verified: false until a person checks the source. Review, edit the summary, then move to feed/records/.\n")
                path.write_text(header + yaml.safe_dump(d, sort_keys=False, allow_unicode=True, width=120))
            results.append((key, d, written))
    return results
