"""A tiny MCP server for tests. Serves tools from a JSON file, one tool per tools/list page.

era:
  legacy  handshake-only (2025-06-18). Unknown methods before initialize → -32601 (stdio) or
          400 "no session" (http), like the 2025 SDKs.
  silent  legacy, but never answers unknown methods (exercises the stdio probe timeout).
  modern  stateless 2026-07-28 only. initialize → error naming supported versions.
  dual    both: server/discover and _meta requests are modern; initialize selects legacy.
  future  modern server that only speaks a version newer than the client knows.

stdio:  python fake_mcp_server.py <tools.json> [version] [era]
http:   make_http_server(tools_path, version, sse, require_auth, era) -> ThreadingHTTPServer
"""
from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MODERN = "2026-07-28"
META = "io.modelcontextprotocol/"


class Server:
    def __init__(self, tools_path, version="1.0.0", era="legacy"):
        self.tools_path, self.version, self.era = tools_path, version, era
        self.initialized = False
        self.supported = ["2027-06-01"] if era == "future" else [MODERN]

    def tools(self):
        return json.loads(Path(self.tools_path).read_text())["tools"]

    def info(self):
        return {"name": "fake-docs", "version": self.version}

    def handle(self, msg: dict) -> dict | None:
        """Return a JSON-RPC reply, or None for notifications / deliberate silence."""
        if "id" not in msg:
            if msg.get("method") == "notifications/initialized":
                self.initialized = True
            return None
        rid, method = msg["id"], msg.get("method")
        params = msg.get("params") or {}
        meta = params.get("_meta") or {}
        modern_era = self.era in ("modern", "dual", "future")

        def err(code, message, data=None):
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message,
                                                           **({"data": data} if data is not None else {})}}

        if method == "initialize":
            if self.era in ("modern", "future"):
                return err(-32601, f"initialize is not supported; supported versions: {self.supported}")
            self.initialized = True
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": self.info()}}

        is_modern_request = f"{META}protocolVersion" in meta
        if modern_era and (is_modern_request or method == "server/discover"):
            requested = meta.get(f"{META}protocolVersion")
            if requested not in self.supported:
                return err(-32022, "Unsupported protocol version", {"supported": self.supported, "requested": requested})
            if method == "server/discover":
                return {"jsonrpc": "2.0", "id": rid, "result": {
                    "resultType": "complete", "supportedVersions": self.supported,
                    "capabilities": {"tools": {}}, "_meta": {f"{META}serverInfo": self.info()},
                    "ttlMs": 60000, "cacheScope": "private"}}
            if method == "tools/list":
                return self._tools_page(rid, params, modern=True)
            return err(-32601, f"method not found: {method}")

        # legacy semantics from here on
        if not self.initialized:
            if self.era == "silent":
                return None
            return err(-32601, f"method not found: {method}")
        if method == "tools/list":
            return self._tools_page(rid, params, modern=False)
        return err(-32601, f"method not found: {method}")

    def _tools_page(self, rid, params, modern):
        tools = self.tools()
        i = int(params.get("cursor") or 0)
        result = {"tools": tools[i:i + 1]}
        if i + 1 < len(tools):
            result["nextCursor"] = str(i + 1)
        if modern:
            result.update({"resultType": "complete", "ttlMs": 60000, "cacheScope": "private"})
        return {"jsonrpc": "2.0", "id": rid, "result": result}


def stdio_main(path: str, version: str = "1.0.0", era: str = "legacy", startup_delay: str = "0"):
    import time
    time.sleep(float(startup_delay))   # simulate a slow cold start (e.g. npx fetching a package)
    srv = Server(path, version, era)
    print("fake-docs starting (non-JSON noise on stdout)", flush=True)
    for line in sys.stdin:
        reply = srv.handle(json.loads(line))
        if reply:
            # a stray server notification first, which the client must skip
            print(json.dumps({"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info"}}), flush=True)
            print(json.dumps(reply), flush=True)


def make_http_server(tools_path, version: str = "1.0.0", sse: bool = True,
                     require_auth: str | None = None, era: str = "legacy"):
    sessions: dict[str, Server] = {}
    modern_srv = Server(tools_path, version, era)
    seen: list[dict] = []   # request headers, for assertions

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, status, body, sid=None):
            data = json.dumps(body).encode()
            self.send_response(status)
            if sid:
                self.send_header("Mcp-Session-Id", sid)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            if require_auth and self.headers.get("Authorization") != require_auth:
                self.send_response(401)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            msg = json.loads(raw)
            seen.append({k.lower(): v for k, v in self.headers.items()} | {"_method": msg.get("method")})
            meta = (msg.get("params") or {}).get("_meta") or {}
            modern_req = f"{META}protocolVersion" in meta

            if era in ("modern", "dual", "future") and modern_req:
                if self.headers.get("MCP-Protocol-Version") != meta[f"{META}protocolVersion"] or \
                        self.headers.get("Mcp-Method") != msg.get("method"):
                    return self._json(400, {"jsonrpc": "2.0", "id": msg.get("id"),
                                            "error": {"code": -32020, "message": "Header mismatch"}})
                if self.headers.get("Mcp-Session-Id"):
                    return self._json(400, {"jsonrpc": "2.0", "id": msg.get("id"),
                                            "error": {"code": -32600, "message": "sessions are not part of 2026-07-28"}})
                reply = modern_srv.handle(msg)
                status = 400 if reply.get("error", {}).get("code") == -32022 else 200
                return self._reply(status, msg, reply, None)

            # legacy (session) semantics
            if era in ("modern", "future"):
                return self._json(400, {"jsonrpc": "2.0", "id": msg.get("id"),
                                        "error": {"code": -32600, "message": "missing _meta protocol version"}})
            if msg.get("method") == "initialize":
                sid = f"sess-{len(sessions) + 1}"
                sessions[sid] = Server(tools_path, version, "legacy")
                return self._reply(200, msg, sessions[sid].handle(msg), sid)
            sid = self.headers.get("Mcp-Session-Id")
            if sid not in sessions:
                # what the 2025 TypeScript SDK does for a request without a session
                return self._json(400, {"jsonrpc": "2.0", "id": None,
                                        "error": {"code": -32000, "message": "Bad Request: No valid session ID provided"}})
            assert self.headers.get("MCP-Protocol-Version"), "protocol version header missing"
            reply = sessions[sid].handle(msg)
            if reply is None:
                self.send_response(202)
                self.end_headers()
                return
            self._reply(200, msg, reply, sid)

        def _reply(self, status, msg, reply, sid):
            body = json.dumps(reply)
            if status == 200 and sse and msg.get("method") == "tools/list":
                self.send_response(200)
                if sid:
                    self.send_header("Mcp-Session-Id", sid)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                note = json.dumps({"jsonrpc": "2.0", "method": "notifications/progress", "params": {}})
                self.wfile.write(f"event: message\ndata: {note}\n\nevent: message\ndata: {body}\n\n".encode())
            else:
                self._json(status, reply, sid)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.seen = seen
    return srv


if __name__ == "__main__":
    stdio_main(*sys.argv[1:])
