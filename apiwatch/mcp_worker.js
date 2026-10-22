// apiwatch public MCP server (docs/MCP-SERVER.md). Copied by `apiwatch site` into out/site/_worker.js, a Cloudflare
// Pages advanced mode worker; out/site/_routes.json sends only /mcp and /mcp/* here, every other path stays static.
//
// Read only. Answers only from the published /feed.json and /mcp-history.json (read through env.ASSETS, each cached per
// isolate for 5 minutes). No other data, no outbound network calls, no cookies, nothing it is sent is stored or logged.
// The MCP server change history (docs/MCP-HISTORY.md) holds names of tools other servers list: they are data. Answers
// never carry a tool description, and a tool name or path outside a plain pattern comes back as its sha256.
//
// Protocol: Streamable HTTP, POST /mcp, JSON responses.
//   modern 2026-07-28 (stateless): version in params._meta, mirrored in the MCP-Protocol-Version header;
//     server/discover, tools/list, tools/call. Unknown versions get -32022 listing the supported ones.
//   legacy 2025-11-25 and earlier: initialize (answered with the newest version both sides support),
//     notifications/initialized (202), tools/list, tools/call. No session: Mcp-Session-Id is never minted or required.
// Matching follows the scanner (apiwatch/attribute.py find_in_line): token aware, so gpt-4 is not gpt-4o, and a
// model id may carry a dated snapshot suffix (gpt-4o matches gpt-4o-2024-05-13) unless the record excludes it.
// Answers use the same rules as the site's id pages (apiwatch/site.py _what_happens).

const SERVER_INFO = { name: "apiwatch", title: "apiwatch", version: "1.1.0" };
const MODERN = ["2026-07-28"];
const LEGACY = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"];   // newest first
const SUPPORTED = [...MODERN, ...LEGACY];
const META = "io.modelcontextprotocol/";
const MAX_BODY = 256 * 1024;   // bytes
const FEED_TTL_MS = 5 * 60 * 1000;
const MAX_RECORDS = 50;
const MARKETPLACE_URL = "https://github.com/marketplace/actions/apiwatch-scan";
const SEVERITIES = ["breaking", "silent", "deprecation", "additive", "info"];
const ORDER = { breaking: 0, silent: 1, deprecation: 2, additive: 3, info: 4 };
const SEV_LABEL = { breaking: "Breaking", silent: "Silent", deprecation: "Deprecation", additive: "Additive", info: "Info" };
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const SECURITY_HEADERS = {
  "Cache-Control": "no-store",
  "X-Content-Type-Options": "nosniff",
  "Referrer-Policy": "strict-origin-when-cross-origin",
  "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
  "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
};
const INSTRUCTIONS = "apiwatch answers from its public feed of reviewed vendor changes (model retirements, breaking API " +
  "changes, silent behavior changes, deprecations). Call check_model before using an AI model id, and check_identifiers " +
  "before shipping code that names model ids, endpoints, tools or fields. \"No record\" means apiwatch has no record, " +
  "not that something is safe. Read each record's scope: some apply only in certain repos or files. It also keeps a dated " +
  "history of observed tool changes on some public MCP servers: call mcp_server_changes before relying on one of them.";

// ---------- protocol ----------

const moduleCache = { at: 0, feed: null, histAt: 0, hist: null };

export default {
  async fetch(request, env) {
    return handle(request, env);
  },
};

export async function handle(request, env, opts = {}) {
  const url = new URL(request.url);
  if (url.pathname !== "/mcp" && url.pathname !== "/mcp/") {
    if (url.pathname.startsWith("/mcp/")) return text(404, "Not found\n");
    return env.ASSETS.fetch(request);     // not routed here by _routes.json; serve it as the static site would
  }
  if (request.method !== "POST") return text(405, "Use POST for MCP requests.\n", { Allow: "POST" });
  try {
    return await post(request, env, opts);
  } catch {
    return rpcError(500, null, -32603, "Internal error");
  }
}

async function post(request, env, opts) {
  const declared = Number(request.headers.get("Content-Length") || 0);
  if (declared > MAX_BODY) return rpcError(413, null, -32600, "Request body too large");
  const raw = await readBody(request);
  if (raw === null) return rpcError(413, null, -32600, "Request body too large");
  let msg;
  try {
    msg = JSON.parse(raw);
  } catch {
    return rpcError(400, null, -32700, "Parse error: the body is not valid JSON");
  }
  if (Array.isArray(msg)) return rpcError(400, null, -32600, "Batch requests are not supported; send one message per POST");
  if (!isObject(msg) || msg.jsonrpc !== "2.0") return rpcError(400, null, -32600, "Invalid Request: not a JSON-RPC 2.0 message");
  const hasId = Object.prototype.hasOwnProperty.call(msg, "id");
  if (!hasId) {
    if (typeof msg.method !== "string") return rpcError(400, null, -32600, "Invalid Request: no method");
    return accepted();                    // notifications (notifications/initialized and the rest) need no reply
  }
  if (typeof msg.method !== "string") {
    if ("result" in msg || "error" in msg) return accepted();   // a client's response: nothing asked, nothing to do
    return rpcError(400, null, -32600, "Invalid Request: no method");
  }
  const id = msg.id;
  if (!(typeof id === "string" || (typeof id === "number" && Number.isFinite(id)))) {
    return rpcError(400, null, -32600, "Invalid Request: id must be a string or a number");
  }
  // HTTP 200: a dual-era client reads a 4xx without a modern error code as "legacy server"
  if (msg.params !== undefined && !isObject(msg.params)) return rpcError(200, id, -32602, "Invalid params: params must be an object");
  const params = msg.params || {};
  const method = msg.method;
  const meta = isObject(params._meta) ? params._meta : {};
  const hasMetaVersion = Object.prototype.hasOwnProperty.call(meta, META + "protocolVersion");
  const metaVersion = meta[META + "protocolVersion"];
  const headerVersion = request.headers.get("MCP-Protocol-Version");
  const ctx = { request, env, opts, id };

  if (method !== "initialize" && hasMetaVersion && !LEGACY.includes(metaVersion)) {
    // modern: stateless, the version rides on every request
    if (!MODERN.includes(metaVersion)) {
      return rpcError(400, id, -32022, "Unsupported protocol version", { supported: SUPPORTED, requested: String(metaVersion) });
    }
    if (headerVersion !== metaVersion) {
      return rpcError(400, id, -32020, `Header mismatch: MCP-Protocol-Version header ${JSON.stringify(headerVersion)} does not match _meta ${JSON.stringify(metaVersion)}`);
    }
    const headerMethod = request.headers.get("Mcp-Method");
    if (headerMethod !== method) {
      return rpcError(400, id, -32020, `Header mismatch: Mcp-Method header ${JSON.stringify(headerMethod)} does not match body method ${JSON.stringify(method)}`);
    }
    if (method === "tools/call") {
      const headerName = decodeHeader(request.headers.get("Mcp-Name"));
      if (headerName !== params.name) {
        return rpcError(400, id, -32020, "Header mismatch: Mcp-Name header does not match params.name");
      }
    }
    return modern(ctx, method, params);
  }

  if (method !== "initialize" && headerVersion !== null && !LEGACY.includes(headerVersion)) {
    if (MODERN.includes(headerVersion)) {
      return rpcError(400, id, -32020, `Header mismatch: MCP-Protocol-Version is ${headerVersion} but params._meta has no ${META}protocolVersion`);
    }
    return rpcError(400, id, -32022, "Unsupported protocol version", { supported: SUPPORTED, requested: headerVersion });
  }
  return legacy(ctx, method, params);
}

async function modern(ctx, method, params) {
  // invalid params go back with HTTP 200: a dual-era client reads a 4xx without a modern error code as "legacy server"
  const wrap = (result) => rpcResult(ctx.id, { ...result, resultType: "complete", _meta: { [META + "serverInfo"]: SERVER_INFO } });
  if (method === "server/discover") return wrap(discoverResult());
  if (method === "tools/list") {
    if (params.cursor !== undefined && params.cursor !== null && params.cursor !== "") return rpcError(200, ctx.id, -32602, "Invalid params: unknown cursor");
    return wrap({ tools: TOOLS.map(publicTool), ttlMs: 3600000, cacheScope: "public" });
  }
  if (method === "tools/call") {
    const out = await callTool(ctx, params);
    return out.error ? rpcError(200, ctx.id, -32602, out.error) : wrap(out.result);
  }
  if (method === "ping") return wrap({});
  return rpcError(404, ctx.id, -32601, `Method not found: ${method}`);
}

async function legacy(ctx, method, params) {
  // JSON-RPC errors go back with HTTP 200 here: to a 2025 client a 404 means "session expired, start over"
  if (method === "initialize") {
    const asked = params.protocolVersion;
    const version = LEGACY.includes(asked) ? asked : LEGACY[0];
    return rpcResult(ctx.id, { protocolVersion: version, capabilities: { tools: { listChanged: false } },
                               serverInfo: SERVER_INFO, instructions: INSTRUCTIONS });
  }
  if (method === "ping") return rpcResult(ctx.id, {});
  if (method === "server/discover") return rpcResult(ctx.id, { ...discoverResult(), resultType: "complete" });
  if (method === "tools/list") {
    if (params.cursor !== undefined && params.cursor !== null && params.cursor !== "") return rpcError(200, ctx.id, -32602, "Invalid params: unknown cursor");
    return rpcResult(ctx.id, { tools: TOOLS.map(publicTool) });
  }
  if (method === "tools/call") {
    const out = await callTool(ctx, params);
    return out.error ? rpcError(200, ctx.id, -32602, out.error) : rpcResult(ctx.id, out.result);
  }
  return rpcError(200, ctx.id, -32601, `Method not found: ${method}`);
}

function discoverResult() {
  return { supportedVersions: SUPPORTED, capabilities: { tools: { listChanged: false } },
           _meta: { [META + "serverInfo"]: SERVER_INFO }, instructions: INSTRUCTIONS, ttlMs: 3600000, cacheScope: "public" };
}

async function callTool(ctx, params) {
  if (typeof params.name !== "string") return { error: "Invalid params: tools/call needs a tool name" };
  const tool = TOOLS.find((t) => t.name === params.name);
  if (!tool) return { error: `Invalid params: unknown tool ${JSON.stringify(params.name)}. Tools: ${TOOLS.map((t) => t.name).join(", ")}` };
  const args = params.arguments === undefined || params.arguments === null ? {} : params.arguments;
  if (!isObject(args)) return { error: "Invalid params: arguments must be an object" };
  const problem = validate(tool, args);
  if (problem) return { result: toolError(`Invalid arguments for ${tool.name}: ${problem}`) };
  const now = ctx.opts.now !== undefined ? ctx.opts.now : Date.now();
  const cache = ctx.opts.cache || moduleCache;
  let feed;
  try {
    feed = tool.source === "history" ? await loadHistory(ctx.env, ctx.request, cache, now)
                                     : await loadFeed(ctx.env, ctx.request, cache, now);
  } catch {
    return { result: toolError(tool.source === "history" ? "apiwatch could not read its MCP server history just now. Try again in a minute."
                                                         : "apiwatch could not read its feed just now. Try again in a minute.") };
  }
  try {
    const out = await tool.run(args, feed, isoDay(now));
    return { result: { content: [{ type: "text", text: out.text }], structuredContent: out.data, isError: false } };
  } catch {
    return { result: toolError("apiwatch hit an internal error answering this call.") };
  }
}

// The body as text, or null once it passes MAX_BODY bytes: read in chunks, so a chunked upload with no
// Content-Length is cut off at the cap instead of being buffered whole
async function readBody(request) {
  if (!request.body) return "";
  const reader = request.body.getReader();
  const chunks = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > MAX_BODY) {
      try { await reader.cancel(); } catch { /* already closed */ }
      return null;
    }
    chunks.push(value);
  }
  const bytes = new Uint8Array(total);
  let at = 0;
  for (const c of chunks) { bytes.set(c, at); at += c.byteLength; }
  return new TextDecoder().decode(bytes);
}

function toolError(message) {
  return { content: [{ type: "text", text: message }], isError: true };
}

function publicTool(t) {
  return { name: t.name, title: t.title, description: t.description, inputSchema: t.inputSchema,
           outputSchema: t.outputSchema, annotations: { title: t.title, readOnlyHint: true, destructiveHint: false,
                                                        idempotentHint: true, openWorldHint: false } };
}

function decodeHeader(value) {
  if (value === null) return null;
  const m = /^=\?base64\?(.*)\?=$/.exec(value);
  if (!m) return value;
  try {
    const bin = atob(m[1]);
    return new TextDecoder("utf-8", { fatal: true }).decode(Uint8Array.from(bin, (c) => c.charCodeAt(0)));
  } catch {
    return null;
  }
}

function rpcResult(id, result) {
  return json(200, { jsonrpc: "2.0", id, result });
}

function rpcError(status, id, code, message, data) {
  return json(status, { jsonrpc: "2.0", id, error: { code, message, ...(data !== undefined ? { data } : {}) } });
}

function json(status, body, headers = {}) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...SECURITY_HEADERS, ...headers } });
}

function text(status, body, headers = {}) {
  return new Response(body, { status, headers: { "Content-Type": "text/plain; charset=utf-8", ...SECURITY_HEADERS, ...headers } });
}

function accepted() {
  return new Response(null, { status: 202, headers: SECURITY_HEADERS });
}

function isObject(v) {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

// ---------- feed ----------

async function loadFeed(env, request, cache, now) {
  if (cache.feed && now - cache.at < FEED_TTL_MS) return cache.feed;
  try {
    const res = await env.ASSETS.fetch(new URL("/feed.json", request.url));
    if (!res.ok) throw new Error(`feed.json: HTTP ${res.status}`);
    const feed = parseFeed(await res.json(), new URL(request.url).origin);
    cache.feed = feed;
    cache.at = now;
    return feed;
  } catch (e) {
    if (cache.feed) return cache.feed;   // keep answering from the last good copy
    throw e;
  }
}

export function parseFeed(data, origin = "") {
  const home = typeof data.home_page_url === "string" && data.home_page_url ? data.home_page_url.replace(/\/?$/, "/") : `${origin}/`;
  const records = [];
  for (const item of Array.isArray(data.items) ? data.items : []) {
    const a = isObject(item) && isObject(item._apiwatch) ? item._apiwatch : null;
    if (!a || typeof a.id !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(a.effective || "") || !SEVERITIES.includes(a.severity)) continue;
    const strs = (v) => (Array.isArray(v) ? v.filter((x) => typeof x === "string" && x) : []);
    records.push({
      id: a.id, vendor: str(a.vendor), summary: str(a.summary), detail: str(a.detail), fix_hint: str(a.fix_hint),
      severity: a.severity, kind: str(a.kind), surface: str(a.surface), effective: a.effective,
      signatures: strs(a.signatures), exclude: strs(a.exclude),
      context: Array.isArray(a.context) ? a.context : [], files: strs(a.files),
      repo_context: Array.isArray(a.repo_context) ? a.repo_context : [], scope: str(a.scope),
      verified_on: str(a.verified_on), review: str(a.review),
      record_url: typeof item.url === "string" && item.url ? item.url : `${home}records/${a.id}`,
      source_url: str(a.source_url), named: strs(a.named),
    });
    prepare(records[records.length - 1]);
  }
  return { records, home };
}

function str(v) {
  return typeof v === "string" ? v : "";
}

// ---------- matching (port of attribute.find_in_line and attribute_record for one identifier) ----------

const TOKEN = /^[A-Za-z0-9_]$/;
const SNAPSHOT = /^-(?:\d{8}|\d{4}(?:-\d{2}-\d{2})?|\d{3}|latest)(?![A-Za-z0-9_.])/;
const MCP_PREFIX = /mcp__[\w-]*__$/;
// partner platforms serve some vendors' models under their own ids and retire them on their own dates
// (Bedrock ...-20250219-v1:0, Vertex AI ...@20250219), so a first-party record doesn't cover them (attribute._PARTNER)
const PARTNER = { Anthropic: /^(?:-\d{8})?(?:-v\d+:\d+|@)/ };

// Model ids are matched without regard to case (agents retype them); other signatures, like the scanner, exactly.
function prepare(r) {
  const model = r.kind === "model";
  const norm = (x) => (model ? x.toLowerCase() : x);
  r._model = model;
  r._sigs = [...new Set(r.signatures.map(norm))].sort((x, y) => y.length - x.length);
  r._excl = r.exclude.map(norm);
  r._exclFlat = r._excl.map((e) => e.replace(/\s+/g, ""));
  r._named = new Set(r.named.map((x) => x.toLowerCase()));
  r._partner = model ? PARTNER[r.vendor] || null : null;
}

function snapshotOf(line, j, named) {
  const m = SNAPSHOT.exec(line.slice(j));
  if (!m) return false;
  const rest = /^-[a-z][a-z0-9]*/.exec(line.slice(j + m[0].length).toLowerCase());
  return !rest || named.has(rest[0]);
}

function namedVariant(line, i, j, named) {
  if (!named.size) return false;
  const low = line;               // model lines are lowercased once, in matchRecord
  for (const n of named) {
    if (n[0] !== "-" && low.startsWith(n, i) && !/^(?:[a-z0-9_]|[.-][a-z0-9])/.test(low.slice(i + n.length))) return true;
  }
  const nxt = /^-[a-z][a-z0-9]*/.exec(low.slice(j));
  return !!nxt && named.has(nxt[0]);
}

export function findAll(line, sig, model, named = new Set(), limit = Infinity) {
  const out = [];
  if (!sig) return out;
  const checkLeft = TOKEN.test(sig[0]) && sig[0] !== "_";
  const checkRight = TOKEN.test(sig[sig.length - 1]) && sig[sig.length - 1] !== "_";
  let start = 0;
  for (;;) {
    const i = out.length < limit ? line.indexOf(sig, start) : -1;
    if (i < 0) return out;
    start = i + 1;
    if (checkLeft && i > 0 && TOKEN.test(line[i - 1]) && !MCP_PREFIX.test(line.slice(0, i))) continue;
    const j = i + sig.length;
    if (checkRight && j < line.length) {
      const nxt = line[j];
      if (TOKEN.test(nxt)) continue;
      if (nxt === "." && j + 1 < line.length && /\d/.test(line[j + 1])) continue;          // gpt-4 vs gpt-4.1
      if (line.startsWith("\\.", j) && j + 2 < line.length && /\d/.test(line[j + 2])) continue;
      if (model && nxt === "-" && !snapshotOf(line, j, named) && !namedVariant(line, i, j, named)) continue;   // gpt-4 vs gpt-4-turbo
    }
    out.push(i);
  }
}

// An identifier is at most 200 characters and names an id once or twice; past this many occurrences of one
// string the rest are not looked at, so a crafted identifier can't make a call expensive
const MAX_HITS = 4;

// The signature of r that the identifier names, or "". Same steps as attribute.scan on one line: signatures
// longest first; a hit inside an excluded id, or overlapping a longer signature's hit, doesn't count; an exclude
// that starts with the signature drops a hit it prefixes (gpt-3.5-turbo-0301-... under gpt-3.5-turbo); then
// attribute_record drops partner platform forms.
export function matchRecord(identifier, r) {
  let rr = r;
  if (!rr._sigs) prepare((rr = { named: [], vendor: "", exclude: [], ...r }));   // a bare record (tests)
  const line = rr._model ? identifier.toLowerCase() : identifier;
  if (!rr._sigs.some((sig) => line.includes(sig))) return "";          // the common case, kept cheap
  const taken = rr._excl.flatMap((e) => findAll(line, e, rr._model, rr._named, MAX_HITS).map((x) => [x, x + e.length]));
  const found = [];
  for (const sig of rr._sigs) {
    const end = (k) => k + sig.length;
    let ks = findAll(line, sig, rr._model, rr._named, MAX_HITS).filter((k) =>
      !taken.some(([a, b]) => a <= k && b >= end(k)) && !found.some(([a, b]) => k < b && a < end(k)));
    const starts = rr._exclFlat.filter((e) => e.startsWith(sig));
    if (starts.length) ks = ks.filter((k) => !starts.some((e) => line.slice(k).replace(/\s+/g, "").startsWith(e)));
    if (!ks.length) continue;
    for (const k of ks) found.push([k, end(k)]);
    if (rr._partner) {
      const all = findAll(line, sig, true, undefined, MAX_HITS);
      const occ = all.length ? all : findAll(line, sig, false, undefined, MAX_HITS);
      if (occ.length && occ.every((k) => rr._partner.test(line.slice(end(k))))) continue;
    }
    return rr.signatures.find((x) => (rr._model ? x.toLowerCase() : x) === sig) || sig;
  }
  return "";
}

function lookup(identifier, feed) {
  const out = [];
  for (const r of feed.records) {
    const sig = matchRecord(identifier, r);
    if (sig) out.push({ r, sig });
  }
  return out.sort((a, b) => cmp(a.r.effective, b.r.effective) || cmp(a.r.id, b.r.id));
}

// ---------- wording (same rules as apiwatch/site.py) ----------

export function fmt(iso) {
  const [y, m, d] = iso.split("-").map(Number);
  return `${MONTHS[m - 1]} ${d}, ${y}`;
}

function isoDay(ms) {
  return new Date(ms).toISOString().slice(0, 10);
}

function addDays(iso, days) {
  return new Date(Date.parse(`${iso}T00:00:00Z`) + days * 86400000).toISOString().slice(0, 10);
}

function cmp(a, b) {
  return a < b ? -1 : a > b ? 1 : 0;
}

// site.py _what_happens: breaking = calls fail after the date, silent = behavior changes on it, deprecation = still
// works with removal announced (its date may be the removal date, so no verb is tied to it)
export function whatHappens(r, today) {
  const future = r.effective > today;
  const when = fmt(r.effective);
  if (r.severity === "breaking") return future ? `stops working on ${when}` : `stopped working on ${when}`;
  if (r.severity === "silent") return future ? `changes behavior on ${when}` : `changed behavior on ${when}`;
  if (r.severity === "deprecation") {
    return future ? `is deprecated: it still works, and removal is announced. Key date: ${when}`
                  : `is deprecated, and removal was announced. Key date: ${when}`;
  }
  return future ? `changes on ${when}` : `changed on ${when}`;
}

// site.py _lead: the next date to act on, else the most recent one (rs sorted by date)
function leadOf(rs, today) {
  const upcoming = rs.filter((r) => r.effective > today);
  return upcoming.length ? upcoming[0] : rs[rs.length - 1];
}

function scopeClause(r) {
  // "Applies only in files that mention "notion"." -> "applies only in files that mention "notion""
  return r.scope ? r.scope.charAt(0).toLowerCase() + r.scope.slice(1).replace(/\.$/, "") + shortNote(r) : "";
}

// scope_text cuts long lists to a few alternatives and "or N more"; say where the full list is
function shortNote(r) {
  return / or \d+ more\b/.test(r.scope) ? `; get_record ${r.id} lists every alternative` : "";
}

export function answerFor(identifier, matches, today) {
  if (!matches.length) {
    return `apiwatch has no record for ${identifier}. That does not mean it is safe: apiwatch only knows the changes ` +
           "in its feed, so check the vendor's own deprecation and changelog pages.";
  }
  const rs = matches.map((m) => m.r);
  const unscoped = rs.filter((r) => !r.scope);
  const lead = leadOf(unscoped.length ? unscoped : rs, today);
  let line;
  if (lead.kind === "model") {
    line = `${identifier} ${whatHappens(lead, today)}` + (lead.scope ? ` (${scopeClause(lead)}).` : ".");
  } else {
    // API, SDK and MCP changes often hang on an API version, SDK major or protocol revision a one line answer
    // can't carry (why the site gives them no id pages), so name the record instead of saying what happens
    line = `${identifier} is named in ${recordNoun(lead)} with key date ${fmt(lead.effective)}: ` +
           `${lead.summary.replace(/\.$/, "")}. Whether it applies can depend on the API, SDK or protocol version you ` +
           "use; see the record." + (lead.scope ? ` ${scopeClause(lead).replace(/^a/, "A")}.` : "");
  }
  // a scoped record dated before the lead (already in effect in some code, or due sooner there), or one still
  // ahead when the lead's date is behind, is part of the answer (rs is sorted by date)
  const sooner = rs.filter((r) => r !== lead && r.scope &&
                                  (r.effective < lead.effective || (lead.effective <= today && r.effective > today)))[0];
  if (sooner) {
    line += sooner.kind === "model" ? ` In some code (${scopeClause(sooner)}) it ${whatHappens(sooner, today)}.`
                                    : ` In some code (${scopeClause(sooner)}) ${recordNoun(sooner)} has key date ${fmt(sooner.effective)}.`;
  }
  if (rs.length > 1) line += ` ${rs.length} records match; each is listed with its date and scope.`;
  return line;
}

function recordNoun(r) {
  const sev = SEV_LABEL[r.severity].toLowerCase();
  return `${/^[aeiou]/.test(sev) ? "an" : "a"} ${sev} record`;
}

function dateStatus(r, today) {
  return r.effective < today ? "date passed" : r.effective === today ? "today" : "ahead";
}

// ---------- tools ----------

const S = { type: "string" };
const RECORD_PROPS = {
  id: S, vendor: S, severity: { type: "string", enum: SEVERITIES }, kind: S, surface: S,
  effective: { type: "string", description: "Key date, YYYY-MM-DD" },
  date_passed: { type: "boolean", description: "True when the key date is today or earlier (UTC)" },
  summary: S, fix_hint: S,
  scope: { type: "string", description: "Empty when the record applies to every caller; otherwise where it applies" },
  record_url: S, source_url: S,
};
const RECORD = { type: "object", properties: RECORD_PROPS, required: Object.keys(RECORD_PROPS) };
const FULL_PROPS = {
  ...RECORD_PROPS, detail: S, signatures: { type: "array", items: S },
  exclude: { type: "array", items: S, description: "Ids the signatures would also match that this change does not cover" },
  context: { type: "array" }, files: { type: "array", items: S }, repo_context: { type: "array" },
  verified_on: S, review: S,
};
const TAIL = { record_urls: { type: "array", items: S }, next_step: S };
const MATCH = { type: "object", properties: { ...RECORD_PROPS, matched_signature: S },
                required: [...Object.keys(RECORD_PROPS), "matched_signature"] };

function obj(properties, required = Object.keys(properties)) {
  return { type: "object", properties, required };
}

function summary(r, today) {
  return { id: r.id, vendor: r.vendor, severity: r.severity, kind: r.kind, surface: r.surface, effective: r.effective,
           date_passed: r.effective <= today, summary: r.summary, fix_hint: r.fix_hint, scope: r.scope,
           record_url: r.record_url, source_url: r.source_url };
}

function full(r, today) {
  return { ...summary(r, today), detail: r.detail, signatures: r.signatures, exclude: r.exclude, context: r.context,
           files: r.files, repo_context: r.repo_context, verified_on: r.verified_on, review: r.review };
}

function nextStep(feed) {
  return `Next step: scan a whole repo in CI with the free apiwatch GitHub Action (${MARKETPLACE_URL}), or have ` +
         `apiwatch Pro scan every repo in a GitHub organization each night (${feed.home}pro/).`;
}

function recordLines(r, today) {
  const lines = [`- ${r.vendor}, ${SEV_LABEL[r.severity]}, ${fmt(r.effective)} (${dateStatus(r, today)}): ${r.summary}`];
  if (r.fix_hint) lines.push(`  What to do: ${r.fix_hint}`);
  if (r.scope) lines.push(`  ${scopeClause(r).replace(/^a/, "A")}.`);
  lines.push(`  Record: ${r.record_url}`);
  return lines.join("\n");
}

function finish(parts, rs, feed) {
  const urls = [...new Set(rs.map((r) => r.record_url))];
  const data = { record_urls: urls, next_step: nextStep(feed) };
  const textOut = [...parts, data.next_step, urls.length ? "Records:\n" + urls.join("\n") : "Records: none"].join("\n\n");
  return { textOut, tail: data };
}

function done(parts, rs, feed, data) {
  const { textOut, tail } = finish(parts, rs, feed);
  return { text: textOut, data: { ...data, ...tail } };
}

function checkModel(args, feed, today) {
  const model = args.model.trim();
  const matches = lookup(model, feed);
  const shown = matches.slice(0, MAX_RECORDS);
  const answer = answerFor(model, matches, today);
  const parts = [answer, ...shown.map((m) => recordLines(m.r, today))];
  return done(parts, shown.map((m) => m.r), feed, {
    model, answer, found: matches.length > 0, truncated: matches.length > shown.length,
    matches: shown.map((m) => ({ ...summary(m.r, today), matched_signature: m.sig })),
  });
}

function checkIdentifiers(args, feed, today) {
  const ids = [...new Set(args.identifiers.map((x) => x.trim()))];
  const results = [];
  const seen = new Map();
  for (const ident of ids) {
    const matches = lookup(ident, feed);
    for (const m of matches) if (!seen.has(m.r.id)) seen.set(m.r.id, m.r);
    results.push({ identifier: ident, answer: answerFor(ident, matches, today), found: matches.length > 0,
                   record_ids: matches.map((m) => m.r.id) });
  }
  const all = [...seen.values()];
  const shown = all.slice(0, MAX_RECORDS);
  const hits = results.filter((r) => r.found).length;
  const parts = [`${hits} of ${results.length} identifier${results.length === 1 ? "" : "s"} named in apiwatch records. ` +
                 "An identifier with no record is not known to be safe; apiwatch only knows the changes in its feed.",
                 results.map((r) => `- ${r.answer}`).join("\n"),
                 ...shown.map((r) => recordLines(r, today))];
  if (all.length > shown.length) parts.push(`${all.length - shown.length} more records not shown; call get_record with an id from record_ids.`);
  return done(parts, shown, feed, { results, records: shown.map((r) => summary(r, today)), truncated: all.length > shown.length });
}

function vendorMatches(r, vendor) {
  const v = vendor.trim().toLowerCase();
  return r.vendor.toLowerCase() === v || slug(r.vendor) === slug(v);
}

function slug(s) {
  return s.toLowerCase().replace(/[^a-z0-9]/g, "-").replace(/^-+|-+$/g, "");
}

function vendorNote(feed, vendor) {
  if (!vendor || feed.records.some((r) => vendorMatches(r, vendor))) return [];
  const names = [...new Set(feed.records.map((r) => r.vendor))].sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase()));
  return [`No vendor named ${vendor} in the feed. Vendors: ${names.join(", ")}.`];
}

function listUpcoming(args, feed, today) {
  const days = args.days === undefined ? 90 : args.days;
  const vendor = args.vendor === undefined ? "" : args.vendor.trim();
  const end = addDays(today, days);
  const rs = feed.records
    .filter((r) => r.effective >= today && r.effective <= end && (!vendor || vendorMatches(r, vendor)))
    .sort((a, b) => cmp(a.effective, b.effective) || ORDER[a.severity] - ORDER[b.severity] || cmp(a.id, b.id));
  const shown = rs.slice(0, MAX_RECORDS);
  const head = `${rs.length} dated change${rs.length === 1 ? "" : "s"} from ${fmt(today)} through ${fmt(end)}` +
               `${vendor ? ` for ${vendor}` : ""}, soonest first.` +
               (rs.length > shown.length ? ` Showing the first ${shown.length}.` : "");
  const parts = [head, ...vendorNote(feed, vendor), ...shown.map((r) => recordLines(r, today))];
  return done(parts, shown, feed, { from: today, to: end, vendor, total: rs.length, truncated: rs.length > shown.length,
                                    records: shown.map((r) => summary(r, today)) });
}

function searchChanges(args, feed, today) {
  const query = args.query.trim();
  const q = query.toLowerCase();
  const vendor = args.vendor === undefined ? "" : args.vendor.trim();
  const limit = args.limit === undefined ? 20 : args.limit;
  const rs = feed.records.filter((r) => (!vendor || vendorMatches(r, vendor)) &&
    (r.summary.toLowerCase().includes(q) || r.vendor.toLowerCase().includes(q) || r.signatures.some((s) => s.toLowerCase().includes(q))));
  const shown = rs.slice(0, limit);
  const head = rs.length ? `${rs.length} record${rs.length === 1 ? "" : "s"} match ${JSON.stringify(query)}${vendor ? ` for ${vendor}` : ""}, newest key date first.` +
                           (rs.length > shown.length ? ` Showing ${shown.length}.` : "")
                         : `No record matches ${JSON.stringify(query)}${vendor ? ` for ${vendor}` : ""}.`;
  const parts = [head, ...vendorNote(feed, vendor), ...shown.map((r) => recordLines(r, today))];
  return done(parts, shown, feed, { query, vendor, total: rs.length, truncated: rs.length > shown.length,
                                    records: shown.map((r) => summary(r, today)) });
}

function getRecord(args, feed, today) {
  const id = args.id.trim();
  const r = feed.records.find((x) => x.id === id);
  if (!r) {
    return done([`No published record has the id ${JSON.stringify(id)}. Ids come from the other tools' results.`], [], feed, { found: false });
  }
  const lines = [`${r.vendor}: ${r.summary}`, `${SEV_LABEL[r.severity]} (${r.kind}), key date ${fmt(r.effective)}` +
                 ` (${dateStatus(r, today)}). Surface: ${r.surface}`];
  if (r.detail) lines.push(`What changes: ${r.detail}`);
  if (r.fix_hint) lines.push(`What to do: ${r.fix_hint}`);
  if (r.scope) {
    // the full lists, not the shortened sentence: an agent reading text gets the whole scope
    const words = (items) => items.map((c) => (Array.isArray(c) ? c.join(" + ") : c)).join(" | ");
    lines.push(r.scope, "Full scope (a hit counts only where every line below holds; + means both, | means either):");
    if (r.files.length) lines.push(`  files matching: ${r.files.join(" | ")}`);
    if (r.context.length) lines.push(`  files that mention: ${words(r.context)}`);
    if (r.repo_context.length) lines.push(`  repos that depend on or mention (dep:ecosystem:name@version is a pinned dependency): ${words(r.repo_context)}`);
  }
  if (r.signatures.length) lines.push(`Strings to search for: ${r.signatures.join(", ")}`);
  if (r.exclude.length) lines.push(`Not affected: ${r.exclude.join(", ")}`);
  if (r.source_url) lines.push(`Source: ${r.source_url}`);
  lines.push(`Reviewed against the source on ${r.verified_on}${r.review === "ai" ? " (AI-assisted review)" : ""}.`);
  return done([lines.join("\n")], [r], feed, { found: true, record: full(r, today) });
}

// ---------- MCP server change history (docs/MCP-HISTORY.md) ----------
// /mcp-history.json is built by `apiwatch site` from apiwatch's own checks of public MCP servers. It carries no tool
// description text, and parseHistory keeps only the fields below, checked against plain patterns, so a crafted or
// stale file can't put a server's own words into an answer: a tool name or path outside the pattern becomes its sha256.

const TOOL_NAME = /^[A-Za-z0-9_.\-/]{1,128}$/;
const HASHED = /^sha256:[0-9a-f]{64}$/;
const SAFE_PATH = /^[A-Za-z0-9_.\-/[\]:]{1,600}$/;
const SAFE_REST = /^[A-Za-z0-9_.\-/[\]]{0,256}$/;
const DAY = /^\d{4}-\d{2}-\d{2}$/;
const STAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/;
const SLUG = /^[a-z0-9][a-z0-9-]{0,62}$/;
const HEX = /^[0-9a-f]{64}$/;
const STATUSES = ["pending_first_check", "ok", "needs_login", "not_reachable", "empty_tools", "failing"];
const FLAGS = ["description_changed", "annotation_widened", "hidden_characters", "instruction_like_text", "unusual_tool_name"];
// a flag's detail in an answer: an annotation widening or code points; anything else (a matched phrase) is dropped
const FLAG_DETAIL = /^(?:(?:readOnlyHint|destructiveHint|idempotentHint|openWorldHint) (?:true|false) to (?:true|false)|U\+[0-9A-F]{4,6}(?: U\+[0-9A-F]{4,6}){0,40})$/;
const MAX_EVENTS = 100;

async function sha256(s) {
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function safeTool(v) {
  if (typeof v !== "string" || v === "") return "";
  return TOOL_NAME.test(v) || HASHED.test(v) ? v : `sha256:${await sha256(v)}`;
}

// Same as history.safe_path: tool:<safe name><rest>, with a rest that isn't plain (a parameter or annotation name
// could say anything) replaced by its sha256 after its .input, .output or .annotations head
async function safePath(v, tool) {
  if (typeof v !== "string" || v === "") return "";
  const head = typeof tool === "string" && tool ? `tool:${tool}` : "";
  if (!head || !v.startsWith(head)) return SAFE_PATH.test(v) ? v : `sha256:${await sha256(v)}`;
  let rest = v.slice(head.length);
  if (!SAFE_REST.test(rest)) {
    const m = /^\.(input|output|annotations)/.exec(rest);
    rest = `${m ? m[0] : ""}.sha256:${await sha256(rest)}`;
  }
  return `tool:${await safeTool(tool)}${rest}`;
}

function plain(v, max = 100) {
  // names and URLs come from apiwatch's own watch list; still: no control or invisible characters, bounded
  return typeof v === "string" ? v.replace(/[\u0000-\u001f\u007f-\u009f\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]/g, "").slice(0, max) : "";
}

function int(v) {
  return Number.isInteger(v) && v >= 0 ? v : null;
}

async function parseEvents(list) {
  const out = [];
  for (const e of Array.isArray(list) ? list : []) {
    if (!isObject(e) || !/^[a-z_]{1,40}$/.test(e.type || "") || !SEVERITIES.includes(e.severity)) continue;
    const ev = { type: e.type, tool: await safeTool(e.tool), path: await safePath(e.path, e.tool), severity: e.severity };
    if (e.type === "description_changed" || e.type === "title_changed") {
      ev.old_len = int(e.old_len); ev.new_len = int(e.new_len);
      ev.old_sha256 = HEX.test(e.old_sha256 || "") ? e.old_sha256 : "";
      ev.new_sha256 = HEX.test(e.new_sha256 || "") ? e.new_sha256 : "";
    }
    out.push(ev);
  }
  return out;
}

async function parseFlags(list) {
  const out = [];
  for (const f of Array.isArray(list) ? list : []) {
    if (!isObject(f) || !FLAGS.includes(f.flag)) continue;
    // the phrase an instruction_like_text flag matched stays on the page: no reason to put it in front of an agent
    const detail = typeof f.detail === "string" && FLAG_DETAIL.test(f.detail) ? f.detail : "";
    out.push({ flag: f.flag, tool: await safeTool(f.tool), detail });
  }
  return out;
}

export async function parseHistory(data, origin = "") {
  const index = typeof data.index_url === "string" && /^https?:\/\//.test(data.index_url) ? data.index_url : `${origin}/mcp-servers/`;
  const servers = [];
  for (const s of Array.isArray(data.servers) ? data.servers : []) {
    if (!isObject(s) || !SLUG.test(s.slug || "") || typeof s.url !== "string" || !/^https:\/\/\S+$/.test(s.url)) continue;
    const entries = [];
    for (const e of Array.isArray(s.entries) ? s.entries : []) {
      if (!isObject(e) || !DAY.test(e.date || "") || !["first_seen", "change", "unstable"].includes(e.kind)) continue;
      const row = { date: e.date, kind: e.kind };
      if (e.kind === "change") {
        row.events = await parseEvents(e.events);
        row.flags = await parseFlags(e.flags);
      } else if (e.kind === "first_seen") {
        row.tool_count = int(e.tool_count);
        row.flags = await parseFlags(e.flags);
      } else {
        row.hashes = (Array.isArray(e.hashes) ? e.hashes : []).filter((h) => HEX.test(h));
      }
      entries.push(row);
    }
    servers.push({
      slug: s.slug, name: plain(s.name) || s.slug, url: plain(s.url, 300), registry_name: plain(s.registry_name, 200),
      status: STATUSES.includes(s.status) ? s.status : "failing",
      status_since: STAMP.test(s.status_since || "") ? s.status_since : "",
      tool_count: int(s.tool_count),
      last_checked: STAMP.test(s.last_checked || "") ? s.last_checked : "",
      first_seen: DAY.test(s.first_seen || "") ? s.first_seen : "",
      last_change: DAY.test(s.last_change || "") ? s.last_change : "",
      open_flags: (Array.isArray(s.open_flags) ? s.open_flags : []).filter((f) => FLAGS.includes(f)),
      page_url: `${index.replace(/\/?$/, "/")}${s.slug}/`,
      entries,
    });
  }
  return { servers, index };
}

async function loadHistory(env, request, cache, now) {
  if (cache.hist && now - cache.histAt < FEED_TTL_MS) return cache.hist;
  try {
    const res = await env.ASSETS.fetch(new URL("/mcp-history.json", request.url));
    if (!res.ok) throw new Error(`mcp-history.json: HTTP ${res.status}`);
    const hist = await parseHistory(await res.json(), new URL(request.url).origin);
    cache.hist = hist;
    cache.histAt = now;
    return hist;
  } catch (e) {
    if (cache.hist) return cache.hist;   // keep answering from the last good copy
    throw e;
  }
}

function utc(stamp) {
  return stamp ? `${fmt(stamp.slice(0, 10))} ${stamp.slice(11, 16)} UTC` : "never";
}

function statusText(s) {
  const since = utc(s.status_since);
  return {
    pending_first_check: "first check pending",
    ok: "answering tools/list",
    needs_login: `needs a login since ${since}`,
    not_reachable: `not reachable since ${since}`,
    empty_tools: `returned an empty tool list since ${since}`,
    failing: `tools/list failing since ${since}`,
  }[s.status];
}

function serverSummary(s) {
  const changes = s.entries.filter((e) => e.kind === "change");
  return { slug: s.slug, name: s.name, url: s.url, registry_name: s.registry_name, status: s.status,
           status_since: s.status_since, tool_count: s.tool_count, last_checked: s.last_checked,
           first_seen: s.first_seen, last_change: changes.length ? changes[changes.length - 1].date : "",
           changes_total: changes.length, open_flags: s.open_flags, page_url: s.page_url };
}

const HISTORY_NOTE = "Observations of what each server's tools/list returned, checked by apiwatch without credentials and " +
  "without calling tools. A change counts only after two checks agree. Flags mean worth a look, not a finding; no " +
  "flags and no changes do not mean a server is safe.";

function listWatched(args, hist) {
  const servers = hist.servers.map(serverSummary);
  const lines = servers.map((s) => `- ${s.name} (${s.slug}), ${s.url}: ` +
    (s.status === "pending_first_check" ? "first check pending" :
     `${statusText(s)}${s.tool_count !== null ? `, ${s.tool_count} tools at the last good check` : ""}, last checked ${utc(s.last_checked)}, ` +
     (s.last_change ? `last change ${fmt(s.last_change)}` : "no change observed yet")) +
    (s.open_flags.length ? `, flags (last 30 days): ${s.open_flags.join(", ")}` : "") + `. ${s.page_url}`);
  const head = `apiwatch watches ${servers.length} public MCP server${servers.length === 1 ? "" : "s"}. ${HISTORY_NOTE}`;
  return { text: [head, lines.join("\n"), `Index: ${hist.index}`].join("\n\n"),
           data: { servers, total: servers.length, note: HISTORY_NOTE, index_url: hist.index } };
}

function normUrl(x) {
  try {
    const u = new URL(/^[a-z][a-z0-9+.-]*:\/\//i.test(x) ? x : `https://${x}`);
    return `${u.protocol}//${u.host.toLowerCase()}${u.pathname.replace(/\/+$/, "")}${u.search}`;
  } catch {
    return "";
  }
}

function findServer(hist, query) {
  const q = query.trim().toLowerCase();
  const nu = normUrl(query.trim());
  return hist.servers.find((s) => s.slug === q) ||
         hist.servers.find((s) => s.registry_name && s.registry_name.toLowerCase() === q) ||
         hist.servers.find((s) => nu && normUrl(s.url) === nu) ||
         hist.servers.find((s) => s.name.toLowerCase() === q) || null;
}

const BREAKING_WORDS = { added_required: "new required parameter", became_required: "parameter became required",
                         type_changed: "type changed", enum_values_removed: "allowed values removed",
                         enum_added: "allowed values newly imposed" };

function eventWords(e) {
  const toolLevel = e.path === `tool:${e.tool}`;
  const target = toolLevel || !e.path.startsWith("tool:") ? (e.tool || e.path) : e.path.slice(5);
  if (e.type === "removed") return `${toolLevel ? "tool removed" : e.path.includes(".output") ? "output field removed" : "parameter removed"}: ${target}`;
  return `${BREAKING_WORDS[e.type] || e.type.replace(/_/g, " ")}: ${target}`;
}

function times(n) {
  return n === 1 ? "once" : n === 2 ? "twice" : `${n} times`;
}

function serverChanges(args, hist, today) {
  const query = args.server.trim();
  const days = args.days === undefined ? 90 : args.days;
  const from = addDays(today, 1 - days);   // the first day counted: days=1 is today only
  const s = findServer(hist, query);
  if (!s) {
    const names = hist.servers.map((x) => `${x.name} (${x.slug})`).join(", ");
    const answer = `apiwatch does not watch ${JSON.stringify(query)}, so it has no change history for it. That says nothing ` +
      `about whether its tools changed or can be trusted. Watched servers: ${names || "none yet"}. Index: ${hist.index}`;
    return { text: answer, data: { query, watched: false, days, from, to: today, answer, server: null, changes: [], notes: [],
                                   truncated: false, index_url: hist.index } };
  }
  const sum = serverSummary(s);
  const inWindow = s.entries.filter((e) => e.date >= from && e.date <= today).reverse();
  const changes = inWindow.filter((e) => e.kind === "change");
  const notes = inWindow.filter((e) => e.kind !== "change");
  let answer;
  if (s.status === "pending_first_check") {
    answer = `apiwatch watches ${s.name} but has not checked it yet, so it has no history for it yet.`;
  } else if (!changes.length) {
    const since = s.first_seen && s.first_seen > from ? ` (it has been watched only since ${fmt(s.first_seen)})` : "";
    answer = `apiwatch observed no change to the tools of ${s.name} in the last ${days} days${since}. That is not a statement that its tools are safe.`;
  } else {
    const events = changes.flatMap((c) => c.events);
    const desc = events.filter((e) => e.type === "description_changed").length;
    const parts = [];
    for (const sev of SEVERITIES) {
      const evs = events.filter((e) => e.severity === sev && e.type !== "description_changed");
      if (!evs.length) continue;
      if (sev === "breaking") {
        const shown = evs.slice(0, 3).map(eventWords).join("; ");
        parts.push(`${evs.length} breaking (${shown}${evs.length > 3 ? `; and ${evs.length - 3} more` : ""})`);
      } else {
        parts.push(`${evs.length} ${sev}`);
      }
    }
    if (desc) parts.push(`${desc} description change${desc === 1 ? "" : "s"}`);
    answer = `${s.name} changed its tools ${times(changes.length)} in the last ${days} days: ${parts.join(", ") || "no classified events"}.`;
  }
  const flags = inWindow.flatMap((e) => (e.flags || []).map((f) => `${f.flag} on ${f.tool}`));
  if (flags.length) {
    const uniq = [...new Set(flags)];
    answer += ` Flags, worth a look and not findings: ${uniq.slice(0, 5).join(", ")}${uniq.length > 5 ? `, and ${uniq.length - 5} more` : ""}.`;
  }
  const unstable = notes.filter((e) => e.kind === "unstable");
  if (unstable.length) answer += ` It went back and forth between two tool lists (noted ${fmt(unstable[0].date)}); a list counts as a change only when two checks in a row return it.`;
  if (s.status !== "ok" && s.status !== "pending_first_check") answer += ` Its latest check: ${statusText(s)}.`;
  if (s.last_checked) answer += ` Last checked ${utc(s.last_checked)}.`;
  const shown = changes.slice(0, MAX_RECORDS).map((c) => ({ date: c.date, events: c.events.slice(0, MAX_EVENTS), flags: c.flags,
                                                            truncated: c.events.length > MAX_EVENTS }));
  const lines = shown.map((c) => `- ${fmt(c.date)}: ` + c.events.map((e) => `${e.severity} ${e.type} ${e.path || e.tool}`).join("; ") +
                                  (c.flags.length ? ` | flags: ${c.flags.map((f) => `${f.flag} on ${f.tool}`).join(", ")}` : ""));
  return { text: [answer, ...(lines.length ? [lines.join("\n")] : []), HISTORY_NOTE, `Page: ${s.page_url}`].join("\n\n"),
           data: { query, watched: true, days, from, to: today, answer, server: sum, changes: shown,
                   notes: notes.map((n) => ({ date: n.date, kind: n.kind })), truncated: changes.length > shown.length,
                   index_url: hist.index } };
}

const SERVER_PROPS = { slug: S, name: S, url: S, registry_name: S,
  status: { type: "string", enum: STATUSES }, status_since: S,
  tool_count: { type: ["integer", "null"], description: "Tools at the last good check; null before the first one" },
  last_checked: S, first_seen: S, last_change: S, changes_total: { type: "integer" },
  open_flags: { type: "array", items: { type: "string", enum: FLAGS }, description: "Flags from the last 30 days" }, page_url: S };
const SERVER = obj(SERVER_PROPS);
const EVENT = obj({ type: S, tool: { type: "string", description: "Tool name, or sha256:<hex> when the name is not plain" },
                    path: S, severity: { type: "string", enum: SEVERITIES } }, ["type", "tool", "path", "severity"]);
const FLAG = obj({ flag: { type: "string", enum: FLAGS }, tool: S, detail: S });

const HISTORY_TOOLS = [
  {
    name: "list_watched_mcp_servers", title: "List watched MCP servers", source: "history", run: listWatched,
    description: "List the public MCP servers whose tools apiwatch checks every 6 hours (tools/list only, no login, no tool " +
      "calls), with each one's status (answering, needs a login, not reachable), tool count, last check and last observed " +
      "change. Use it to see which servers mcp_server_changes can answer about. Returns no tool descriptions.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
    outputSchema: obj({ servers: { type: "array", items: SERVER }, total: { type: "integer" }, note: S, index_url: S }),
  },
  {
    name: "mcp_server_changes", title: "Recent tool changes on an MCP server", source: "history", run: serverChanges,
    description: "Before you rely on a public MCP server's tools, ask whether apiwatch saw them change: tools removed or " +
      "renamed, new required parameters, rewritten descriptions, changed safety hints. Give a watched server's slug, URL " +
      "or registry name. Returns a one line answer and the confirmed changes in the last N days (default 90, max 365), " +
      "newest first, with flags worth a look. Observations only: no changes is not a statement that a server is safe. " +
      "Never includes tool description text.",
    inputSchema: { type: "object", properties: {
      server: { type: "string", minLength: 1, maxLength: 300, description: "Slug (context7), URL (https://mcp.context7.com/mcp) or registry name" },
      days: { type: "integer", minimum: 1, maximum: 365, description: "How many days back, default 90" } },
                   required: ["server"], additionalProperties: false },
    outputSchema: obj({ query: S, watched: { type: "boolean" }, days: { type: "integer" }, from: S, to: S, answer: S,
                        server: { type: ["object", "null"], properties: SERVER_PROPS },
                        changes: { type: "array", items: obj({ date: S, events: { type: "array", items: EVENT },
                                                                flags: { type: "array", items: FLAG }, truncated: { type: "boolean" } }) },
                        notes: { type: "array", items: obj({ date: S, kind: S }) }, truncated: { type: "boolean" }, index_url: S }),
  },
];

const VENDOR_ARG = { type: "string", minLength: 1, maxLength: 100, description: "Only this vendor, for example OpenAI, Anthropic, Google, Stripe" };

const TOOLS = [
  {
    name: "check_model", title: "Check an AI model id", run: checkModel,
    description: "Look up one AI model id (for example gpt-4o-2024-05-13 or claude-sonnet-4-5) in apiwatch's reviewed " +
      "feed of vendor changes before you use it in code. Returns a one line answer (when it stops working, changes " +
      "behavior, or its deprecation key date) and each matching record with the date, what to do and the replacement, " +
      "and links to the record and the vendor's source. \"No record\" means apiwatch has no record for it, not that it is safe. " +
      "Records that apply only in some repos or files say so in scope.",
    inputSchema: { type: "object", properties: { model: { type: "string", minLength: 1, maxLength: 200, description: "The model id as it appears in code" } },
                   required: ["model"], additionalProperties: false },
    outputSchema: obj({ model: S, answer: S, found: { type: "boolean" }, truncated: { type: "boolean" },
                        matches: { type: "array", items: MATCH }, ...TAIL }),
  },
  {
    name: "check_identifiers", title: "Check many identifiers", run: checkIdentifiers,
    description: "Look up many identifiers at once before shipping code: model ids, API endpoints (/v1/assistants), MCP " +
      "tool names, SDK methods or field names. For each one, a one line answer and the ids of matching records, plus the " +
      "records themselves. A record scoped to some code (for example only repos that depend on a given SDK major version) " +
      "is returned with that scope spelled out, never as an unconditional hit. Max 100 identifiers.",
    inputSchema: { type: "object", properties: { identifiers: { type: "array", minItems: 1, maxItems: 100,
                     items: { type: "string", minLength: 1, maxLength: 200 }, description: "Identifiers as they appear in code" } },
                   required: ["identifiers"], additionalProperties: false },
    outputSchema: obj({ results: { type: "array", items: obj({ identifier: S, answer: S, found: { type: "boolean" },
                                                                record_ids: { type: "array", items: S } }) },
                        records: { type: "array", items: RECORD }, truncated: { type: "boolean" }, ...TAIL }),
  },
  {
    name: "list_upcoming", title: "List upcoming changes", run: listUpcoming,
    description: "List the dated changes in apiwatch's feed from today forward, soonest first: model retirements, " +
      "breaking API changes, silent behavior changes and deprecation key dates. Use it to plan migrations. " +
      "Optionally limit to the next N days (default 90, max 365) or one vendor. Max 50 records.",
    inputSchema: { type: "object", properties: { days: { type: "integer", minimum: 1, maximum: 365, description: "How many days ahead, default 90" },
                                                 vendor: VENDOR_ARG },
                   additionalProperties: false },
    outputSchema: obj({ from: S, to: S, vendor: S, total: { type: "integer" }, truncated: { type: "boolean" },
                        records: { type: "array", items: RECORD }, ...TAIL }),
  },
  {
    name: "search_changes", title: "Search changes", run: searchChanges,
    description: "Search apiwatch's feed of reviewed vendor changes by text: a case-insensitive match on each record's " +
      "summary, vendor and signatures (the strings to look for in code). Use it when you know a product or feature name " +
      "but not the exact id. Newest key date first; default 20 results, max 50.",
    inputSchema: { type: "object", properties: { query: { type: "string", minLength: 1, maxLength: 200 }, vendor: VENDOR_ARG,
                                                 limit: { type: "integer", minimum: 1, maximum: 50, description: "Default 20" } },
                   required: ["query"], additionalProperties: false },
    outputSchema: obj({ query: S, vendor: S, total: { type: "integer" }, truncated: { type: "boolean" },
                        records: { type: "array", items: RECORD }, ...TAIL }),
  },
  {
    name: "get_record", title: "Get one record", run: getRecord,
    description: "Get one apiwatch record in full, as published: what changes, what to do, the strings to search for, " +
      "ids it does not cover, where it applies, the vendor source and when it was reviewed. Use an id from another tool's result.",
    inputSchema: { type: "object", properties: { id: { type: "string", minLength: 1, maxLength: 200, description: "Record id, for example openai-2026-10-23-gpt-4o-2024-05-13" } },
                   required: ["id"], additionalProperties: false },
    outputSchema: obj({ found: { type: "boolean" }, record: { type: "object", properties: FULL_PROPS, required: Object.keys(FULL_PROPS) }, ...TAIL },
                      ["found", "record_urls", "next_step"]),
  },
  ...HISTORY_TOOLS,
];

// Checks arguments against the tool's inputSchema (only the keywords these schemas use). "" when they are fine.
function validate(tool, args) {
  const schema = tool.inputSchema;
  for (const key of Object.keys(args)) {
    if (!Object.prototype.hasOwnProperty.call(schema.properties, key)) return `unknown argument ${JSON.stringify(key)}; expected ${Object.keys(schema.properties).join(", ")}`;
  }
  for (const key of schema.required || []) {
    if (args[key] === undefined) return `${key} is required`;
  }
  for (const [key, spec] of Object.entries(schema.properties)) {
    if (args[key] === undefined) continue;
    const problem = check(key, args[key], spec);
    if (problem) return problem;
  }
  return "";
}

function check(name, value, spec) {
  if (spec.type === "string") {
    if (typeof value !== "string") return `${name} must be a string`;
    const t = value.trim();
    if (t.length < (spec.minLength || 0) || !t) return `${name} must not be empty`;
    if (value.length > spec.maxLength) return `${name} must be at most ${spec.maxLength} characters`;
    return "";
  }
  if (spec.type === "integer") {
    if (!Number.isInteger(value)) return `${name} must be an integer`;
    if (value < spec.minimum || value > spec.maximum) return `${name} must be between ${spec.minimum} and ${spec.maximum}`;
    return "";
  }
  if (spec.type === "array") {
    if (!Array.isArray(value)) return `${name} must be an array`;
    if (value.length < spec.minItems) return `${name} must have at least ${spec.minItems} item`;
    if (value.length > spec.maxItems) return `${name} must have at most ${spec.maxItems} items`;
    for (let i = 0; i < value.length; i++) {
      const problem = check(`${name}[${i}]`, value[i], spec.items);
      if (problem) return problem;
    }
    return "";
  }
  return "";
}

export { TOOLS, SUPPORTED, MODERN, LEGACY };
