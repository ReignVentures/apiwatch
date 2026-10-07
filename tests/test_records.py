"""Consistency checks across the published feed records."""
from pathlib import Path

import pytest

from apiwatch.attribute import scan
from apiwatch.models import load_records

RECORDS = [r for r in load_records(Path(__file__).parent.parent / "feed" / "records") if r.published]

# Two different changes that genuinely land on the same code (same surface, separate announcements).
SAME_SURFACE = {
    ("atlassian-2027-03-01-mcp-v1-switches-to-v2-tools", "atlassian-2026-05-27-mcp-oauth-server-change"),
    ("notion-2026-09-02-mcp-search-split", "notion-2026-09-17-mcp-search-drops-unavailable-options"),
    ("notion-2026-09-17-mcp-search-drops-unavailable-options", "notion-2026-09-02-mcp-search-split"),
    ("notion-2026-09-29-mcp-query-database-view-removed", "notion-2026-08-13-mcp-query-database-view-dropped"),
    ("notion-2026-08-13-mcp-query-database-view-dropped", "notion-2026-09-29-mcp-query-database-view-removed"),
    ("stripe-2026-09-30-payment-method-types-removed", "stripe-2026-08-26-intents-payment-method-types-removed"),
    ("stripe-2026-08-26-intents-payment-method-types-removed", "stripe-2026-09-30-payment-method-types-removed"),
}


def test_file_names_match_ids_and_ids_are_unique():
    ids = [r.id for r in RECORDS]
    assert len(ids) == len(set(ids))
    for r in RECORDS:
        assert (Path(__file__).parent.parent / "feed" / "records" / f"{r.id}.yml").exists(), r.id


@pytest.mark.parametrize("owner", RECORDS, ids=lambda r: r.id)
def test_no_record_claims_an_id_another_record_dates_differently(owner, tmp_path):
    """If record B lists an id, no other record's signatures may match it unless excluded. Otherwise a
    scan reports the same line under two dates (e.g. bare gpt-4 catching gpt-4-0314, retired months earlier)."""
    (tmp_path / "ids.py").write_text("".join(f'X = "{sig}"\n' for sig in owner.signatures))
    for other in RECORDS:
        # across vendors too (OpenAI's computer-use-preview once matched inside a Gemini model id), except when
        # both records are scoped to their own vendor's files: generic parameter names can't collide then
        scoped = lambda r: bool(r.context or r.files or r.repo_context)   # noqa: E731
        if other.id == owner.id or (owner.id, other.id) in SAME_SURFACE:
            continue
        if other.vendor != owner.vendor and scoped(owner) and scoped(other):
            continue
        hits = scan(tmp_path, other.signatures, None, other.exclude)
        assert not hits, (f"{other.id} also matches {[owner.signatures[h.line - 1] for h in hits]} "
                          f"from {owner.id}; add them to its exclude")



# Field test 2026-10-06 (docs/FIELD-TEST.md): scopes tightened for lines that matched unrelated code. Each case is
# (record id, {file: text}); a file whose name starts with "fp_" must not be hit, every other file must be.
SCOPE_CASES = [
    ("anthropic-2026-09-24-compliance-activity-names-removed", {
        "fp_experts_test.py": 'ROLE = "Privacy & Compliance"  # activities\nPage(title="body", filename="a.txt")\n',
        "vector.toml": 'endpoint = "https://api.anthropic.com/v1/compliance/activities"\n'
                       'source = ".file.name = .filename"\n',          # a log shipper's config counts
        "audit.py": 'r = s.get("https://api.anthropic.com/v1/compliance/activities")\n'
                    'names = [a["filename"] for a in r.json()["data"]]\n',
    }),
    ("openai-2026-09-24-sora-2-videos-api-shutdown", {
        "fp_brave_video_tool.py": 'search_url: str = "https://api.search.brave.com/res/v1/videos/search"\n',
        "fp_brave_test.py": '# vs openai\nURL = "https://api.search.brave.com/res/v1/videos/search"\n',
        "fp_klingai-video-model.ts": "const paths = {\n  t2v: '/v1/videos/text2video',\n"
                                     "  mi2v: '/v1/videos/multi-image2video',\n};\n",
        "video.py": 'r = http.post("https://api.openai.com/v1/videos", json={"prompt": p})\n',
        "replicate-sora.ts": "model: replicate.video('openai/sora-2'),\n",
    }),
    ("openai-2026-11-30-prompts-api-shutdown", {
        "fp_main.py": "from open_webui.routers import openai, prompts\n"
                      "app.include_router(prompts.router, prefix='/api/v1/prompts', tags=['prompts'])\n",
        "base_url.py": 'r = s.get(BASE + "/v1/prompts")\n',                   # no context needed
        "prompts.py": 'import openai\nr = s.get("https://api.openai.com/v1/prompts")\n',
        "agent.py": 'agent = Agent(prompt={"id": "pmpt_123"})\n',
    }),
    ("github-2026-01-26-mcp-actions-projects-consolidation", {
        "fp_t0_dataset.json": '{"source": "github", "mcp_tool_names": {\n "mcp.linear.app": [\n  "get_project",\n'
                              '  "list_projects"\n ]\n}}\n',
        "mcp_effects.json": '{"mcp_github": {\n "source": "https://github.com/github/github-mcp-server",\n'
                            ' "read": [\n  "get_workflow_run"\n ]\n}}\n',
        "agent.py": 'allowed_tools = ["mcp__github__list_workflows"]\n',
        "copilot.py": 'server = MCPServer(url="https://api.githubcopilot.com/mcp/")\ntools = ["get_project"]\n',
    }),
    ("github-2026-08-10-mcp-search-issues-semantic", {
        "fp_t0_dataset.json": '{"source": "github", "mcp_tool_names": {\n "mcp.sentry.dev": [\n  "search_issues"\n'
                              ' ]\n}}\n',
        "mcp_effects.json": '{"mcp_github": {\n "source": "https://github.com/github/github-mcp-server",\n'
                            ' "read": [\n  "search_issues"\n ]\n}}\n',
        "agent.py": 'allowed_tools = ["mcp__github__search_issues"]\n',
    }),
    ("notion-2026-08-25-mcp-update-content-atomic", {
        "fp_auth.py": 'provider = "notion"\nscopes = ["read_content", "insert_content", "update_content"]\n',
        "mcp_edit.py": 'await session.call_tool("notion-update-page", {"command": "update_content", "updates": ups})\n',
        "rest_edit.ts": 'await notion.pages.updateMarkdown({\n  page_id: id,\n  type: "update_content",\n'
                        '  update_content: { content_updates: [{ old_str: a, new_str: b }] },\n});\n',
    }),
    ("notion-2026-04-20-query-pagination-cap", {
        "fp_notion_search.py": 'r = s.post("https://api.notion.com/v1/search", json=body).json()\n'
                               'body["start_cursor"] = r["next_cursor"]\n',
        "fp_notion_blocks.py": 'url = f"https://api.notion.com/v1/blocks/{bid}/children"\n'
                               'params["start_cursor"] = cursor\n',
        "notion_sync.py": 'resp = notion.data_sources.query(data_source_id=ds, start_cursor=cursor)\n'
                          'cursor = resp["next_cursor"]\n',
        "notion_views.ts": 'const page = await notion.views.queries.results({ view_id, query_id, start_cursor });\n',
    }),
    ("mcp-spec-2026-07-28-stateless-protocol", {
        "fp_bridge.ts": "switch (msg.method) {\n  case 'ui/notifications/initialized':\n    break;\n}\n",
        "client.ts": "await transport.send({ jsonrpc: '2.0', method: 'notifications/initialized' });\n",
    }),
]


@pytest.mark.parametrize("rid,files", SCOPE_CASES, ids=[c[0] for c in SCOPE_CASES])
def test_field_test_scopes(rid, files, tmp_path):
    from apiwatch.attribute import attribute_record
    rec = next(r for r in RECORDS if r.id == rid)
    for name, text in files.items():
        (tmp_path / name).write_text(text)
    hit = {s.file for s in attribute_record(tmp_path, rec)}
    assert not {f for f in hit if f.startswith("fp_")}, hit
    assert hit == {f for f in files if not f.startswith("fp_")}


def test_dependency_gates_name_their_ecosystem():
    """dep:stripe@16. would also match the npm package stripe 16.x; records must say dep:pypi:… / dep:npm:…"""
    import re
    for r in RECORDS:
        for c in r.repo_context:
            for w in ([c] if isinstance(c, str) else c):
                if w.startswith("dep:"):
                    assert re.match(r"dep:(npm|pypi|rubygems|packagist|go|cargo):", w), (r.id, w)
