import json
import shlex
import sys
import threading
from pathlib import Path

import pytest

from apiwatch import mcp_client, probe
from apiwatch.cli import main

import fake_mcp_server

S = Path(__file__).parent.parent / "samples" / "responses"
FAKE = Path(__file__).parent / "fake_mcp_server.py"


def tools_file(tmp_path, tools, name="tools.json"):
    p = tmp_path / name
    p.write_text(json.dumps({"tools": tools}))
    return p


def stdio_source(path, version="1.0.0", era="legacy", startup_delay=0):
    return "mcp:stdio:" + shlex.join([sys.executable, str(FAKE), str(path), version, era, str(startup_delay)])


def serve(tools_path, **kw):
    srv = fake_mcp_server.make_http_server(tools_path, **kw)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/mcp"


def kinds(events):
    return {(e.path, e.change, e.severity) for e in events}


BEFORE = [
    {"name": "search_documents", "description": "Search docs",
     "inputSchema": {"type": "object", "properties": {"q": {"type": "string"},
                                                      "sort": {"type": "string", "enum": ["relevance", "date", "title"]}},
                     "required": ["q"]}},
    {"name": "get_document", "description": "Fetch one doc",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}, "format": {"type": "string"}},
                     "required": ["id"]}},
]
AFTER = [
    {"name": "query_documents", "description": "Search docs",
     "inputSchema": {"type": "object", "properties": {"q": {"type": "string"},
                                                      "sort": {"type": "string", "enum": ["relevance", "date"]}},
                     "required": ["q"]}},
    {"name": "get_document", "description": "Fetch one document by id. Prefer this over search.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "integer"}, "format": {"type": "string"},
                                                      "workspace": {"type": "string"}},
                     "required": ["id", "format", "workspace"]}},
]


def test_input_schema_drift_is_judged_from_the_callers_side():
    k = kinds(probe.mcp_tool_drift("docs", BEFORE, AFTER))
    assert ("tool:search_documents", "removed", "breaking") in k
    assert ("tool:query_documents", "added", "additive") in k
    assert ("tool:search_documents", "possible_rename", "info") in k
    assert ("tool:get_document.input.id", "type_changed", "breaking") in k
    assert ("tool:get_document.input.format", "became_required", "breaking") in k
    assert ("tool:get_document.input.workspace", "added_required", "breaking") in k
    assert ("tool:get_document.description", "description_changed", "info") in k


def test_enum_value_removed_is_breaking():
    before = [BEFORE[0]]
    after = [{**BEFORE[0], "inputSchema": AFTER[0]["inputSchema"]}]
    events = probe.mcp_tool_drift("docs", before, after)
    assert [(e.path, e.change, e.before) for e in events] == [
        ("tool:search_documents.input.sort", "enum_values_removed", ["title"])]


def test_optional_param_added_is_additive_only():
    after = [{**BEFORE[1], "inputSchema": {**BEFORE[1]["inputSchema"],
              "properties": {**BEFORE[1]["inputSchema"]["properties"], "lang": {"type": "string"}}}}]
    assert kinds(probe.mcp_tool_drift("docs", [BEFORE[1]], after)) == {("tool:get_document.input.lang", "added", "additive")}


def test_stdio_list_tools_paginates_and_skips_noise(tmp_path):
    p = tools_file(tmp_path, BEFORE + [{"name": "third", "inputSchema": {"type": "object"}}])
    out = mcp_client.list_tools(stdio_source(p)[4:], timeout=10)
    assert [t["name"] for t in out["tools"]] == ["search_documents", "get_document", "third"]
    assert out["server"] == {"name": "fake-docs", "version": "1.0.0"}


def test_stdio_probe_baseline_then_drift(tmp_path):
    p = tools_file(tmp_path, BEFORE)
    ep = probe.Endpoint(id="docs", source=stdio_source(p))
    assert ep.kind == "mcp"
    assert probe.probe(ep, tmp_path) == []  # baseline
    assert probe.probe(ep, tmp_path) == []  # stable
    tools_file(tmp_path, AFTER)
    ep2 = probe.Endpoint(id="docs", source=stdio_source(p, "1.1.0"))
    k = kinds(probe.probe(ep2, tmp_path))
    assert ("tool:search_documents", "removed", "breaking") in k
    assert ("server.version", "version_changed", "info") in k


def test_stdio_server_that_dies_is_an_error():
    with pytest.raises(mcp_client.McpError, match="exited"):
        mcp_client.list_tools("stdio:" + shlex.join([sys.executable, "-c", "pass"]), timeout=10)


@pytest.mark.parametrize("sse", [True, False])
def test_http_probe_json_and_event_stream(tmp_path, sse):
    p = tools_file(tmp_path, BEFORE)
    srv, url = serve(p, sse=sse, require_auth="Bearer t0k")
    try:
        with pytest.raises(mcp_client.McpError, match="HTTP 401"):
            mcp_client.list_tools(url, timeout=10)
        ep = probe.Endpoint(id="docs", source=f"mcp:{url}", headers={"Authorization": "Bearer t0k"})
        assert probe.probe(ep, tmp_path) == []
        tools_file(tmp_path, AFTER)
        assert ("tool:search_documents", "removed", "breaking") in kinds(probe.probe(ep, tmp_path))
    finally:
        srv.shutdown()


def test_mcp_file_source_matches_the_saved_samples(tmp_path):
    ep = probe.Endpoint(id="exampledocs", source=f"mcp:file:{S / 'mcp_tools_before.json'}")
    assert probe.probe(ep, tmp_path) == []
    ep2 = probe.Endpoint(id="exampledocs", source=f"mcp:file:{S / 'mcp_tools_after.json'}")
    k = kinds(probe.probe(ep2, tmp_path))
    assert ("tool:search_documents", "possible_rename", "info") in k


def test_cli_mcp_probe_exit_codes(tmp_path, capsys):
    p = tools_file(tmp_path, BEFORE)
    args = ["probe", "docs", stdio_source(p), "--root", str(tmp_path), "--fail-on-drift"]
    assert main(args) == 0
    assert "baseline created" in capsys.readouterr().out
    tools_file(tmp_path, AFTER)
    assert main(args) == 1
    assert "search_documents" in capsys.readouterr().out
    bad = ["probe", "x", "mcp:stdio:" + shlex.join([sys.executable, "-c", "pass"]), "--root", str(tmp_path)]
    assert main(bad) == 2


def test_rest_baseline_is_not_reused_for_mcp(tmp_path):
    rest = probe.Endpoint(id="same", source=f"file:{S / 'shipments_v1.json'}")
    probe.probe(rest, tmp_path)
    with pytest.raises(ValueError, match="not an MCP baseline"):
        probe.probe(probe.Endpoint(id="same", source=f"mcp:file:{S / 'mcp_tools_before.json'}"), tmp_path)


# --- protocol eras (2026-07-28 stateless vs. handshake-era servers) ---

@pytest.mark.parametrize("era,expect_era,expect_version", [
    ("legacy", "legacy", "2025-06-18"),
    ("silent", "legacy", "2025-06-18"),   # never answers the probe → timeout → handshake
    ("modern", "modern", "2026-07-28"),
    ("dual", "modern", "2026-07-28"),
])
def test_stdio_detects_server_era(tmp_path, era, expect_era, expect_version):
    p = tools_file(tmp_path, BEFORE)
    # only the silent server should hit the probe timeout; the others answer
    out = mcp_client.list_tools(stdio_source(p, era=era)[4:], timeout=10,
                                discover_timeout=0.5 if era == "silent" else 5)
    assert (out["era"], out["protocolVersion"]) == (expect_era, expect_version)
    assert [t["name"] for t in out["tools"]] == ["search_documents", "get_document"]
    assert out["server"]["name"] == "fake-docs"


@pytest.mark.parametrize("era,expect_era", [("legacy", "legacy"), ("modern", "modern"), ("dual", "modern")])
@pytest.mark.parametrize("sse", [True, False])
def test_http_detects_server_era(tmp_path, era, expect_era, sse):
    p = tools_file(tmp_path, BEFORE)
    srv, url = serve(p, era=era, sse=sse)
    try:
        out = mcp_client.list_tools(url, timeout=10)
        assert out["era"] == expect_era
        assert [t["name"] for t in out["tools"]] == ["search_documents", "get_document"]
        modern_reqs = [h for h in srv.seen if h.get("mcp-protocol-version") == "2026-07-28"]
        assert all(h.get("mcp-method") == h["_method"] for h in modern_reqs)
        assert all("mcp-session-id" not in h for h in modern_reqs)
    finally:
        srv.shutdown()


@pytest.mark.parametrize("transport", ["stdio", "http"])
def test_server_speaking_only_unknown_versions_is_a_clear_error(tmp_path, transport):
    p = tools_file(tmp_path, BEFORE)
    if transport == "stdio":
        with pytest.raises(mcp_client.McpError, match="no mutually supported MCP version"):
            mcp_client.list_tools(stdio_source(p, era="future")[4:], timeout=10)
    else:
        srv, url = serve(p, era="future")
        try:
            with pytest.raises(mcp_client.McpError, match="no mutually supported MCP version"):
                mcp_client.list_tools(url, timeout=10)
        finally:
            srv.shutdown()


def test_server_moving_to_modern_protocol_is_reported(tmp_path):
    p = tools_file(tmp_path, BEFORE)
    assert probe.probe(probe.Endpoint(id="docs", source=stdio_source(p, era="legacy")), tmp_path) == []
    events = probe.probe(probe.Endpoint(id="docs", source=stdio_source(p, era="modern")), tmp_path)
    assert [(e.path, e.change, e.before, e.after) for e in events] == [
        ("server.protocolVersion", "protocol_changed", "2025-06-18", "2026-07-28")]


@pytest.mark.parametrize("era,expect_era", [("modern", "modern"), ("dual", "legacy"), ("legacy", "legacy")])
def test_slow_starting_server_that_misses_the_probe_timeout(tmp_path, era, expect_era):
    # the probe times out; a modern-only server then rejects initialize, and its late
    # discover reply rescues the session. Dual-era servers accept initialize (legacy is fine).
    p = tools_file(tmp_path, BEFORE)
    out = mcp_client.list_tools(stdio_source(p, era=era, startup_delay=1.0)[4:], timeout=10, discover_timeout=0.3)
    assert out["era"] == expect_era
    assert [t["name"] for t in out["tools"]] == ["search_documents", "get_document"]


# --- outputSchema, $ref, combinators ---

def _tool(name, input_schema=None, output_schema=None):
    t = {"name": name, "inputSchema": input_schema or {"type": "object"}}
    if output_schema is not None:
        t["outputSchema"] = output_schema
    return t


def test_output_schema_is_judged_from_the_readers_side():
    before = _tool("get_weather", output_schema={
        "type": "object", "required": ["temp", "unit"],
        "properties": {"temp": {"type": "number"}, "unit": {"type": "string", "enum": ["C", "F"]},
                       "wind": {"type": "number"}, "alerts": {"type": "array", "items": {"type": "string"}}}})
    after = _tool("get_weather", output_schema={
        "type": "object", "required": ["temp"],
        "properties": {"temp": {"type": "string"}, "unit": {"type": "string", "enum": ["C", "F", "K"]},
                       "alerts": {"type": "array", "items": {"type": "object"}}, "humidity": {"type": "number"}}})
    k = kinds(probe.mcp_tool_drift("wx", [before], [after]))
    assert ("tool:get_weather.output.temp", "type_changed", "breaking") in k
    assert ("tool:get_weather.output.wind", "removed", "breaking") in k
    assert ("tool:get_weather.output.unit", "became_optional", "silent") in k
    assert ("tool:get_weather.output.unit", "enum_values_added", "silent") in k
    assert ("tool:get_weather.output.alerts[]", "type_changed", "breaking") in k
    assert ("tool:get_weather.output.humidity", "added", "additive") in k


def test_output_schema_dropped_entirely_is_breaking():
    k = kinds(probe.mcp_tool_drift("wx", [_tool("t", output_schema={"type": "object"})], [_tool("t")]))
    assert ("tool:t.output", "removed", "breaking") in k


def test_local_refs_are_resolved_before_diffing():
    def schema(id_type):
        return {"type": "object", "$defs": {"Doc": {"type": "object", "properties": {"id": {"type": id_type}}}},
                "properties": {"doc": {"$ref": "#/$defs/Doc"}}}
    k = kinds(probe.mcp_tool_drift("d", [_tool("get", schema("string"))], [_tool("get", schema("integer"))]))
    assert ("tool:get.input.doc.id", "type_changed", "breaking") in k


def test_combinator_changes_are_reported_not_ignored():
    b = {"type": "object", "properties": {"q": {"anyOf": [{"type": "string"}, {"type": "integer"}]}}}
    a = {"type": "object", "properties": {"q": {"anyOf": [{"type": "string"}]}}}
    k = kinds(probe.mcp_tool_drift("d", [_tool("s", b)], [_tool("s", a)]))
    assert ("tool:s.input.q", "combinator_changed", "silent") in k
    assert probe.mcp_tool_drift("d", [_tool("s", b)], [_tool("s", b)]) == []
