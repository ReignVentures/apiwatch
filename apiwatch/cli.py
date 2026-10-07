from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import attribute, brief, feed, probe, report, site
from .models import dump_json, load_records


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="apiwatch")
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("feed", help="build the public feed from feed/records")
    f.add_argument("--records", default="feed/records", type=Path)
    f.add_argument("--out", default="out/feed", type=Path)

    st = sub.add_parser("site", help="build the public feed as a static site (index, record and vendor pages, Atom, JSON Feed)")
    st.add_argument("--records", default="feed/records", type=Path)
    st.add_argument("--out", default="out/site", type=Path)
    st.add_argument("--base-url", default="", help="absolute URL the site will be served from (for feed links)")
    st.add_argument("--check-results", type=Path, metavar="PATH",
                    help="output of `check --json`: leave out records whose evidence is gone from their source")
    st.add_argument("--require-check-results", action="store_true",
                    help="fail (exit 2) if --check-results is missing, instead of publishing without it (CI)")
    st.add_argument("--pro-checkout-url", default="", help="Stripe payment link for apiwatch Pro (buy.stripe.com/...)")
    st.add_argument("--pro-app-slug", default="", help="the apiwatch Pro GitHub App's slug (for its install link)")

    ck = sub.add_parser("check", help="confirm each record's evidence still appears on its vendor source (network)")
    ck.add_argument("--records", default="feed/records", type=Path)
    ck.add_argument("--id", action="append", help="only this record (repeatable)")
    ck.add_argument("--json", type=Path, metavar="PATH", help="also write results as JSON (for `site --check-results`)")

    ig = sub.add_parser("ingest", help="draft records from vendor model-deprecation pages (network; output is unverified)")
    ig.add_argument("--source", action="append", choices=["anthropic", "openai", "google"],
                    help="which page(s) to read (repeatable; default: all)")
    ig.add_argument("--since", default="2026-01-01", help="ignore shutdowns before this ISO date")
    ig.add_argument("--records", default="feed/records", type=Path, help="existing records, for the coverage check")
    ig.add_argument("--out", default="feed/drafts", type=Path)
    ig.add_argument("--from-dir", type=Path, help="read saved <source>.md files instead of fetching")
    ig.add_argument("--force", action="store_true", help="overwrite existing draft files")

    wa = sub.add_parser("watch", help="list new changelog entries that may need a record (network; output is leads, not records)")
    wa.add_argument("--source", action="append", help="changelog key (repeatable; default: all). See apiwatch/watch.py SOURCES")
    wa.add_argument("--since", default="", help="ignore entries dated before this ISO date (default: 60 days ago)")
    wa.add_argument("--records", default="feed/records", type=Path)
    wa.add_argument("--seen", default="feed/watch-seen.json", type=Path, help="entries already triaged")
    wa.add_argument("--all", action="store_true", help="list unflagged entries too")
    wa.add_argument("--mark-seen", action="store_true", help="record the listed leads as triaged")
    wa.add_argument("--json", type=Path, metavar="PATH", help="also write leads as JSON")

    pr = sub.add_parser("probe", help="probe an endpoint against its baseline")
    pr.add_argument("endpoint_id")
    pr.add_argument("source", help="file:path.json | https://... | mcp:stdio:<cmd> | mcp:https://... | mcp:file:tools.json")
    pr.add_argument("--root", default=".", type=Path)
    pr.add_argument("--header", action="append", default=[], metavar="'Name: value'",
                    help="request header (repeatable), e.g. 'Authorization: Bearer $TOKEN'")
    pr.add_argument("--update-baseline", action="store_true")
    pr.add_argument("--fail-on-drift", action="store_true")

    sc = sub.add_parser("scan", help="attribute feed records to call sites in a repo")
    sc.add_argument("repo", type=Path)
    sc.add_argument("--records", default="feed/records", type=Path)
    sc.add_argument("--out", default="out/briefs", type=Path)
    sc.add_argument("--fail-on-breaking", action="store_true")
    sc.add_argument("--sarif", type=Path, metavar="PATH", help="also write results as SARIF 2.1.0 (GitHub code scanning)")
    sc.add_argument("--json", type=Path, metavar="PATH", help="also write results as JSON")
    sc.add_argument("--markdown", type=Path, metavar="PATH", help="also write a short report (CI job summary / PR comment)")
    sc.add_argument("--link-base", default="", help="URL prefix for file links in the markdown report")
    sc.add_argument("--report-key", default="", help="tag for the report's marker (one PR comment per scanned path)")
    sc.add_argument("--include-illustrative", action="store_true",
                    help="also use made-up demo records (feed/records/example-*); off by default")
    sc.add_argument("--only-files", type=Path, metavar="PATH",
                    help="newline-separated repo-relative paths (e.g. a PR's changed files): report hits only there")

    args = p.parse_args(argv)

    if args.cmd == "feed":
        n, path = feed.build(args.records, args.out)
        print(f"feed: {n} records → {path}")
        return 0

    if args.cmd == "site":
        exclude = set()
        if args.check_results and not args.check_results.exists():
            if args.require_check_results:
                print(f"site: error: no check results at {args.check_results}", file=sys.stderr)
                return 2
            print(f"site: warning: no check results at {args.check_results}; publishing without them", file=sys.stderr)
        elif args.check_results:
            from . import check
            exclude = check.failed_ids(args.check_results)
            for rid in sorted(exclude):
                print(f"site: leaving out {rid} (evidence missing from its source)")
        n, path = site.build(args.records, args.out, base_url=args.base_url, exclude=exclude,
                             pro_checkout_url=args.pro_checkout_url, pro_app_slug=args.pro_app_slug)
        print(f"site: {n} records → {path}")
        return 0

    if args.cmd == "check":
        from . import check
        results = check.run(args.records, only=args.id)
        for r in results:
            print(f"  {r.status:11s} {r.id}")
            for m in r.missing:
                print(f"      missing: {m}")
            for e in r.errors:
                print(f"      error:   {e}")
        if args.json:
            check.write_json(results, args.json)
        counts = {k: sum(r.status == k for r in results) for k in ("ok", "missing", "error", "no_evidence")}
        print("check: " + ", ".join(f"{v} {k}" for k, v in counts.items()))
        return 1 if counts["missing"] or counts["error"] else 0

    if args.cmd == "ingest":
        from . import ingest
        try:
            results = ingest.run(args.records, args.out, args.source or list(ingest.SOURCES), args.since,
                                 from_dir=args.from_dir, force=args.force)
        except OSError as e:
            print(f"ingest: error: {e}", file=sys.stderr)
            return 2
        for key, d, written in results:
            mark = "new " if written else "kept"
            print(f"  {mark} {d['effective']}  {d['severity']:11s} {d['id']}")
        new = sum(w for _, _, w in results)
        print(f"ingest: {len(results)} uncovered retirement group(s), {new} draft file(s) written → {args.out} (all unverified)")
        return 0

    if args.cmd == "watch":
        from datetime import date, timedelta
        from . import watch
        unknown = set(args.source or []) - set(watch.SOURCES)
        if unknown:
            print(f"watch: unknown source(s) {sorted(unknown)}; known: {', '.join(watch.SOURCES)}", file=sys.stderr)
            return 2
        since = args.since or (date.today() - timedelta(days=60)).isoformat()
        try:
            date.fromisoformat(since)
        except ValueError:
            print(f"watch: --since must be YYYY-MM-DD, got {since!r}", file=sys.stderr)
            return 2
        leads, errors = watch.run(args.records, args.source or list(watch.SOURCES), args.seen, since, date.today(),
                                  include_all=args.all)
        for e in sorted(leads, key=lambda e: (e.source, e.date), reverse=True):
            rel = f"  (see {', '.join(e.related)})" if e.related else ""
            print(f"  {e.date or '??':10s} {e.source:18s} {e.title[:90]}{rel}")
            print(f"  {'':10s} {'':18s} [{', '.join(e.flags)}] {e.url}")
        for err in errors:
            print(f"watch: error: {err}", file=sys.stderr)
        if args.json:
            watch.write_json(leads, args.json)
        if args.mark_seen:
            watch.mark_seen(leads, args.seen)
        print(f"watch: {len(leads)} lead(s) since {since}" + (f", {len(errors)} source error(s)" if errors else "")
              + (f"; marked seen in {args.seen}" if args.mark_seen else ""))
        return 2 if errors else 0      # leads are still printed; a broken source shouldn't pass quietly

    if args.cmd == "probe":
        headers = dict(h.split(":", 1) for h in args.header)
        ep = probe.Endpoint(id=args.endpoint_id, source=args.source,
                            headers={k.strip(): v.strip() for k, v in headers.items()} or None)
        fresh = not probe.baseline_path(args.root, ep.id).exists()
        try:
            events = probe.probe(ep, args.root, update_baseline=args.update_baseline)
        except (probe.mcp_client.McpError, OSError, ValueError) as e:
            print(f"probe {args.endpoint_id}: error: {e}", file=sys.stderr)
            return 2
        if fresh:
            print(f"probe {args.endpoint_id}: baseline created → {probe.baseline_path(args.root, ep.id)}")
            return 0
        if not events:
            print(f"probe {args.endpoint_id}: no drift")
            return 0
        print(f"probe {args.endpoint_id}: {len(events)} drift event(s)")
        for e in events:
            print(f"  {e.severity:10s} {e.change:16s} {e.path}  {e.before!r} → {e.after!r}")
        return 1 if args.fail_on_drift else 0

    if args.cmd == "scan":
        recs = [r for r in load_records(args.records) if args.include_illustrative or not r.illustrative]
        args.out.mkdir(parents=True, exist_ok=True)
        index = attribute.RepoIndex(args.repo)
        only = None
        if args.only_files:
            only = set()
            for line in args.only_files.read_text().splitlines():
                path = line.strip().replace("\\", "/")
                while path.startswith("./"):
                    path = path[2:]
                if path:
                    only.add(path.lstrip("/"))
        hits = 0
        breaking = 0
        results, all_results = [], []
        for r in recs:
            sites = attribute.attribute_record(index, r)
            if sites:
                all_results.append((r, sites))    # SARIF keeps every hit: code scanning would read a gap as "fixed"
            if only is not None:
                sites = [s for s in sites if s.file.replace("\\", "/") in only]
            if not sites:
                continue
            results.append((r, sites))
            hits += 1
            if r.severity in {"breaking", "silent"}:
                breaking += 1
            (args.out / f"{r.id}.md").write_text(brief.brief_for_record(r, sites))
            n_test = sum(s.in_test for s in sites)
            n_listed = sum(s.tier != "use" and not s.in_test for s in sites)
            parts = [f"{len(sites) - n_test - n_listed} in code"] + ([f"{n_listed} listed"] if n_listed else []) \
                + ([f"{n_test} in tests"] if n_test else [])
            split = f" ({', '.join(parts)})" if (n_test or n_listed) else ""
            print(f"  {r.severity:11s} {r.vendor:12s} {r.summary}  → {len(sites)} site(s){split}")
        print(f"scan: {hits} of {len(recs)} feed records touch this repo → {args.out}")
        if args.sarif:
            args.sarif.parent.mkdir(parents=True, exist_ok=True)
            args.sarif.write_text(report.sarif(all_results))
            print(f"sarif → {args.sarif}")
        if args.json:
            args.json.parent.mkdir(parents=True, exist_ok=True)
            args.json.write_text(report.as_json(results))
            print(f"json → {args.json}")
        if args.markdown and str(args.markdown) not in ("", "."):
            args.markdown.parent.mkdir(parents=True, exist_ok=True)
            args.markdown.write_text(report.markdown(
                results, len(recs), args.link_base, reviewed=sum(r.verified for r in recs),
                human=sum(r.human_verified for r in recs), key=args.report_key,
                scope="the files this PR changes" if only is not None else ""))
            print(f"markdown → {args.markdown}")
        return 1 if (args.fail_on_breaking and breaking) else 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
