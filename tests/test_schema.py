import json
from pathlib import Path

from apiwatch import schema

S = Path(__file__).parent.parent / "samples" / "responses"


def test_infer_and_diff_catches_nesting_nullability_and_additions():
    before = schema.infer(json.loads((S / "shipments_v1.json").read_text()))
    after = schema.infer(json.loads((S / "shipments_v2.json").read_text()))
    events = schema.diff("shipments", before, after)
    changes = {(e.path, e.change) for e in events}
    assert ("$.data[].tracking_number", "removed") in changes
    assert ("$.data[].shipment", "added") in changes
    assert ("$.data[].eta", "became_nullable") in changes
    assert ("$.next_cursor", "added") in changes
    sev = {e.path: e.severity for e in events}
    assert sev["$.data[].tracking_number"] == "breaking"
    assert sev["$.data[].eta"] == "silent"


def test_type_change_is_breaking():
    events = schema.diff("x", schema.infer({"amount": "1000"}), schema.infer({"amount": 1000.0}))
    assert events[0].change == "type_changed" and events[0].severity == "breaking"


def test_no_drift_on_identical_shape():
    a = schema.infer({"a": [{"b": 1}]})
    assert schema.diff("x", a, a) == []
