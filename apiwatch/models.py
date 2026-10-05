"""Data models. Everything is plain dataclasses + YAML/JSON on disk. No database in v0.1."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

SEVERITIES = ("breaking", "silent", "deprecation", "additive", "info")
KINDS = ("rest", "mcp", "sdk", "model")


@dataclass
class ChangeRecord:
    """One entry in the public feed: a change at a vendor that consumers need to know about."""
    id: str
    vendor: str
    surface: str            # e.g. "REST v3 /candidates", "MCP tool search_documents", "model claude-opus-4-0"
    kind: str               # rest | mcp | sdk | model
    severity: str           # breaking | silent | deprecation | additive | info
    effective: str          # ISO date the change takes effect (or took effect)
    summary: str            # one line, human
    detail: str = ""        # what actually changes, what silently breaks
    signatures: list[str] = field(default_factory=list)   # strings to look for in consumer code
    source_url: str = ""
    verified: bool = False  # True only after an independent review against the primary source (docs/REVIEW.md)
    verified_by: str = ""   # who reviewed it: a person's name, or "claude" (or "claude-…") for the independent AI review
    verified_on: str = ""   # ISO date of that review
    # short verbatim facts from the source (ids, dates, tool names) that `apiwatch check` must find on the page.
    # A plain string is looked for at source_url; {url: ..., text: ...} looks on another primary page.
    evidence: list = field(default_factory=list)
    fix_hint: str = ""
    illustrative: bool = False  # made-up example for demos/tests; never published on the public site
    # a hit counts only in files whose path or text mentions one of these (case-insensitive); an item that is a
    # list is an AND group: ["stripe", "flight_data"] needs both words in the file
    context: list = field(default_factory=list)
    files: list[str] = field(default_factory=list)       # glob patterns a hit's file must match, e.g. ["*.py"]
    repo_context: list = field(default_factory=list)     # like context, but anywhere in the repo incl. manifests
                                                          # (requirements.txt, go.mod…): a pinned preview SDK, say
    exclude: list[str] = field(default_factory=list)  # ids a signature would also match that this change doesn't cover (e.g. a live sibling or the replacement); a hit inside one of these is dropped

    def __post_init__(self):
        if self.severity not in SEVERITIES:
            raise ValueError(f"{self.id}: severity must be one of {SEVERITIES}")
        if self.kind not in KINDS:
            raise ValueError(f"{self.id}: kind must be one of {KINDS}")
        date.fromisoformat(self.effective)
        if self.verified and not (self.source_url and self.verified_by and self.verified_on and self.evidence):
            raise ValueError(f"{self.id}: verified records need source_url, verified_by, verified_on and evidence")
        for name in ("signatures", "context", "repo_context", "files", "exclude", "evidence"):
            if not isinstance(getattr(self, name), list):
                raise ValueError(f"{self.id}: {name} must be a list")
        if any("dep:" in w for c in self.context for w in ([c] if isinstance(c, str) else c)):
            raise ValueError(f"{self.id}: dep:… lines exist only repo-wide; put them in repo_context, not context")
        for name in ("context", "repo_context"):
            for c in getattr(self, name):
                if not ((isinstance(c, str) and c) or (isinstance(c, list) and c and all(isinstance(w, str) and w for w in c))):
                    raise ValueError(f"{self.id}: {name} items are words or lists of words (AND groups)")
        if not all(isinstance(g, str) and g for g in self.files):
            raise ValueError(f"{self.id}: files are glob patterns")
        if not isinstance(self.evidence, list):
            raise ValueError(f"{self.id}: evidence must be a list")
        for e in self.evidence:
            text = e.get("text") if isinstance(e, dict) else e
            if isinstance(e, dict) and not (isinstance(e.get("url"), str) and e["url"].startswith("https://")):
                raise ValueError(f"{self.id}: evidence {{url, text}} items need an https:// url")
            # an item that's empty after markup is stripped would match every page
            if not isinstance(text, str) or len(text.replace("`", "").replace("*", "").strip()) < 4:
                raise ValueError(f"{self.id}: evidence items are strings or {{url, text}} mappings with text of 4+ characters")
        if self.verified_on:
            date.fromisoformat(self.verified_on)

    @property
    def human_verified(self) -> bool:
        # AI reviews record a verified_by starting with "claude"; anything else is a person's name
        return self.verified and not self.verified_by.strip().lower().startswith("claude")

    @property
    def published(self) -> bool:
        """Only reviewed records with checkable evidence go on the public site."""
        return self.verified and not self.illustrative

    def evidence_items(self) -> list[tuple[str, str]]:
        """(url, text) pairs for the source check."""
        return [(e["url"], e["text"]) if isinstance(e, dict) else (self.source_url, e) for e in self.evidence]


@dataclass
class DriftEvent:
    """A change observed by probing a live (or sampled) endpoint against its baseline."""
    endpoint_id: str
    path: str               # path inside the response, e.g. "$.data[].tracking_number"
    change: str             # removed | added | type_changed | became_nullable | status_changed
    before: Any = None
    after: Any = None
    severity: str = "silent"


@dataclass
class CallSite:
    file: str
    line: int
    signature: str
    snippet: str
    via: str = ""   # set when this line uses a constant whose definition matched, e.g. "MODEL (src/config.py:3)"
    cell: str = ""  # notebooks: "cell 7, line 3" (line is then the line in the .ipynb file)
    tier: str = "use"  # "use": the value is used or configured here; "listed": one entry in a list/table/enum of strings

    @property
    def in_test(self) -> bool:
        parts = self.file.replace("\\", "/").lower().split("/")
        name = parts[-1]
        return (any(p in {"test", "tests", "__tests__", "spec", "specs", "testdata", "fixtures"} for p in parts[:-1])
                or name.startswith("test_") or name.endswith(("_test.py", "_test.go", ".test.ts", ".test.js",
                                                               ".spec.ts", ".spec.js", ".test.tsx", ".spec.tsx")))


def load_records(dirpath: Path) -> list[ChangeRecord]:
    out = []
    for p in sorted(dirpath.glob("*.yml")) + sorted(dirpath.glob("*.yaml")):
        out.append(ChangeRecord(**yaml.safe_load(p.read_text())))
    return out


def dump_json(obj: Any) -> str:
    def conv(o):
        if hasattr(o, "__dataclass_fields__"):
            return asdict(o)
        raise TypeError(type(o))
    return json.dumps(obj, indent=2, default=conv)
