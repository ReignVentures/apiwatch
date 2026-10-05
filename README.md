# apiwatch

**Know when an API, model, or MCP server you depend on changes, and exactly which lines of your code it breaks.**

Vendors retire models, rename MCP tools, and change API behavior on a schedule you don't control. Most of these changes are announced in a changelog nobody on your team reads. apiwatch reads them for you, and its GitHub Action points at the exact lines they land on, before the shutoff date.

* **The feed:** reviewed records of breaking, silent, and deprecation changes from OpenAI, Anthropic, Google, Stripe, Twilio, Notion, Atlassian, GitHub, xAI, and more. Every record cites the vendor's own page and is rechecked daily. Browse it at **[apiwatch.reignventures.co](https://apiwatch.reignventures.co)** (Atom and JSON Feed included).
* **The scan:** finds the call sites each change lands on in your repo. It is token aware (`gpt-4` does not match `gpt-4o`), follows constants, reads notebooks, and separates real uses from entries in model lists.
* **The brief:** for each hit, what changed, where, a suggested fix, and acceptance criteria you can hand to a coding agent.
* **The probe:** baseline a live JSON endpoint or an MCP server's `tools/list` and get told when it drifts, no spec required.

## GitHub Action

```yaml
name: apiwatch
on: [push, pull_request]
jobs:
  scan:
    runs-on: ubuntu-latest
    permissions: { contents: read, pull-requests: write }
    steps:
      - uses: actions/checkout@v4
      - uses: ReignVentures/apiwatch@v1
        with:
          fail-on-breaking: "true"   # fail CI when a breaking or silent change touches your code
          comment: "true"            # keep one up to date report comment on pull requests
          only-changed: "true"       # on PRs, gate only on the files the PR changes
```

Every run writes a short report to the job summary: which changes touch your code, how severe, whether the date is past or coming, and links to each location. Add SARIF upload to see hits as code scanning annotations:

```yaml
      - uses: github/codeql-action/upload-sarif@v3
        if: always()
        with: { sarif_file: apiwatch.sarif }
```

Tip: add a `schedule:` trigger (for example daily) so you hear about new changes even when nobody is pushing.

## CLI

```bash
pip install git+https://github.com/ReignVentures/apiwatch
apiwatch scan . --fail-on-breaking --markdown report.md
apiwatch probe docs "mcp:stdio:npx -y @modelcontextprotocol/server-everything"
apiwatch probe docs mcp:https://example.com/mcp --header 'Authorization: Bearer $TOKEN' --fail-on-drift
```

Scans run locally with no network calls. Your code never leaves your machine or your CI runner.

## How records are verified

A record is published only after a reviewer that did not write it checks every field against the vendor's primary source, and an automated check confirms the quoted evidence is still on that page. The full rules are in [docs/REVIEW.md](docs/REVIEW.md). If a vendor edits or removes the page, the record comes off the site until it is reviewed again.

Found a change we missed, or a record that's wrong? Open an issue with a link to the vendor's page.

## apiwatch Pro

Pro watches every repository in your GitHub organization each night, with nothing to add to your workflows, and opens an issue when a new change lands on your code. $19 per month. Email **hello@reignventures.co** to get early access.

## License

MIT. Built by [Reign Ventures LLC](https://reignventures.co).
