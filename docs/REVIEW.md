# Record review

A record is published only after it passes both checks below. No human step is required. The
reviewer is an AI agent that did **not** write or edit the record in the same pass: authoring and
reviewing stay separate, as they would with two people.

## 1. Independent review against the primary source

The reviewer opens `source_url` and checks every field.

- **Source.** It must be primary: the vendor's docs, changelog, release notes, status page, or official
  repo. Blogs, news, aggregators, and model memory don't count. If the record cites a secondary source,
  find the primary one or reject the record.
- **effective.** The exact date the change takes effect: the shutdown date, not the announcement date,
  unless the change shipped on announcement.
- **severity.** Use these definitions:
  - breaking: calls fail after this date.
  - silent: no error, but behavior, results, or cost change.
  - deprecation: still works, and removal is announced; or the vendor labels it deprecated and tells
    callers to stop using it, even with no removal date yet (say so in the detail).
  - additive: new capability; nothing breaks.
  - info: no action needed.

  A dated retirement within 180 days counts as breaking.
- **summary, detail, surface.** Every claim must be supported by the source and not overstated. Remove
  anything the source doesn't say.
- **signatures.** These must be identifiers that really appear in consumer code: model ids, endpoint
  paths, tool names, field names. Keep them specific. If one is generic, the record needs `context`.
- **aliases.** Include an alias (`-latest`, an undated name) only when an official vendor source lists it for the
  retired model: the docs, or the vendor's official SDK model list. Cite that source as a `{url, text}` evidence item.
- **exclude.** If a signature would also match an id this change doesn't cover, list that id in `exclude`. Examples:
  a live sibling (`gemini-3.1-flash-lite-image`), the replacement (`gpt-realtime-2.1`), or an id another record dates
  differently (`gpt-4-0314` under bare `gpt-4`). `tests/test_records.py` fails when two records claim the same id.
- **scope.** Narrow generic signatures so they only count where the change can apply:
  - `context`: words a file must mention; a list item is an AND group (`["stripe", "flight_data"]`).
  - `files`: glob patterns (`["*.py"]`) when the change is one language's SDK.
  - `repo_context`: words that must appear somewhere in the repo, dependency manifests included. Use it for
    changes that only apply when an SDK or API version is pinned. Prefer the canonical dependency form
    `dep:<ecosystem>:<name>@<major>.` (`dep:pypi:stripe@16.`, `dep:npm:@stripe/stripe-js@10.`), which matches
    any manifest or lockfile layout; an API version string needs a digit before it (`0.endive`…`9.endive`).
- **fix_hint.** It must be correct and safe, and it must not promise behavior the source doesn't describe. Don't point
  to a replacement that is itself retired or scheduled to retire; name the current target.
- **evidence.** Two to five short strings copied verbatim from the source. Each should be twelve words
  or fewer: prefer ids, dates, and tool or field names, not prose. Together they should support both
  *what* changes and *when*.
  - The strings must appear in the page as served without JavaScript. For docs sites that render in the
    browser, use the Markdown twin (`.md`, or `.md.txt` for ai.google.dev).
  - If the fact is on another primary page, use a `{url, text}` item.

Verdict: **pass**, **fix** (with the exact field edits), or **reject** (no primary source supports it).
A rejected record goes back to `feed/drafts/`.

## 2. Automated source check

`apiwatch check` fetches each source and confirms every evidence string is still there. It ignores
case, whitespace, Markdown backticks and asterisks, and dash variants. A record passes when it returns `ok`.

The daily deploy runs the check first. A record whose evidence has disappeared from a page that did load
is left off the site, and the run is marked failed so the change gets noticed. Fetch errors are reported
but don't pull a record: a flaky vendor site shouldn't take it down for a day.

## Marking a record reviewed

```yaml
verified: true
verified_by: claude          # or a person's name, which the site shows as "Verified"
verified_on: "YYYY-MM-DD"    # date of the review
evidence:
  - "gpt-4-0613"
  - "2026-10-23"
  - {url: "https://…/changelog", text: "removed in v0.31.0"}
```

Keep a header comment in the file that says what the review found or corrected.

## After each section of work

Before committing each section of work (a feature, a batch of records, a fix):

1. `pytest` is green.
2. `apiwatch check` passes for any record touched.
3. A review pass by a fresh agent covers the diff (code) or the records (content), and its findings are
   fixed or written into `docs/GAPS.md`.
4. One line goes in `docs/LOG.md`.
