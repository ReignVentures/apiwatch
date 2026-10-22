"""Public MCP server change history (docs/MCP-HISTORY.md, Part 1).

`apiwatch history run` reads feed/mcp-watch.yml, snapshots each server's tools/list with probe.fetch_mcp (no
credentials, nothing but discover or initialize and tools/list) and keeps these files under feed/mcp-history/<slug>/:

  current.json            hash of the current confirmed snapshot, tool count, protocolVersion, era
  history.json            entries, oldest first: first_seen, change (confirmed), unstable (two hashes alternating)
  state.json              last run, last success, pending hash, last error, failing_since
  pending.json            the snapshot behind the pending hash, until a later run confirms or drops it
  snapshots/<hash12>.json every confirmed snapshot, so any change can be re-diffed later

Rules:
  - A failed fetch is never a change. A timeout, an HTTP error, a 401, bad JSON or an empty tool list only
    updates state.json; no tool events come from it.
  - A new hash is a change only when the next successful run sees the same hash. The change is dated by the run
    that first saw it. A hash seen once and then gone is dropped; a pair of hashes that keeps alternating is
    noted once as unstable.
  - Everything after the fetch is deterministic and runs with no network (tests pass their own fetch).

Tool names, titles and descriptions come from servers apiwatch doesn't control and are data, never instructions.
history.json keeps no description or title text: those changes are stored as lengths and sha256s, and the site
quotes the text from the snapshots under a label. Flags are text checks that mean "worth a look", nothing more.
"""
from __future__ import annotations

import hashlib
import json
import re
import socket
import unicodedata
import urllib.error
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from . import mcp_client, probe
from .models import SEVERITIES

SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
TOOL_NAME = re.compile(r"^[A-Za-z0-9_.\-/]{1,128}$")
# zero width, bidi controls, word joiner and invisible operators, BOM, Unicode tags
HIDDEN = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff\U000e0000-\U000e007f]")
# matched case insensitive, whitespace collapsed, hidden characters removed, curly apostrophes made straight
INSTRUCTION_PHRASES = (
    "ignore previous", "ignore all previous", "ignore all prior", "ignore the above", "disregard previous",
    "do not tell the user", "don't tell the user", "do not mention", "don't mention",
    "before using any other tool", "system prompt", "<important>", "~/.ssh", ".env", "id_rsa",
)
FLAGS = ("description_changed", "annotation_widened", "hidden_characters", "instruction_like_text", "unusual_tool_name")
# MCP's defaults for absent hints (readOnlyHint false, destructiveHint true, idempotentHint false, openWorldHint true)
# and, per hint, the value that lets a tool do more
HINTS = {"readOnlyHint": (False, False), "destructiveHint": (True, True), "idempotentHint": (False, False),
         "openWorldHint": (True, True)}
TEXT_EVENTS = ("description_changed", "title_changed")
UNSTABLE_AFTER = 2       # reverts of the same pair of hashes before it is noted as unstable
OPEN_FLAG_DAYS = 30      # flags from changes this recent show on the index and in the MCP answers


@dataclass
class Server:
    slug: str
    name: str
    url: str
    registry_name: str = ""
    added: str = ""


def load_watch(path: Path) -> list[Server]:
    data = yaml.safe_load(Path(path).read_text()) or {}
    out: list[Server] = []
    for row in data.get("servers") or []:
        s = Server(**{k: str(v) for k, v in row.items()})
        if not SLUG.match(s.slug):
            raise ValueError(f"{path}: bad slug {s.slug!r}")
        # https only: a watched server is a public remote, never a local command (no stdio:, no file:)
        if not re.match(r"^https://[^\s/?#]+[^\s]*$", s.url):
            raise ValueError(f"{path}: {s.slug}: url must be https://")
        if not s.name or len(s.name) > 100:
            raise ValueError(f"{path}: {s.slug}: name is required (at most 100 characters)")
        if s.added:
            date.fromisoformat(s.added)
        if any(o.slug == s.slug for o in out):
            raise ValueError(f"{path}: duplicate slug {s.slug}")
        out.append(s)
    return out


# ---------- snapshots ----------

def snapshot_of(fetched: dict) -> dict:
    """The part of a probe.fetch_mcp result that is kept. Raises ValueError for a tool list the differ can't read
    (the run records that as a failed check, never as a change). `required` lists are sorted: their order means
    nothing, and a server that builds them from a set must not look like it changed."""
    tools = fetched.get("tools")
    if not isinstance(tools, list):
        raise ValueError("no tools array")
    tools = [_normalize(t) for t in tools]
    for t in tools:
        if not (isinstance(t, dict) and isinstance(t.get("name"), str)):
            raise ValueError("a tool without a name")
        for k in ("inputSchema", "outputSchema", "annotations"):
            if k in t and t[k] is not None and not isinstance(t[k], dict):
                raise ValueError(f"{k} is not an object")
        for k in ("inputSchema", "outputSchema"):
            _check_schema(t.get(k) or {})
    snap: dict[str, Any] = {"kind": "mcp", "tools": tools}
    for k in ("protocolVersion", "era"):
        if fetched.get(k):
            snap[k] = fetched[k]
    return snap


def _check_schema(s: Any, depth: int = 0) -> None:
    if depth > 64:
        raise ValueError("schema nested too deep")
    if not isinstance(s, dict):
        raise ValueError("a schema is not an object")
    props = s.get("properties")
    if props is not None:
        if not isinstance(props, dict):
            raise ValueError("properties is not an object")
        for v in props.values():
            _check_schema(v, depth + 1)
    if s.get("required") is not None and not (isinstance(s["required"], list) and all(isinstance(x, str) for x in s["required"])):
        raise ValueError("required is not a list of names")
    if isinstance(s.get("items"), dict):
        _check_schema(s["items"], depth + 1)
    if s.get("enum") is not None and not isinstance(s["enum"], list):
        raise ValueError("enum is not a list")
    for k in ("$defs", "definitions"):
        if isinstance(s.get(k), dict):
            for v in s[k].values():
                if isinstance(v, dict):
                    _check_schema(v, depth + 1)


def _normalize(v: Any, key: str = "") -> Any:
    """required lists sorted; JSON Schema's boolean subschemas (true, false) under properties and items written as
    the objects they mean ({} and {"not": {}}), which the differ reads."""
    if isinstance(v, dict):
        out = {}
        for k, x in v.items():
            if k == "required" and isinstance(x, list) and all(isinstance(i, str) for i in x):
                out[k] = sorted(x)
            elif k == "properties" and isinstance(x, dict):
                out[k] = {n: _bool_schema(y) for n, y in x.items()}
            elif k == "items":
                out[k] = _bool_schema(x)
            else:
                out[k] = _normalize(x, k)
        return out
    if isinstance(v, list):
        return [_normalize(x) for x in v]
    return v


def _bool_schema(x: Any) -> Any:
    if x is True:
        return {}
    if x is False:
        return {"not": {}}
    return _normalize(x)


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def snap_hash(snap: dict) -> str:
    """The tools only. The protocol version and era are what apiwatch's client negotiated, which a passing error on
    server/discover (or a new client version) can move, so they are kept in current.json but are never a change."""
    return hashlib.sha256(canonical(snap.get("tools", []))).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def safe_name(name: str) -> str:
    """A tool name as MCP answers may carry it: itself if it passes the pattern, else its sha256."""
    return name if isinstance(name, str) and TOOL_NAME.match(name) else "sha256:" + sha256_text(str(name))


_SAFE_REST = re.compile(r"^[A-Za-z0-9_.\-/\[\]]{0,256}$")


def safe_path(path: str, tool: str) -> str:
    """An event path for MCP answers: tool:<safe name><rest>, where a rest holding characters outside the tool
    name pattern (a parameter or annotation name could say anything) is replaced by its sha256."""
    if not tool:
        return path if _SAFE_REST.match(path) else "sha256:" + sha256_text(path)
    rest = path[len("tool:" + tool):]
    if not _SAFE_REST.match(rest):
        head = re.match(r"^\.(input|output|annotations)", rest)
        rest = (head.group(0) if head else "") + ".sha256:" + sha256_text(rest)
    return "tool:" + safe_name(tool) + rest


# ---------- flags ----------

def _phrase_re(p: str) -> re.Pattern:
    body = r"\s+".join(re.escape(w) for w in p.split(" "))
    left = r"(?<![a-z0-9_])" if p[0].isalnum() else ""
    right = r"s?(?![a-z0-9_])" if p[-1].isalnum() else ""
    return re.compile(left + body + right)


_PHRASES = [(p, _phrase_re(p)) for p in INSTRUCTION_PHRASES]


def _norm(text: str) -> str:
    return " ".join(HIDDEN.sub("", text).replace("\u2019", "'").replace("\u2018", "'").lower().split())


def phrases_in(text: str) -> set[str]:
    t = _norm(text)
    return {p for p, rx in _PHRASES if rx.search(t)}


def tool_text(tool: dict | None) -> str:
    """Every string an agent reads about a tool: name, title, description, and the titles and descriptions inside
    its annotations and schemas."""
    if not tool:
        return ""
    parts = [str(tool.get("name", ""))]

    def walk(v):
        if isinstance(v, dict):
            for k, x in v.items():
                if k in ("title", "description") and isinstance(x, str):
                    parts.append(x)
                else:
                    walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk({k: tool.get(k) for k in ("title", "description", "annotations", "inputSchema", "outputSchema")})
    return "\n".join(parts)


def _hint(ann: Any, key: str) -> bool:
    v = ann.get(key) if isinstance(ann, dict) else None
    return v if isinstance(v, bool) else HINTS[key][0]


def flags(before_tools: list[dict], after_tools: list[dict]) -> list[dict]:
    """Text checks on what is new in after_tools. Each flag means "worth a look", never a finding."""
    b = {t.get("name"): t for t in before_tools}
    out: list[dict] = []
    for t in after_tools:
        name = t.get("name")
        old = b.get(name)
        if old is None and not (isinstance(name, str) and TOOL_NAME.match(name)):
            out.append({"flag": "unusual_tool_name", "tool": name, "detail": ""})
        if old is not None and (old.get("description") or "") != (t.get("description") or ""):
            out.append({"flag": "description_changed", "tool": name, "detail": ""})
        if old is not None:
            for key, (_, wide) in HINTS.items():
                was, now = _hint(old.get("annotations"), key), _hint(t.get("annotations"), key)
                if was != now and now == wide:
                    out.append({"flag": "annotation_widened", "tool": name, "detail": f"{key} {str(was).lower()} to {str(now).lower()}"})
        new_text, old_text = tool_text(t), tool_text(old)
        hidden_new, hidden_old = HIDDEN.findall(new_text), HIDDEN.findall(old_text)
        if len(hidden_new) > len(hidden_old):
            codes = sorted({f"U+{ord(c):04X}" for c in hidden_new})
            out.append({"flag": "hidden_characters", "tool": name, "detail": " ".join(codes)})
        for p in sorted(phrases_in(new_text) - phrases_in(old_text)):
            out.append({"flag": "instruction_like_text", "tool": name, "detail": p})
    return out


# ---------- diff ----------

def _tool_of(path: str, names: list[str]) -> str:
    for n in names:   # longest first: a name may contain dots
        p = "tool:" + n
        if path == p or path.startswith(p + ".") or path.startswith(p + "["):
            return n
    return ""


def diff(before: dict, after: dict) -> list[dict]:
    """Events between two snapshots, as stored in history.json: mcp_tool_drift's events and a changed_unclassified event for a tool whose JSON changed in a way the probe does not classify (say, a
    parameter's description), so nothing changes silently. Description and title text is not stored."""
    bt, at = before.get("tools", []), after.get("tools", [])
    names = sorted({t.get("name") for t in bt + at if isinstance(t.get("name"), str)}, key=len, reverse=True)
    out = []
    for e in probe.mcp_tool_drift("history", bt, at):
        tool = _tool_of(e.path, names)
        ev = {"type": e.change, "tool": tool, "path": e.path, "severity": e.severity}
        if e.change in TEXT_EVENTS or (e.change == "annotation_changed" and e.path.endswith(".annotations.title")):
            for side, v in (("old", e.before), ("new", e.after)):
                s = v if isinstance(v, str) else ""
                ev[f"{side}_len"], ev[f"{side}_sha256"] = len(s), sha256_text(s)
        else:
            ev["before"], ev["after"] = e.before, e.after
        out.append(ev)
    seen = {e["tool"] for e in out}
    b = {t.get("name"): t for t in bt}
    for t in at:
        n = t.get("name")
        if n in b and n not in seen and canonical(b[n]) != canonical(t):
            out.append({"type": "changed_unclassified", "tool": n, "path": f"tool:{n}", "severity": "info"})
    return out


def counts(events: list[dict]) -> dict[str, int]:
    c = {s: 0 for s in SEVERITIES}
    for e in events:
        c[e["severity"]] = c.get(e["severity"], 0) + 1
    return {s: n for s, n in c.items() if n}


# ---------- a run ----------

def describe_error(e: BaseException) -> tuple[str, str]:
    """(kind, short text) for a failed fetch. Never the server's own message: it is third party text."""
    status = getattr(e, "status", None)
    if status in (401, 403):
        return "auth", f"HTTP {status}"
    if isinstance(e, mcp_client.HttpStatusError):
        return "error", f"HTTP {status}"
    if isinstance(e, mcp_client.JsonRpcError):
        return "error", f"JSON-RPC error {e.code}" if isinstance(e.code, int) else "JSON-RPC error"
    if isinstance(e, (mcp_client.NoResponse, socket.timeout, TimeoutError)):
        return "unreachable", "timeout"
    if isinstance(e, urllib.error.URLError):
        return ("unreachable", "timeout") if isinstance(e.reason, (socket.timeout, TimeoutError)) else ("unreachable", "connection failed")
    if isinstance(e, (ConnectionError, OSError)):
        return "unreachable", "connection failed"
    if isinstance(e, (ValueError, UnicodeDecodeError)):
        return "error", "invalid response"
    if isinstance(e, mcp_client.McpError):
        return "error", "protocol error"
    return "error", type(e).__name__


def _read(p: Path, default=None):
    return json.loads(p.read_text()) if p.exists() else default


def _write(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=True, sort_keys=False) + "\n")
    tmp.replace(p)


def _save_snapshot(d: Path, h: str, snap: dict) -> None:
    p = d / "snapshots" / f"{h[:12]}.json"
    if not p.exists():
        _write(p, snap)


def load_snapshot(d: Path, h: str) -> dict | None:
    return _read(d / "snapshots" / f"{h[:12]}.json")


def default_fetch(s: Server) -> dict:
    # no headers: never a token, cookie or key (the client adds only its User-Agent)
    return probe.fetch_mcp(probe.Endpoint(id=s.slug, source="mcp:" + s.url))


def _stamp(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(servers: list[Server], root: Path, now: datetime | None = None,
        fetch: Callable[[Server], dict] = default_fetch) -> list[tuple[str, bool, str]]:
    """One check of every server. Returns (slug, fetched ok, what happened) per server. One server's bad answer
    never stops the others."""
    now = now or datetime.now(timezone.utc)
    results = []
    for s in servers:
        d = root / s.slug
        try:
            fetched = fetch(s)
            if not isinstance(fetched, dict):
                raise ValueError("no tools array")
            snap = snapshot_of(fetched)
        except Exception as e:   # noqa: BLE001  every failure is recorded the same way: never as a change
            kind, text = describe_error(e)
            results.append((s.slug, False, _failed(d, now, kind, text)))
            continue
        try:
            results.append((s.slug, True, observe(d, snap, now)))
        except Exception as e:   # noqa: BLE001  apiwatch's own problem (a bug, a missing file), not the server's:
            # kept apart from the server's status, any pending hash dropped, and the other servers still checked
            st = _read(d / "state.json", {})
            st.pop("pending", None)
            st.update(internal_error=f"could not compare ({type(e).__name__})", internal_error_at=_stamp(now))
            _write(d / "state.json", st)
            (d / "pending.json").unlink(missing_ok=True)
            results.append((s.slug, False, f"apiwatch could not compare this check ({type(e).__name__}); nothing recorded"))
    return results


def _failed(d: Path, now: datetime, kind: str, text: str) -> str:
    st = _read(d / "state.json", {})
    stamp = _stamp(now)
    # the streak start moves only when a login wall starts or ends: a timeout that turns into a 502 is the same outage
    if not st.get("failing_since") or (st.get("error_kind") == "auth") != (kind == "auth"):
        st["failing_since"] = stamp
    st.update(last_run=stamp, last_result="error", error_kind=kind, last_error=text, last_error_at=stamp)
    _write(d / "state.json", st)   # a pending hash stays pending: only two successful runs that agree confirm it
    return f"check failed ({kind}: {text}); no change recorded"


def observe(d: Path, snap: dict, now: datetime) -> str:
    """Apply one successful fetch to a server's files. Returns what happened, in a few words.
    current.json is written last, so a crash part way leaves the old current and the change is seen again."""
    st = _read(d / "state.json", {})
    cur = _read(d / "current.json")
    stamp, day = _stamp(now), now.astimezone(timezone.utc).date().isoformat()
    tools = snap["tools"]
    if not tools and (cur is None or cur.get("tool_count")):
        # an empty list where there were tools (or a first look) is a failure, not "every tool removed"
        return _failed(d, now, "empty", "empty tool list")
    for k in ("error_kind", "failing_since"):
        st.pop(k, None)
    st.update(last_run=stamp, last_result="ok", last_ok=stamp)
    h = snap_hash(snap)
    hist = _read(d / "history.json", [])
    pending = st.get("pending")
    new_current = None
    if cur is None:
        _save_snapshot(d, h, snap)
        hist.append({"kind": "first_seen", "date": day, "at": stamp, "to": h, "tool_count": len(tools),
                     "flags": flags([], tools)})
        new_current = (h, day)
        msg = f"first check: {len(tools)} tools"
    elif h == cur["hash"]:
        msg = "no change"
        if pending:
            msg = "no change (a different tool list seen once last time was not seen again)"
            _flip(d, st, hist, cur["hash"], pending["hash"], day, stamp, {pending["hash"]: _read(d / "pending.json")})
        if (snap.get("protocolVersion", ""), snap.get("era", "")) != (cur.get("protocolVersion", ""), cur.get("era", "")):
            new_current = (h, cur.get("since", day))   # same tools, other protocol: noted in current.json, not a change
    elif pending and pending["hash"] == h:
        old = load_snapshot(d, cur["hash"])
        if old is None:
            raise RuntimeError(f"{d.name}: snapshot {cur['hash'][:12]} is missing")
        events = diff(old, snap)
        _save_snapshot(d, h, snap)
        first = pending["first_seen"]
        last = next((e for e in reversed(hist) if e["kind"] == "change"), None)
        if last and last["from"] == cur["hash"] and last["to"] == h:
            # had it been applied, current.json would be h: a run stopped before writing it. Record it once,
            # dated by the run that first saw it
            first = min(first, last["first_seen_at"])
            hist.remove(last)
        hist.append({"kind": "change", "date": first[:10], "first_seen_at": first,
                     "confirmed_at": stamp, "from": cur["hash"], "to": h, "events": events,
                     "flags": flags(old.get("tools", []), tools)})
        prev = [e for e in hist[:-1] if e["kind"] == "change"]
        if prev and prev[-1]["from"] == h:   # back to what it was before the last change
            _flip(d, st, hist, cur["hash"], h, day, stamp, {h: snap})
        new_current = (h, day)
        c = counts(events)
        msg = "change confirmed: " + (", ".join(f"{n} {s}" for s, n in c.items()) or "no classified events")
    else:
        if pending:
            # two new lists taking turns (a rollout split across two builds): each flip counts toward unstable
            msg = f"another new tool list ({h[:12]}), pending until the next run"
            _flip(d, st, hist, pending["hash"], h, day, stamp, {pending["hash"]: _read(d / "pending.json"), h: snap})
        else:
            msg = f"new tool list ({h[:12]}), pending until the next run confirms it"
        st["pending"] = {"hash": h, "first_seen": stamp}
        _write(d / "pending.json", snap)
        _write(d / "history.json", hist)
        _write(d / "state.json", st)
        return msg
    st.pop("pending", None)
    _write(d / "history.json", hist)
    _write(d / "state.json", st)
    (d / "pending.json").unlink(missing_ok=True)
    if new_current:
        _set_current(d, new_current[0], snap, new_current[1])
    return msg


def _set_current(d: Path, h: str, snap: dict, since: str) -> None:
    _write(d / "current.json", {"hash": h, "tool_count": len(snap["tools"]), "protocolVersion": snap.get("protocolVersion", ""),
                                "era": snap.get("era", ""), "since": since})


def _flip(d, st, hist, a, b, day, stamp, snaps):
    """The server went from list a to list b and has gone between them before. Noted once per pair as unstable
    after UNSTABLE_AFTER flips; both lists are kept so the difference can be looked at."""
    pair = ":".join(sorted((a[:12], b[:12])))
    flips = st.setdefault("reverts", {})
    flips[pair] = flips.get(pair, 0) + 1
    noted = st.setdefault("unstable_noted", [])
    if flips[pair] >= UNSTABLE_AFTER and pair not in noted:
        noted.append(pair)
        for h, snap in snaps.items():
            if snap is not None:
                _save_snapshot(d, h, snap)
        hist.append({"kind": "unstable", "date": day, "at": stamp, "hashes": sorted([a, b])})


# ---------- reading it back (site and /mcp-history.json) ----------

@dataclass
class View:
    server: Server
    dir: Path
    current: dict | None
    history: list
    state: dict

    @property
    def status(self) -> tuple[str, str]:
        """(status, since): pending_first_check | ok | needs_login | not_reachable | empty_tools | failing."""
        kind = self.state.get("error_kind")
        if kind:
            return {"auth": "needs_login", "unreachable": "not_reachable", "empty": "empty_tools"}.get(kind, "failing"), \
                self.state.get("failing_since", "")
        if self.current is None:
            return "pending_first_check", ""
        return "ok", ""

    @property
    def changes(self) -> list[dict]:
        return [e for e in self.history if e.get("kind") == "change"]

    def open_flags(self, today: date) -> list[dict]:
        since = (today - timedelta(days=OPEN_FLAG_DAYS)).isoformat()
        return [f for e in self.history if e.get("date", "") >= since for f in e.get("flags", [])]

    def snapshot(self, h: str) -> dict | None:
        return load_snapshot(self.dir, h)


def load_views(watch_file: Path | None, history_dir: Path | None) -> list[View]:
    if not watch_file or not Path(watch_file).exists():
        return []
    views = []
    for s in load_watch(Path(watch_file)):
        d = Path(history_dir or "") / s.slug
        views.append(View(s, d, _read(d / "current.json"), _read(d / "history.json", []), _read(d / "state.json", {})))
    return views


def public_events(events: list[dict]) -> list[dict]:
    out = []
    for e in events:
        tool = e.get("tool") or ""
        ev = {"type": e["type"], "tool": safe_name(tool) if tool else "", "path": safe_path(e.get("path", ""), tool),
              "severity": e["severity"]}
        if e["type"] in TEXT_EVENTS:
            ev.update({k: e[k] for k in ("old_len", "new_len", "old_sha256", "new_sha256") if k in e})
        out.append(ev)
    return out


def public_flags(fl: list[dict]) -> list[dict]:
    # detail is built by flags() from apiwatch's own constants (phrase list, hint names, code points)
    return [{"flag": f["flag"], "tool": safe_name(f.get("tool") or ""), "detail": f.get("detail", "")} for f in fl]


def public_data(views: list[View], base: str, today: date) -> dict:
    """/mcp-history.json: what the MCP worker answers from. No description or title text, tool names that fail the
    pattern as sha256, no error messages from servers."""
    servers = []
    for v in views:
        status, since = v.status
        entries = []
        for e in v.history:
            row = {"date": e["date"], "kind": e["kind"]}
            if e["kind"] == "change":
                row.update(events=public_events(e.get("events", [])), flags=public_flags(e.get("flags", [])),
                           counts=counts(e.get("events", [])))
            elif e["kind"] == "first_seen":
                row.update(tool_count=e.get("tool_count", 0), flags=public_flags(e.get("flags", [])))
            elif e["kind"] == "unstable":
                row.update(hashes=e.get("hashes", []))
            entries.append(row)
        changes = v.changes
        first = next((e["date"] for e in v.history if e["kind"] == "first_seen"), "")
        servers.append({
            "slug": v.server.slug, "name": v.server.name, "url": v.server.url, "registry_name": v.server.registry_name,
            "added": v.server.added, "status": status, "status_since": since,
            "tool_count": v.current["tool_count"] if v.current else None,
            "last_checked": v.state.get("last_run", ""), "last_ok": v.state.get("last_ok", ""),
            "first_seen": first, "last_change": changes[-1]["date"] if changes else "",
            "open_flags": sorted({f["flag"] for f in v.open_flags(today)}),
            "page_url": f"{base}mcp-servers/{v.server.slug}/",
            "entries": entries,
        })
    return {"version": 1, "generated": today.isoformat(), "index_url": f"{base}mcp-servers/",
            "note": "Observed tools/list results of public MCP servers, checked without credentials. Flags mean worth a "
                    "look, not a finding. No tool description text is included.",
            "servers": servers}


# ---------- wording (the probe's words; docs/MCP-PROBE.md) ----------

def event_phrase(e: dict) -> str:
    t, path = e["type"], e.get("path", "")
    where = "output" if ".output" in path else "input"
    tool_level = path == f"tool:{e.get('tool')}"
    if t == "removed":
        return "Tool removed" if tool_level else ("Output field removed" if where == "output" else "Parameter removed")
    if t == "added":
        return "Tool added" if tool_level else ("Output field added" if where == "output" else "Optional parameter added")
    return {
        "possible_rename": "Possibly renamed (an added tool shares most of its parameters)",
        "added_required": "New required parameter",
        "became_required": "Parameter became required" if where == "input" else "Output field became required",
        "became_optional": "Parameter became optional" if where == "input" else "Output field no longer required",
        "type_changed": "Type changed",
        "enum_values_removed": "Allowed values removed",
        "enum_values_added": "Allowed values added",
        "enum_added": "Allowed values newly imposed",
        "combinator_changed": "anyOf, oneOf or allOf changed (not judged yet)",
        "description_changed": "Description changed",
        "title_changed": "Title changed",
        "annotation_changed": "Annotation changed",
        "protocol_changed": "Protocol version changed",
        "era_changed": "Protocol era changed",
        "changed_unclassified": "Changed in a way the probe does not classify yet (for example a parameter description)",
    }.get(t, t.replace("_", " ").capitalize())


FLAG_TEXT = {
    "description_changed": "description changed",
    "annotation_widened": "annotation widened",
    "hidden_characters": "hidden characters",
    "instruction_like_text": "instruction-like text",
    "unusual_tool_name": "unusual tool name",
}


def visible(text: str) -> str:
    """Invisible and control characters shown as <U+XXXX> (newlines and tabs kept)."""
    def rep(c: str) -> str:
        if c in "\n\t":
            return c
        if HIDDEN.match(c) or unicodedata.category(c) in ("Cc", "Cf", "Co", "Cs", "Zl", "Zp"):
            return f"<U+{ord(c):04X}>"
        return c
    return "".join(rep(c) for c in text)
