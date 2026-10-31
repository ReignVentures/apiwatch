# apiwatch

**Know when an API, model, or MCP server you depend on changes, and exactly which lines of your code it breaks.**

Vendors retire models, rename MCP tools, and change API behavior on a schedule you don't control. Most of these changes are announced in a changelog nobody on your team reads. apiwatch reads them for you, and its GitHub Action points at the exact lines they land on, before the shutoff date.

* **The feed:** reviewed records of breaking, silent, and deprecation changes from OpenAI, Anthropic, Google, Stripe, Twilio, Notion, Atlassian, GitHub, xAI, and more. Every record cites the vendor's own page and is rechecked daily. Browse it at **[apiwatch.reignventures.co](https://apiwatch.reignventures.co)** (Atom and JSON Feed included).
* **The scan:** finds the call sites each change lands on in your repo. It is token aware (`gpt-4` does not match `gpt-4o`), follows constants, reads notebooks, and separates real uses from entries in model lists.
* **The brief:** for each hit, what changed, where, a suggested fix, and acceptance criteria you can hand to a coding agent.
* **The probe:** baseline a live JSON endpoint or an MCP server's `tools/list` and get told when it drifts, no spec required.

## What it caught

The feed holds 133 reviewed changes, each checked against the vendor's own page.

* **By vendor:** Google 33, Stripe 30, OpenAI 26, Anthropic 16, Notion MCP 8, Atlassian Rovo MCP 4, GitHub MCP Server 4, xAI 4, Notion 3, Twilio 3, Greenhouse 1, MCP specification 1
* **By severity:** breaking 99, silent 25, deprecation 9

Next dates ([everything in the next 180 days](https://apiwatch.reignventures.co/upcoming)):

* **Nov 2, 2026**, xAI, silent: [From November 2, 2026, grok-imagine-image-quality requests are served by grok-imagine-image-2.0 at low quality; the slug keeps working, but the model and price change](https://apiwatch.reignventures.co/records/xai-2026-11-02-grok-imagine-image-quality-retirement)
* **Nov 30, 2026**, Anthropic, breaking: [Anthropic will retire Claude Sonnet 4.5 (claude-sonnet-4-5-20250929) on the Claude API on November 30, 2026; requests to it will fail after that date.](https://apiwatch.reignventures.co/records/anthropic-2026-11-30-claude-sonnet-4-5-model)
* **Nov 30, 2026**, OpenAI, breaking: [OpenAI reusable prompt objects and the v1/prompts API shut down November 30, 2026](https://apiwatch.reignventures.co/records/openai-2026-11-30-prompts-api-shutdown)
* **Dec 1, 2026**, OpenAI, breaking: [OpenAI will remove gpt-image-1-mini, gpt-image-1.5 and chatgpt-image-latest from the API on December 1, 2026; the replacement is gpt-image-2.5-sunburst or gpt-image-2.5-flare.](https://apiwatch.reignventures.co/records/openai-2026-12-01-gpt-image-model-deprecations)
* **Dec 11, 2026**, OpenAI, breaking: [OpenAI will remove the older GPT-5 and o3 snapshots (gpt-5-2025-08-07, gpt-5-mini/nano-2025-08-07, gpt-5-pro-2025-10-06, o3-2025-04-16, o3-pro-2025-06-10) on December 11, 2026.](https://apiwatch.reignventures.co/records/openai-2026-12-11-gpt-5-and-o3-model-deprecations)

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

To leave generated or copied files out of a scan, add a `.apiwatchignore` file at the repository root: one glob per line (`agents/*.json`), a trailing `/` for a directory (`generated/`), `#` for comments. Translation folders (`locales/`, `i18n/`) are skipped by default.

## MCP server

AI agents can ask the feed directly while they work: does this model id or API have a published change, when, and what replaces it?
One public URL, no install, no account: `https://apiwatch.reignventures.co/mcp` (Streamable HTTP, both the
2026-07-28 and the earlier `initialize` protocol eras).

```bash
claude mcp add --transport http apiwatch https://apiwatch.reignventures.co/mcp
```

Tools: `check_model`, `check_identifiers`, `list_upcoming`, `search_changes`, `get_record`. It answers only from the
reviewed records the site publishes, stores nothing it is sent, and says "no record" (never "safe") for an id it
doesn't know. Setup for other clients: https://apiwatch.reignventures.co/mcp-server

Two more tools, `list_watched_mcp_servers` and `mcp_server_changes`, answer from apiwatch's history of observed tool
changes on some public MCP servers (https://apiwatch.reignventures.co/mcp-servers/): tools removed or renamed, new
required parameters, rewritten descriptions, changed hints. They never return a server's own tool descriptions.

## How records are verified

A record is published only after a reviewer that did not write it checks every field against the vendor's primary source, and an automated check confirms the quoted evidence is still on that page. The full rules are in [docs/REVIEW.md](docs/REVIEW.md). If a vendor edits or removes the page, the record comes off the site until it is reviewed again.

Found a change we missed, or a record that's wrong? Open an issue with a link to the vendor's page.

## apiwatch Pro

Pro watches every repository in your GitHub organization each night, with nothing to add to your workflows, and opens an issue when a new change lands on your code. $19 per month. See [how Pro works](https://apiwatch.reignventures.co/pro/).

## License

MIT. Built by [Reign Ventures LLC](https://reignventures.co).
