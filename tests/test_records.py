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



def test_dependency_gates_name_their_ecosystem():
    """dep:stripe@16. would also match the npm package stripe 16.x; records must say dep:pypi:… / dep:npm:…"""
    import re
    for r in RECORDS:
        for c in r.repo_context:
            for w in ([c] if isinstance(c, str) else c):
                if w.startswith("dep:"):
                    assert re.match(r"dep:(npm|pypi|rubygems|packagist|go|cargo):", w), (r.id, w)
