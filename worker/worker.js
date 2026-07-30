// Cloudflare Worker serving the whole boss-farm-calculator deployment:
// static assets (the built dashboard, see ../build, configured via the
// [assets] block in wrangler.toml) plus a CORS-friendly reverse proxy for
// the two upstreams boss.py's build_index()/build_payload() otherwise fetch
// server-side, plus the trade-sniper's SnipeSession Durable Object.
//
// poe.ninja sends no Access-Control-Allow-Origin header, so a browser
// calling it directly (as the static build must, since it has no backend of
// its own) gets blocked by CORS. poe.watch already sends
// `Access-Control-Allow-Origin: *`, but its /compact feed is ~12MB — routed
// through here too so Cloudflare's edge cache absorbs it once for every
// visitor instead of every browser refetching it on each poll.
//
// Cloudflare tries to match a static asset first (see [assets] in
// wrangler.toml) and only invokes this fetch() handler when nothing matched
// — so by the time we get here, the request is either an API-proxy path or
// something that should redirect to /bosses (the dashboard's real URL; see
// build_static.py, which writes it to build/bosses/index.html).
//
// Routes (mirror boss.py's CURRENCY_URL/EXCHANGE_URL/ITEM_URL/WATCH_URL path
// shapes 1:1, so the mapping is easy to eyeball against boss.py):
//   GET /ninja/<path>?<query>  -> https://poe.ninja/poe1/api/economy/<path>?<query>
//   GET /watch/compact?<query> -> https://api.poe.watch/compact?<query>
//   POST /snipe/start, GET /snipe/poll, POST /snipe/stop -> SnipeSession DO
//   anything else (root, typos, old links) -> 302 redirect to /bosses

const NINJA_BASE = "https://poe.ninja/poe1/api/economy";
const WATCH_BASE = "https://api.poe.watch";
const UA = "boss-dashboard-static-proxy/1.0 (contact: you@example.com)";
const CACHE_TTL = 300; // seconds; matches boss.py's CACHE_TTL

function withCors(body, status, contentType) {
  return new Response(body, {
    status,
    headers: {
      "Content-Type": contentType || "application/json",
      "X-Content-Type-Options": "nosniff",
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
      "Access-Control-Allow-Headers": "*",
    },
  });
}

// --------------------------------------------------------------------------- //
// SnipeSession — one Durable Object instance per active trade-sniper search.
//
// Why this exists at all: PoE's live-search WebSocket
// (wss://www.pathofexile.com/api/trade/live/<league>/<searchId>) requires a
// Cookie: POESESSID=... header and an Origin: https://www.pathofexile.com
// header on the upgrade request. Browsers can't do either from a
// third-party page (JS can't override Origin; cross-origin fetch/WebSocket
// can't attach a Cookie header at all) — confirmed live against the real
// endpoint before building this (see CLAUDE.md). So the browser never holds
// a WebSocket itself; it just polls this DO over plain HTTP.
//
// Confirmed live: plain trade search + fetch (steps 1 and 3 below) work
// WITHOUT any POESESSID at all — only the live-search socket itself needs
// it, so that's the only place the credential is used, and it's never
// logged, persisted to storage, or echoed back in any response.
//
// Rate-limit discipline (GGG's documented trade API limits: search 5/12s,
// 15/62s, 30/302s; fetch 12/6s, 16/14s): exactly one search call per
// session start, fetch calls batched to <=10 ids at a time and only fired
// when the live socket actually pushes new ids (never polled/repeated).
export class SnipeSession {
  constructor(state, env) {
    this.state = state;
    this.env = env;
    this.ws = null;
    this.buffer = [];
    this.searchId = null;
    this.referenceChaos = null;
    this.divineRate = null;
    this.thresholdPct = 20;
  }

  async fetch(request) {
    const url = new URL(request.url);
    if (url.pathname === "/start") return this.handleStart(request);
    if (url.pathname === "/poll") return this.handlePoll();
    if (url.pathname === "/stop") return this.handleStop();
    return new Response(JSON.stringify({ ok: false, error: "not found" }), { status: 404 });
  }

  async handleStart(request) {
    if (this.ws) {
      return new Response(JSON.stringify({ ok: false, error: "session already running" }), { status: 409 });
    }
    let body;
    try {
      body = await request.json();
    } catch (e) {
      return new Response(JSON.stringify({ ok: false, error: "bad JSON body" }), { status: 400 });
    }
    const { poesessid, league, itemName, itemCategory, thresholdPct } = body;
    if (!poesessid || !league || !itemName || !itemCategory) {
      return new Response(JSON.stringify({ ok: false, error: "need poesessid, league, itemName, itemCategory" }), { status: 400 });
    }
    this.thresholdPct = Math.min(90, Math.max(1, Number(thresholdPct) || 20));

    try {
      const ref = await fetchNinjaReferencePrice(league, itemCategory, itemName);
      this.referenceChaos = ref.chaos;
      this.divineRate = ref.divineRate;
    } catch (e) {
      return new Response(JSON.stringify({ ok: false, error: "poe.ninja price lookup failed: " + e }), { status: 502 });
    }
    if (this.referenceChaos == null) {
      return new Response(JSON.stringify({ ok: false, error: "no current poe.ninja price found for that exact item name/category" }), { status: 400 });
    }

    const isUnique = itemCategory.startsWith("Unique");
    const query = isUnique
      ? { query: { status: { option: "online" }, name: itemName }, sort: { price: "asc" } }
      : { query: { status: { option: "online" }, type: itemName }, sort: { price: "asc" } };
    let searchResp;
    try {
      searchResp = await fetch(`https://www.pathofexile.com/api/trade/search/${encodeURIComponent(league)}`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "User-Agent": TRADE_UA },
        body: JSON.stringify(query),
      });
    } catch (e) {
      return new Response(JSON.stringify({ ok: false, error: "trade search request failed: " + e }), { status: 502 });
    }
    if (!searchResp.ok) {
      return new Response(JSON.stringify({ ok: false, error: "trade search API returned " + searchResp.status }), { status: 502 });
    }
    const searchData = await searchResp.json();
    if (!searchData.id) {
      return new Response(JSON.stringify({ ok: false, error: "trade search returned no search id" }), { status: 502 });
    }
    this.searchId = searchData.id;

    if (Array.isArray(searchData.result) && searchData.result.length) {
      await this.fetchAndBufferListings(searchData.result.slice(0, 10));
    }

    try {
      const wsResp = await fetch(`https://www.pathofexile.com/api/trade/live/${encodeURIComponent(league)}/${encodeURIComponent(this.searchId)}`, {
        headers: {
          "Upgrade": "websocket",
          "Cookie": `POESESSID=${poesessid}`,
          "Origin": "https://www.pathofexile.com",
          "User-Agent": TRADE_UA,
        },
      });
      const ws = wsResp.webSocket;
      if (!ws) {
        return new Response(JSON.stringify({ ok: false, error: "live-search connection rejected (HTTP " + wsResp.status + ") — check your POESESSID is current" }), { status: 502 });
      }
      ws.accept();
      this.ws = ws;
      ws.addEventListener("message", (e) => this.onMessage(e));
      ws.addEventListener("close", () => { this.ws = null; });
      ws.addEventListener("error", () => { this.ws = null; });
    } catch (e) {
      return new Response(JSON.stringify({ ok: false, error: "live-search websocket connect failed: " + e }), { status: 502 });
    }

    await this.state.storage.setAlarm(Date.now() + 10 * 60 * 1000); // auto-stop after 10 min with no polls
    return new Response(JSON.stringify({ ok: true }), { status: 200 });
  }

  onMessage(event) {
    let data;
    try { data = JSON.parse(event.data); } catch (e) { return; }
    if (data.auth) return; // initial connection-confirmed message, not a listing update
    const ids = Array.isArray(data.new) ? data.new : (Array.isArray(data.ids) ? data.ids : null);
    if (ids && ids.length) this.fetchAndBufferListings(ids).catch(() => {});
  }

  async fetchAndBufferListings(ids) {
    const chunk = ids.slice(0, 10).join(",");
    let resp;
    try {
      resp = await fetch(`https://www.pathofexile.com/api/trade/fetch/${chunk}?query=${this.searchId}`, {
        headers: { "User-Agent": TRADE_UA },
      });
    } catch (e) { return; }
    if (!resp.ok) return;
    let data;
    try { data = await resp.json(); } catch (e) { return; }
    for (const r of (data.result || [])) {
      if (!r || !r.listing || !r.listing.price) continue;
      const { amount, currency } = r.listing.price;
      if (amount == null || !currency) continue;
      const chaosEquiv = currency === "chaos" ? amount
        : (currency === "divine" && this.divineRate) ? amount * this.divineRate
        : null;
      if (chaosEquiv == null) continue; // unsupported currency for comparison, skip
      if (chaosEquiv > this.referenceChaos * (1 - this.thresholdPct / 100)) continue; // not underpriced enough
      this.buffer.push({
        id: r.id,
        itemName: (r.item && (r.item.name || r.item.typeLine || r.item.baseType)) || "?",
        icon: (r.item && r.item.icon) || null,
        amount, currency,
        chaosEquiv: Math.round(chaosEquiv * 100) / 100,
        referenceChaos: this.referenceChaos,
        account: (r.listing.account && r.listing.account.name) || "?",
        whisper: r.listing.whisper || "",
        seenAt: Date.now(),
      });
    }
    if (this.buffer.length > 200) this.buffer = this.buffer.slice(-200);
  }

  async handlePoll() {
    await this.state.storage.setAlarm(Date.now() + 10 * 60 * 1000); // extend idle timer
    const listings = this.buffer;
    this.buffer = [];
    return new Response(JSON.stringify({ ok: true, running: !!this.ws, listings }), { status: 200 });
  }

  async handleStop() {
    if (this.ws) { try { this.ws.close(); } catch (e) {} this.ws = null; }
    await this.state.storage.deleteAlarm();
    return new Response(JSON.stringify({ ok: true }), { status: 200 });
  }

  async alarm() {
    if (this.ws) { try { this.ws.close(); } catch (e) {} this.ws = null; }
  }
}

const TRADE_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36";

// Simplified single-category poe.ninja lookup — NOT the full multi-source
// floor-price merge boss.py's build_index() does. Good enough to flag
// "clearly underpriced" for the sniper; if you need the exact same
// realistic-floor logic boss.py uses, port build_index() here instead.
async function fetchNinjaReferencePrice(league, itemCategory, itemName) {
  const isExchangeCategory = ["Currency", "Fragment", "Astrolabe"].includes(itemCategory);
  const base = isExchangeCategory
    ? `${NINJA_BASE}/exchange/current/overview`
    : `${NINJA_BASE}/stash/current/item/overview`;
  const qs = new URLSearchParams({ league, type: itemCategory });
  const resp = await fetch(`${base}?${qs}`, { headers: { "User-Agent": UA } });
  const data = await resp.json();

  let chaos = null;
  if (isExchangeCategory) {
    const idToMeta = {};
    for (const it of (data.items || [])) if (it.id) idToMeta[it.id] = it;
    for (const line of (data.lines || [])) {
      const meta = idToMeta[line.id] || {};
      const nm = meta.name || line.currencyTypeName || line.name;
      if (nm === itemName) { chaos = line.primaryValue; break; }
    }
  } else {
    for (const line of (data.lines || [])) {
      if (line.name === itemName) { chaos = line.chaosValue ?? line.chaosEquivalent; break; }
    }
  }

  let divineRate = null;
  const divResp = await fetch(`${NINJA_BASE}/exchange/current/overview?${new URLSearchParams({ league, type: "Currency" })}`, { headers: { "User-Agent": UA } });
  const divData = await divResp.json();
  const idToMeta2 = {};
  for (const it of (divData.items || [])) if (it.id) idToMeta2[it.id] = it;
  for (const line of (divData.lines || [])) {
    const meta = idToMeta2[line.id] || {};
    if ((meta.name || line.currencyTypeName) === "Divine Orb") { divineRate = line.primaryValue; break; }
  }

  return { chaos, divineRate };
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === "/snipe/start" || url.pathname === "/snipe/stop") {
      if (request.method === "OPTIONS") return withCors(null, 204);
      if (request.method !== "POST") return withCors(JSON.stringify({ ok: false, error: "method not allowed" }), 405);
      let payload;
      try { payload = await request.json(); } catch (e) { return withCors(JSON.stringify({ ok: false, error: "bad JSON" }), 400); }

      if (url.pathname === "/snipe/start") {
        const token = crypto.randomUUID();
        const id = env.SNIPE_SESSION.idFromName(token);
        const stub = env.SNIPE_SESSION.get(id);
        const doResp = await stub.fetch("https://do/start", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
        });
        const data = await doResp.json();
        if (!data.ok) return withCors(JSON.stringify(data), doResp.status);
        return withCors(JSON.stringify({ ok: true, session: token }), 200);
      } else {
        const token = payload.session;
        if (!token) return withCors(JSON.stringify({ ok: false, error: "missing session" }), 400);
        const id = env.SNIPE_SESSION.idFromName(token);
        const stub = env.SNIPE_SESSION.get(id);
        const doResp = await stub.fetch("https://do/stop", { method: "POST" });
        return withCors(await doResp.text(), doResp.status, "application/json");
      }
    }

    if (url.pathname === "/snipe/poll") {
      if (request.method === "OPTIONS") return withCors(null, 204);
      if (request.method !== "GET") return withCors(JSON.stringify({ ok: false, error: "method not allowed" }), 405);
      const token = url.searchParams.get("session");
      if (!token) return withCors(JSON.stringify({ ok: false, error: "missing session" }), 400);
      const id = env.SNIPE_SESSION.idFromName(token);
      const stub = env.SNIPE_SESSION.get(id);
      const doResp = await stub.fetch("https://do/poll");
      return withCors(await doResp.text(), doResp.status, "application/json");
    }

    if (!url.pathname.startsWith("/ninja/") && url.pathname !== "/watch/compact") {
      // Not an API-proxy path, and no static asset matched (Cloudflare tries
      // that first) — send the visitor to the dashboard's real URL.
      return Response.redirect(url.origin + "/bosses", 302);
    }

    if (request.method === "OPTIONS") return withCors(null, 204);
    if (request.method !== "GET") return withCors("method not allowed", 405, "text/plain");

    const upstream = url.pathname.startsWith("/ninja/")
      ? NINJA_BASE + url.pathname.slice("/ninja".length) + url.search
      : WATCH_BASE + "/compact" + url.search;

    let resp;
    try {
      resp = await fetch(upstream, {
        headers: { "User-Agent": UA },
        cf: { cacheTtl: CACHE_TTL, cacheEverything: true },
      });
    } catch (e) {
      return withCors(JSON.stringify({ error: String(e) }), 502);
    }

    const body = await resp.arrayBuffer();
    return withCors(body, resp.status, resp.headers.get("Content-Type"));
  },
};
