"""Minimal MCP client: snapshot a server's tools/list. Stdlib only. Dual-era.

Eras (per the 2026-07-28 spec's backward-compatibility rules):
  - modern (2026-07-28+): stateless. No handshake; every request carries protocol version,
    client info and capabilities in params._meta. server/discover advertises supported versions.
  - legacy (2025-11-25 and earlier): initialize → notifications/initialized, then requests;
    on HTTP the server may assign an Mcp-Session-Id.
Era detection:
  - stdio: probe with server/discover. A DiscoverResult or a recognized modern error means modern;
    any other error, or no reply within discover_timeout, means legacy.
  - http:  send the modern server/discover. Fall back to legacy only on a 4xx whose body is not
    a recognized modern JSON-RPC error. 401/403 are auth failures, never a fallback signal.
Transports: stdio and Streamable HTTP. The deprecated HTTP+SSE transport is not supported (docs/GAPS.md).
"""
from __future__ import annotations

import json
import queue
import shlex
import subprocess
import threading
import urllib.error
import urllib.request
from typing import Any

MODERN_VERSION = "2026-07-28"
LEGACY_VERSION = "2025-11-25"   # newest handshake-era revision; servers negotiate down from it
CLIENT_INFO = {"name": "apiwatch", "version": "0.1.0"}
MAX_PAGES = 100

UNSUPPORTED_PROTOCOL_VERSION = -32022
MODERN_ERROR_CODES = {-32020, -32021, UNSUPPORTED_PROTOCOL_VERSION}  # HeaderMismatch, MissingRequiredClientCapability
META = "io.modelcontextprotocol/"


class McpError(RuntimeError):
    pass


class JsonRpcError(McpError):
    def __init__(self, method: str, code: int | None, message: str, data: Any = None, status: int | None = None):
        super().__init__(f"{method}: {message} (code {code})")
        self.code, self.data, self.status = code, data, status


class HttpStatusError(McpError):
    def __init__(self, method: str, status: int):
        super().__init__(f"{method}: HTTP {status}")
        self.status = status


class NoResponse(McpError):
    pass


def list_tools(target: str, headers: dict[str, str] | None = None, timeout: float = 30,
               discover_timeout: float | None = None) -> dict:
    """Return {"server", "protocolVersion", "era", "tools"} for an MCP server.

    target is "stdio:<command line>" or an http(s) URL.
    """
    dt = min(timeout, 5) if discover_timeout is None else discover_timeout
    if target.startswith("stdio:"):
        with _StdioSession(shlex.split(target[6:]), timeout) as s:
            return _run(s, dt)
    if target.startswith(("https://", "http://")):
        return _run(_HttpSession(target, headers or {}, timeout), dt)
    raise ValueError(f"unsupported MCP target: {target!r} (use stdio:<cmd> or an https URL)")


def _meta(version: str) -> dict:
    return {f"{META}protocolVersion": version, f"{META}clientInfo": CLIENT_INFO, f"{META}clientCapabilities": {}}


def _run(session, discover_timeout: float) -> dict:
    era, version, server, timed_out = _negotiate(session, discover_timeout)
    if era == "legacy":
        session.reset()
        try:
            init = session.request("initialize", {"protocolVersion": LEGACY_VERSION, "capabilities": {},
                                                  "clientInfo": CLIENT_INFO}, version=None)
        except JsonRpcError:
            # A slow-starting modern server can miss the probe timeout, then reject initialize.
            # Its late reply to server/discover arrived first (stdio is ordered); use it if present.
            late = session.late_reply(timed_out) if timed_out is not None else None
            if late is None:
                raise
            era, version, server = _classify_discover(late)
            if era != "modern":
                raise
        else:
            version = init.get("protocolVersion", LEGACY_VERSION)
            server = init.get("serverInfo", {})
            session.notify("notifications/initialized", version=version)
    if era == "modern":
        call = lambda params: session.request("tools/list", {**params, "_meta": _meta(version)}, version=version)
    else:
        call = lambda params: session.request("tools/list", params, version=version)
    tools: list[dict] = []
    cursor = None
    for _ in range(MAX_PAGES):
        result = call({"cursor": cursor} if cursor else {})
        tools += result.get("tools", [])
        server = server or (result.get("_meta") or {}).get(f"{META}serverInfo", {})
        cursor = result.get("nextCursor")
        if not cursor:
            break
    else:
        raise McpError(f"tools/list did not finish after {MAX_PAGES} pages")
    return {"server": server or {}, "protocolVersion": version, "era": era, "tools": tools}


def _negotiate(session, discover_timeout: float) -> tuple[str, str | None, dict, int | None]:
    """Return (era, version, serverInfo, probe_id_if_timed_out)."""
    probe_id = session.next_id
    try:
        res = session.request("server/discover", {"_meta": _meta(MODERN_VERSION)},
                              version=MODERN_VERSION, timeout=discover_timeout)
    except JsonRpcError as e:
        if e.status in (401, 403):
            raise
        if e.code == UNSUPPORTED_PROTOCOL_VERSION:
            return (*_pick((e.data or {}).get("supported", []), {}), None)
        if e.code in MODERN_ERROR_CODES:
            raise
        return "legacy", None, {}, None
    except HttpStatusError as e:
        if e.status in (401, 403):
            raise
        return "legacy", None, {}, None
    except NoResponse:
        return "legacy", None, {}, probe_id
    return (*_classify_discover({"result": res}), None)


def _classify_discover(msg: dict) -> tuple[str, str | None, dict]:
    if "error" in msg:
        e = msg["error"] or {}
        if e.get("code") == UNSUPPORTED_PROTOCOL_VERSION:
            return _pick((e.get("data") or {}).get("supported", []), {})
        return "legacy", None, {}
    res = msg.get("result") or {}
    return _pick(res.get("supportedVersions", []), (res.get("_meta") or {}).get(f"{META}serverInfo", {}))


def _pick(supported: list[str], server: dict) -> tuple[str, str | None, dict]:
    if MODERN_VERSION in supported:
        return "modern", MODERN_VERSION, server
    if any(v <= LEGACY_VERSION for v in supported):
        return "legacy", None, server
    raise McpError(f"no mutually supported MCP version: server supports {supported}, "
                   f"apiwatch speaks {MODERN_VERSION} and legacy ≤{LEGACY_VERSION}")


def _check(msg: dict, method: str, status: int | None = None) -> Any:
    if "error" in msg:
        e = msg["error"] if isinstance(msg["error"], dict) else {"message": str(msg["error"])}
        raise JsonRpcError(method, e.get("code"), e.get("message", ""), e.get("data"), status)
    return msg.get("result", {})


class _StdioSession:
    def __init__(self, argv: list[str], timeout: float):
        if not argv:
            raise ValueError("stdio: needs a command")
        self.timeout = timeout
        self.next_id = 1
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.late: dict[Any, dict] = {}   # replies to requests we stopped waiting for
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for line in self.proc.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=2)
        except Exception:
            self.proc.kill()

    def reset(self):
        pass  # same process serves the legacy handshake after a failed probe

    def late_reply(self, rid) -> dict | None:
        return self.late.get(rid)

    def _send(self, msg: dict):
        try:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            raise McpError(f"{msg.get('method')}: server exited (code {self.proc.poll()})") from None

    def notify(self, method: str, params: dict | None = None, version: str | None = None):
        self._send({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})})

    def request(self, method: str, params: dict, version: str | None = None, timeout: float | None = None) -> Any:
        rid = self.next_id
        self.next_id += 1
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        wait = self.timeout if timeout is None else timeout
        while True:
            try:
                line = self.lines.get(timeout=wait)
            except queue.Empty:
                raise NoResponse(f"{method}: no response within {wait}s") from None
            if line is None:
                raise McpError(f"{method}: server exited (code {self.proc.poll()})")
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue  # servers that log to stdout; not our message
            if msg.get("id") == rid:
                return _check(msg, method)
            if "id" in msg and ("result" in msg or "error" in msg):
                self.late[msg["id"]] = msg   # late reply to an abandoned request (e.g. the probe)
            # notifications are ignored


class _HttpSession:
    def __init__(self, url: str, headers: dict[str, str], timeout: float):
        self.url = url
        self.headers = headers
        self.timeout = timeout
        self.next_id = 1
        self.session_id: str | None = None

    def reset(self):
        self.session_id = None

    def late_reply(self, rid) -> dict | None:
        return None  # each HTTP request has its own response; nothing arrives late

    def _post(self, msg: dict, version: str | None, timeout: float | None):
        method = msg.get("method")
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", **self.headers}
        if version:
            h["MCP-Protocol-Version"] = version
            if version >= MODERN_VERSION:
                h["Mcp-Method"] = method
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        req = urllib.request.Request(self.url, data=json.dumps(msg).encode(), headers=h, method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout if timeout is None else timeout)
        except urllib.error.HTTPError as e:
            try:
                body = e.read()
            except OSError:
                body = b""   # server rejected without reading our body and reset the connection
            try:
                err = json.loads(body)
            except (json.JSONDecodeError, UnicodeDecodeError):
                err = None
            if isinstance(err, dict) and "error" in err:
                _check(err, method, e.code)
            raise HttpStatusError(method, e.code) from None
        sid = resp.headers.get("Mcp-Session-Id")
        if sid and not (version and version >= MODERN_VERSION):
            self.session_id = sid
        return resp

    def notify(self, method: str, params: dict | None = None, version: str | None = None):
        self._post({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})}, version, None).close()

    def request(self, method: str, params: dict, version: str | None = None, timeout: float | None = None) -> Any:
        rid = self.next_id
        self.next_id += 1
        with self._post({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}, version, timeout) as resp:
            ctype = resp.headers.get("Content-Type", "")
            if ctype.startswith("text/event-stream"):
                for msg in _sse_messages(resp):
                    if msg.get("id") == rid:
                        return _check(msg, method)
                raise McpError(f"{method}: event stream ended without a response")
            return _check(json.loads(resp.read()), method)


def _sse_messages(resp):
    data: list[str] = []
    for raw in resp:
        line = raw.decode("utf-8").rstrip("\r\n")
        if line.startswith("data:"):
            data.append(line[5:].lstrip())
        elif line == "" and data:
            try:
                yield json.loads("\n".join(data))
            except json.JSONDecodeError:
                pass
            data = []
    if data:
        try:
            yield json.loads("\n".join(data))
        except json.JSONDecodeError:
            pass
