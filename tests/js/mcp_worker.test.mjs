// Tests for apiwatch/mcp_worker.js (the public MCP server). Node's built-in runner, no dependencies:
//   node --test tests/js/
// The real-feed tests read APIWATCH_FEED (tests/test_mcp_worker.py builds the site and sets it), else
// out/site/feed.json if it exists, else they are skipped.
import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import worker, { handle, matchRecord, answerFor, TOOLS } from "../../apiwatch/mcp_worker.js";

const URL_ = "https://apiwatch.example/mcp";
const MODERN = "2026-07-28";
const META = "io.modelcontextprotocol/";
const NOW = Date.parse("2026-10-21T12:00:00Z");   // today = 2026-10-21

function item(a) {
  const base = { exclude: [], context: [], files: [], repo_context: [], scope: "", kind: "model", detail: "",
                 fix_hint: "Switch to new-model.", source_url: "https://vendor.example/deprecations", verified: true,
                 verified_by: "claude", verified_on: "2026-10-01", review: "ai", surface: "API" };
  const rec = { ...base, ...a };
  return { id: `urn:apiwatch:${rec.id}`, url: `https://apiwatch.example/records/${rec.id}`, title: `${rec.vendor}: ${rec.summary}`,
           _apiwatch: rec };
}

const FIXTURE = {
  home_page_url: "https://apiwatch.example/",
  items: [
    item({ id: "v-2026-11-01-alpha", vendor: "OpenAI", severity: "breaking", effective: "2026-11-01",
           summary: "alpha-1 shuts down.", signatures: ["alpha-1"], exclude: ["alpha-1-2025-01-01"] }),
    item({ id: "v-2025-01-01-beta", vendor: "Anthropic", severity: "breaking", effective: "2025-01-01",
           summary: "beta-2 retired.", signatures: ["beta-2"] }),
    item({ id: "v-2027-03-01-gamma", vendor: "Google", severity: "deprecation", effective: "2027-03-01",
           summary: "gamma-3 is deprecated.", signatures: ["gamma-3"] }),
    item({ id: "v-2026-10-21-delta", vendor: "Google", severity: "silent", effective: "2026-10-21",
           summary: "delta-4 output changes.", signatures: ["delta-4"] }),
    item({ id: "v-2026-12-01-sdk", vendor: "Stripe", severity: "breaking", effective: "2026-12-01", kind: "sdk",
           summary: "Stripe SDK removes widgetize.", signatures: ["widgetize"], repo_context: ["dep:pypi:stripe@16."],
           scope: "Applies only in repos that depend on \"stripe 16.x\" (pypi)." }),
    item({ id: "v-2026-11-15-api", vendor: "Stripe", severity: "breaking", effective: "2026-11-15", kind: "rest",
           summary: "The /v1/widgets endpoint is removed.", signatures: ["/v1/widgets"] }),
  ],
};

function env(feed = FIXTURE, counter = { n: 0 }) {
  return { ASSETS: { fetch: async (u) => {
    counter.n++;
    assert.equal(new URL(u).pathname, "/feed.json");      // the worker reads nothing else
    return new Response(JSON.stringify(feed), { status: 200, headers: { "Content-Type": "application/json" } });
  } } };
}

async function send(body, { headers = {}, method = "POST", e = env(), opts } = {}) {
  const init = { method, headers: { "Content-Type": "application/json", Accept: "application/json, text/event-stream", ...headers } };
  if (method === "POST") init.body = typeof body === "string" ? body : JSON.stringify(body);
  const res = await handle(new Request(URL_, init), e, { now: NOW, cache: {}, ...opts });
  const raw = await res.text();
  return { status: res.status, headers: res.headers, raw, body: raw ? JSON.parse(raw) : null };
}

function modernReq(method, params = {}, id = 1, extraHeaders = {}) {
  const headers = { "MCP-Protocol-Version": MODERN, "Mcp-Method": method, ...extraHeaders };
  if (method === "tools/call" && !("Mcp-Name" in extraHeaders)) headers["Mcp-Name"] = params.name;
  return [{ jsonrpc: "2.0", id, method, params: { ...params, _meta: { [META + "protocolVersion"]: MODERN,
            [META + "clientInfo"]: { name: "t", version: "1" }, [META + "clientCapabilities"]: {} } } }, { headers }];
}

async function modern(method, params, extraHeaders, opts) {
  const [body, o] = modernReq(method, params, 1, extraHeaders);
  return send(body, { ...o, ...opts });
}

async function legacy(method, params = {}, version = "2025-11-25", opts = {}) {
  const headers = version ? { "MCP-Protocol-Version": version } : {};
  return send({ jsonrpc: "2.0", id: 7, method, params }, { headers, ...opts });
}

async function call(name, args, opts) {
  const r = await modern("tools/call", { name, arguments: args }, undefined, opts);
  assert.equal(r.status, 200, r.raw);
  return r.body.result;
}

// ---------- protocol: modern ----------

test("server/discover lists versions, capabilities and identity", async () => {
  const r = await modern("server/discover");
  assert.equal(r.status, 200);
  assert.equal(r.headers.get("Content-Type"), "application/json");
  const res = r.body.result;
  assert.equal(res.resultType, "complete");
  assert.deepEqual(res.supportedVersions, ["2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]);
  assert.deepEqual(res.capabilities, { tools: { listChanged: false } });
  assert.equal(res._meta[META + "serverInfo"].name, "apiwatch");
  assert.ok(res.ttlMs > 0 && res.cacheScope === "public");
});

test("tools/list (modern) returns seven read-only tools with input and output schemas", async () => {
  const r = await modern("tools/list");
  const res = r.body.result;
  assert.equal(res.resultType, "complete");
  assert.ok(res.ttlMs > 0 && ["public", "private"].includes(res.cacheScope));
  assert.deepEqual(res.tools.map((t) => t.name), ["check_model", "check_identifiers", "list_upcoming", "search_changes", "get_record",
                                                 "list_watched_mcp_servers", "mcp_server_changes"]);
  for (const t of res.tools) {
    assert.equal(t.inputSchema.type, "object");
    assert.equal(t.outputSchema.type, "object");
    assert.equal(t.annotations.readOnlyHint, true);
    assert.equal(t.annotations.openWorldHint, false);
    assert.ok(t.description.length > 80);
    assert.ok(!/[\u2013\u2014]/.test(t.description), "no en or em dashes");
  }
});

test("unknown protocol version gets -32022 with the supported list (HTTP 400)", async () => {
  const [body, o] = modernReq("tools/list");
  body.params._meta[META + "protocolVersion"] = "2099-01-01";
  o.headers["MCP-Protocol-Version"] = "2099-01-01";
  const r = await send(body, o);
  assert.equal(r.status, 400);
  assert.equal(r.body.error.code, -32022);
  assert.equal(r.body.error.data.requested, "2099-01-01");
  assert.ok(r.body.error.data.supported.includes(MODERN));
  // an unknown version in the header alone (no _meta) is refused the same way
  const h = await legacy("tools/list", {}, "1900-01-01");
  assert.equal(h.status, 400);
  assert.equal(h.body.error.code, -32022);
});

test("header mismatches are -32020 (HTTP 400)", async () => {
  let r = await modern("tools/list", {}, { "MCP-Protocol-Version": "2025-11-25" });
  assert.equal(r.status, 400);
  assert.equal(r.body.error.code, -32020);
  r = await modern("tools/list", {}, { "Mcp-Method": "tools/call" });
  assert.equal(r.body.error.code, -32020);
  const [body, o] = modernReq("tools/list");
  delete o.headers["Mcp-Method"];
  r = await send(body, o);
  assert.equal(r.body.error.code, -32020);
  r = await modern("tools/call", { name: "check_model", arguments: { model: "alpha-1" } }, { "Mcp-Name": "get_record" });
  assert.equal(r.body.error.code, -32020);
  // a modern header with no _meta version
  r = await legacy("tools/list", {}, MODERN);
  assert.equal(r.status, 400);
  assert.equal(r.body.error.code, -32020);
});

test("Mcp-Name may be base64 encoded", async () => {
  const r = await modern("tools/call", { name: "check_model", arguments: { model: "alpha-1" } },
                         { "Mcp-Name": `=?base64?${btoa("check_model")}?=` });
  assert.equal(r.status, 200);
  assert.equal(r.body.result.isError, false);
});

test("unknown modern method is 404 with -32601", async () => {
  const r = await modern("resources/list");
  assert.equal(r.status, 404);
  assert.equal(r.body.error.code, -32601);
});

test("modern requests ignore Mcp-Session-Id and never mint one", async () => {
  const r = await modern("tools/list", {}, { "Mcp-Session-Id": "abc" });
  assert.equal(r.status, 200);
  assert.equal(r.headers.get("Mcp-Session-Id"), null);
});

// ---------- protocol: legacy ----------

test("initialize answers with the newest version both sides support, no session", async () => {
  for (const [asked, got] of [["2025-11-25", "2025-11-25"], ["2025-06-18", "2025-06-18"], ["2025-03-26", "2025-03-26"],
                              ["2024-11-05", "2024-11-05"], ["2099-01-01", "2025-11-25"], [MODERN, "2025-11-25"]]) {
    const r = await legacy("initialize", { protocolVersion: asked, capabilities: {}, clientInfo: { name: "t", version: "1" } }, null);
    assert.equal(r.status, 200);
    assert.equal(r.body.result.protocolVersion, got);
    assert.equal(r.body.result.serverInfo.name, "apiwatch");
    assert.ok(r.body.result.capabilities.tools);
    assert.equal(r.headers.get("Mcp-Session-Id"), null);
  }
});

test("notifications get 202 with no body", async () => {
  const r = await send({ jsonrpc: "2.0", method: "notifications/initialized" }, { headers: { "MCP-Protocol-Version": "2025-11-25" } });
  assert.equal(r.status, 202);
  assert.equal(r.raw, "");
});

test("legacy tools/list and tools/call work without a session or a prior initialize", async () => {
  let r = await legacy("tools/list");
  assert.equal(r.status, 200);
  assert.equal(r.body.result.tools.length, 7);
  assert.equal(r.body.result.resultType, undefined);
  r = await legacy("tools/list", {}, null);          // pre-2025-06-18 clients send no version header
  assert.equal(r.body.result.tools.length, 7);
  r = await legacy("tools/call", { name: "check_model", arguments: { model: "alpha-1" } }, "2025-06-18");
  assert.equal(r.body.result.isError, false);
  assert.equal(r.body.result.structuredContent.found, true);
});

test("legacy unknown method is -32601 with HTTP 200 (a 404 means 'session gone' to 2025 clients)", async () => {
  const r = await legacy("resources/list");
  assert.equal(r.status, 200);
  assert.equal(r.body.error.code, -32601);
});

test("ping answers in both eras", async () => {
  assert.deepEqual((await legacy("ping")).body.result, {});
  assert.equal((await modern("ping")).body.result.resultType, "complete");
});

// ---------- protocol: transport errors ----------

test("GET, DELETE and OPTIONS are 405 with Allow: POST", async () => {
  for (const method of ["GET", "DELETE", "OPTIONS"]) {
    const res = await handle(new Request(URL_, { method }), env(), { now: NOW, cache: {} });
    assert.equal(res.status, 405);
    assert.equal(res.headers.get("Allow"), "POST");
  }
});

test("bad JSON, batches, non-requests and bad params return JSON-RPC errors, never 500", async () => {
  let r = await send("{not json");
  assert.equal(r.status, 400);
  assert.equal(r.body.error.code, -32700);
  assert.equal(r.body.id, null);
  r = await send([{ jsonrpc: "2.0", id: 1, method: "ping" }]);
  assert.equal(r.body.error.code, -32600);
  r = await send({ id: 1, method: "ping" });
  assert.equal(r.body.error.code, -32600);
  r = await send({ jsonrpc: "2.0", id: { x: 1 }, method: "ping" });
  assert.equal(r.body.error.code, -32600);
  r = await send({ jsonrpc: "2.0", id: 1, method: "tools/list", params: [1] });
  assert.equal(r.body.error.code, -32602);
  r = await send("null");
  assert.equal(r.body.error.code, -32600);
  r = await legacy("tools/call", { arguments: {} });
  assert.equal(r.body.error.code, -32602);
  r = await legacy("tools/call", { name: "drop_tables", arguments: {} });
  assert.equal(r.body.error.code, -32602);
  r = await modern("tools/call", { name: "check_model", arguments: [1] });
  assert.equal(r.status, 200);
  assert.equal(r.body.error.code, -32602);
  r = await legacy("tools/list", { cursor: "zzz" });
  assert.equal(r.body.error.code, -32602);
  r = await send("x".repeat(300 * 1024));
  assert.equal(r.status, 413);
  assert.equal(r.body.error.code, -32600);
});

test("responses carry no cookies and safe headers", async () => {
  const r = await modern("tools/list");
  assert.equal(r.headers.get("Set-Cookie"), null);
  assert.equal(r.headers.get("Cache-Control"), "no-store");
  assert.equal(r.headers.get("X-Content-Type-Options"), "nosniff");
});

test("paths other than /mcp: /mcp/x is 404, anything else goes to the static assets", async () => {
  let res = await handle(new Request("https://apiwatch.example/mcp/x", { method: "POST" }), env(), { now: NOW, cache: {} });
  assert.equal(res.status, 404);
  const seen = [];
  const e = { ASSETS: { fetch: async (req) => { seen.push(new URL(req.url).pathname); return new Response("ok"); } } };
  res = await worker.fetch(new Request("https://apiwatch.example/upcoming"), e);
  assert.equal(await res.text(), "ok");
  assert.deepEqual(seen, ["/upcoming"]);
  res = await handle(new Request("https://apiwatch.example/mcp/", { method: "GET" }), env(), { now: NOW, cache: {} });
  assert.equal(res.status, 405);
});

test("the feed is cached for 5 minutes per isolate, and a failed reload keeps the last good copy", async () => {
  const counter = { n: 0 };
  const cache = {};
  const e = env(FIXTURE, counter);
  await call("check_model", { model: "alpha-1" }, { e, opts: { cache, now: NOW } });
  await call("check_model", { model: "alpha-1" }, { e, opts: { cache, now: NOW + 4 * 60 * 1000 } });
  assert.equal(counter.n, 1);
  await call("check_model", { model: "alpha-1" }, { e, opts: { cache, now: NOW + 6 * 60 * 1000 } });
  assert.equal(counter.n, 2);
  const down = { ASSETS: { fetch: async () => new Response("no", { status: 503 }) } };
  const res = await call("check_model", { model: "alpha-1" }, { e: down, opts: { cache, now: NOW + 20 * 60 * 1000 } });
  assert.equal(res.structuredContent.found, true);
  const cold = await call("check_model", { model: "alpha-1" }, { e: down, opts: { cache: {}, now: NOW } });
  assert.equal(cold.isError, true);
  assert.match(cold.content[0].text, /could not read its feed/);
});

// ---------- tools ----------

test("check_model: future breaking, past breaking, deprecation, silent today, no record", async () => {
  let r = await call("check_model", { model: "alpha-1" });
  assert.equal(r.isError, false);
  assert.equal(r.structuredContent.answer, "alpha-1 stops working on Nov 1, 2026.");
  assert.equal(r.structuredContent.matches[0].date_passed, false);
  assert.equal(r.structuredContent.matches[0].record_url, "https://apiwatch.example/records/v-2026-11-01-alpha");
  assert.equal(r.structuredContent.matches[0].source_url, "https://vendor.example/deprecations");
  assert.equal(r.structuredContent.matches[0].fix_hint, "Switch to new-model.");
  r = await call("check_model", { model: "beta-2" });
  assert.equal(r.structuredContent.answer, "beta-2 stopped working on Jan 1, 2025.");
  assert.equal(r.structuredContent.matches[0].date_passed, true);
  r = await call("check_model", { model: "gamma-3" });
  assert.equal(r.structuredContent.answer, "gamma-3 is deprecated: it still works, and removal is announced. Key date: Mar 1, 2027.");
  r = await call("check_model", { model: "delta-4" });   // the key date is today: the site counts it as passed
  assert.equal(r.structuredContent.answer, "delta-4 changed behavior on Oct 21, 2026.");
  r = await call("check_model", { model: "omega-9" });
  assert.equal(r.structuredContent.found, false);
  assert.deepEqual(r.structuredContent.matches, []);
  assert.match(r.structuredContent.answer, /^apiwatch has no record for omega-9\. That does not mean it is safe/);
  assert.doesNotMatch(r.content[0].text.replace("does not mean it is safe", ""), /\bsafe\b/);
});

test("check_model: snapshots match, siblings and excluded ids do not", async () => {
  assert.equal((await call("check_model", { model: "alpha-1-20250805" })).structuredContent.found, true);
  assert.equal((await call("check_model", { model: "ALPHA-1" })).structuredContent.found, true);
  assert.equal((await call("check_model", { model: "alpha-1-mini" })).structuredContent.found, false);
  assert.equal((await call("check_model", { model: "alpha-1.5" })).structuredContent.found, false);
  assert.equal((await call("check_model", { model: "alpha-10" })).structuredContent.found, false);
  assert.equal((await call("check_model", { model: "alpha-1-2025-01-01" })).structuredContent.found, false);  // excluded
  assert.equal((await call("check_model", { model: "openai/alpha-1" })).structuredContent.found, true);
});

test("deprecation wording never ties a verb to the key date, before or after it", () => {
  const r = { severity: "deprecation", kind: "model", effective: "2026-01-01", summary: "s.", scope: "" };
  for (const today of ["2025-06-01", "2026-06-01"]) {
    const a = answerFor("m", [{ r, sig: "m" }], today);
    assert.match(a, /Key date: Jan 1, 2026\.$/);
    assert.doesNotMatch(a, /(stops|stopped|shuts|retires|removed on|removal on)/);
  }
});

test("scoped records always carry their scope, in the answer and in the record", async () => {
  const r = await call("check_identifiers", { identifiers: ["widgetize", "/v1/widgets", "alpha-1", "nothing-here"] });
  const sc = r.structuredContent;
  const w = sc.results.find((x) => x.identifier === "widgetize");
  assert.match(w.answer, /Applies only in repos that depend on "stripe 16\.x" \(pypi\)\.$/);
  assert.deepEqual(w.record_ids, ["v-2026-12-01-sdk"]);
  const rec = sc.records.find((x) => x.id === "v-2026-12-01-sdk");
  assert.match(rec.scope, /^Applies only in repos that depend on/);
  assert.match(r.content[0].text, /Applies only in repos that depend on "stripe 16\.x"/);
  // non-model records name the record, they don't claim what happens to the identifier
  const api = sc.results.find((x) => x.identifier === "/v1/widgets");
  assert.match(api.answer, /is named in a breaking record with key date Nov 15, 2026/);
  assert.doesNotMatch(api.answer, /stops working/);
  assert.equal(sc.results.find((x) => x.identifier === "nothing-here").found, false);
  assert.equal(sc.results.find((x) => x.identifier === "alpha-1").answer, "alpha-1 stops working on Nov 1, 2026.");
  assert.ok(sc.record_urls.length === sc.records.length && sc.next_step.startsWith("Next step:"));
});

test("an unscoped record leads the answer over a scoped one", () => {
  const scoped = { severity: "breaking", kind: "model", effective: "2026-11-01", summary: "a", scope: "Applies only in files matching \"*.py\"." };
  const plain = { severity: "breaking", kind: "model", effective: "2027-01-01", summary: "b", scope: "" };
  const a = answerFor("m", [{ r: scoped, sig: "m" }, { r: plain, sig: "m" }], "2026-10-21");
  assert.equal(a, "m stops working on Jan 1, 2027. In some code (applies only in files matching \"*.py\") it stops working " +
                  "on Nov 1, 2026. 2 records match; each is listed with its date and scope.");
  // once the scoped date has passed it is still in the answer line, in the past tense
  assert.equal(answerFor("m", [{ r: scoped, sig: "m" }, { r: plain, sig: "m" }], "2026-11-02"),
               'm stops working on Jan 1, 2027. In some code (applies only in files matching "*.py") it stopped working ' +
               "on Nov 1, 2026. 2 records match; each is listed with its date and scope.");
  // a scoped record already in effect (or in effect today) while the lead is ahead
  const early = { ...scoped, effective: "2026-09-01" };
  assert.equal(answerFor("m", [{ r: early, sig: "m" }, { r: plain, sig: "m" }], "2026-10-21"),
               'm stops working on Jan 1, 2027. In some code (applies only in files matching "*.py") it stopped working ' +
               "on Sep 1, 2026. 2 records match; each is listed with its date and scope.");
  assert.match(answerFor("m", [{ r: { ...scoped, effective: "2026-10-21" }, sig: "m" }, { r: plain, sig: "m" }], "2026-10-21"),
               /it stopped working on Oct 21, 2026\./);
  // a scoped model lead carries its scope in the sentence
  assert.equal(answerFor("m", [{ r: scoped, sig: "m" }], "2026-10-21"), 'm stops working on Nov 1, 2026 (applies only in files matching "*.py").');
});

test("list_upcoming: from today forward, soonest first, within the window, by vendor", async () => {
  let r = await call("list_upcoming", {});
  assert.deepEqual(r.structuredContent.records.map((x) => x.id), ["v-2026-10-21-delta", "v-2026-11-01-alpha", "v-2026-11-15-api", "v-2026-12-01-sdk"]);
  assert.equal(r.structuredContent.from, "2026-10-21");
  assert.equal(r.structuredContent.to, "2027-01-19");
  r = await call("list_upcoming", { days: 365 });
  assert.equal(r.structuredContent.records.at(-1).id, "v-2027-03-01-gamma");
  r = await call("list_upcoming", { days: 365, vendor: "google" });
  assert.deepEqual(r.structuredContent.records.map((x) => x.id), ["v-2026-10-21-delta", "v-2027-03-01-gamma"]);
  r = await call("list_upcoming", { vendor: "Nobody" });
  assert.equal(r.structuredContent.total, 0);
  assert.match(r.content[0].text, /No vendor named Nobody in the feed\. Vendors: Anthropic, Google, OpenAI, Stripe\./);
});

test("search_changes: summary, vendor and signatures, case-insensitive, limit", async () => {
  let r = await call("search_changes", { query: "WIDGET" });
  assert.deepEqual(r.structuredContent.records.map((x) => x.id).sort(), ["v-2026-11-15-api", "v-2026-12-01-sdk"]);
  r = await call("search_changes", { query: "anthropic" });
  assert.deepEqual(r.structuredContent.records.map((x) => x.id), ["v-2025-01-01-beta"]);
  r = await call("search_changes", { query: "gamma-3" });
  assert.equal(r.structuredContent.total, 1);
  r = await call("search_changes", { query: "e", limit: 2 });
  assert.equal(r.structuredContent.records.length, 2);
  assert.equal(r.structuredContent.truncated, true);
  r = await call("search_changes", { query: "widget", vendor: "OpenAI" });
  assert.equal(r.structuredContent.total, 0);
});

test("get_record: full record, or found false", async () => {
  let r = await call("get_record", { id: "v-2026-12-01-sdk" });
  const rec = r.structuredContent.record;
  assert.equal(r.structuredContent.found, true);
  assert.deepEqual(rec.repo_context, ["dep:pypi:stripe@16."]);
  assert.deepEqual(rec.signatures, ["widgetize"]);
  assert.match(r.content[0].text, /Applies only in repos/);
  r = await call("get_record", { id: "nope" });
  assert.equal(r.structuredContent.found, false);
  assert.deepEqual(r.structuredContent.record_urls, []);
});

test("every tool result ends with the record URLs and carries next_step", async () => {
  for (const [name, args] of [["check_model", { model: "alpha-1" }], ["check_identifiers", { identifiers: ["beta-2"] }],
                              ["list_upcoming", {}], ["search_changes", { query: "alpha" }], ["get_record", { id: "v-2025-01-01-beta" }]]) {
    const r = await call(name, args);
    const sc = r.structuredContent;
    assert.ok(sc.record_urls.length >= 1, name);
    assert.match(sc.next_step, /^Next step: .*GitHub Action \(https:\/\/github\.com\/marketplace\/actions\/apiwatch-scan\).*Pro.*\(https:\/\/apiwatch\.example\/pro\/\)\.$/);
    const text = r.content[0].text;
    assert.ok(text.endsWith(sc.record_urls.at(-1)), name);
    assert.ok(text.includes(sc.next_step), name);
    assert.ok(!/[\u2013\u2014]/.test(text), "no en or em dashes");
    assert.ok(conforms(sc, TOOLS.find((t) => t.name === name).outputSchema), name);
  }
});

test("input validation: tool errors the model can read (isError), not crashes", async () => {
  const bad = [
    ["check_model", {}], ["check_model", { model: "" }], ["check_model", { model: "   " }], ["check_model", { model: 5 }],
    ["check_model", { model: "x".repeat(201) }], ["check_model", { model: "a", extra: 1 }],
    ["check_model", { model: "a", constructor: 1 }],
    ["check_identifiers", { identifiers: [] }], ["check_identifiers", { identifiers: Array(101).fill("a") }],
    ["check_identifiers", { identifiers: ["ok", 3] }], ["check_identifiers", { identifiers: "a" }],
    ["check_identifiers", { identifiers: ["y".repeat(201)] }],
    ["list_upcoming", { days: 0 }], ["list_upcoming", { days: 366 }], ["list_upcoming", { days: 1.5 }], ["list_upcoming", { days: "9" }],
    ["search_changes", { query: "" }], ["search_changes", { query: "a", limit: 51 }], ["get_record", {}],
  ];
  for (const [name, args] of bad) {
    const r = await call(name, args);
    assert.equal(r.isError, true, `${name} ${JSON.stringify(args)}`);
    assert.match(r.content[0].text, /^Invalid arguments for /);
  }
  const ok = await call("check_identifiers", { identifiers: Array(100).fill("alpha-1") });
  assert.equal(ok.isError, false);
  assert.equal(ok.structuredContent.results.length, 1);   // duplicates collapse
});

test("matchRecord follows the scanner's token rules", () => {
  const r = (sigs, kind = "model", exclude = []) => ({ signatures: sigs, kind, exclude });
  assert.equal(matchRecord("gpt-4o", r(["gpt-4"])), "");
  assert.equal(matchRecord("gpt-4-0613", r(["gpt-4"])), "gpt-4");
  assert.equal(matchRecord("gpt-4-turbo", r(["gpt-4"])), "");
  assert.equal(matchRecord("gpt-4-1106-vision-preview", r(["gpt-4"])), "");
  assert.equal(matchRecord("gpt-4.1", r(["gpt-4"])), "");
  assert.equal(matchRecord("chatgpt-4", r(["gpt-4"])), "");
  assert.equal(matchRecord("claude-3-haiku-latest", r(["claude-3-haiku"])), "claude-3-haiku");
  assert.equal(matchRecord("gpt-4o-2024-11-20", r(["gpt-4o"], "model", ["gpt-4o-2024-11-20"])), "");
  assert.equal(matchRecord("gpt-4o-2024-05-13", r(["gpt-4o"], "model", ["gpt-4o-2024-11-20"])), "gpt-4o");
  assert.equal(matchRecord("gpt-4o-2024-05-13", r(["gpt-4o-2024-05-13"], "model", ["gpt-4o"])), "gpt-4o-2024-05-13");
  assert.equal(matchRecord("mcp__notion__search", r(["search"], "mcp")), "search");
  assert.equal(matchRecord("/v1/prompts/abc", r(["/v1/prompts"], "rest")), "/v1/prompts");
  assert.equal(matchRecord("fx_rate_limit", r(["fx_rate"], "sdk")), "");
  assert.equal(matchRecord("pmpt_123", r(["pmpt_"], "rest")), "pmpt_");
});

test("partner platform ids, record-named variants and case rules match the scanner", () => {
  const anth = { signatures: ["claude-sonnet-4-5"], kind: "model", vendor: "Anthropic" };
  assert.equal(matchRecord("claude-sonnet-4-5-20250929", anth), "claude-sonnet-4-5");
  assert.equal(matchRecord("claude-sonnet-4-5@20250929", anth), "");                       // Vertex AI
  assert.equal(matchRecord("claude-sonnet-4-5-20250929-v1:0", anth), "");                  // Bedrock
  assert.equal(matchRecord("anthropic.claude-sonnet-4-5-20250929-v1:0", anth), "");
  assert.equal(matchRecord("claude-sonnet-4-5@20250929", { ...anth, vendor: "Google" }), "claude-sonnet-4-5");
  const named = { signatures: ["gpt-4", "gpt-5.1-codex"], kind: "model", named: ["-completions", "gpt-5.1-codex-max"] };
  assert.equal(matchRecord("gpt-4-0613-completions", named), "gpt-4");
  assert.equal(matchRecord("gpt-4-0613-preview", named), "");
  assert.equal(matchRecord("gpt-5.1-codex-max", named), "gpt-5.1-codex");
  assert.equal(matchRecord("gpt-5.1-codex-mini", named), "");
  const sdk = { signatures: ["paymentMethodTypes"], kind: "sdk" };
  assert.equal(matchRecord("PaymentMethodTypes", sdk), "");                                // exact case, as the scanner
  assert.equal(matchRecord("paymentMethodTypes", sdk), "paymentMethodTypes");
  assert.equal(matchRecord("GPT-4-0613", { signatures: ["gpt-4"], kind: "model" }), "gpt-4");
});

test("a body over the cap is refused even without Content-Length (chunked)", async () => {
  const chunk = new TextEncoder().encode("x".repeat(64 * 1024));
  let sent = 0;
  const body = new ReadableStream({ pull(c) { if (sent++ < 10) c.enqueue(chunk); else c.close(); } });
  const req = new Request(URL_, { method: "POST", body, duplex: "half", headers: { "Content-Type": "application/json" } });
  assert.equal(req.headers.get("Content-Length"), null);
  const res = await handle(req, env(), { now: NOW, cache: {} });
  assert.equal(res.status, 413);
  assert.ok(sent < 10, "stopped reading at the cap");
});

test("a key date of today is labeled today", async () => {
  const r = await call("list_upcoming", { days: 1 });
  assert.match(r.content[0].text, /Oct 21, 2026 \(today\): delta-4/);
});

// ---------- the real feed ----------

const REAL = process.env.APIWATCH_FEED || (existsSync("out/site/feed.json") ? "out/site/feed.json" : "");

test("real feed: every tool answers, ids from the feed match their own records", { skip: !REAL && "no built feed" }, async () => {
  const feed = JSON.parse(readFileSync(REAL, "utf8"));
  const e = env(feed);
  const items = feed.items.map((i) => i._apiwatch);
  assert.ok(items.length > 50);
  const now = Date.now();
  const today = new Date(now).toISOString().slice(0, 10);
  const opts = { cache: {}, now };
  // every unscoped model signature finds its own record unless that record excludes it
  for (const a of items.filter((x) => x.kind === "model" && !x.scope)) {
    for (const sig of a.signatures) {
      const r = await call("check_model", { model: sig }, { e, opts });
      assert.equal(r.isError, false);
      const ids = r.structuredContent.matches.map((m) => m.id);
      if (!a.exclude.some((x) => x.toLowerCase() === sig.toLowerCase())) assert.ok(ids.includes(a.id), `${sig} -> ${a.id}`);
      assert.ok(conforms(r.structuredContent, TOOLS[0].outputSchema));
    }
  }
  // a future breaking model, a past one and a deprecation, worded as on the site
  const pick = (f) => items.filter(f).sort((x, y) => (x.effective < y.effective ? -1 : 1))[0];
  const fut = pick((x) => x.kind === "model" && x.severity === "breaking" && x.effective > today && !x.scope);
  if (fut) {
    const r = await call("check_model", { model: fut.signatures[0] }, { e, opts });
    assert.match(r.structuredContent.answer, /stops working on /);
  }
  const past = pick((x) => x.kind === "model" && x.severity === "breaking" && x.effective <= today && !x.scope);
  const rp = await call("check_model", { model: past.signatures[0] }, { e, opts });
  assert.ok(rp.structuredContent.matches.some((m) => m.id === past.id));
  // scoped records keep their scope everywhere
  const scoped = items.find((x) => x.scope);
  const rs = await call("check_identifiers", { identifiers: [scoped.signatures[0]] }, { e, opts });
  const rec = rs.structuredContent.records.find((x) => x.id === scoped.id);
  assert.equal(rec.scope, scoped.scope);
  const res = rs.structuredContent.results[0];
  const leadIsScoped = rs.structuredContent.records.filter((x) => res.record_ids.includes(x.id)).every((x) => x.scope);
  if (leadIsScoped) assert.match(res.answer, /applies only in/i);
  for (const [name, args] of [["list_upcoming", { days: 365 }], ["search_changes", { query: "gpt" }],
                              ["get_record", { id: items[0].id }], ["check_model", { model: "no-such-model-zz9" }]]) {
    const r = await call(name, args, { e, opts });
    assert.equal(r.isError, false, name);
    assert.ok(conforms(r.structuredContent, TOOLS.find((t) => t.name === name).outputSchema), name);
    assert.ok(r.structuredContent.records === undefined || r.structuredContent.records.length <= 50);
  }
});

// a minimal JSON Schema check for the keywords the output schemas use
function conforms(v, s) {
  if (!s || !s.type) return true;
  if (s.type === "object") {
    if (typeof v !== "object" || v === null || Array.isArray(v)) return false;
    for (const k of s.required || []) if (!(k in v)) return false;
    return Object.entries(s.properties || {}).every(([k, sub]) => !(k in v) || conforms(v[k], sub));
  }
  if (s.type === "array") return Array.isArray(v) && v.every((x) => conforms(x, s.items));
  if (s.type === "string") return typeof v === "string" && (!s.enum || s.enum.includes(v));
  if (s.type === "boolean") return typeof v === "boolean";
  if (s.type === "integer") return Number.isInteger(v);
  return true;
}
