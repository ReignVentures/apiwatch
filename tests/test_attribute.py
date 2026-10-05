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
