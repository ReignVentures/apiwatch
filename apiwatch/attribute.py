"""Find where a change lands in a consumer's codebase.

Matching is token-aware, not raw substring:
  - A signature must not be glued to identifier characters on either side, so `gpt-4` doesn't hit
    `gpt-4o`, `chatgpt-4`, or `gpt-4.1`, and `fx_rate` doesn't hit `fx_rate_limit`.
  - A trailing `-suffix` is allowed, so `claude-opus-4-1` hits `claude-opus-4-1-20250805` and
    `gpt-4` hits `gpt-4-0613` (dated snapshots and variants of the same model).
  - A signature that starts or ends with punctuation (`/v1/prompts`, `pmpt_`) is a deliberate
    prefix/suffix and skips the boundary check on that side.
  - When several signatures hit the same line, only the most specific (longest) is reported.
  - Whole-line comments are skipped.
Constants are followed to their uses: `MODEL = "claude-opus-4-0"` reports the definition and every
line that uses MODEL, in the same file or in files that import it (Python via ast, JS/TS via regex).
"""
from __future__ import annotations

import ast
import fnmatch
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from . import deps
from .models import CallSite, ChangeRecord, DriftEvent

CODE_SUFFIXES = {
    ".py", ".js", ".mjs", ".cjs", ".ts", ".mts", ".cts", ".tsx", ".jsx", ".go", ".rb", ".java", ".kt", ".cs",
    ".php", ".rs", ".swift", ".sh", ".yml", ".yaml", ".json", ".toml", ".ini", ".cfg", ".ipynb",
}
HASH_COMMENT = {".py", ".ipynb", ".rb", ".sh", ".yml", ".yaml", ".toml", ".ini", ".cfg", ""}   # "" = .env files
SLASH_COMMENT = {".js", ".mjs", ".cjs", ".ts", ".mts", ".cts", ".tsx", ".jsx", ".go", ".java", ".kt", ".cs", ".php",
                 ".rs", ".swift"}
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "dist", "build", "out", "__pycache__", ".apiwatch",
             "site-packages", ".next", "coverage", ".tox", ".mypy_cache", ".pytest_cache", "vendor"}
TOKEN = re.compile(r"[A-Za-z0-9_]")
COMMENT_PREFIXES = ("#", "//", "/*", "*", "--")
JS_SUFFIXES = {".js", ".mjs", ".cjs", ".ts", ".mts", ".cts", ".tsx", ".jsx"}


def is_code_file(p: Path) -> bool:
    return p.suffix in CODE_SUFFIXES or p.name == ".env" or p.name.startswith(".env.")


def iter_files(repo: Path):
    for p in sorted(repo.rglob("*")):
        if not p.is_file() or not is_code_file(p):
            continue
        if any(part in SKIP_DIRS for part in p.relative_to(repo).parts[:-1]):
            continue
        yield p


_MCP_PREFIX = re.compile(r"mcp__[\w-]*__$")


def _notebook_code(raw: str) -> tuple[str, list[tuple[int, int, int]]]:
    """A notebook's code cells, one after another, and for each code line (cell number, line in cell,
    line in the .ipynb file) so hits can point at the real file (SARIF) and the cell a reader will see."""
    try:
        cells = json.loads(raw).get("cells", [])
    except (ValueError, AttributeError):
        return "", []
    out, locs, pos, cell_no = [], [], 0, 0
    for c in cells:
        if not isinstance(c, dict):
            continue
        cell_no += 1
        if c.get("cell_type") != "code":
            continue
        src, in_cell = c.get("source", ""), 0
        parts = src if isinstance(src, list) else [str(src)]
        for part in parts:
            # nbformat writes each source line as its own JSON string; find it to learn its line in the file
            found = -1
            for enc in (json.dumps(part, ensure_ascii=False), json.dumps(part)):
                found = raw.find(enc, pos)
                if found >= 0:
                    pos = found + len(enc)
                    break
            raw_line = raw.count("\n", 0, found) + 1 if found >= 0 else raw.count("\n", 0, pos) + 1
            for sub in part.splitlines() or [""]:
                # IPython magics and shell escapes aren't Python
                out.append("" if sub.lstrip().startswith(("%", "!")) else sub)
                in_cell += 1
                locs.append((cell_no, in_cell, raw_line))
    return "\n".join(out), locs


def _byte_col(line: str, col: int) -> int:
    """ast column offsets are UTF-8 byte offsets; convert to a str index."""
    return len(line.encode("utf-8")[:col].decode("utf-8", errors="ignore"))


def _string_statement_spans(source_lines: list[str], offset: int = 0) -> dict[int, list[tuple[int, int]]]:
    """Spans of bare string statements (docstrings, attribute docs), keyed by 1-based line (+offset)."""
    try:
        tree = ast.parse("\n".join(source_lines))
    except (SyntaxError, ValueError):
        return {}
    spans: dict[int, list[tuple[int, int]]] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
            continue
        first, last = node.lineno, node.end_lineno or node.lineno
        for ln in range(first, last + 1):
            text = source_lines[ln - 1] if ln - 1 < len(source_lines) else ""
            a = _byte_col(text, node.col_offset) if ln == first else 0
            b = _byte_col(text, node.end_col_offset) if (ln == last and node.end_col_offset is not None) else len(text)
            spans.setdefault(ln + offset, []).append((a, b))
    return spans


PY_LIKE = {".py", ".ipynb"}                         # '#' starts a comment anywhere outside a string
BACKTICK_MULTILINE = {".js", ".mjs", ".cjs", ".ts", ".mts", ".cts", ".tsx", ".jsx", ".go"}
_TRIPLES = ('"' * 3, "'" * 3)


def lang_of(rel: str) -> str:
    name = rel.replace("\\", "/").rsplit("/", 1)[-1]
    return "" if name.startswith(".env") else os.path.splitext(name)[1]


def comment_mask(lines: list[str], suffix: str) -> list[tuple[bool, list[tuple[int, int]]]]:
    """Per line: (the whole line is comment, [comment spans]). A small lexer that carries block comments,
    Python triple-quoted strings and JS/Go backtick strings across lines; ' and " strings end with the line.
    '#' is a comment anywhere in Python and Ruby, but elsewhere only at the start of a line or after
    whitespace (so shell's $# and a YAML url's /#/ aren't comments)."""
    hash_c, slash_c = suffix in HASH_COMMENT, suffix in SLASH_COMMENT
    py, btick = suffix in PY_LIKE, suffix in BACKTICK_MULTILINE
    out: list[tuple[bool, list[tuple[int, int]]]] = []
    state = ""            # "" | "block" | a triple quote | "`"
    for line in lines:
        n, i, spans, quote = len(line), 0, [], ""
        block_from = 0 if state == "block" else None
        while i < n:
            if state == "block":
                close = line.find("*/", i)
                if close < 0:
                    i = n
                    break
                spans.append((block_from, close + 2))
                state, block_from, i = "", None, close + 2
                continue
            if state in _TRIPLES:
                close = line.find(state, i)
                if close < 0:
                    i = n
                    break
                state, i = "", close + 3
                continue
            if state == "`":
                j = i
                while j < n and line[j] != "`":
                    j += 2 if (line[j] == "\\" and suffix != ".go") else 1
                if j >= n:
                    i = n
                    break
                state, i = "", j + 1
                continue
            ch = line[i]
            if quote:
                if ch == "\\":
                    i += 2
                    continue
                if ch == quote:
                    quote = ""
                i += 1
                continue
            if py and line.startswith(_TRIPLES, i):
                state, i = line[i:i + 3], i + 3
                continue
            if ch == "`" and btick:
                state, i = "`", i + 1
                continue
            if ch in "\"'`":
                quote, i = ch, i + 1
                continue
            if hash_c and ch == "#" and (py or suffix == ".rb" or i == 0 or line[i - 1].isspace()
                                         or (suffix == ".sh" and line[i - 1] == ";")):
                spans.append((i, n))
                break
            if slash_c and line.startswith("//", i):
                spans.append((i, n))
                break
            if slash_c and line.startswith("/*", i):
                state, block_from, i = "block", i, i + 2
                continue
            i += 1
        if state == "block" and block_from is not None:
            spans.append((block_from, n))
        out.append((bool(spans) and not _code_outside(line, spans), spans))
    return out


def _code_outside(line: str, spans: list[tuple[int, int]]) -> bool:
    keep, last = [], 0
    for a, b in sorted(spans):
        keep.append(line[last:a])
        last = max(last, b)
    keep.append(line[last:])
    return bool("".join(keep).strip())


def _in_spans(k: int, spans) -> bool:
    return any(a <= k < b for a, b in spans)


def _scan_context(text: str) -> tuple[bool, list[tuple[str, str]]]:
    """Left-to-right over code (comments already blanked): is the end inside a string, and which brackets are
    still open (innermost last), each with the text just before it on its line."""
    quote, stack, i, n, line_start = "", [], 0, len(text), 0
    while i < n:
        ch = text[i]
        if ch == "\n":
            quote, line_start = "", i + 1                    # ' and " strings end with the line
        elif quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "\"'`":
            quote = ch
        elif ch in "([{":
            stack.append((ch, text[line_start:i]))
        elif ch in ")]}" and stack:
            stack.pop()
        i += 1
    return bool(quote), stack


def hit_tier(lines: list[str], i: int, k: int, kind: str = "model") -> str:
    """"listed" only when a model id is one entry in a standalone list, a Literal[...], or a table's key;
    everything else is "use". Conservative on purpose: demoting a real request is the worse mistake.
    `lines` are the file's lines with comments blanked, `i` is 1-based."""
    if kind != "model":
        return "use"                                          # endpoints, tools, headers: every mention matters
    text = "\n".join(lines[max(0, i - 31):i - 1] + [lines[i - 1][:k]])
    in_string, stack = _scan_context(text)
    if not in_string or not stack:
        return "use"
    if any(ch == "(" and re.search(r"[\w\])]\s*$", before) for ch, before in stack):
        return "use"                                          # inside a call's arguments: it's being passed
    ch, before = stack[-1]
    if ch == "[" and re.search(r"Literal\s*$", before):
        return "listed"
    rest = lines[i - 1][k:]
    close = re.match(r"""[^"'`]*["'`]\s*(.)""", rest)          # what follows the string holding the hit
    after = close.group(1) if close else ""
    if ch == "{":
        return "listed" if after == ":" else "use"            # a table's key is listed; a dict value is a setting
    if ch in "[(":
        if after == "]" and re.search(r"\[\s*$", lines[i - 1][:k].rsplit('"', 1)[0].rsplit("'", 1)[0]):
            return "use"                                      # a one-element list: it's the value in use
        return "listed"
    return "use"


def _call_text(lines: list[str], i: int, k: int, limit: int = 6) -> str:
    """The text from a hit to the end of its call (parens balanced), across up to `limit` lines, whitespace
    removed: lets an exclude like `conferences.list({ status:` see an argument on the next line."""
    parts, depth, opened = [], 0, False
    for j, line in enumerate(lines[i - 1:i - 1 + limit]):
        seg = line[k:] if j == 0 else line
        for pos, ch in enumerate(seg):
            if ch in "([{":
                depth, opened = depth + 1, True
            elif ch in ")]}":
                depth -= 1
                if opened and depth <= 0:
                    parts.append(seg[:pos + 1])
                    return "".join("".join(parts).split())
        parts.append(seg)
        if not opened and j == 0:
            break
    return "".join("".join(parts).split())


def find_in_line(line: str, sig: str, start: int = 0) -> int:
    """Return the index of the first token-aware match of sig in line at or after start, or -1."""
    if not sig:
        return -1
    # a signature that starts/ends with punctuation (incl. "_", as in "pmpt_") is a deliberate prefix/suffix
    check_left = bool(TOKEN.match(sig[0])) and sig[0] != "_"
    check_right = bool(TOKEN.match(sig[-1])) and sig[-1] != "_"
    while True:
        i = line.find(sig, start)
        if i < 0:
            return -1
        start = i + 1
        # MCP clients prefix tool names (mcp__github__search_issues); that "__" is a join, not part of the name.
        # Other "__" (self.__x, Django field__lookup, sklearn step__param) still counts as part of a word.
        if check_left and i > 0 and TOKEN.match(line[i - 1]) and not _MCP_PREFIX.search(line, 0, i):
            continue
        j = i + len(sig)
        if check_right and j < len(line):
            nxt = line[j]
            if TOKEN.match(nxt):
                continue
            if nxt == "." and j + 1 < len(line) and line[j + 1].isdigit():
                continue  # gpt-4 vs gpt-4.1: a version continues after the dot
            if line.startswith("\\.", j) and j + 2 < len(line) and line[j + 2].isdigit():
                continue  # the same inside a regex: gpt-5 vs gpt-5\.1
        return i


def _is_comment(line: str) -> bool:
    return line.lstrip().startswith(COMMENT_PREFIXES)


class RepoIndex:
    """Read a repo's code files once; reuse across every record in a scan."""

    def __init__(self, repo: Path):
        self.repo = repo
        self.files: dict[str, tuple[str, str, list[str]]] = {}   # rel -> (suffix, text, lines)
        self._trees: dict[str, ast.AST | None] = {}
        self._prose: dict[str, set[int]] = {}
        self._masks: dict[str, list] = {}
        self.nb_locs: dict[str, list[tuple[int, int, int]]] = {}
        for p in iter_files(repo):
            try:
                text = p.read_text(errors="ignore")
            except OSError:
                continue
            if p.suffix == ".ipynb":
                text, self.nb_locs[str(p.relative_to(repo))] = _notebook_code(text)
            self.files[str(p.relative_to(repo))] = (p.suffix, text, text.splitlines())
        self._lower = {rel: (rel + "\n" + t[1]).lower() for rel, t in self.files.items()}
        self._repo_lower: str | None = None

    def tree(self, rel: str):
        if rel not in self._trees:
            try:
                self._trees[rel] = ast.parse(self.files[rel][1])
            except SyntaxError:
                self._trees[rel] = None
        return self._trees[rel]

    def prose_spans(self, rel: str) -> dict[int, list[tuple[int, int]]]:
        """Python string statements (docstrings, attribute docs) by line: prose about a model isn't a call."""
        if rel not in self._prose:
            suffix, _, lines = self.files[rel]
            spans: dict[int, list[tuple[int, int]]] = {}
            if suffix == ".py":
                spans = _string_statement_spans(lines)
            elif suffix == ".ipynb":                    # each cell parses on its own
                locs, start = self.nb_locs.get(rel, []), 0
                while start < len(locs):
                    end = start
                    while end < len(locs) and locs[end][0] == locs[start][0]:
                        end += 1
                    spans.update(_string_statement_spans(lines[start:end], offset=start))
                    start = end
            self._prose[rel] = spans
        return self._prose[rel]

    def comment_mask(self, rel: str) -> list[tuple[bool, list[tuple[int, int]]]]:
        """Per line: (whole line is comment, comment spans)."""
        if rel not in self._masks:
            self._masks[rel] = comment_mask(self.files[rel][2], lang_of(rel))
        return self._masks[rel]

    def mentions(self, rel: str, words: list) -> bool:
        return context_matches(self._lower[rel], words)

    def repo_mentions(self, words: list) -> bool:
        """Anywhere in the repo, including dependency manifests the scan doesn't read for hits, plus canonical
        dep:<ecosystem>:name@version lines from manifests and lockfiles (deps.py)."""
        if self._repo_lower is None:
            parts = list(self._lower.values())
            dep_parts: list[str] = []
            for root, dirs, names in os.walk(self.repo):
                dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
                here = [Path(root) / n for n in sorted(names) if _is_manifest(Path(root) / n)]
                locked = {deps.ecosystem(q) for q in here if deps.is_lockfile(q)}
                for q in here:
                    try:
                        raw = q.read_text(errors="ignore")
                    except OSError:
                        continue
                    rel = str(q.relative_to(self.repo))
                    if rel not in self._lower:              # code files (.json/.toml/.yaml) are already in parts
                        parts.append(raw.lower())
                    # resolved versions win: a lockfile beside a manifest makes the manifest's ranges noise
                    if deps.is_lockfile(q) or deps.ecosystem(q) not in locked:
                        dep_parts.append(deps.dep_lines(q, raw))
            self._repo_lower = "\n".join(parts + dep_parts)
        return context_matches(self._repo_lower, words)


MANIFEST_NAMES = {"go.mod", "gemfile", "gemfile.lock", "pom.xml", "build.gradle", "build.gradle.kts", "pipfile",
                  "pipfile.lock", "poetry.lock", "uv.lock", "cargo.toml", "cargo.lock", "composer.json", "composer.lock",
                  "yarn.lock", "pnpm-lock.yaml", "package.json", "package-lock.json", "npm-shrinkwrap.json",
                  "pyproject.toml"}


def _is_manifest(p: Path) -> bool:
    n = p.name.lower()
    return n in MANIFEST_NAMES or deps.is_requirements(p.name, p.parent.name) or n.endswith(".csproj")


def context_matches(text_lower: str, words: list) -> bool:
    """Any item matches: a word that appears, or an AND group whose words all appear."""
    return any(all(w.lower() in text_lower for w in ([c] if isinstance(c, str) else c)) for c in words)


def file_matches(rel: str, patterns: list[str]) -> bool:
    name = rel.replace("\\", "/").rsplit("/", 1)[-1]
    return any(fnmatch.fnmatchcase(rel, g) or fnmatch.fnmatchcase(name, g) for g in patterns)   # '*' crosses '/' 


def _index(repo_or_index) -> RepoIndex:
    return repo_or_index if isinstance(repo_or_index, RepoIndex) else RepoIndex(repo_or_index)


def scan(repo, signatures: list[str], context: list | None = None,
         exclude: list[str] | None = None, files: list[str] | None = None, kind: str = "") -> list[CallSite]:
    idx = _index(repo)
    # (text to find, signature to report): model ids also appear regex-escaped, e.g. r"^gpt-5\.1-chat-latest$"
    pats = sorted({(v, s) for s in signatures if s for v in {s, s.replace(".", "\\.")}},
                  key=lambda t: len(t[0]), reverse=True)
    sigs = [v for v, _ in pats]
    excl = [e for e in (exclude or []) if e]
    excl_all = excl + [e.replace(".", "\\.") for e in excl if "." in e]      # regex-escaped forms too
    excl_flat = ["".join(e.split()) for e in excl]
    sites: list[CallSite] = []
    for rel, (suffix, text, lines) in idx.files.items():
        if files and not file_matches(rel, files):
            continue
        if context and not idx.mentions(rel, context):
            continue
        if not any(s in text for s in sigs):
            continue
        prose = idx.prose_spans(rel) if suffix in PY_LIKE else {}
        mask = idx.comment_mask(rel)
        locs = idx.nb_locs.get(rel)
        _blank: list[str] = []

        def blanked(lines=lines, mask=mask, _blank=_blank) -> list[str]:
            if not _blank:                                    # comment text replaced by spaces, for hit tiers
                for ln, (_, spans) in zip(lines, mask):
                    for a, b in spans:
                        ln = ln[:a] + " " * (b - a) + ln[b:]
                    _blank.append(ln)
            return _blank
        for i, line in enumerate(lines, 1):
            whole, comments = mask[i - 1]
            if whole:
                continue
            if not any(s in line for s in sigs):
                continue
            skip = comments + prose.get(i, [])
            # spans of excluded ids on this line: a hit inside one isn't this change (gpt-realtime in gpt-realtime-2.1)
            taken: list[tuple[int, int]] = [(k, k + len(e)) for e in excl_all for k in _all_in_line(line, e)]
            for v, s in pats:  # longest first; a shorter signature inside a longer hit is the same hit
                ks = [k for k in _all_in_line(line, v)
                      if not _in_spans(k, skip) and not any(a <= k and k + len(v) <= b for a, b in taken)]
                # an exclude that starts at the hit may need the rest of the call (arguments on later lines)
                starts = [e for e in excl_flat if e.startswith(s)]
                if starts:
                    ks = [k for k in ks if not any(_call_text(lines, i, k).startswith(e) for e in starts)]
                if not ks:
                    continue
                taken += [(k, k + len(v)) for k in ks]      # every occurrence, so a repeat doesn't feed a shorter sig
                tier = hit_tier(blanked(), i, ks[0], kind) if kind == "model" else "use"
                if locs:
                    cell, cl, raw_line = locs[i - 1]
                    sites.append(CallSite(rel, raw_line, s, line.strip()[:160], cell=f"cell {cell}, line {cl}", tier=tier))
                else:
                    sites.append(CallSite(rel, i, s, line.strip()[:160], tier=tier))
    return sites


def _all_in_line(line: str, sig: str) -> list[int]:
    out, k = [], find_in_line(line, sig)
    while k >= 0:
        out.append(k)
        k = find_in_line(line, sig, k + 1)
    return out


# ---------- constant propagation ----------

@dataclass
class _Const:
    name: str
    file: str       # repo-relative path of the defining file
    line: int
    module: str     # dotted module path for Python ("pkg.config"), path stem for JS


def _py_module(rel: str) -> str:
    parts = Path(rel).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _py_constants(tree: ast.AST, rel: str, sites_by_line: set[int]) -> list[_Const]:
    out = []
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if node.lineno not in sites_by_line:
            continue
        if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
            continue
        for t in targets:
            if isinstance(t, ast.Name):
                out.append(_Const(t.id, rel, node.lineno, _py_module(rel)))
            elif isinstance(t, ast.Attribute):
                out.append(_Const(t.attr, rel, node.lineno, _py_module(rel)))
    return out


def _py_uses(tree: ast.AST, rel: str, lines: list[str], consts: list[_Const]) -> list[tuple[int, _Const]]:
    """Lines in this Python file that use one of consts (defined here, or imported by name)."""
    visible: dict[str, _Const] = {c.name: c for c in consts if c.file == rel}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            mod = node.module
            if node.level:  # relative import: resolve against this file's package
                base = _py_module(rel).split(".")
                base = base[:len(base) - node.level] if node.level <= len(base) else []
                mod = ".".join([*base, node.module]) if node.module else ".".join(base)
            for a in node.names:
                for c in consts:
                    if c.name == a.name and (c.module == mod or c.module.endswith("." + mod) or mod.endswith(c.module)):
                        visible[a.asname or a.name] = c
    hits = []
    for node in ast.walk(tree):
        name = None
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            name = node.id
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            name = node.attr
        if name in visible:
            c = visible[name]
            if not (c.file == rel and c.line == node.lineno):
                hits.append((node.lineno, c))
    return hits


_JS_CONST = re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(['\"`])")
_JS_IMPORT = re.compile(r"import\s*(?:type\s+)?\{([^}]*)\}\s*from\s*['\"]([^'\"]+)['\"]")


def _js_constants(lines: list[str], rel: str, sites_by_line: set[int]) -> list[_Const]:
    out = []
    for n in sites_by_line:
        m = _JS_CONST.match(lines[n - 1]) if 0 < n <= len(lines) else None
        if m:
            out.append(_Const(m.group(1), rel, n, str(Path(rel).with_suffix(""))))
    return out


def _js_uses(text: str, lines: list[str], rel: str, consts: list[_Const]) -> list[tuple[int, _Const]]:
    visible: dict[str, _Const] = {c.name: c for c in consts if c.file == rel}
    here = Path(rel).parent
    for m in _JS_IMPORT.finditer(text):
        spec = m.group(2)
        if not spec.startswith("."):
            continue
        norm = os.path.normpath(str(here / spec))
        if Path(norm).suffix in JS_SUFFIXES:
            norm = str(Path(norm).with_suffix(""))
        for part in m.group(1).split(","):
            bits = [b.strip() for b in part.strip().split(" as ")]
            if not bits[0]:
                continue
            for c in consts:
                if c.name == bits[0] and c.module in (norm, str(Path(norm) / "index")):
                    visible[bits[-1]] = c
    hits = []
    for n, line in enumerate(lines, 1):
        if _is_comment(line) or line.lstrip().startswith("import "):
            continue
        for name, c in visible.items():
            if c.file == rel and c.line == n:
                continue
            if re.search(rf"(?<![\w$.]){re.escape(name)}(?![\w$])", line):
                hits.append((n, c))
    return hits


def follow_constants(repo, sites: list[CallSite]) -> list[CallSite]:
    """Add the lines that use string constants whose definitions matched a signature."""
    idx = _index(repo)
    by_file: dict[str, set[int]] = {}
    for s in sites:
        by_file.setdefault(s.file, set()).add(s.line)
    sig_at = {(s.file, s.line): s.signature for s in sites}
    consts: list[_Const] = []
    for rel, lines_hit in by_file.items():
        suffix, _, lines = idx.files[rel]
        if suffix == ".py":
            tree = idx.tree(rel)
            if tree is not None:
                consts += _py_constants(tree, rel, lines_hit)
        elif suffix in JS_SUFFIXES:
            consts += _js_constants(lines, rel, lines_hit)
    if not consts:
        return []
    names = {c.name for c in consts}
    extra: list[CallSite] = []
    seen = {(s.file, s.line) for s in sites}
    for rel, (suffix, text, lines) in idx.files.items():
        if suffix != ".py" and suffix not in JS_SUFFIXES:
            continue
        if not any(n in text for n in names):
            continue
        if suffix == ".py":
            tree = idx.tree(rel)
            if tree is None:
                continue
            uses = _py_uses(tree, rel, lines, consts)
        else:
            uses = _js_uses(text, lines, rel, consts)
        for n, c in uses:
            if (rel, n) in seen:
                continue
            seen.add((rel, n))
            extra.append(CallSite(rel, n, sig_at[(c.file, c.line)], lines[n - 1].strip()[:160],
                                  via=f"{c.name} ({c.file}:{c.line})"))
    return extra


# ---------- entry points ----------

def signatures_for_drift(ev: DriftEvent) -> list[str]:
    """Turn a drift event into strings worth grepping for."""
    if ev.path.startswith("tool:"):
        return [ev.path[5:].split(".")[0]]
    leaf = ev.path.split(".")[-1].replace("[]", "")
    return [leaf] if leaf and leaf != "$" else []


def attribute_record(repo, rec: ChangeRecord) -> list[CallSite]:
    """repo may be a Path or a RepoIndex (build one index to scan many records)."""
    idx = _index(repo)
    if getattr(rec, "repo_context", None) and not idx.repo_mentions(rec.repo_context):
        return []
    sites = scan(idx, rec.signatures, getattr(rec, "context", None), getattr(rec, "exclude", None),
                 getattr(rec, "files", None), getattr(rec, "kind", ""))
    return sites + follow_constants(idx, sites)


def attribute_drift(repo, ev: DriftEvent) -> list[CallSite]:
    idx = _index(repo)
    sites = scan(idx, signatures_for_drift(ev))
    return sites + follow_constants(idx, sites)
