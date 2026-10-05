"""Infer a structural schema from a JSON response, and diff two schemas into DriftEvents.

No OpenAPI spec required: the baseline is whatever the endpoint returned last time.
"""
from __future__ import annotations

from typing import Any

from .models import DriftEvent


def infer(value: Any) -> dict:
    """Return a compact structural schema: {"type": ..., "nullable": bool, "fields": {...}, "items": {...}}."""
    if value is None:
        return {"type": "null", "nullable": True}
    if isinstance(value, bool):
        return {"type": "boolean", "nullable": False}
    if isinstance(value, int):
        return {"type": "integer", "nullable": False}
    if isinstance(value, float):
        return {"type": "number", "nullable": False}
    if isinstance(value, str):
        return {"type": "string", "nullable": False}
    if isinstance(value, list):
        merged: dict | None = None
        for item in value:
            s = infer(item)
            merged = s if merged is None else _merge(merged, s)
        return {"type": "array", "nullable": False, "items": merged or {"type": "unknown", "nullable": False}}
    if isinstance(value, dict):
        return {"type": "object", "nullable": False, "fields": {k: infer(v) for k, v in value.items()}}
    return {"type": "unknown", "nullable": False}


def _merge(a: dict, b: dict) -> dict:
    """Merge two schemas seen for the same slot (e.g. items in a list)."""
    if a["type"] == "null":
        return {**b, "nullable": True}
    if b["type"] == "null":
        return {**a, "nullable": True}
    if a["type"] != b["type"]:
        return {"type": f"{a['type']}|{b['type']}", "nullable": a["nullable"] or b["nullable"]}
    out = dict(a)
    out["nullable"] = a["nullable"] or b["nullable"]
    if a["type"] == "object":
        fields = dict(a.get("fields", {}))
        for k, v in b.get("fields", {}).items():
            fields[k] = _merge(fields[k], v) if k in fields else {**v, "optional": True}
        for k in a.get("fields", {}):
            if k not in b.get("fields", {}):
                fields[k] = {**fields[k], "optional": True}
        out["fields"] = fields
    if a["type"] == "array":
        out["items"] = _merge(a["items"], b["items"])
    return out


def diff(endpoint_id: str, before: dict, after: dict, path: str = "$") -> list[DriftEvent]:
    events: list[DriftEvent] = []
    if before["type"] != after["type"]:
        sev = "silent" if after["type"] == "null" or before["type"] == "null" else "breaking"
        events.append(DriftEvent(endpoint_id, path, "type_changed", before["type"], after["type"], sev))
        return events
    if not before.get("nullable") and after.get("nullable"):
        events.append(DriftEvent(endpoint_id, path, "became_nullable", False, True, "silent"))
    if before["type"] == "object":
        bf, af = before.get("fields", {}), after.get("fields", {})
        for k in bf:
            if k not in af:
                events.append(DriftEvent(endpoint_id, f"{path}.{k}", "removed", bf[k]["type"], None, "breaking"))
            else:
                events += diff(endpoint_id, bf[k], af[k], f"{path}.{k}")
        for k in af:
            if k not in bf:
                events.append(DriftEvent(endpoint_id, f"{path}.{k}", "added", None, af[k]["type"], "additive"))
    if before["type"] == "array":
        events += diff(endpoint_id, before["items"], after["items"], f"{path}[]")
    return events
