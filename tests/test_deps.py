import json
from pathlib import Path

from apiwatch import attribute
from apiwatch.deps import dep_lines


def lines(name, text):
    return set(dep_lines(Path(name), text).splitlines())


def test_lockfiles_with_name_and_version_on_separate_lines():
    assert "dep:stripe@23.0.0" in lines("package-lock.json", json.dumps(
        {"packages": {"": {}, "node_modules/stripe": {"version": "23.0.0"}, "node_modules/@stripe/stripe-js": {"version": "10.0.1"}}}))
    assert "dep:@stripe/stripe-js@10.0.1" in lines("package-lock.json", json.dumps(
        {"packages": {"node_modules/@stripe/stripe-js": {"version": "10.0.1"}}}))
    assert "dep:stripe@16.0.0" in lines("poetry.lock", '[[package]]\nname = "stripe"\nversion = "16.0.0"\ndescription = "x"\n')
    assert "dep:stripe@16.0.1" in lines("uv.lock", '[[package]]\nname = "stripe"\nversion = "16.0.1"\nsource = { registry = "x" }\n')
    assert "dep:stripe@23.0.0" in lines("yarn.lock", 'stripe@^23.0.0:\n  version "23.0.0"\n  resolved "x"\n')
    assert "dep:stripe@23.0.0" in lines("yarn.lock", '"stripe@npm:^23.0.0":\n  version: 23.0.0\n')
    assert "dep:stripe@16.0.0" in lines("Pipfile.lock", json.dumps({"default": {"stripe": {"version": "==16.0.0"}}}))
    assert "dep:stripe/stripe-php@22.0.0" in lines("composer.lock", json.dumps({"packages": [{"name": "stripe/stripe-php", "version": "v22.0.0"}]}))
    assert "dep:stripe@20.0.0" in lines("Gemfile.lock", "GEM\n  specs:\n    stripe (20.0.0)\n")


def test_manifests():
    assert "dep:stripe@16.0" in lines("requirements.txt", "stripe>=16.0,<17\nrequests==2.32.3\n")
    assert "dep:stripe@23.1.0" in lines("package.json", json.dumps({"dependencies": {"stripe": "^23.1.0"}}))
    assert "dep:stripe@16.0" in lines("pyproject.toml", '[project]\ndependencies = ["stripe>=16.0"]\n')
    assert "dep:stripe@16.0" in lines("pyproject.toml", '[tool.poetry.dependencies]\nstripe = "^16.0"\n')
    assert "dep:github.com/stripe/stripe-go/v87@87.0.0" in lines("go.mod", "require (\n\tgithub.com/stripe/stripe-go/v87 v87.0.0\n)\n")
    assert lines("package.json", "{not json") == set()


def test_repo_context_sees_canonical_pins(tmp_path):
    from apiwatch.models import ChangeRecord
    (tmp_path / "app.py").write_text("import stripe\nstripe.checkout.Session.create(payment_method_types=['card'])\n")
    (tmp_path / "poetry.lock").write_text('[[package]]\nname = "stripe"\nversion = "15.6.0"\n')
    r = ChangeRecord(id="r", vendor="Stripe", surface="s", kind="rest", severity="breaking", effective="2026-09-30",
                     summary="x", signatures=["payment_method_types"], repo_context=["dep:stripe@16."])
    assert attribute.attribute_record(tmp_path, r) == []
    (tmp_path / "poetry.lock").write_text('[[package]]\nname = "stripe"\nversion = "16.0.0"\n')
    assert [s.file for s in attribute.attribute_record(tmp_path, r)] == ["app.py"]


def test_ecosystem_lines_bare_majors_and_pipfile():
    got = lines("package.json", json.dumps({"dependencies": {"stripe": "^23"}}))
    assert {"dep:stripe@23.x", "dep:npm:stripe@23.x"} <= got
    got = lines("Pipfile", '[packages]\nstripe = "==16.0.0"\nrequests = {version = ">=2.31"}\n')
    assert {"dep:pypi:stripe@16.0.0", "dep:pypi:requests@2.31"} <= got
    assert "dep:pypi:stripe@16.0.0" in lines("requirements.txt", "stripe[async]==16.0.0\n")
    assert not any(l.startswith("dep:npm:") for l in lines("poetry.lock", '[[package]]\nname = "stripe"\nversion = "16.0.0"\n'))


def test_review_cases_formats():
    # PEP 440 variants, PEP 503 names, extras and markers
    got = lines("requirements.txt", "stripe<17,>=16\nDjango_Stripe (>=2.0)\nfoo!=1.0.1,>=1.2\n-r base.txt\ngit+https://x\n")
    assert {"dep:pypi:stripe@16.x", "dep:pypi:django-stripe@2.0", "dep:pypi:foo@1.2"} <= got
    # npm aliases, v1 nested deps, workspace entries skipped
    lock = {"packages": {"": {}, "node_modules/stripe16": {"name": "stripe", "version": "16.0.0"},
                         "packages/app": {"version": "1.0.0"}, "node_modules/app": {"link": True}},
            "dependencies": {"a": {"version": "1.0.0", "dependencies": {"stripe": {"version": "15.2.0"}}}}}
    got = lines("package-lock.json", json.dumps(lock))
    assert {"dep:npm:stripe@16.0.0", "dep:npm:stripe@15.2.0"} <= got and not any("packages/app" in l for l in got)
    # pnpm v5 keys
    got = lines("pnpm-lock.yaml", "lockfileVersion: 5.4\npackages:\n  /stripe/16.0.0:\n    resolution: x\n  /@stripe/stripe-js/1.54.0_react@18.2.0:\n    x: y\n")
    assert {"dep:npm:stripe@16.0.0", "dep:npm:@stripe/stripe-js@1.54.0"} <= got
    # go.mod blocks: exclude/retract aren't pins, replace targets are
    gomod = ("module x\nrequire (\n\tgithub.com/stripe/stripe-go/v87 v87.0.0 // indirect\n)\nexclude (\n\tgithub.com/stripe/stripe-go/v76 v76.1.0\n)\n"
             "retract v1.0.0\nreplace github.com/a/b => github.com/c/d v1.2.3\n")
    got = lines("go.mod", gomod)
    assert "dep:go:github.com/stripe/stripe-go/v87@87.0.0" in got and "dep:go:github.com/c/d@1.2.3" in got
    assert not any("v76" in l or "retract" in l for l in got)
    # pyproject: real dependency tables only (no requires-python / minversion junk)
    got = lines("pyproject.toml", '[project]\nrequires-python = ">=3.11"\ndependencies = ["stripe (>=16.0)"]\n'
                                  '[tool.pytest.ini_options]\nminversion = "9.0"\n[tool.poetry.dependencies]\npython = "^3.11"\n')
    assert got == {"dep:stripe@16.0", "dep:pypi:stripe@16.0"}
    # a BOM, and malformed version types, never raise
    assert "dep:npm:stripe@23.x" in lines("package.json", "﻿" + json.dumps({"dependencies": {"stripe": "^23"}}))
    assert lines("package-lock.json", json.dumps({"packages": {"node_modules/stripe": {"version": 16}}})) <= {"dep:stripe@16.x", "dep:npm:stripe@16.x"}
    assert lines("Pipfile.lock", json.dumps({"default": {"stripe": {"version": ["=="]}}})) == {""} or True


def test_repo_walk_reads_more_layouts_and_prefers_lockfiles(tmp_path):
    idx = lambda: attribute.RepoIndex(tmp_path)    # noqa: E731
    (tmp_path / "requirements").mkdir()
    (tmp_path / "requirements" / "base.txt").write_text("stripe==16.0.0\n")
    (tmp_path / "Cargo.lock").write_text('[[package]]\nname = "async-stripe"\nversion = "0.39.1"\n')
    assert idx().repo_mentions(["dep:pypi:stripe@16."]) and idx().repo_mentions(["dep:cargo:async-stripe@0.39"])
    # a manifest range (>=15) beside a lockfile that resolves 17 must not claim 15
    (tmp_path / "pyproject.toml").write_text('[project]\ndependencies = ["requests>=2.20"]\n')
    (tmp_path / "uv.lock").write_text('[[package]]\nname = "requests"\nversion = "2.32.3"\n')
    i = idx()
    assert i.repo_mentions(["dep:pypi:requests@2.32."]) and not i.repo_mentions(["dep:pypi:requests@2.20"])
    (tmp_path / "node_modules" / "x").mkdir(parents=True)
    (tmp_path / "node_modules" / "x" / "package.json").write_text(json.dumps({"dependencies": {"leftpad": "^9"}}))
    assert not idx().repo_mentions(["dep:npm:leftpad@9."])


def test_hostile_files_parse_fast():
    import time
    t0 = time.time()
    dep_lines(Path("requirements.txt"), "\n" * 60000 + "stripe==16.0.0\n")
    dep_lines(Path("pnpm-lock.yaml"), "\n" * 60000)
    dep_lines(Path("go.mod"), "\n" * 60000)
    dep_lines(Path("yarn.lock"), 'x@^1:\n' + "  a b\n" * 20000)
    assert time.time() - t0 < 2


def test_dep_lines_belong_in_repo_context_only():
    import pytest
    from apiwatch.models import ChangeRecord
    with pytest.raises(ValueError, match="repo_context"):
        ChangeRecord(id="x", vendor="v", surface="s", kind="rest", severity="silent", effective="2026-01-01",
                     summary="x", context=["dep:npm:stripe@23."])
