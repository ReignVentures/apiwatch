"""Build the public feed from records: feed.json (machine) + feed.md (human) + per-vendor pages."""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from .models import ChangeRecord, dump_json, load_records

ORDER = {"breaking": 0, "silent": 1, "deprecation": 2, "additive": 3, "info": 4}


def build(records_dir: Path, out: Path) -> tuple[int, Path]:
    recs = load_records(records_dir)
    recs.sort(key=lambda r: (r.effective, ORDER[r.severity]), reverse=True)
    out.mkdir(parents=True, exist_ok=True)
    (out / "feed.json").write_text(dump_json({"count": len(recs), "records": recs}))
    (out / "feed.md").write_text(_markdown(recs))
    by_vendor: dict[str, list[ChangeRecord]] = defaultdict(list)
    for r in recs:
        by_vendor[r.vendor].append(r)
    vd = out / "vendors"
    vd.mkdir(exist_ok=True)
    for vendor, rs in by_vendor.items():
        (vd / f"{vendor.lower().replace(' ', '-')}.md").write_text(_markdown(rs, title=f"{vendor} — API changes"))
    return len(recs), out / "feed.md"


def _markdown(recs: list[ChangeRecord], title: str = "API & MCP change feed") -> str:
    icon = {"breaking": "🔴", "silent": "🟠", "deprecation": "🟡", "additive": "🟢", "info": "⚪"}
    lines = [f"# {title}", "", "| Effective | Vendor | Surface | Severity | Summary | Verified |", "|---|---|---|---|---|---|"]
    for r in recs:
        lines.append(f"| {r.effective} | {r.vendor} | `{r.surface}` | {icon[r.severity]} {r.severity} | {r.summary} | {'✓' if r.verified else '—'} |")
    lines += ["", "## Details", ""]
    for r in recs:
        lines += [f"### {r.vendor}: {r.summary}", "",
                  f"*{r.kind} · {r.severity} · effective {r.effective}*", "",
                  r.detail or "", "",
                  f"Signatures: {', '.join('`'+s+'`' for s in r.signatures) or '—'}", "",
                  f"Fix: {r.fix_hint or '—'}", "",
                  f"Source: {r.source_url or '—'}", ""]
    return "\n".join(lines)
