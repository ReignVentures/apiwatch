"""Probe an endpoint (live or from a sampled response file), keep a baseline, emit drift.

Sources:
  - file:           a JSON file on disk (used by tests and demos, no network)
  - https:          a GET with optional headers (requires the 'live' extra: httpx)
  - mcp:stdio:<cmd> an MCP server spawned locally; baseline is its tools/list
  - mcp:https://... an MCP server over Streamable HTTP
  - mcp:file:<path> a saved tools/list result ({"tools": [...]}), no network
Baselines live under .apiwatch/baselines/<endpoint_id>.json.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import mcp_client, schema
from .models import DriftEvent


@dataclass
class Endpoint:
    id: str
    source: str                 # "file:samples/responses/x.json" or "https://..."
    headers: dict[str, str] | None = None
    kind: str = "rest"          # rest | mcp

    def __post_init__(self):
        if self.source.startswith("mcp:"):
            self.kind = "mcp"


def fetch(ep: Endpoint) -> Any:
    if ep.source.startswith("file:"):
        return json.loads(Path(ep.source[5:]).read_text())
    if ep.source.startswith("http"):
        import httpx  # optional dependency
        r = httpx.get(ep.source, headers=ep.headers or {}, timeout=30)
        return {"__status__": r.status_code, "body": r.json()}
    raise ValueError(f"unsupported source: {ep.source}")


def baseline_path(root: Path, endpoint_id: str) -> Path:
    return root / ".apiwatch" / "baselines" / f"{endpoint_id}.json"


def fetch_mcp(ep: Endpoint) -> dict:
    """Snapshot an MCP server's tool surface: {"kind": "mcp", "server": {...}, "tools": [...]}."""
    target = ep.source[4:]
    if target.startswith("file:"):
        raw = json.loads(Path(target[5:]).read_text())
    else:
        raw = mcp_client.list_tools(target, headers=ep.headers)
    tools = sorted((_tool_snapshot(t) for t in raw.get("tools", [])), key=lambda t: t["name"])
    snap = {"kind": "mcp", "server": raw.get("server", {}), "tools": tools}
    for k in ("protocolVersion", "era"):
        if raw.get(k):
            snap[k] = raw[k]
    return snap


def _tool_snapshot(t: dict) -> dict:
    keep = ("name", "title", "description", "inputSchema", "outputSchema", "annotations")
    return {k: t[k] for k in keep if k in t}


def probe(ep: Endpoint, root: Path, update_baseline: bool = False) -> list[DriftEvent]:
    if ep.kind == "mcp":
        return _probe_mcp(ep, root, update_baseline)
    body = fetch(ep)
    current = schema.infer(body)
    bp = baseline_path(root, ep.id)
    if not bp.exists():
        bp.parent.mkdir(parents=True, exist_ok=True)
        bp.write_text(json.dumps(current, indent=2))
        return []  # first run establishes the baseline
    previous = json.loads(bp.read_text())
    events = schema.diff(ep.id, previous, current)
    if update_baseline:
        bp.write_text(json.dumps(current, indent=2))
    return events


def _probe_mcp(ep: Endpoint, root: Path, update_baseline: bool) -> list[DriftEvent]:
    current = fetch_mcp(ep)
    bp = baseline_path(root, ep.id)
    if not bp.exists():
        bp.parent.mkdir(parents=True, exist_ok=True)
        bp.write_text(json.dumps(current, indent=2))
        return []
    previous = json.loads(bp.read_text())
    if previous.get("kind") != "mcp":
        raise ValueError(f"{ep.id}: baseline is not an MCP baseline; use a different endpoint id")
    events = mcp_tool_drift(ep.id, previous["tools"], current["tools"])
    bv, av = previous.get("server", {}).get("version"), current.get("server", {}).get("version")
    if bv and av and bv != av:
        events.append(DriftEvent(ep.id, "server.version", "version_changed", bv, av, "info"))
    bp_, ap_ = previous.get("protocolVersion"), current.get("protocolVersion")
    if bp_ and ap_ and bp_ != ap_:
        # e.g. a server moving to the stateless 2026-07-28 protocol: old clients may stop working
        events.append(DriftEvent(ep.id, "server.protocolVersion", "protocol_changed", bp_, ap_, "info"))
    if update_baseline:
        bp.write_text(json.dumps(current, indent=2))
    return events


def mcp_tool_drift(endpoint_id: str, before_tools: list[dict], after_tools: list[dict]) -> list[DriftEvent]:
    """Compare two MCP tools/list results. Renamed or removed tools are the agent-killers.

    Input schemas are compared as JSON Schema from the caller's side: anything an existing
    call would now fail on (removed/retyped/newly required param, removed enum value) is breaking.
    """
    b = {t["name"]: t for t in before_tools}
    a = {t["name"]: t for t in after_tools}
    removed = [n for n in b if n not in a]
    added = [n for n in a if n not in b]
    events: list[DriftEvent] = []
    for name in removed:
        events.append(DriftEvent(endpoint_id, f"tool:{name}", "removed", name, None, "breaking"))
    for name in added:
        events.append(DriftEvent(endpoint_id, f"tool:{name}", "added", None, name, "additive"))
    for old in removed:
        new = _likely_rename(b[old], [a[n] for n in added])
        if new:
            events.append(DriftEvent(endpoint_id, f"tool:{old}", "possible_rename", old, new, "info"))
    for name in sorted(b.keys() & a.keys()):
        bt, at = b[name], a[name]
        events += _input_schema_drift(endpoint_id, f"tool:{name}.input", bt.get("inputSchema", {}), at.get("inputSchema", {}))
        if bt.get("outputSchema") or at.get("outputSchema"):
            events += _output_schema_drift(endpoint_id, f"tool:{name}.output",
                                           bt.get("outputSchema") or {}, at.get("outputSchema") or {})
        if (bt.get("description") or "") != (at.get("description") or ""):
            # agents pick tools by description, so a rewrite can change which tool gets called
            events.append(DriftEvent(endpoint_id, f"tool:{name}.description", "description_changed",
                                     bt.get("description"), at.get("description"), "info"))
    return events


def _props(s: dict) -> tuple[dict, set]:
    return s.get("properties", {}) or {}, set(s.get("required", []) or [])


def _jtype(s: dict) -> str:
    t = s.get("type")
    if isinstance(t, list):
        return "|".join(sorted(t))
    return t or "any"


COMBINATORS = ("anyOf", "oneOf", "allOf", "not", "if", "then", "else")


def _resolve(schema: dict, root: dict, depth: int = 0) -> dict:
    """Inline a local "$ref": "#/$defs/X" (or #/definitions/X). Remote refs are left as-is."""
    ref = schema.get("$ref") if isinstance(schema, dict) else None
    if not (isinstance(ref, str) and ref.startswith("#/")) or depth > 20:
        return schema
    node = root
    for part in ref[2:].split("/"):
        if not isinstance(node, dict) or part not in node:
            return schema
        node = node[part]
    merged = {**node, **{k: v for k, v in schema.items() if k != "$ref"}}
    return _resolve(merged, root, depth + 1)


def _combinators_changed(eid, path, before, after) -> list[DriftEvent]:
    b = {k: before[k] for k in COMBINATORS if k in before}
    a = {k: after[k] for k in COMBINATORS if k in after}
    if json.dumps(b, sort_keys=True) != json.dumps(a, sort_keys=True):
        # not classified precisely yet: report it rather than let it pass silently (docs/GAPS.md)
        return [DriftEvent(eid, path, "combinator_changed", sorted(b), sorted(a), "silent")]
    return []


def _input_schema_drift(eid: str, path: str, before: dict, after: dict,
                        broot: dict | None = None, aroot: dict | None = None) -> list[DriftEvent]:
    broot, aroot = broot if broot is not None else before, aroot if aroot is not None else after
    before, after = _resolve(before, broot), _resolve(after, aroot)
    events: list[DriftEvent] = _combinators_changed(eid, path, before, after)
    if _jtype(before) != _jtype(after):
        return [DriftEvent(eid, path, "type_changed", _jtype(before), _jtype(after), "breaking")]
    be, ae = before.get("enum"), after.get("enum")
    if be is not None and ae is not None:
        gone = [v for v in be if v not in ae]
        new = [v for v in ae if v not in be]
        if gone:
            events.append(DriftEvent(eid, path, "enum_values_removed", gone, None, "breaking"))
        if new:
            events.append(DriftEvent(eid, path, "enum_values_added", None, new, "additive"))
    elif be is None and ae is not None:
        events.append(DriftEvent(eid, path, "enum_added", None, ae, "breaking"))
    bp, breq = _props(before)
    ap, areq = _props(after)
    for k in bp:
        if k not in ap:
            events.append(DriftEvent(eid, f"{path}.{k}", "removed", _jtype(bp[k]), None, "breaking"))
    for k in ap:
        if k not in bp:
            req = k in areq
            events.append(DriftEvent(eid, f"{path}.{k}", "added_required" if req else "added",
                                     None, _jtype(ap[k]), "breaking" if req else "additive"))
    for k in bp:
        if k in ap:
            if k in areq and k not in breq:
                events.append(DriftEvent(eid, f"{path}.{k}", "became_required", False, True, "breaking"))
            elif k in breq and k not in areq:
                events.append(DriftEvent(eid, f"{path}.{k}", "became_optional", True, False, "additive"))
            events += _input_schema_drift(eid, f"{path}.{k}", bp[k], ap[k], broot, aroot)
    if _jtype(before) == "array" and isinstance(before.get("items"), dict) and isinstance(after.get("items"), dict):
        events += _input_schema_drift(eid, f"{path}[]", before["items"], after["items"], broot, aroot)
    return events


def _output_schema_drift(eid: str, path: str, before: dict, after: dict,
                         broot: dict | None = None, aroot: dict | None = None) -> list[DriftEvent]:
    """Tool output, judged from the reader's side: what breaks code that parses structuredContent?"""
    broot, aroot = broot if broot is not None else before, aroot if aroot is not None else after
    before, after = _resolve(before, broot), _resolve(after, aroot)
    events: list[DriftEvent] = _combinators_changed(eid, path, before, after)
    if not before and after:
        return [DriftEvent(eid, path, "added", None, _jtype(after), "additive")]
    if before and not after:
        return [DriftEvent(eid, path, "removed", _jtype(before), None, "breaking")]
    if _jtype(before) != _jtype(after):
        return events + [DriftEvent(eid, path, "type_changed", _jtype(before), _jtype(after), "breaking")]
    be, ae = before.get("enum"), after.get("enum")
    if be is not None and ae is not None:
        new = [v for v in ae if v not in be]
        gone = [v for v in be if v not in ae]
        if new:   # readers with a switch over known values meet one they don't handle
            events.append(DriftEvent(eid, path, "enum_values_added", None, new, "silent"))
        if gone:
            events.append(DriftEvent(eid, path, "enum_values_removed", gone, None, "additive"))
    bp, breq = _props(before)
    ap, areq = _props(after)
    for k in bp:
        if k not in ap:
            events.append(DriftEvent(eid, f"{path}.{k}", "removed", _jtype(bp[k]), None, "breaking"))
    for k in ap:
        if k not in bp:
            events.append(DriftEvent(eid, f"{path}.{k}", "added", None, _jtype(ap[k]), "additive"))
    for k in bp:
        if k in ap:
            if k in breq and k not in areq:   # may now be missing; readers that index it break quietly
                events.append(DriftEvent(eid, f"{path}.{k}", "became_optional", True, False, "silent"))
            elif k in areq and k not in breq:
                events.append(DriftEvent(eid, f"{path}.{k}", "became_required", False, True, "additive"))
            events += _output_schema_drift(eid, f"{path}.{k}", bp[k], ap[k], broot, aroot)
    if _jtype(before) == "array" and isinstance(before.get("items"), dict) and isinstance(after.get("items"), dict):
        events += _output_schema_drift(eid, f"{path}[]", before["items"], after["items"], broot, aroot)
    return events


def _likely_rename(old: dict, candidates: list[dict]) -> str | None:
    """A removed tool whose parameters mostly reappear on exactly one added tool."""
    op = set(_props(old.get("inputSchema", {}))[0])
    best = []
    for c in candidates:
        cp = set(_props(c.get("inputSchema", {}))[0])
        union = op | cp
        if union and len(op & cp) / len(union) >= 0.5:
            best.append(c["name"])
    return best[0] if len(best) == 1 else None
