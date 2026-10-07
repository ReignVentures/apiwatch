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
import bisect
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
             "site-packages", ".next", "coverage", ".tox", ".mypy_cache", ".pytest_cache", "vendor",
             "locales", "i18n"}                          # translations: UI prose about models, never a call
IGNORE_FILE = ".apiwatchignore"                          # repo-relative globs to leave out, one per line
DATA_SUFFIXES = {".json", ".yaml", ".yml", ".toml"}
TOKEN = re.compile(r"[A-Za-z0-9_]")
COMMENT_PREFIXES = ("#", "//", "/*", "*", "--")
JS_SUFFIXES = {".js", ".mjs", ".cjs", ".ts", ".mts", ".cts", ".tsx", ".jsx"}


def is_code_file(p: Path) -> bool:
    return p.suffix in CODE_SUFFIXES or p.name == ".env" or p.name.startswith(".env.")


def ignore_patterns(repo: Path) -> list[str]:
    """Globs from the repo's .apiwatchignore: `agents/*.json`, `generated/` (a directory), `*.snap`."""
    try:
        text = (repo / IGNORE_FILE).read_text(errors="ignore")
    except OSError:
        return []
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def is_ignored(rel: str, patterns: list[str]) -> bool:
    rel = rel.replace("\\", "/")
    parts = rel.split("/")
    for pat in patterns:
        p = pat.strip("/")
        if pat.endswith("/"):                                  # a directory anywhere, or a path prefix
            if any(fnmatch.fnmatchcase(d, p) for d in parts[:-1]) or fnmatch.fnmatchcase(rel, p + "/*"):
                return True
        elif fnmatch.fnmatchcase(rel, p) or ("/" not in p and any(fnmatch.fnmatchcase(x, p) for x in parts)):
            return True                                        # a bare name: that file or directory anywhere
    return False


def iter_files(repo: Path):
    ignore = ignore_patterns(repo)
    for p in sorted(repo.rglob("*")):
        if not p.is_file() or not is_code_file(p):
            continue
        rel = p.relative_to(repo)
        if any(part in SKIP_DIRS for part in rel.parts[:-1]):
            continue
        if ignore and is_ignored(str(rel), ignore):
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
    except (SyntaxError, ValueError, MemoryError, RecursionError):   # odd code (deep nesting) must not stop a scan
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


def _scan_context(text: str, stack: list | None = None) -> tuple[bool, list[tuple[str, str]]]:
    """Left-to-right over code (comments already blanked): is the end inside a string, and which brackets are
    still open (innermost last), each with the text just before it on its line. `stack` starts it mid-file."""
    quote, i, n, line_start = "", 0, len(text), 0
    stack = list(stack or [])
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


_NOT_A_CALL = r"(?<![\w.])(?:in|of)\s*$"   # `for x in (`, `for (const m of (`: a loop over a list, not a call


# a top-level statement starts here, so no bracket is still open: bounds the damage of an unbalanced bracket in a
# multi-line string or docstring
_TOP_LEVEL = re.compile(r"(?:def|class|import|from|async def|function|export|const|let|var|type|interface|func|fn|impl|"
                        r"pub|fun|package|struct|enum|mod|use|namespace|public|private|internal|object)\b|@")
_MAX_DEPTH = 32                     # deeper nesting is a broken count (a regex like /[(]/), not a real table


def line_stacks(lines: list[str]) -> list[list[tuple[str, str]]]:
    """The open brackets at the start of each line, in one pass over the file (quotes end with their line)."""
    out, stack = [], []
    for ln in lines:
        if ln[:1].isalpha() or ln[:1] == "@":
            if _TOP_LEVEL.match(ln):
                stack = []
        out.append(stack)
        _, stack = _scan_context(ln + "\n", stack)
        if len(stack) > _MAX_DEPTH:
            stack = stack[-_MAX_DEPTH:]
        stack = [(ch, before[-40:]) for ch, before in stack]   # bounded memory per line
    return out


def hit_tier(lines: list[str], i: int, k: int, kind: str = "model", stacks: list | None = None) -> str:
    """"listed" only when a model id is one entry in a standalone list, a Literal[...], or a table's key;
    everything else is "use". Conservative on purpose: demoting a real request is the worse mistake.
    `lines` are the file's lines with comments blanked, `i` is 1-based."""
    if kind != "model":
        return "use"                                          # endpoints, tools, headers: every mention matters
    if stacks is not None:                                     # whole-file bracket state (long model tables)
        in_string, stack = _scan_context(lines[i - 1][:k], stacks[i - 1])
    else:
        in_string, stack = _scan_context("\n".join(lines[max(0, i - 401):i - 1] + [lines[i - 1][:k]]))
    if in_string and re.match(r"""\s*\|\s*["'`]""", lines[i - 1][:k] + lines[i - 1][k:k + 1]) and \
            not lines[i - 1][:k].rstrip().endswith("||"):
        return "listed"                                       # a TypeScript union member: | 'gpt-4'
    if not in_string or not stack:
        return "use"
    if any(ch == "(" and re.search(r"[\w\])]\s*$", before) and not re.search(_NOT_A_CALL, before)
           for ch, before in stack):
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


# what may follow a model id and still be that model: a dated or numbered snapshot, or -latest
# (claude-3-haiku-20240307, gpt-4-0613, gemini-1.5-flash-001). Anything else is a sibling (gpt-4-turbo, grok-3-mini)
_SNAPSHOT = re.compile(r"-(?:\d{8}|\d{4}(?:-\d{2}-\d{2})?|\d{3}|latest)(?![A-Za-z0-9_.])")


MODEL_ID = re.compile(r"[a-z][a-z0-9]*(?:[-.][a-z0-9]+)+")      # ids a record's own text names
NAMED_SUFFIX = re.compile(r"(?:(?<=\s)|^)-([a-z][a-z0-9]*)\b")   # "plus -completions variants" (not "gpt-4o(-mini)")


def record_names(text: str) -> frozenset:
    """Ids and -suffixes a record's own text names: they count as the record's even when a signature is a stem."""
    low = text.lower()
    return frozenset(MODEL_ID.findall(low)) | frozenset("-" + s for s in NAMED_SUFFIX.findall(low))


def _snapshot_of(line: str, i: int, j: int, named: frozenset) -> bool:
    """A snapshot suffix right after the id (j), and after it the id ends or goes on only with a suffix the
    record names (gpt-4-0613-completions): gpt-4-1106-vision-preview is a different model, not a gpt-4 snapshot."""
    m = _SNAPSHOT.match(line, j)
    if not m:
        return False
    rest = re.match(r"-[a-z][a-z0-9]*", line[m.end():].lower())
    return not rest or rest.group(0) in named


def _named_variant(line: str, i: int, j: int, named: frozenset) -> bool:
    """The id at line[i:] (sig ends at j) is one the record names, or continues with a suffix it names."""
    if not named:
        return False
    low = line.lower()
    for n in named:
        if n[:1] != "-" and low.startswith(n, i) and not re.match(r"[a-z0-9_]|[.-][a-z0-9]", low[i + len(n):]):
            return True                                      # the whole id is one the record names
    nxt = re.match(r"-[a-z][a-z0-9]*", line[j:].lower())
    return bool(nxt) and nxt.group(0) in named


def find_in_line(line: str, sig: str, start: int = 0, model: bool = False, named: frozenset = frozenset()) -> int:
    """Return the index of the first token-aware match of sig in line at or after start, or -1. With model=True,
    a '-' after the id must start a snapshot suffix (_SNAPSHOT), not a sibling model's name, unless the whole id
    is one the record names (`named`: gpt-5.1-codex-max under a gpt-5.1-codex signature)."""
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
            if model and nxt == "-" and not _snapshot_of(line, i, j, named) and not _named_variant(line, i, j, named):
                continue  # gpt-4 vs gpt-4-turbo, grok-3 vs grok-3-mini, gpt-4 vs gpt-4-1106-vision-preview
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
            except (SyntaxError, ValueError, MemoryError, RecursionError):
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
         exclude: list[str] | None = None, files: list[str] | None = None, kind: str = "",
         named: frozenset = frozenset()) -> list[CallSite]:
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
        model = kind == "model"
        # model ids in docs corpora and descriptions are prose; endpoint and field names in a data file are
        # often code (a log shipper's remap: `.file.name = .filename`), so the rule is for model records only
        data = model and suffix in DATA_SUFFIXES
        mask = idx.comment_mask(rel)
        locs = idx.nb_locs.get(rel)
        _blank: list[str] = []

        _stacks: list = []

        def blanked(lines=lines, mask=mask, _blank=_blank, prose=prose) -> list[str]:
            if not _blank:                    # comment text and docstrings replaced by spaces, for hit tiers
                for n_, (ln, (_, spans)) in enumerate(zip(lines, mask), 1):
                    for a, b in list(spans) + prose.get(n_, []):
                        ln = ln[:a] + " " * (b - a) + ln[b:]
                    _blank.append(ln)
            return _blank

        def stacks(_stacks=_stacks) -> list:
            if not _stacks:
                _stacks.extend(line_stacks(blanked()))
            return _stacks
        for i, line in enumerate(lines, 1):
            whole, comments = mask[i - 1]
            if whole:
                continue
            if not any(s in line for s in sigs):
                continue
            skip = comments + prose.get(i, [])
            # spans of excluded ids on this line: a hit inside one isn't this change (gpt-realtime in gpt-realtime-2.1)
            taken = _Spans([(k, k + len(e)) for e in excl_all for k in _all_in_line(line, e, model, named)])
            found = _Spans([])      # hits so far: a shorter or offset signature overlapping one is the same id
            for v, s in pats:  # longest first; a shorter signature inside a longer hit is the same hit
                ks = [k for k in _all_in_line(line, v, model, named)
                      if not _in_spans(k, skip) and not taken.covers(k, k + len(v)) and not found.overlaps(k, k + len(v))]
                if data and ks:
                    qs = _quoted_spans(line, suffix)
                    qstarts = [a for a, _ in qs]
                    ks = [k for k in ks if not _data_prose(line, k, len(v), qs, qstarts)]
                # an exclude that starts at the hit may need the rest of the call (arguments on later lines)
                starts = [e for e in excl_flat if e.startswith(s)]
                if starts:
                    ks = [k for k in ks if not any(_call_text(lines, i, k).startswith(e) for e in starts)]
                if not ks:
                    continue
                found.add([(k, k + len(v)) for k in ks])    # every occurrence, so a repeat doesn't feed a shorter sig
                tier = hit_tier(blanked(), i, ks[0], kind, stacks()) if kind == "model" else "use"
                if locs:
                    cell, cl, raw_line = locs[i - 1]
                    sites.append(CallSite(rel, raw_line, s, line.strip()[:160], cell=f"cell {cell}, line {cl}", tier=tier))
                else:
                    sites.append(CallSite(rel, i, s, line.strip()[:160], tier=tier))
    return sites


class _Spans:
    """Spans on one line; covers(a, b) in log time, so a minified line with thousands of hits stays linear."""

    def __init__(self, spans: list[tuple[int, int]]):
        self._spans: list[tuple[int, int]] = []
        self.add(spans)

    def add(self, spans: list[tuple[int, int]]) -> None:
        if not spans:
            return
        self._spans = sorted(self._spans + spans)
        self._starts = [a for a, _ in self._spans]
        self._reach, best = [], -1                      # furthest end among spans starting at or before each one
        for _, b in self._spans:
            best = max(best, b)
            self._reach.append(best)

    def covers(self, a: int, b: int) -> bool:
        i = bisect.bisect_right(self._starts, a) - 1 if self._spans else -1
        return i >= 0 and self._reach[i] >= b

    def overlaps(self, a: int, b: int) -> bool:
        i = bisect.bisect_left(self._starts, b) - 1 if self._spans else -1     # spans starting before b
        return i >= 0 and self._reach[i] > a


def _all_in_line(line: str, sig: str, model: bool = False, named: frozenset = frozenset()) -> list[int]:
    out, k = [], find_in_line(line, sig, model=model, named=named)
    while k >= 0:
        out.append(k)
        k = find_in_line(line, sig, k + 1, model=model, named=named)
    return out


def _quoted_spans(line: str, suffix: str) -> list[tuple[int, int]]:
    """Interiors of the quoted strings on a data-file line, in one pass. JSON strings use only double quotes; in
    YAML/TOML a single quote opens a string only where a value starts (not the apostrophe in `don't`)."""
    spans, quote, start, pos, n, last = [], "", 0, 0, len(line), ""     # last: previous non-space character
    while pos < n:
        ch = line[pos]
        if quote:
            if ch == "\\":
                pos += 2
                continue
            if ch == quote:
                spans.append((start, pos))
                quote = ""
        elif ch == '"' or (ch == "'" and suffix != ".json" and
                           (last in ("", ":", "-", "[", ",", "{", "=") or line[pos - 1].isspace())):
            quote, start = ch, pos + 1
        if not ch.isspace():
            last = ch
        pos += 1
    if quote:
        spans.append((start, n))
    return spans


_WORD = re.compile(r"[A-Za-z]{2,}")
# what comes right before an id that's being configured, not discussed: a flag (-m, --model, --model=), a key that
# starts its value (model:, "model":, model=, - OPENAI_MODEL=, {model: ...), or a list/collection opener.
# "New models: gemini-..." mid-sentence and an escaped \"model\": inside docs text don't count
_FLAG = re.compile(r"""(?:^|\s)-{1,2}\w[\w-]*[=\s]\s*["']?$""")                       # -m, --model=, --model '
_ITEM = re.compile(r"""^\s*-\s+["']?$""")                                              # - gpt-4
_KEY = re.compile(r"""(?:^\s*(?:-\s+)?|[{,]\s*)[\w"'-]+\s*[:=]\s*["']?$""")              # model: / "model": / X=
_OPEN = re.compile(r"""[\[{(,]\s*["']?$""")                                          # [gpt-4, / {a, gpt-4


def _plain_words(text: str) -> int:
    return len([w for w in text.split() if _WORD.fullmatch(w.strip(".,;:!?()"))])


def _data_prose(line: str, k: int, n: int, spans: list[tuple[int, int]], starts: list[int]) -> bool:
    """A model id inside a sentence in a data file (a docs corpus, a description, an API spec), rather than config.
    The id is config after a flag (-m, --model=) or as a list item; after a key (model:, "model":, X=) unless a
    sentence follows it (`summary: gpt-4 is retired and ...`); after a list opener unless plain words come before
    it (`The older models, gpt-4 included`). Otherwise the text holding it (its quoted string, or the line) is prose
    when it has 8+ words, 4+ of them plain, and 40+ characters."""
    i = bisect.bisect_right(starts, k) - 1
    if i >= 0 and spans[i][0] <= k < spans[i][1]:
        a, b = spans[i]
        text, before, after = line[a:b], line[a:k], line[k + n:b]
    else:
        text = line.split(" #", 1)[0]
        before, after = line[:k], text[k + n:]
    if _FLAG.search(before) or _ITEM.search(before):
        return False
    if _KEY.search(before) and _plain_words(re.split(r"""["',}\]]""", after, maxsplit=1)[0]) < 5:
        return False                                          # the value ends at its quote, comma or bracket
    if _OPEN.search(before) and _plain_words(before[max(before.rfind("["), before.rfind("{")) + 1:]) < 2:
        return False
    words = text.split()
    return len(text.strip()) >= 40 and len(words) >= 8 and _plain_words(text) >= 4


# ---------- constant propagation ----------

@dataclass
class _Const:
    name: str
    file: str       # repo-relative path of the defining file
    line: int
    module: str     # dotted module path for Python ("pkg.config"), path stem for JS
    # where it can be used: None = the whole file (importable unless attr); (start, end) lines = a function or
    # block (a local: uses after its line, inside that range), or with attr, the class a `self.x` belongs to
    scope: tuple[int, int] | None = None
    attr: bool = False
    owner: tuple[int, int] | None = None   # a class attribute's class body, where its bare name works too,
    holes: tuple = ()                      # except inside the class's methods (there a bare name is the global)


def _py_module(rel: str) -> str:
    parts = Path(rel).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _py_constants(tree: ast.AST, rel: str, sites_by_line: set[int]) -> list[_Const]:
    """String assignments on hit lines, each with its scope: module-level names are file-wide and importable,
    function locals reach only the rest of their function, class and `self.` attributes only their class."""
    out: list[_Const] = []
    mod = _py_module(rel)

    def visit(node, scope, cls):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, (child.lineno, child.end_lineno or child.lineno), cls)
                continue
            if isinstance(child, ast.ClassDef):
                span = (child.lineno, child.end_lineno or child.lineno)
                # a method's body (not its defaults or annotations, which run in the class body)
                holes = tuple(((f.body[0].lineno if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) else f.lineno),
                               f.end_lineno or f.lineno) for f in child.body
                              if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
                visit(child, ("class", span, holes), span)
                continue
            if isinstance(child, (ast.Assign, ast.AnnAssign)) and child.lineno in sites_by_line:
                targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                value = child.value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    for tg in targets:
                        if isinstance(tg, ast.Name):
                            if scope is None:
                                out.append(_Const(tg.id, rel, child.lineno, mod))
                            elif scope[0] == "class":           # Cls.NAME (enum members too): file-wide,
                                out.append(_Const(tg.id, rel, child.lineno, mod, None, attr=True,   # and by
                                                  owner=scope[1], holes=scope[2]))                  # name inside
                            else:
                                out.append(_Const(tg.id, rel, child.lineno, mod, scope))
                        elif isinstance(tg, ast.Attribute) and cls:      # self.model = "..."
                            out.append(_Const(tg.attr, rel, child.lineno, mod, cls, attr=True))
            if isinstance(child, (ast.stmt, ast.excepthandler, ast.match_case)):
                visit(child, scope, cls)                  # if/for/with/try/match bodies keep the enclosing scope

    visit(tree, None, None)
    return out


def _py_uses(tree: ast.AST, rel: str, lines: list[str], consts: list[_Const]) -> list[tuple[int, _Const]]:
    """Lines in this Python file that use one of consts (defined here, or imported by name)."""
    names: dict[str, list[_Const]] = {}
    for c in consts:
        if c.file == rel:
            names.setdefault(c.name, []).append(c)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            mod = node.module
            if node.level:  # relative import: resolve against this file's package
                base = _py_module(rel).split(".")
                base = base[:len(base) - node.level] if node.level <= len(base) else []
                mod = ".".join([*base, node.module]) if node.module else ".".join(base)
            for a in node.names:
                for c in consts:
                    if c.scope is None and not c.attr and c.name == a.name and (
                            c.module == mod or c.module.endswith("." + mod) or mod.endswith(c.module)):
                        names.setdefault(a.asname or a.name, []).append(c)
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            name, is_attr = node.id, False
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            name, is_attr = node.attr, True
        else:
            continue
        for c in names.get(name, []):
            if c.file == rel and c.line == node.lineno:
                continue
            if c.scope is None:
                # module constant: by name or module.NAME; class attribute: Cls.NAME, or its bare name in the class
                ok = is_attr or not c.attr or bool(c.owner and c.owner[0] < node.lineno <= c.owner[1]
                                                   and not any(a <= node.lineno <= b for a, b in c.holes))
            elif c.attr:
                ok = is_attr and c.scope[0] <= node.lineno <= c.scope[1]
            else:
                ok = not is_attr and c.line < node.lineno <= c.scope[1]
            if ok:
                hits.append((node.lineno, c))
                break
    return hits


_JS_CONST = re.compile(r"^(\s*)(export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(['\"`])")
_JS_IMPORT = re.compile(r"import\s*(?:type\s+)?\{([^}]*)\}\s*from\s*['\"]([^'\"]+)['\"]")


def _js_block_end(lines: list[str], n: int) -> int:
    """Last line of the brace block that line n sits in (braces counted after the definition, strings ignored)."""
    depth = 0
    for j in range(n - 1, len(lines)):
        m = _JS_CONST.match(lines[j]) if j == n - 1 else None
        seg = lines[j][m.end() - 1:] if m else lines[j]     # from the opening quote, so the string closes right
        quote = ""
        for ch in seg:
            if quote:
                if ch == quote:
                    quote = ""
            elif ch in "\"'`":
                quote = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth < 0:
                    return j + 1
    return len(lines)


def _js_constants(lines: list[str], rel: str, sites_by_line: set[int]) -> list[_Const]:
    out = []
    for n in sites_by_line:
        m = _JS_CONST.match(lines[n - 1]) if 0 < n <= len(lines) else None
        if m:
            stem = str(Path(rel).with_suffix(""))
            if not m.group(1) or m.group(2):               # top level (or exported): the whole module
                out.append(_Const(m.group(3), rel, n, stem))
            else:                                          # indented: a local, for the rest of its block
                out.append(_Const(m.group(3), rel, n, stem, (n, _js_block_end(lines, n))))
    return out


def _js_uses(text: str, lines: list[str], rel: str, consts: list[_Const]) -> list[tuple[int, _Const]]:
    visible: dict[str, list[_Const]] = {}
    for c in consts:
        if c.file == rel:
            visible.setdefault(c.name, []).append(c)
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
                if c.scope is None and c.name == bits[0] and c.module in (norm, str(Path(norm) / "index")):
                    visible.setdefault(bits[-1], []).append(c)
    hits = []
    for n, line in enumerate(lines, 1):
        if _is_comment(line) or line.lstrip().startswith("import "):
            continue
        for name, cs in visible.items():
            for c in cs:
                if c.file == rel and c.line == n:
                    continue
                if c.scope is not None and not (c.line < n <= c.scope[1]):
                    continue
                if any(not re.match(r"\s*:(?!:)", line[mm.end():]) or _js_ternary(line, mm.start())
                       or re.search(r"\bcase\s+$", line[:mm.start()])
                       for mm in re.finditer(rf"(?<![\w$.]){re.escape(name)}(?![\w$])", line)):
                    hits.append((n, c))                   # a use; an object key (`model:`) alone isn't one
                    break
    return hits


def _js_ternary(line: str, k: int) -> bool:
    """`cond ? model : other`: the name before a ':' is a value, not a key."""
    return line[:k].rstrip().endswith("?")


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


# Partner platforms serve some vendors' models under their own ids and retire them on their own dates, so a
# first-party retirement doesn't cover them: Bedrock (anthropic.claude-...-20250219-v1:0, with us./eu./apac.
# prefixes on inference profiles) and Vertex AI (claude-...@20250219). The suffix decides: other tools also write
# "anthropic.claude-..." for the first-party model. See GAPS.
_PARTNER = {"Anthropic": re.compile(r"(?:-\d{8})?(?:-v\d+:\d+|@)")}


def _partner_form(line: str, sig: str, vendor: str) -> bool:
    """Every occurrence of sig on this line is a partner platform's id for the vendor's model."""
    after = _PARTNER.get(vendor)
    ks = _all_in_line(line, sig, model=True) or _all_in_line(line, sig)
    return bool(after and ks) and all(after.match(line, k + len(sig)) for k in ks)


def attribute_record(repo, rec: ChangeRecord) -> list[CallSite]:
    """repo may be a Path or a RepoIndex (build one index to scan many records)."""
    idx = _index(repo)
    if getattr(rec, "repo_context", None) and not idx.repo_mentions(rec.repo_context):
        return []
    named = record_names(" ".join([rec.summary, rec.detail, rec.surface])) \
        if getattr(rec, "kind", "") == "model" else frozenset()
    sites = scan(idx, rec.signatures, getattr(rec, "context", None), getattr(rec, "exclude", None),
                 getattr(rec, "files", None), getattr(rec, "kind", ""), named)
    if getattr(rec, "kind", "") == "model" and rec.vendor in _PARTNER:
        sites = [s for s in sites
                 if not _partner_form(idx.files[s.file][2][_file_line(idx, s) - 1], s.signature, rec.vendor)]
    return sites + follow_constants(idx, sites)


def _file_line(idx: RepoIndex, s: CallSite) -> int:
    """The line in the indexed text: notebooks report the .ipynb line, the index holds the cells' code."""
    if s.cell:
        locs = idx.nb_locs.get(s.file, [])
        for n, (_, _, raw) in enumerate(locs, 1):
            if raw == s.line:
                return n
    return s.line


def attribute_drift(repo, ev: DriftEvent) -> list[CallSite]:
    idx = _index(repo)
    sites = scan(idx, signatures_for_drift(ev))
    return sites + follow_constants(idx, sites)
