"""Machine-readable scan output: SARIF 2.1.0 (GitHub code scanning annotations) and plain JSON."""
from __future__ import annotations

import html
import json
from datetime import date
from urllib.parse import quote
from dataclasses import asdict

from . import __version__ as VERSION
from .models import CallSite, ChangeRecord

LEVEL = {"breaking": "error", "silent": "warning", "deprecation": "warning", "additive": "note", "info": "note"}


def _trust(r: ChangeRecord) -> str:
    if r.human_verified:
        return f"verified by {r.verified_by}"
    return f"reviewed against source {r.verified_on} (AI-assisted)" if r.verified else "unreviewed draft: confirm before acting"


def sarif(results: list[tuple[ChangeRecord, list[CallSite]]]) -> str:
    rules, out = [], []
    for r, sites in results:
        rules.append({
            "id": r.id,
            "name": r.id.replace("-", "_"),
            "shortDescription": {"text": f"{r.vendor}: {r.summary}"[:1000]},
            "fullDescription": {"text": (r.detail or r.summary)[:4000]},
            "help": {"text": f"{r.fix_hint or 'See source.'}\n\nSource: {r.source_url or 'n/a'} ({_trust(r)})"},
            **({"helpUri": r.source_url} if r.source_url.startswith(("https://", "http://")) else {}),
            "defaultConfiguration": {"level": LEVEL[r.severity]},
            "properties": {"tags": [r.vendor, r.kind, r.severity], "effective": r.effective},
        })
        for s in sites:
            how = f"uses {s.via}, which holds `{s.signature}`" if s.via else f"`{s.signature}`"
            out.append({
                "ruleId": r.id,
                # an entry in a model list or enum is real but not a request; keep it visible without failing review
                "level": LEVEL[r.severity] if s.tier == "use" else "note",
                "message": {"text": f"{r.vendor} {r.severity} change effective {r.effective}: {r.summary} ({how})"
                                    + ("" if s.tier == "use" else " [listed in a table or enum, not a request]")},
                "locations": [{"physicalLocation": {
                    "artifactLocation": {"uri": s.file.replace("\\", "/")},
                    "region": {"startLine": s.line}}}],
                "partialFingerprints": {"apiwatch/v1": f"{r.id}:{s.file}:{s.signature}:{s.snippet}"},
                "properties": {"inTest": s.in_test, "tier": s.tier},
            })
    doc = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{"tool": {"driver": {"name": "apiwatch", "version": VERSION,
                                      "informationUri": "https://github.com/ReignVentures/APIWatch", "rules": rules}},
                  "results": out}],
    }
    return json.dumps(doc, indent=2)


def as_json(results: list[tuple[ChangeRecord, list[CallSite]]]) -> str:
    return json.dumps({"records": [
        {"id": r.id, "vendor": r.vendor, "severity": r.severity, "effective": r.effective, "summary": r.summary,
         "source_url": r.source_url, "trust": _trust(r),
         "sites": [asdict(s) | {"in_test": s.in_test} for s in sites]}
        for r, sites in results]}, indent=2)


_ICON = {"breaking": "🔴", "silent": "🟠", "deprecation": "🟡", "additive": "🔵", "info": "⚪"}
_RANK = {"breaking": 0, "silent": 1, "deprecation": 2, "additive": 3, "info": 4}
MAX_CHARS = 60_000          # GitHub rejects comments over 65,536 characters


def marker(key: str = "") -> str:
    """First line of every report; the action finds its own PR comment by it (one per scanned path)."""
    return f"<!-- apiwatch{':' + key if key else ''} -->"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _safe_path(path: str) -> str:
    """File paths come from the scanned repo (untrusted on fork PRs): no control characters, and HTML-escaped
    inside <code>, so a crafted name can't break out of the link or inject markdown."""
    clean = "".join(ch if ch.isprintable() else "?" for ch in path)
    return html.escape(clean, quote=True)


def markdown(results: list[tuple[ChangeRecord, list[CallSite]]], checked: int, link_base: str = "",
             per_change: int = 5, reviewed: int | None = None, human: int = 0, key: str = "",
             today: date | None = None, scope: str = "", max_chars: int = MAX_CHARS) -> str:
    """A short report for a CI job summary or a PR comment: which changes touch this code, where."""
    today = today or date.today()
    hits = sum(len(sites) for _, sites in results)
    head = [marker(key), f"<!-- apiwatch-hits: {hits} -->"]
    where = f" in {scope}" if scope else ""
    if not results:
        return "\n".join(head + [f"### apiwatch: no known API or model change touches this code{where}", "",
                                  f"{checked} change records checked.", ""])

    def split(sites):
        return ([s for s in sites if not s.in_test and s.tier == "use"],
                [s for s in sites if not s.in_test and s.tier != "use"], [s for s in sites if s.in_test])

    # most severe first; within a severity, changes that touch real code before test-only ones
    results = sorted(results, key=lambda rs: (_RANK[rs[0].severity], -len(split(rs[1])[0]), -len(split(rs[1])[1]),
                                               rs[0].effective))

    def loc(s: CallSite) -> str:
        text = _safe_path(f"{s.file}:{s.line}") + (f" ({_safe_path(s.cell)})" if s.cell else "")
        if not link_base:
            return f"<code>{text}</code>"
        plain = "?plain=1" if s.file.endswith((".ipynb", ".md")) else ""
        return f'<a href="{html.escape(link_base + quote(s.file.replace(chr(92), "/")), quote=True)}{plain}#L{s.line}"><code>{text}</code></a>'

    rows, details = [], []
    for r, sites in results:
        use, listed, tests = split(sites)
        src = f" ([source]({r.source_url}))" if r.source_url.startswith(("https://", "http://")) else ""
        when = "in effect" if r.effective <= today.isoformat() else "coming"
        rows.append(f"| {_ICON[r.severity]} {r.severity} | **{_cell(r.vendor)}**: {_cell(r.summary)}{src} | {r.effective} ({when}) "
                    f"| {len(use)} | {len(listed)} | {len(tests)} |")
        details.append((r, use + listed + tests))

    def render_details(n: int) -> list[str]:
        out = []
        for r, ordered in details:
            shown = ordered[:n]
            more = len(ordered) - len(shown)
            out.append(f"**{_cell(r.vendor)}: {_cell(r.summary)}**\n"
                       + "\n".join(f"- {loc(s)}" + (" (listed)" if s.tier != "use" and not s.in_test else "")
                                   + (" (test)" if s.in_test else "") for s in shown)
                       + (f"\n- …and {more} more (in the fix briefs)" if more else "")
                       + (f"\n\nFix: {r.fix_hint}" if r.fix_hint else ""))
        return ["<details><summary>Where each change lands</summary>", "", "\n\n".join(out), "", "</details>", ""]

    in_code = sum(1 for r, sites in results if r.severity in ("breaking", "silent") and any(not s.in_test for s in sites))
    n = len(results)
    if reviewed is None or not reviewed:
        trust = ""
    elif human:
        trust = f", {reviewed} of them reviewed against the vendor's primary source ({human} by a person)"
    else:
        trust = f", {reviewed} of them reviewed against the vendor's primary source (AI-assisted)"
    top = head + [
        f"### apiwatch: {n} known change{'s' if n != 1 else ''} touch{'es' if n == 1 else ''} this code{where}"
        + (f" ({in_code} breaking or silent in non-test code)" if in_code else ""),
        "",
        "| Severity | Change | Effective | In code | Listed | In tests |",
        "|---|---|---|---|---|---|",
        *rows,
        "",
    ]
    foot = [f"{checked} change records checked{trust}. “Listed” means the id appears in a list or table, not in a request.", ""]
    for n_sites in (per_change, 3, 1, 0):
        body = top + (render_details(n_sites) if n_sites else
                      ["_Locations omitted to fit GitHub's size limit; see the fix briefs or the job summary._", ""]) + foot
        text = "\n".join(body)
        if len(text) <= max_chars:
            return text
    return text[:max_chars - 200] + "\n\n_Report truncated to fit GitHub's size limit._\n"
