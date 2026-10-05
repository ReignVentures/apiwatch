from pathlib import Path

from apiwatch.cli import main

ROOT = Path(__file__).parent.parent


def test_feed_build(tmp_path):
    assert main(["feed", "--records", str(ROOT / "feed/records"), "--out", str(tmp_path)]) == 0
    assert (tmp_path / "feed.json").exists() and (tmp_path / "feed.md").exists()
    assert (tmp_path / "vendors" / "stripe.md").exists()


def test_scan_fails_on_breaking(tmp_path):
    rc = main(["scan", str(ROOT / "samples/repo"), "--records", str(ROOT / "feed/records"),
               "--out", str(tmp_path), "--fail-on-breaking"])
    assert rc == 1
    assert (tmp_path / "anthropic-2026-06-15-claude-4-retirement.md").exists()


def test_probe_cli(tmp_path):
    v1 = str(ROOT / "samples/responses/shipments_v1.json")
    v2 = str(ROOT / "samples/responses/shipments_v2.json")
    assert main(["probe", "shipments", f"file:{v1}", "--root", str(tmp_path)]) == 0
    assert main(["probe", "shipments", f"file:{v2}", "--root", str(tmp_path), "--fail-on-drift"]) == 1
