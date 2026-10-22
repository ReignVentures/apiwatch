"""The public MCP server (docs/MCP-SERVER.md): what the site build ships for it. The protocol and tools are tested
in tests/js/mcp_worker.test.mjs with Node's runner; test_node_tests runs them here against the real built feed."""
import json
import os
import re
import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest
from apiwatch import attribute, legal, site
from apiwatch.models import ChangeRecord

from test_site import build, rec

ROOT = Path(__file__).parent.parent
DASHES = ("\u2013", "\u2014")


def test_build_writes_the_worker_and_routes_only_mcp_to_it(tmp_path):
    build(tmp_path, base_url="https://feed.example.com")
    out = tmp_path / "site"
    assert (out / "_worker.js").read_text() == (ROOT / "apiwatch" / "mcp_worker.js").read_text()
    assert json.loads((out / "_routes.json").read_text()) == {"version": 1, "include": ["/mcp", "/mcp/*"], "exclude": []}
    # static pages keep their headers file exactly as before; the worker never sees them
    assert (out / "_headers").read_text().startswith(site.HEADERS)


def test_worker_reads_only_the_feed_and_calls_nothing_else():
    js = (ROOT / "apiwatch" / "mcp_worker.js").read_text()
    code = "\n".join(line for line in js.splitlines() if not line.lstrip().startswith("//"))
    assert "env.ASSETS.fetch(new URL(\"/feed.json\", request.url))" in code
    assert "env.ASSETS.fetch(new URL(\"/mcp-history.json\", request.url))" in code
    assert code.count("fetch(") == 4          # the feed and history reads, the static fallback, the export's fetch handler
    for banned in ("console.", "caches.", "Set-Cookie", "KV", "localStorage", "import "):
        assert banned not in code, banned
    assert not any(d in js for d in DASHES)


def test_feed_carries_what_the_worker_answers_from(tmp_path):
    build(tmp_path, base_url="https://feed.example.com")
    items = {i["_apiwatch"]["id"]: i for i in json.loads((tmp_path / "site" / "feed.json").read_text())["items"]}
    a = items["acme-2026-10-01-soon"]["_apiwatch"]
    assert a["vendor"] == "Acme" and a["summary"] == "Acme retires widget-1"
    assert a["source_url"] == "https://v.example/changelog" and a["fix_hint"] == "" and a["detail"] == ""
    assert a["scope"] == ('Applies only in files matching "*.py"; files that mention "acme" and "billing" or "acme_sdk".')
    assert items["acme-2026-08-01-past"]["_apiwatch"]["scope"] == ""


def test_scope_text_names_dependency_pins_and_shortens_long_lists():
    r = ChangeRecord(**rec("s-2026-10-01-x", kind="sdk", signatures=["x"],
                           repo_context=["dep:pypi:stripe@16.", "stripe==16", "a", "b", "c", "d", "e"]))
    assert site.scope_text(r) == ('Applies only in repos that depend on "stripe 16.x" (pypi); or repos that mention '
                                  '"stripe==16" or "a" or "b" or "c" or 2 more (including dependency manifests).')


def test_record_pages_keep_their_scope_wording(tmp_path):
    build(tmp_path)
    page = (tmp_path / "site" / "records" / "acme-2026-10-01-soon.html").read_text()
    assert ('Counts only in files matching <code>*.py</code>; files that mention <code>acme</code> + '
            '<code>billing</code> or <code>acme_sdk</code>.') in page


def test_mcp_server_page(tmp_path):
    build(tmp_path, base_url="https://feed.example.com")
    out = tmp_path / "site"
    html = (out / "mcp-server.html").read_text()
    assert "claude mcp add --transport http apiwatch https://feed.example.com/mcp" in html
    assert '<link rel="canonical" href="https://feed.example.com/mcp-server">' in html
    for name, _ in site.MCP_TOOLS:
        assert f"<code>{name}</code>" in html
    assert "Add custom connector" in html and ".cursor/mcp.json" in html and "&quot;mcpServers&quot;" in html
    assert html.count('<aside class="cta"') == 1
    assert not any(d in html for d in DASHES)
    assert "https://feed.example.com/mcp-server" in (out / "sitemap.xml").read_text()
    for page in (out / "index.html", out / "records" / "acme-2026-10-01-soon.html", out / "404.html"):
        assert "mcp-server.html\">MCP server</a>" in page.read_text(), page
    # without a base URL the snippets still point at the live server
    (tmp_path / "nobase").mkdir()
    build(tmp_path / "nobase")
    assert "https://apiwatch.reignventures.co/mcp" in (tmp_path / "nobase" / "site" / "mcp-server.html").read_text()


def test_privacy_policy_mentions_the_mcp_endpoint():
    assert "The MCP server at /mcp answers from the public feed and stores nothing it is sent." in legal.PRIVACY


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_node_tests(tmp_path):
    """Runs tests/js against the real records, built today, as the deploy builds them."""
    out = tmp_path / "site"
    site.build(ROOT / "feed" / "records", out, today=date.today(), base_url="https://apiwatch.reignventures.co/",
               mcp_watch=ROOT / "feed" / "mcp-watch.yml", mcp_history=ROOT / "feed" / "mcp-history")
    env = {**os.environ, "APIWATCH_FEED": str(out / "feed.json"), "APIWATCH_HISTORY": str(out / "mcp-history.json")}
    tests = sorted(str(p) for p in (ROOT / "tests" / "js").glob("*.test.mjs"))
    proc = subprocess.run(["node", "--test", "--test-reporter=tap", *tests], cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]
    assert "# fail 0" in proc.stdout and "# skipped 0" in proc.stdout


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_worker_matches_what_the_scanner_matches(tmp_path):
    """Every model id the real records name (signatures, excludes, named ids, and partner platform forms): the worker
    attaches exactly the unscoped records the scanner would report for that id in code."""
    from apiwatch.models import load_records
    out = tmp_path / "site"
    site.build(ROOT / "feed" / "records", out, today=date.today(), base_url="https://apiwatch.reignventures.co/")
    feed = json.loads((out / "feed.json").read_text())
    published = {i["_apiwatch"]["id"] for i in feed["items"]}
    recs = [r for r in load_records(ROOT / "feed" / "records") if r.id in published and r.kind == "model"
            and not (r.context or r.files or r.repo_context)]
    ok = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]*$")
    ids = set()
    for r in recs:
        names = attribute.record_names(" ".join([r.summary, r.detail, r.surface]))
        for x in [*r.signatures, *r.exclude, *(n for n in names if n[0] != "-")]:
            if ok.match(x):
                ids |= {x, x + "@20250101", x + "-20250101-v1:0", x + "-2025-01-01", x + "-mini"}
    ids = sorted(ids)
    repo = tmp_path / "repo"
    repo.mkdir()
    for n, x in enumerate(ids):
        (repo / f"m{n}.py").write_text(f'MODEL = "{x}"\n')
    idx = attribute.RepoIndex(repo)
    expected = {x: set() for x in ids}
    for r in recs:
        for site_ in attribute.attribute_record(idx, r):
            expected[ids[int(Path(site_.file).stem[1:])]].add(r.id)
    script = (
        "import { parseFeed, matchRecord } from './apiwatch/mcp_worker.js';"
        "import { readFileSync } from 'node:fs';"
        "const feed = parseFeed(JSON.parse(readFileSync(process.argv[1], 'utf8')));"
        "const ids = JSON.parse(readFileSync(process.argv[2], 'utf8'));"
        "const keep = new Set(JSON.parse(readFileSync(process.argv[3], 'utf8')));"
        "const out = {};"
        "for (const x of ids) out[x] = feed.records.filter((r) => keep.has(r.id) && matchRecord(x, r)).map((r) => r.id);"
        "process.stdout.write(JSON.stringify(out));")
    (tmp_path / "ids.json").write_text(json.dumps(ids))
    (tmp_path / "keep.json").write_text(json.dumps([r.id for r in recs]))
    proc = subprocess.run(["node", "--input-type=module", "-e", script, str(out / "feed.json"), str(tmp_path / "ids.json"),
                           str(tmp_path / "keep.json")], cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr
    got = {x: set(v) for x, v in json.loads(proc.stdout).items()}
    diff = {x: (sorted(expected[x]), sorted(got[x])) for x in ids if expected[x] != got[x]}
    assert len(ids) > 500 and not diff, diff


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_worker_keeps_what_the_build_sanitized_and_drops_nothing_else(tmp_path):
    """mcp-history.json as the build writes it (from a hostile fixture) passes through the worker's parseHistory
    unchanged: Python's safe names and paths are already in the worker's pattern, so both sides agree."""
    from test_history import build_history
    out = build_history(tmp_path)
    data = json.loads((out / "mcp-history.json").read_text())
    script = (
        "import { parseHistory } from './apiwatch/mcp_worker.js';"
        "import { readFileSync } from 'node:fs';"
        "const h = await parseHistory(JSON.parse(readFileSync(process.argv[1], 'utf8')));"
        "process.stdout.write(JSON.stringify(h.servers.map((s) => s.entries)));")
    proc = subprocess.run(["node", "--input-type=module", "-e", script, str(out / "mcp-history.json")], cwd=ROOT,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout)
    def answerable(e):   # what the worker keeps: no counts, and no matched phrase in a flag's detail
        e = {k: v for k, v in e.items() if k != "counts"}
        if "flags" in e:
            e["flags"] = [{**f, "detail": "" if f["flag"] == "instruction_like_text" else f["detail"]} for f in e["flags"]]
        return e
    want = [[answerable(e) for e in s["entries"]] for s in data["servers"]]
    assert got == want
    flags = [f for s in data["servers"] for e in s["entries"] for f in e.get("flags", [])]
    assert any(f["flag"] == "unusual_tool_name" and f["tool"].startswith("sha256:") for f in flags)
