from pathlib import Path

import pytest

from apiwatch import attribute
from apiwatch.attribute import find_in_line
from apiwatch.models import DriftEvent


@pytest.mark.parametrize("line,sig,hit", [
    ('model="gpt-4"', "gpt-4", True),
    ('model="gpt-4-0613"', "gpt-4", True),          # dated snapshot of the same model
    ('model="gpt-4-turbo"', "gpt-4", True),         # variant, also retiring
    ('model="gpt-4o"', "gpt-4", False),
    ('model="gpt-4o-mini"', "gpt-4", False),
    ('model="gpt-4.1"', "gpt-4", False),            # a version continues after the dot
    ('model="chatgpt-4o-latest"', "gpt-4", False),
    ('MODEL = "o1"', "o1", True),
    ('MODEL = "o1-pro"', "o1", True),
    ('hash = "a0o1b"', "o1", False),
    ("x = foo1", "o1", False),
    ('"claude-opus-4-1-20250805"', "claude-opus-4-1", True),
    ('"claude-opus-4-1"', "claude-opus-4-1", True),
    ('"claude-opus-4-10"', "claude-opus-4-1", False),
    ('"grok-3.5"', "grok-3", False),
    ("price.fx_rate_limit", "fx_rate", False),
    ("price.fx_rate", "fx_rate", True),
    ("the_unit_amount_decimal", "unit_amount_decimal", False),
    ('url = "https://api.openai.com/v1/prompts/abc"', "v1/prompts", True),
    ('id = "pmpt_68ab12"', "pmpt_", True),                 # trailing punctuation = deliberate prefix
    ('"https://harvest.greenhouse.io/v1/candidates"', "/v1/candidates", True),
    ('"gpt-4o-search-preview-2025-03-11"', "search-preview-2025-03-11", True),  # '-' on the left is a boundary
    ('"anthropic.claude-3-haiku-20240307-v1:0"', "claude-3-haiku", True),       # Bedrock-style prefix
    ('allowed = ["mcp__github__list_workflows"]', "list_workflows", True),      # Claude Code MCP tool names
    ("my_list_workflows()", "list_workflows", False),
    ("self.__temperature = 0.2", "temperature", False),                       # private name, not an MCP join
    ('pipe.set_params(retriever__top_k=5)', "top_k", False),
])
def test_token_aware_matching(line, sig, hit):
    assert (find_in_line(line, sig) >= 0) is hit


def write(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def test_most_specific_signature_wins_on_a_line(tmp_path):
    write(tmp_path, "a.py", 'M = "gpt-4-0613"\n')
    sites = attribute.scan(tmp_path, ["gpt-4", "gpt-4-0613"])
    assert [(s.line, s.signature) for s in sites] == [(1, "gpt-4-0613")]


def test_two_different_hits_on_one_line_are_both_reported(tmp_path):
    write(tmp_path, "a.py", 'MODELS = ["gpt-4", "o1"]\n')
    assert sorted(s.signature for s in attribute.scan(tmp_path, ["gpt-4", "o1"])) == ["gpt-4", "o1"]


def test_comments_env_files_and_skipped_dirs(tmp_path):
    write(tmp_path, "a.py", '# MODEL = "gpt-4"  (old)\nMODEL = "gpt-4"\n')
    write(tmp_path, "b.ts", '// model: "gpt-4"\nconst m = "gpt-4";\n')
    write(tmp_path, ".env", "OPENAI_MODEL=gpt-4\n")
    write(tmp_path, ".env.production", "OPENAI_MODEL=gpt-4\n")
    write(tmp_path, "node_modules/x/index.js", 'const m = "gpt-4";\n')
    write(tmp_path, "notes.md", "we used gpt-4\n")
    got = sorted((s.file, s.line) for s in attribute.scan(tmp_path, ["gpt-4"]))
    assert got == [(".env", 1), (".env.production", 1), ("a.py", 2), ("b.ts", 2)]


def test_python_constant_followed_across_modules(tmp_path):
    write(tmp_path, "app/__init__.py", "")
    write(tmp_path, "app/config.py", 'MODEL: str = "claude-opus-4-0"\nOTHER = "x"\n')
    write(tmp_path, "app/agent.py",
          "from .config import MODEL\n\n"
          "def run(client):\n"
          "    return client.messages.create(model=MODEL)\n")
    write(tmp_path, "scripts/job.py",
          "from app.config import MODEL as M\n"
          "print(M)\n"
          "print(OTHER)\n")
    write(tmp_path, "unrelated.py", "MODEL = 'something else'\nprint(MODEL)\n")
    sites = attribute.attribute_record(tmp_path, type("R", (), {"signatures": ["claude-opus-4-0"]})())
    got = sorted((s.file, s.line, s.via) for s in sites)
    assert got == [
        ("app/agent.py", 4, "MODEL (app/config.py:1)"),
        ("app/config.py", 1, ""),
        ("scripts/job.py", 2, "MODEL (app/config.py:1)"),
    ]


def test_js_ts_constant_followed_through_imports(tmp_path):
    write(tmp_path, "src/models.ts", 'export const CHAT_MODEL: string = "gpt-4";\nexport const OK = "gpt-4o";\n')
    write(tmp_path, "src/chat.ts",
          'import { CHAT_MODEL, OK } from "./models";\n'
          "export async function ask(c) {\n"
          "  return c.chat.completions.create({ model: CHAT_MODEL });\n"
          "}\n")
    write(tmp_path, "src/deep/other.js",
          "import { CHAT_MODEL as M } from '../models.js';\n"
          "send({ model: M });\n"
          "send({ model: OK });\n")
    sites = attribute.attribute_record(tmp_path, type("R", (), {"signatures": ["gpt-4"]})())
    got = sorted((s.file, s.line, s.via) for s in sites)
    assert got == [
        ("src/chat.ts", 3, "CHAT_MODEL (src/models.ts:1)"),
        ("src/deep/other.js", 2, "CHAT_MODEL (src/models.ts:1)"),
        ("src/models.ts", 1, ""),
    ]


def test_drift_signatures_use_the_tool_name_not_the_param():
    ev = DriftEvent("docs", "tool:get_document.input.id", "type_changed", "string", "integer", "breaking")
    assert attribute.signatures_for_drift(ev) == ["get_document"]
    ev = DriftEvent("shipments", "$.data[].tracking_number", "removed", "string", None, "breaking")
    assert attribute.signatures_for_drift(ev) == ["tracking_number"]


def rec(signatures, context=None):
    from apiwatch.models import ChangeRecord
    return ChangeRecord(id="r", vendor="v", surface="s", kind="rest", severity="silent", effective="2026-01-01",
                        summary="x", signatures=signatures, context=context or [])


def test_context_keeps_generic_signatures_to_vendor_files(tmp_path):
    write(tmp_path, "src/mcp_compat.py", "cursor = result.next_cursor\n")            # MCP SDK, not Notion
    write(tmp_path, "src/notion_sync.py", "cursor = page.next_cursor\n")             # path mentions notion
    write(tmp_path, "src/sync.py", "from notion_client import Client\nc = r['next_cursor']\n")
    got = sorted(s.file for s in attribute.attribute_record(tmp_path, rec(["next_cursor"], ["notion"])))
    assert got == ["src/notion_sync.py", "src/sync.py"]
    assert len(attribute.attribute_record(tmp_path, rec(["next_cursor"]))) == 3   # no context: all


def test_hits_in_tests_are_marked_and_listed_after_code(tmp_path):
    from apiwatch import brief
    write(tmp_path, "tests/test_models.py", 'M = "gpt-4"\n')
    write(tmp_path, "src/app.py", 'M = "gpt-4"\n')
    write(tmp_path, "web/chat.spec.ts", 'const m = "gpt-4";\n')
    r = rec(["gpt-4"])
    sites = attribute.attribute_record(tmp_path, r)
    assert {s.file: s.in_test for s in sites} == {"src/app.py": False, "tests/test_models.py": True, "web/chat.spec.ts": True}
    b = brief.brief_for_record(r, sites)
    assert b.index("src/app.py") < b.index("In tests (2)") < b.index("tests/test_models.py")


def test_one_index_serves_many_records(tmp_path):
    write(tmp_path, "a.py", 'X = "gpt-4"\nY = "o1"\n')
    idx = attribute.RepoIndex(tmp_path)
    assert [s.line for s in attribute.attribute_record(idx, rec(["gpt-4"]))] == [1]
    assert [s.line for s in attribute.attribute_record(idx, rec(["o1"]))] == [2]


def test_exclude_drops_hits_inside_excluded_ids(tmp_path):
    write(tmp_path, "a.py", 'A = "gpt-realtime"\nB = "gpt-realtime-2.1"\nC = "gpt-realtime-2.1-mini"\n'
                            'D = ["gpt-realtime-2.1", "gpt-realtime"]\nE = "ft:babbage-002:org::x1"\nF = "babbage-002"\n')
    got = attribute.scan(tmp_path, ["gpt-realtime", "babbage-002"], exclude=["gpt-realtime-2.1", "ft:babbage-002"])
    assert sorted((s.line, s.signature) for s in got) == [(1, "gpt-realtime"), (4, "gpt-realtime"), (6, "babbage-002")]
    r = rec(["gpt-realtime"])
    r.exclude = ["gpt-realtime-2.1"]
    assert [s.line for s in attribute.attribute_record(tmp_path, r)] == [1, 4]


def test_all_matches_on_a_line_keep_boundaries():
    assert attribute._all_in_line('x = ["o1", "o1-pro", "foo1", "o1"]', "o1") == [6, 12, 30]


def test_a_repeated_long_id_is_one_hit_not_two(tmp_path):
    write(tmp_path, "a.py", 'M = {"primary": "claude-opus-4-1-20250805", "fallback": "claude-opus-4-1-20250805"}\n')
    got = attribute.scan(tmp_path, ["claude-opus-4-1", "claude-opus-4-1-20250805"])
    assert [(s.line, s.signature) for s in got] == [(1, "claude-opus-4-1-20250805")]


def test_context_and_groups(tmp_path):
    write(tmp_path, "gds.py", 'fare = flight_data["taxes"]\n')                          # flight app, no Stripe
    write(tmp_path, "pay.py", 'import stripe\nd = {"flight_data": {"total": {"tax": {"taxes": []}}}}\n')
    r = rec(['"taxes"'], [["stripe", "flight_data"]])
    assert [s.file for s in attribute.attribute_record(tmp_path, r)] == ["pay.py"]


def test_files_globs_limit_a_record_to_a_language(tmp_path):
    write(tmp_path, "a.py", "import anthropic\nc.completions.create(prompt=p)\n")
    write(tmp_path, "b.ts", "import Anthropic from '@anthropic-ai/sdk';\nc.completions.create({prompt})\n")
    r = rec(["completions.create"], ["anthropic"])
    assert sorted(s.file for s in attribute.attribute_record(tmp_path, r)) == ["a.py", "b.ts"]
    r.files = ["*.py"]
    assert [s.file for s in attribute.attribute_record(tmp_path, r)] == ["a.py"]


def test_repo_context_reads_manifests(tmp_path):
    write(tmp_path, "app/pay.py", "import stripe\nstripe.PaymentIntent.create(payment_method_types=['card'])\n")
    r = rec(["payment_method_types"], ["stripe"])
    r.repo_context = ["15.7.0b"]
    assert attribute.attribute_record(tmp_path, r) == []                                 # GA SDK: not flagged
    write(tmp_path, "requirements.txt", "stripe==15.7.0b1\n")                             # preview SDK pinned
    assert [s.file for s in attribute.attribute_record(tmp_path, r)] == ["app/pay.py"]
    write(tmp_path, "node_modules/x/requirements.txt", "")                                # skipped dirs stay skipped


def test_scope_fields_are_validated():
    import pytest
    from apiwatch.models import ChangeRecord
    base = dict(id="x", vendor="v", surface="s", kind="rest", severity="silent", effective="2026-01-01", summary="x")
    with pytest.raises(ValueError, match="context"):
        ChangeRecord(**base, context=[["stripe", ""]])
    with pytest.raises(ValueError, match="repo_context"):
        ChangeRecord(**base, repo_context=[5])
    with pytest.raises(ValueError, match="files"):
        ChangeRecord(**base, files=[""])


def test_python_docstrings_are_prose_not_call_sites(tmp_path):
    write(tmp_path, "a.py", '"""Talks to gpt-4."""\n\ndef f():\n    """Uses gpt-4 by default.\n\n    gpt-4 is old.\n    """\n'
                            '    return call(model="gpt-4")\n\nX = """gpt-4 in a plain string"""\n')
    got = [s.line for s in attribute.scan(tmp_path, ["gpt-4"])]
    assert got == [8, 10]                     # the call and the non-docstring string; no docstring lines


def test_comments_are_found_outside_strings():
    m = attribute.comment_mask
    assert m(['x = "gpt-4"  # was gpt-4-0613'], ".py") == [(False, [(13, 29)])]
    assert m(['url = "https://api.example/#gpt-4"'], ".py") == [(False, [])]          # '#' inside a string
    assert m(['const m = "gpt-4"; // old: gpt-4'], ".ts")[0] == (False, [(19, 32)])
    assert m(['glob("src/**/*.ts"); const m = "gpt-4";'], ".ts")[0] == (False, [])     # '/*' inside a string
    lines = ["/* Output Sample", "The temperature was mild.", "*/", "cfg = {temperature: 0.2};"]
    assert [w for w, _ in m(lines, ".js")] == [True, True, True, False]
    assert m(["let x = 1; /* gpt-4 */"], ".js")[0] == (False, [(11, 22)])


def test_lexer_edge_cases_from_review():
    m = attribute.comment_mask
    assert m(['create(/* model */ "gpt-4")'], ".ts")[0] == (False, [(7, 18)])          # code after a mid-line comment
    assert m(["[ $# -eq 0 ] && MODEL=gpt-4"], ".sh")[0] == (False, [])                  # shell $#
    assert m(["url: https://h/#/models/gpt-4  # note"], ".yaml")[0] == (False, [(31, 37)])
    assert m(['P = """', "Rank #1: gpt-4", '"""'], ".py")[1] == (False, [])            # '#' inside a triple-quoted string
    lines = ["const h = `Accept: */*", "x`;", 'const m = "gpt-4";']
    assert m(lines, ".ts")[2] == (False, [])                                             # '*/*' inside a template literal
    assert m(["OPENAI_MODEL=gpt-4  # old"], "")[0] == (False, [(20, 25)])                # .env files


def test_trailing_and_block_comments_are_not_hits(tmp_path):
    write(tmp_path, "a.py", 'M = "gpt-4o"  # replaced gpt-4\nN = "gpt-4"\n')
    write(tmp_path, "b.js", '/* Output Sample\nWe used gpt-4 here.\n*/\nconst m = "gpt-4"; // gpt-4\n')
    got = sorted((s.file, s.line) for s in attribute.scan(tmp_path, ["gpt-4"]))
    assert got == [("a.py", 2), ("b.js", 4)]


def test_attribute_docstrings_are_prose(tmp_path):
    write(tmp_path, "t.py", 'class Params(TypedDict):\n    method: str\n    """Send notifications/initialized first."""\n')
    assert attribute.scan(tmp_path, ["notifications/initialized"]) == []


def test_excludes_see_arguments_on_later_lines(tmp_path):
    write(tmp_path, "c.js", "const a = await client.conferences.list({\n  status: 'in-progress',\n  limit: 20,\n});\n"
                            "const b = await client.conferences.list({\n  limit: 20,\n});\n")
    got = attribute.scan(tmp_path, ["conferences.list"], exclude=["conferences.list({ status:"])
    assert [s.line for s in got] == [5]


def test_regex_escaped_model_ids_match(tmp_path):
    write(tmp_path, "m.py", 'PATTERNS = [r"^gpt-5\\.1-chat-latest$", r"^gpt-5\\.10-chat$"]\n')
    got = attribute.scan(tmp_path, ["gpt-5.1-chat-latest"])
    assert [(s.line, s.signature) for s in got] == [(1, "gpt-5.1-chat-latest")]


def test_notebook_code_cells_are_scanned(tmp_path):
    import json
    nb = {"cells": [
        {"cell_type": "markdown", "source": ["We use gpt-4 below."]},
        {"cell_type": "code", "source": ["%pip install openai gpt-4\n", "MODEL = \"gpt-4\"  # @param [\"gpt-4o\"]\n"]},
        {"cell_type": "code", "source": ["def f():\n", "    \"\"\"Calls gpt-4.\"\"\"\n", "    return 1\n"]},
    ], "metadata": {}, "nbformat": 4}
    raw = json.dumps(nb, indent=1)
    (tmp_path / "demo.ipynb").write_text(raw)
    got = [(s.file, s.line, s.cell, s.snippet) for s in attribute.scan(tmp_path, ["gpt-4"])]
    want_line = next(i for i, l in enumerate(raw.splitlines(), 1) if "@param" in l)
    assert got == [("demo.ipynb", want_line, "cell 2, line 2", 'MODEL = "gpt-4"  # @param ["gpt-4o"]')]


def test_review_fixes_for_excludes_regex_and_prose(tmp_path):
    write(tmp_path, "a.py", 'P = [r"^gpt-4\\.1-mini$"]\nQ = r"^gpt-5\\.1$"\ndef f(model="gpt-4"): """doc about gpt-4"""\n')
    assert attribute.scan(tmp_path, ["gpt-4.1"], exclude=["gpt-4.1-mini"]) == []        # escaped exclude
    assert attribute.scan(tmp_path, ["gpt-5"]) == []                                     # gpt-5 vs gpt-5\.1
    got = attribute.scan(tmp_path, ["gpt-4"])
    assert [(s.line, s.snippet[:8]) for s in got] == [(3, "def f(mo")]                   # code beside a docstring counts once
    write(tmp_path, "c.js", "client.conferences.list(\n  {status: 'completed'});\n")
    assert attribute.scan(tmp_path, ["conferences.list"], exclude=["conferences.list({ status:"]) == []


def test_record_list_fields_must_be_lists():
    import pytest
    from apiwatch.models import ChangeRecord
    with pytest.raises(ValueError, match="context must be a list"):
        ChangeRecord(id="x", vendor="v", surface="s", kind="rest", severity="silent", effective="2026-01-01",
                     summary="x", context="stripe")


@pytest.mark.parametrize("src,tier", [
    ('client.messages.create(model="gpt-4")', "use"),
    ('DEFAULT_MODEL = "gpt-4"', "use"),
    ("OPENAI_MODEL=gpt-4", "use"),
    ('{"model": "gpt-4", "temperature": 0}', "use"),
    ('if name == "gpt-4":', "use"),
    ('m = os.getenv("OPENAI_MODEL", "gpt-4")', "use"),                    # review: env default
    ('parser.add_argument("--model", default="gpt-4", choices=["gpt-4", "o1"])', "use"),
    ('ask("gpt-4", [msg])', "use"),
    ('fallbacks = ["gpt-4"]', "use"),                                    # one-element list in use
    ('MODEL_MAP = {"fast": "gpt-4", "smart": "o1"}', "use"),              # dict values
    ('IMAGE_MODEL = "gpt-4"  # @param ["gpt-4", "o1"]', "use"),           # comment removed first
    ('const m = "gpt-4"; // or "o1" (faster)', "use"),
    ('MODELS = ["gpt-4", "o1", "o3-mini"]', "listed"),
    ('Literal["gpt-4", "gpt-4o"]', "listed"),
    ('TABLE = {"gpt-4": 8192, "o1": 200000}', "listed"),                  # table keys
    ('x = [\n    ("gpt-4", 8192),\n]', "listed"),                        # tuple rows in a list
    ('ROWS = [\n    "gpt-4",\n    "o1",\n]', "listed"),
    ('useState(\n    "gpt-4",\n)', "use"),                                # multi-line call argument
    ('print("don\'t"); M = "gpt-4"', "use"),
])
def test_hit_tiers(tmp_path, src, tier):
    write(tmp_path, "a.py" if "const" not in src else "a.ts", src + "\n")
    got = attribute.scan(tmp_path, ["gpt-4"], kind="model")
    assert got and got[0].tier == tier, [(s.line, s.tier) for s in got]


def test_non_model_records_are_never_demoted(tmp_path):
    write(tmp_path, "a.py", 'BETAS = ["fast-mode-2026-02-01", "context-1m-2025-08-07"]\n')
    assert attribute.scan(tmp_path, ["fast-mode-2026-02-01"], kind="rest")[0].tier == "use"


def test_briefs_and_sarif_put_listed_entries_after_uses(tmp_path):
    import json
    from apiwatch import brief, report
    write(tmp_path, "src/app.py", 'MODELS = ["gpt-4", "o1"]\nclient.create(model="gpt-4")\n')
    r = rec(["gpt-4"])
    r.kind = "model"
    sites = attribute.attribute_record(tmp_path, r)
    assert {s.line: s.tier for s in sites} == {1: "listed", 2: "use"}
    b = brief.brief_for_record(r, sites)
    assert b.index("src/app.py:2") < b.index("Also listed (1)") < b.index("src/app.py:1")
    levels = {res["locations"][0]["physicalLocation"]["region"]["startLine"]: res["level"]
              for res in json.loads(report.sarif([(r, sites)]))["runs"][0]["results"]}
    assert levels == {1: "note", 2: "warning"}


def test_python_the_parser_cannot_handle_does_not_stop_the_scan(tmp_path):
    # a 200k-deep expression overflows CPython's parser (MemoryError); the hit on the other line still counts
    (tmp_path / "m.py").write_text('MODEL = "claude-opus-4-0"\nx = ' + "-" * 200_000 + "1\n")
    index = attribute.RepoIndex(tmp_path)
    sites = attribute.scan(index, ["claude-opus-4-0"])
    assert [s.line for s in sites] == [1]
    assert attribute.follow_constants(index, sites) == []      # used to raise MemoryError from ast.parse


# ---------- precision fixes from the 2026-10-06 field test (16 public repos; docs/FIELD-TEST.md) ----------

@pytest.mark.parametrize("line,sig,named,hit", [
    ('model="gpt-4-0613"', "gpt-4", (), True),                  # dated snapshot of the same model
    ('model="gpt-4-turbo-2024-04-09"', "gpt-4-turbo", (), True),
    ('"claude-3-haiku-20240307"', "claude-3-haiku", (), True),
    ('"gemini-1.5-flash-001"', "gemini-1.5-flash", (), True),
    ('"claude-3-5-sonnet-latest"', "claude-3-5-sonnet", (), True),
    ('model="gpt-4-turbo"', "gpt-4", (), False),                # a different model
    ('"grok-3-mini"', "grok-3", (), False),
    ('"grok-3-fast"', "grok-3", (), False),
    ('"gpt-3.5-turbo-16k"', "gpt-3.5-turbo", (), False),
    ('"gpt-5.1-codex-max"', "gpt-5.1-codex", ("gpt-5.1-codex-max",), True),   # the record names it
])
def test_model_ids_skip_sibling_models(line, sig, named, hit):
    assert (find_in_line(line, sig, model=True, named=frozenset(named)) >= 0) is hit


def model_rec(sigs, vendor="Anthropic", detail=""):
    from apiwatch.models import ChangeRecord
    return ChangeRecord(id="m", vendor=vendor, surface="model", kind="model", severity="breaking",
                        effective="2026-01-01", summary="s", detail=detail, signatures=sigs)


def test_bedrock_and_vertex_ids_are_not_first_party_hits(tmp_path):
    (tmp_path / "a.py").write_text('A = "claude-3-7-sonnet"\n'
                                   'B = "anthropic.claude-3-7-sonnet-20250219-v1:0"\n'
                                   'C = "us.anthropic.claude-3-7-sonnet-20250219-v1:0"\n'
                                   'D = "claude-3-7-sonnet@20250219"\n'
                                   'E = "anthropic.claude-3-7-sonnet-20240607"\n')   # a tool's own spelling
    lines = sorted(s.line for s in attribute.attribute_record(tmp_path, model_rec(["claude-3-7-sonnet"])))
    assert lines == [1, 5]
    other = sorted(s.line for s in attribute.attribute_record(tmp_path, model_rec(["claude-3-7-sonnet"], "Acme")))
    assert other == [1, 4, 5]          # a dated id followed by -v1:0 isn't a snapshot for any vendor; @ is per vendor


def test_python_locals_stay_in_their_function(tmp_path):
    (tmp_path / "t.py").write_text(
        'def test_a():\n'
        '    model = "gemini-3.1-flash-lite-preview"\n'
        '    run(model)\n'
        'def test_b():\n'
        '    model = "glm-4.5"\n'
        '    run(model)\n'
        'class Cfg:\n'
        '    MODEL = "gemini-3.1-flash-lite-preview"\n'
        '    def go(self):\n'
        '        self.m = "gemini-3.1-flash-lite-preview"\n'
        '        return self.m\n'
        'use(Cfg.MODEL)\n'
        'other.m\n')
    rec = model_rec(["gemini-3.1-flash-lite-preview"], "Google")
    via = sorted((s.line, s.via.split(" ")[0]) for s in attribute.attribute_record(tmp_path, rec) if s.via)
    assert via == [(3, "model"), (11, "m"), (12, "MODEL")]


def test_js_locals_stay_in_their_block_and_keys_are_not_uses(tmp_path):
    (tmp_path / "t.spec.ts").write_text(
        "describe('x', () => {\n"
        "  it('a', () => {\n"
        "    const model = 'gemini-3.1-flash-lite-preview';\n"
        "    expect(get(model)).toBe(1);\n"
        "  });\n"
        "  it('b', () => {\n"
        "    expect(get({ model: 'glm-4.5' })).toBe(2);\n"
        "    const x = ok ? model : other;\n"
        "  });\n"
        "});\n")
    (tmp_path / "cfg.ts").write_text("export const MODEL = 'gemini-3.1-flash-lite-preview';\n"
                                     "call({ model: MODEL });\nconst o = { MODEL: 1 };\n")
    rec = model_rec(["gemini-3.1-flash-lite-preview"], "Google")
    via = sorted((s.file, s.line) for s in attribute.attribute_record(tmp_path, rec) if s.via)
    assert via == [("cfg.ts", 2), ("t.spec.ts", 4)]


def test_data_files_skip_sentences_but_keep_config(tmp_path):
    prose = "In our evaluation we compared gpt-4o-2024-05-13 with the newer models on every task in the suite"
    (tmp_path / "corpus.json").write_text('{"model": "gpt-4o-2024-05-13",\n "text": "' + prose + '"}\n')
    (tmp_path / "cfg.yaml").write_text(
        "model: gpt-4o-2024-05-13\n"                          # 1
        f"note: {prose}\n"                                    # 2 prose
        "models: [gpt-4o-2024-05-13, gpt-4o]\n"               # 3 flow sequence
        "  - OPENAI_MODEL=gpt-4o-2024-05-13\n"                # 4 docker-compose env
        "run: llm -m gpt-4o-2024-05-13 'hello'\n"             # 5 a CLI command
        "matrix: {model: gpt-4o-2024-05-13, temperature: 0}\n"  # 6 flow mapping
        "model: gpt-4o-2024-05-13\t# pinned\n")              # 7
    (tmp_path / "pyproject.toml").write_text('cmd = "llm -m gpt-4o-2024-05-13"\n' f'help = "{prose}"\n')
    hits = sorted((s.file, s.line) for s in attribute.scan(tmp_path, ["gpt-4o-2024-05-13"], kind="model"))
    assert hits == [("cfg.yaml", n) for n in (1, 3, 4, 5, 6, 7)] + [("corpus.json", 1), ("pyproject.toml", 1)]


def test_data_prose_rule_is_for_model_records_only(tmp_path):
    (tmp_path / "vector.yaml").write_text("source: |\n  .file.name = .filename and then some more words here ok\n")
    assert [s.line for s in attribute.scan(tmp_path, ["filename"], kind="rest")] == [2]


def test_data_prose_is_linear_on_minified_json(tmp_path):
    import time
    (tmp_path / "big.json").write_text('[' + ','.join('{"model": "gpt-4o-2024-05-13"}' for _ in range(6000)) + ']')
    start = time.time()
    attribute.scan(tmp_path, ["gpt-4o-2024-05-13"], kind="model")
    assert time.time() - start < 5


def test_apiwatchignore_and_locales_are_left_out(tmp_path):
    for rel in ("src/a.py", "agents/agent_1.json", "gen/x/b.py", "client/locales/ja/common.js", "i18n/en.json"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text('m = "gpt-4o-2024-05-13"\n')
    (tmp_path / ".apiwatchignore").write_text("# generated\nagents/*.json\ngen/\n")
    assert [s.file for s in attribute.scan(tmp_path, ["gpt-4o-2024-05-13"])] == ["src/a.py"]


@pytest.mark.parametrize("path", ["src/__fixtures__/x.json", "tests/cassettes/a.yaml", "src/__mocks__/m.ts",
                                  "pkg/__snapshots__/s.snap.ts"])
def test_fixture_folders_are_tests(path):
    from apiwatch.models import CallSite
    assert CallSite(path, 1, "s", "x").in_test


@pytest.mark.parametrize("code,tier", [
    ("type Model =\n  | 'gpt-4'\n  | 'gpt-4o';\n", "listed"),               # a TypeScript union member
    ("for m in (\n    'gpt-4',\n):\n    pass\n", "listed"),                # `in (` isn't a call
    ("MODELS = {\n" + "".join(f"    'm{i}': 1,\n" for i in range(60)) + "    'gpt-4': 1,\n}\n", "listed"),
])
def test_hit_tiers_on_long_tables_unions_and_loops(tmp_path, code, tier):
    (tmp_path / ("a.ts" if code.startswith("type") else "a.py")).write_text(code)
    sites = attribute.scan(tmp_path, ["gpt-4"], kind="model")
    assert [s.tier for s in sites] == [tier]


@pytest.mark.parametrize("line,hit", [
    ('m = "gpt-4-completions"', True),            # the record says "plus -completions variants"
    ('m = "gpt-4-0613"', True),                    # named in parentheses: gpt-4(-0613)
    ('m = "gpt-4-vision-preview"', False),
])
def test_record_named_suffixes(line, hit):
    named = attribute.record_names("Covered: gpt-4(-0613), plus -completions variants.")
    assert (find_in_line(line, "gpt-4", model=True, named=named) >= 0) is hit


def test_odd_model_signature_does_not_crash():
    named = attribute.record_names("@cf/meta/llama-3-8b-instruct retires")
    assert find_in_line('m = "@cf/meta/llama-3-8b"', "@cf/meta/llama-3", model=True, named=named) == -1


@pytest.mark.parametrize("code,tier", [
    ("if (model === 'gpt-4') {\n  go()\n}\n", "use"),
    ("def f(m):\n    if (m == 'gpt-4'):\n        return ('gpt-4')\n", "use"),
])
def test_keyword_parens_stay_use(tmp_path, code, tier):
    (tmp_path / ("a.ts" if "===" in code else "a.py")).write_text(code)
    assert {s.tier for s in attribute.scan(tmp_path, ["gpt-4"], kind="model")} == {tier}


def test_unbalanced_bracket_in_a_docstring_does_not_change_tiers(tmp_path):
    (tmp_path / "a.py").write_text('"""Models (see below\n"""\n' + "x = 1\n" * 70 + 'T = {\n    "gpt-4": 1,\n}\n')
    assert [s.tier for s in attribute.scan(tmp_path, ["gpt-4"], kind="model")] == ["listed"]


def test_tiers_are_fast_on_big_generated_files(tmp_path):
    import time
    (tmp_path / "gen.ts").write_text("export const T = {\n" + "".join(f"  k{i}: call('gpt-4'),\n" for i in range(3000)) + "};\n")
    start = time.time()
    sites = attribute.scan(tmp_path, ["gpt-4"], kind="model")
    assert len(sites) == 3000 and time.time() - start < 5


def test_python_scope_edge_cases(tmp_path):
    (tmp_path / "a.py").write_text(
        'class C:\n    MODEL = "gemini-3.1-flash-lite-preview"\n    DEFAULT = MODEL\n'
        'def f(x):\n    match x:\n        case 1:\n            m = "gemini-3.1-flash-lite-preview"\n            go(m)\n')
    rec = model_rec(["gemini-3.1-flash-lite-preview"], "Google")
    assert sorted(s.line for s in attribute.attribute_record(tmp_path, rec) if s.via) == [3, 8]


def test_js_block_and_case_edge_cases(tmp_path):
    (tmp_path / "a.ts").write_text(
        "function f(x) {\n  const M = 'gemini-3.1-flash-lite-preview'; if (x) {\n    y()\n  }\n"
        "  switch (x) {\n    case M:\n      go(M)\n  }\n}\n")
    rec = model_rec(["gemini-3.1-flash-lite-preview"], "Google")
    assert sorted(s.line for s in attribute.attribute_record(tmp_path, rec) if s.via) == [6, 7]


def test_one_site_per_line(tmp_path):
    (tmp_path / "a.py").write_text('m = "anthropic.claude-3-haiku-20240307-v1:0" if b else "claude-3-haiku-20240307"\n')
    rec = model_rec(["claude-3-haiku-20240307", "claude-3-haiku"])
    assert len(attribute.attribute_record(tmp_path, rec)) == 1


def test_bare_ignore_name_matches_a_directory(tmp_path):
    for rel in ("generated/x/a.py", "src/b.py"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text('m = "gpt-4o-2024-05-13"\n')
    (tmp_path / ".apiwatchignore").write_text("generated\n")
    assert [s.file for s in attribute.scan(tmp_path, ["gpt-4o-2024-05-13"])] == ["src/b.py"]


def test_snapshot_then_sibling_is_not_the_model(tmp_path):
    from apiwatch.models import load_records
    rec = next(r for r in load_records(Path(__file__).parent.parent / "feed" / "records")
               if r.id == "openai-2026-10-23-legacy-gpt-snapshots")
    expect = {"gpt-4-1106-vision-preview": False, "gpt-3.5-turbo-0301": False, "gpt-3.5-turbo-0613": False,   # 2024
              "gpt-4": True, "gpt-4-0613": True, "gpt-4-0613-completions": True, "gpt-3.5-turbo-0125": True,
              "gpt-4-turbo-2024-04-09": True}
    (tmp_path / "a.py").write_text("".join(f'm = "{m}"\n' for m in expect))
    lines = {s.line for s in attribute.attribute_record(tmp_path, rec)}
    assert {m for n, m in enumerate(expect, 1) if n in lines} == {m for m, hit in expect.items() if hit}


# ---------- third scanner review (2026-10-06) ----------

def test_long_minified_line_with_many_strings_is_fast(tmp_path):
    import time
    row = '{"id": 1, "model": "gpt-4o-2024-05-13", "prompt": "hello world", "tag": "x"}'
    (tmp_path / "dump.json").write_text("[" + ",".join([row] * 8000) + "]")
    start = time.time()
    sites = attribute.scan(tmp_path, ["gpt-4o-2024-05-13", "gpt-4o"], kind="model")
    assert len(sites) == 1 and time.time() - start < 5


@pytest.mark.parametrize("line", [
    'run: llm -m gpt-4o-2024-05-13 "Summarize the following changes in this pull request for reviewers"',
    "run: curl https://api.openai.com/v1/chat/completions -d '{\"model\": \"gpt-4o-2024-05-13\", \"messages\": "
    "[{\"role\": \"user\", \"content\": \"say hello to everyone in the channel please and thanks\"}]}'",
    '- run: aider --model gpt-4o-2024-05-13 --message "fix the failing tests in the repo and commit the result"',
    'entrypoint: sh -c "llm -m gpt-4o-2024-05-13 --system you are a helpful assistant who answers briefly"',
])
def test_commands_in_ci_config_are_config(tmp_path, line):
    (tmp_path / "ci.yml").write_text(line + "\n")
    assert len(attribute.scan(tmp_path, ["gpt-4o-2024-05-13"], kind="model")) == 1


def test_yaml_apostrophe_does_not_open_a_string(tmp_path):
    (tmp_path / "d.yaml").write_text("description: Don't use gpt-4o-2024-05-13 for this task because it is slow and costly\n")
    assert attribute.scan(tmp_path, ["gpt-4o-2024-05-13"], kind="model") == []


def test_unbalanced_brackets_keep_memory_bounded(tmp_path):
    (tmp_path / "x.go").write_text("".join(f"    re := regexp.MustCompile(`[(]`) // {i}\n" for i in range(20000))
                                   + '    m := "gpt-4"\n')
    stacks = attribute.line_stacks((tmp_path / "x.go").read_text().splitlines())
    assert max(len(s) for s in stacks) <= 32


def test_parenthesized_text_names_no_suffix():
    named = attribute.record_names("gpt-4o(-mini)-search-preview, gpt-5.1-codex-mini")
    assert "-mini" not in named and "-search" not in named
    assert find_in_line('"gpt-5.2-codex-mini"', "gpt-5.2-codex", model=True, named=named) == -1


@pytest.mark.parametrize("line", ['body = "{\\"model\\": \\"gpt-5.1-codex-max\\"}"', 'u = "gpt-5.1-codex-max:generate"'])
def test_named_id_followed_by_punctuation(line):
    named = attribute.record_names("covers gpt-5.1-codex-max")
    assert find_in_line(line, "gpt-5.1-codex", model=True, named=named) >= 0


def test_two_ids_on_a_line_keep_their_own_tiers(tmp_path):
    (tmp_path / "a.py").write_text('UPGRADE = {"gpt-3.5-turbo": "gpt-4"}\n')
    rec = model_rec(["gpt-3.5-turbo", "gpt-4"], "OpenAI")
    assert sorted((s.signature, s.tier) for s in attribute.attribute_record(tmp_path, rec)) == \
        [("gpt-3.5-turbo", "listed"), ("gpt-4", "use")]


def test_bare_name_in_a_method_is_the_global(tmp_path):
    (tmp_path / "a.py").write_text('MODEL = "gpt-5"\nclass C:\n    MODEL = "gemini-3.1-flash-lite-preview"\n'
                                   '    def f(self):\n        return call(model=MODEL)\n')
    rec = model_rec(["gemini-3.1-flash-lite-preview"], "Google")
    assert [s.line for s in attribute.attribute_record(tmp_path, rec) if s.via] == []


@pytest.mark.parametrize("text", [
    'Example API response JSON { \\\\"id\\\\" : \\\\"msg_01\\\\" , \\\\"model\\\\" : \\\\"gpt-4o-2024-05-13\\\\" , \\\\"stop_reason\\\\" : \\\\"end\\\\" } and more words follow here',
    "Legacy models were retired last year. New models: gpt-4o-2024-05-13, text-embedding-005 and others for search",
])
def test_ids_in_docs_text_are_prose(tmp_path, text):
    (tmp_path / "docs.json").write_text('[{"text": "' + text + '"}]\n')
    assert attribute.scan(tmp_path, ["gpt-4o-2024-05-13"], kind="model") == []


# ---------- fourth scanner review (2026-10-06) ----------

@pytest.mark.parametrize("line", [
    "run: llm -m 'gpt-4o-2024-05-13' 'Summarize the following text into three short sentences for the weekly report'",
    "run: python main.py --model 'gpt-4o-2024-05-13' --prompt 'Summarize the following text into three short sentences please'",
])
def test_single_quoted_id_after_a_flag_is_config(tmp_path, line):
    (tmp_path / "ci.yml").write_text(line + "\n")
    assert len(attribute.scan(tmp_path, ["gpt-4o-2024-05-13"], kind="model")) == 1


@pytest.mark.parametrize("text", [
    "description: The older models, gpt-4o-2024-05-13 included, are being retired and you should move now\n",
    '{"text": "Older models (gpt-4o-2024-05-13 and others) are being retired soon so please move"}\n',
    "summary: gpt-4o-2024-05-13 is retired and replaced by a much larger family of models today\n",
    "note: see the docs -- gpt-4o-2024-05-13 is retired and replaced by newer models this year\n",
])
def test_prose_after_openers_keys_and_dashes(tmp_path, text):
    (tmp_path / ("d.json" if text.startswith("{") else "d.yaml")).write_text(text)
    assert attribute.scan(tmp_path, ["gpt-4o-2024-05-13"], kind="model") == []


def test_method_defaults_see_the_class_attribute(tmp_path):
    (tmp_path / "g.py").write_text('class C:\n    MODEL = "gemini-3.1-flash-lite-preview"\n'
                                   '    def run(self, model=MODEL):\n        return model\n')
    rec = model_rec(["gemini-3.1-flash-lite-preview"], "Google")
    assert [s.line for s in attribute.attribute_record(tmp_path, rec) if s.via] == [3]


def test_overlapping_signatures_report_one_id_once(tmp_path):
    (tmp_path / "a.py").write_text('client.create(model="ft:gpt-4-0613:org::abc")\n')
    rec = model_rec(["gpt-4-0613", "ft:gpt-4"], "OpenAI")
    assert len(attribute.attribute_record(tmp_path, rec)) == 1
