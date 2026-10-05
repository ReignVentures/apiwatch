import json
from pathlib import Path

from apiwatch import probe

S = Path(__file__).parent.parent / "samples" / "responses"


def test_probe_establishes_then_detects(tmp_path):
    ep = probe.Endpoint(id="shipments", source=f"file:{S / 'shipments_v1.json'}")
    assert probe.probe(ep, tmp_path) == []            # baseline created
    ep2 = probe.Endpoint(id="shipments", source=f"file:{S / 'shipments_v2.json'}")
    events = probe.probe(ep2, tmp_path)
    assert any(e.change == "removed" and e.path.endswith("tracking_number") for e in events)
    # baseline unchanged unless asked
    assert probe.probe(ep2, tmp_path) == events
    probe.probe(ep2, tmp_path, update_baseline=True)
    assert probe.probe(ep2, tmp_path) == []


def test_mcp_tool_rename_is_breaking():
    before = json.loads((S / "mcp_tools_before.json").read_text())["tools"]
    after = json.loads((S / "mcp_tools_after.json").read_text())["tools"]
    events = probe.mcp_tool_drift("exampledocs", before, after)
    kinds = {(e.path, e.change, e.severity) for e in events}
    assert ("tool:search_documents", "removed", "breaking") in kinds
    assert ("tool:query_documents", "added", "additive") in kinds
