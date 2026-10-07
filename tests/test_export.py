import importlib.util
from datetime import date
from pathlib import Path

import pytest

from apiwatch.models import load_records

ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "scripts" / "export_public.py"

# the export script lives only in the private repo; this file is exported with the other tests
pytestmark = pytest.mark.skipif(not SCRIPT.exists(), reason="export script is private")


def load_export():
    spec = importlib.util.spec_from_file_location("export_public", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_export_fills_what_it_caught_from_published_records(tmp_path):
    exp = load_export()
    assert exp.main(str(tmp_path / "public"), today=date(2026, 10, 6)) == 0      # passes the banned-term check
    readme = (tmp_path / "public" / "README.md").read_text()
    assert "what-it-caught" not in readme and readme.count("## What it caught\n") == 1
    published = [r for r in load_records(ROOT / "feed" / "records") if r.published]
    assert f"The feed holds {len(published)} reviewed changes" in readme
    assert "ExampleDocs" not in readme                                          # the illustrative record
    section = readme.split("## What it caught\n")[1].split("\n## ")[0]
    nxt = sorted((r for r in published if r.effective >= "2026-10-06"), key=lambda r: r.effective)[:5]
    for r in nxt:
        assert f"(https://apiwatch.reignventures.co/records/{r.id})" in section
    assert section.count("](https://apiwatch.reignventures.co/records/") == len(nxt)
    assert "—" not in section and "–" not in section


def test_export_leaves_out_records_that_failed_the_source_check(tmp_path):
    import json
    exp = load_export()
    published = sorted((r for r in load_records(ROOT / "feed" / "records") if r.published), key=lambda r: r.effective)
    gone = published[-1]                       # the latest dated record, so it would be in the next dates
    results = tmp_path / "check.json"
    results.write_text(json.dumps([{"id": gone.id, "status": "missing"}, {"id": published[0].id, "status": "error"}]))
    assert exp.main(str(tmp_path / "public"), today=date.fromisoformat(gone.effective),
                    check_results=str(results)) == 0
    readme = (tmp_path / "public" / "README.md").read_text()
    assert f"The feed holds {len(published) - 1} reviewed changes" in readme and gone.id not in readme


def test_export_refuses_a_readme_without_the_marker(tmp_path):
    exp = load_export()
    readme = tmp_path / "README.md"
    readme.write_text("# apiwatch\n")
    with pytest.raises(SystemExit):
        exp.fill_readme(readme, date(2026, 10, 6))
