# MCP probe

`apiwatch probe <id> <source>` snapshots an MCP server's `tools/list` and diffs it against the last baseline.

| Source | What it does |
|---|---|
| `mcp:stdio:<command>` | Spawns the server, speaks newline-delimited JSON-RPC on stdin/stdout |
| `mcp:https://host/mcp` | Streamable HTTP. Handles JSON and event-stream replies, `Mcp-Session-Id`, `MCP-Protocol-Version`. `--header` for auth |
| `mcp:file:tools.json` | A saved `{"tools": [...]}`. No network, for tests and demos |

## Protocol eras
MCP 2026-07-28 dropped the `initialize` handshake and sessions. The client handles both eras, following the spec's own compatibility rules:
- **stdio:** sends `server/discover` first. A result or a recognized modern error (-32020/-32021/-32022) means modern. Any other error, or no reply within 5s (`discover_timeout`), means legacy, and it falls back to `initialize` on the same process.
- **HTTP:** sends the modern `server/discover` with `MCP-Protocol-Version` and `Mcp-Method` headers and no session. It falls back to the legacy handshake only on a 4xx without a modern JSON-RPC error body. 401/403 are reported as auth errors.
- A server that only speaks versions apiwatch doesn't know gets a clear "no mutually supported MCP version" error.

The baseline records `era` and `protocolVersion`. A change shows up as an `info` event (`protocol_changed`), since a server going modern-only can break older clients with no tool changes at all.

Exit codes: `0` no drift or baseline created, `1` drift with `--fail-on-drift`, `2` error (server died, timeout, HTTP error, JSON-RPC error).

## What counts as what
Judged from the caller's side — would a call that worked yesterday fail today?

| Change | Severity |
|---|---|
| Tool removed | breaking (+ `possible_rename` info if one added tool shares ≥50% of its params) |
| Param removed, type changed, became required, new required param, enum value removed, enum newly imposed | breaking |
| Tool added, optional param added, param became optional, enum value added | additive |
| Output field removed or retyped, or the whole outputSchema dropped | breaking |
| Output field no longer required, or a new allowed value in an output enum | silent (readers that assume it break quietly) |
| Any `anyOf`/`oneOf`/`allOf`/`not`/`if` change | silent: reported as `combinator_changed` but not yet judged |
| Tool description changed, server version changed | info. Agents choose tools by description, so this can change behavior with no schema change |

## Running it on a schedule
The probe is one-shot; schedule it where you already run jobs. GitHub Actions example:

```yaml
on: { schedule: [{ cron: "0 */6 * * *" }], workflow_dispatch: {} }
jobs:
  mcp-drift:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4          # baselines must be committed: un-ignore .apiwatch/baselines/ in that repo
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install -e .
      - run: python -m apiwatch.cli probe docs mcp:https://example.com/mcp --header "Authorization: Bearer ${{ secrets.DOCS_MCP_TOKEN }}" --fail-on-drift
```

Accept a change with `--update-baseline` and commit the new baseline. This repo's `.gitignore` excludes `.apiwatch/` because its baselines are throwaway test output; a repo that runs scheduled probes should track `.apiwatch/baselines/`.
