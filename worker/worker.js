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
// SnipeSession — one Durable Object instance per active trade-sniper watch.
//
// Watches a curated list (top 50 poe.ninja unique listing-count + top 50
// poe.ninja unique price, deduped — see fetchTopUniqueWatchlist) rather than
// one named item. Watching ~100 items in real time would need either one
// live-search WebSocket per item (100 concurrent authenticated connections —
// a very heavy, unusual footprint) or one broad "all currently-listed
// uniques" live socket filtered client-side (technically one connection, but
// the live feed for "any unique, no name filter" is enormous — filtering it
// down would mean fetching full details on nearly every unique listed
// site-wide just to check the name, blowing through the fetch-endpoint rate
// limit under real volume). Neither is a good trade-off, so this uses
// **rotation polling** instead: no WebSocket, no POESESSID, no live-search at
// all. `alarm()` re-fires every ~3s, checks the single next item in the
// watchlist (one `search` + one `fetch` call), and moves on — a full lap
// through a 100-item list takes ~5 minutes. That's comfortably inside GGG's
// documented rate limits (search 5/12s, fetch 12/6s — this uses one of each
// per 3s, an order of magnitude under both) and needs no credential at all,
// since plain trade search + fetch work fully unauthenticated (confirmed
// live). Trades instant push for a ~5-minute-average staleness, which is the
// deliberate, user-approved choice here over the two risky alternatives above.
export class SnipeSession {
  constructor(state, env) {
    this.state = state;
    this.env = env;
    this.watchlist = [];       // [{name, chaos}] — chaos is the poe.ninja floor reference price
    this.rotationIndex = 0;
    this.buffer = [];
    this.league = null;
    this.divineRate = null;
    this.exaltedRate = null;
    this.thresholdPct = 20;
    this.lastPolledAt = 0;
  }

  async fetch(request) {
    const url = new URL(request.url);
    if (url.pathname === "/start") return this.handleStart(request);
    if (url.pathname === "/poll") return this.handlePoll();
    if (url.pathname === "/stop") return this.handleStop();
    return new Response(JSON.stringify({ ok: false, error: "not found" }), { status: 404 });
  }

  async handleStart(request) {
    if (this.watchlist.length) {
      return new Response(JSON.stringify({ ok: false, error: "session already running" }), { status: 409 });
    }
    let body;
    try {
      body = await request.json();
    } catch (e) {
      return new Response(JSON.stringify({ ok: false, error: "bad JSON body" }), { status: 400 });
    }
    const { league, thresholdPct } = body;
    if (!league) {
      return new Response(JSON.stringify({ ok: false, error: "need league" }), { status: 400 });
    }
    this.league = league;
    this.thresholdPct = Math.min(90, Math.max(1, Number(thresholdPct) || 20));

    let data;
    try {
      data = await fetchTopUniqueWatchlist(league);
    } catch (e) {
      return new Response(JSON.stringify({ ok: false, error: "poe.ninja lookup failed: " + e }), { status: 502 });
    }
    if (!data.watchlist.length) {
      return new Response(JSON.stringify({ ok: false, error: "no current poe.ninja unique price data found for that league" }), { status: 400 });
    }
    this.watchlist = data.watchlist;
    this.divineRate = data.divineRate;
    this.exaltedRate = data.exaltedRate;
    this.rotationIndex = 0;
    this.buffer = [];
    this.lastPolledAt = Date.now();

    await this.state.storage.setAlarm(Date.now() + 1000);
    return new Response(JSON.stringify({ ok: true, watchlistSize: this.watchlist.length }), { status: 200 });
  }

  async checkOneItem(item) {
    let searchResp;
    try {
      searchResp = await fetch(`https://www.pathofexile.com/api/trade/search/${encodeURIComponent(this.league)}`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "User-Agent": TRADE_UA },
        body: JSON.stringify({ query: { status: { option: "online" }, name: item.name }, sort: { price: "asc" } }),
      });
    } catch (e) { return; }
    if (!searchResp.ok) return;
    const searchData = await searchResp.json();
    if (!searchData.id || !Array.isArray(searchData.result) || !searchData.result.length) return;

    const ids = searchData.result.slice(0, 5).join(",");
    let fetchResp;
    try {
      fetchResp = await fetch(`https://www.pathofexile.com/api/trade/fetch/${ids}?query=${searchData.id}`, {
        headers: { "User-Agent": TRADE_UA },
      });
    } catch (e) { return; }
    if (!fetchResp.ok) return;
    const fetchData = await fetchResp.json();

    for (const r of (fetchData.result || [])) {
      if (!r || !r.listing || !r.listing.price) continue;
      const { amount, currency } = r.listing.price;
      if (amount == null || !currency) continue;
      const chaosEquiv = currency === "chaos" ? amount
        : (currency === "divine" && this.divineRate) ? amount * this.divineRate
        : (currency === "exalted" && this.exaltedRate) ? amount * this.exaltedRate
        : null;
      if (chaosEquiv == null) continue; // unsupported currency for comparison, skip
      if (chaosEquiv > item.chaos * (1 - this.thresholdPct / 100)) continue; // not underpriced enough
      this.buffer.push({
        id: r.id,
        itemName: (r.item && (r.item.name || r.item.typeLine || r.item.baseType)) || item.name,
        icon: (r.item && r.item.icon) || null,
        amount, currency,
        chaosEquiv: Math.round(chaosEquiv * 100) / 100,
        referenceChaos: item.chaos,
        account: (r.listing.account && r.listing.account.name) || "?",
        whisper: r.listing.whisper || "",
        seenAt: Date.now(),
      });
    }
    if (this.buffer.length > 200) this.buffer = this.buffer.slice(-200);
  }

  async handlePoll() {
    this.lastPolledAt = Date.now();
    const running = !!this.watchlist.length;
    const listings = this.buffer;
    this.buffer = [];
    return new Response(JSON.stringify({
      ok: true, running, listings,
      progress: running ? { index: this.rotationIndex, total: this.watchlist.length } : null,
    }), { status: 200 });
  }

  async handleStop() {
    this.watchlist = [];
    await this.state.storage.deleteAlarm();
    return new Response(JSON.stringify({ ok: true }), { status: 200 });
  }

  async alarm() {
    if (!this.watchlist.length) return;
    if (Date.now() - this.lastPolledAt > 10 * 60 * 1000) {
      this.watchlist = []; // no poll in 10+ min — stop rotating, matches handleStop's end state
      return;
    }
    try {
      await this.checkOneItem(this.watchlist[this.rotationIndex]);
    } catch (e) { /* transient failure on this one item — keep the rotation going */ }
    this.rotationIndex = (this.rotationIndex + 1) % this.watchlist.length;
    await this.state.storage.setAlarm(Date.now() + 3000);
  }
}

const TRADE_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36";

// Builds the curated watchlist: top 50 uniques by poe.ninja listingCount
// ("most sold" proxy — GGG publishes no real sales data, listing count is
// the best available liquidity signal) plus top 50 by chaos value ("most
// expensive"), deduped by name. Reference price per name is the FLOOR chaos
// value across all its poe.ninja rows (matches this project's established
// floor-price philosophy — see boss.py's build_index() — since a name like
// "Mageblood" has several rows, one per flask-count variant, and the trade
// search-by-name below returns all of them mixed together).
async function fetchTopUniqueWatchlist(league) {
  const categories = ["UniqueWeapon", "UniqueArmour", "UniqueAccessory", "UniqueJewel", "UniqueFlask"];
  const results = await Promise.all(categories.map(async (cat) => {
    const qs = new URLSearchParams({ league, type: cat });
    const resp = await fetch(`${NINJA_BASE}/stash/current/item/overview?${qs}`, { headers: { "User-Agent": UA } });
    return resp.json();
  }));

  const byName = new Map(); // name -> {chaos, listingCount}
  for (const data of results) {
    for (const line of (data.lines || [])) {
      if (!line.name) continue;
      const chaos = line.chaosValue ?? line.chaosEquivalent;
      if (chaos == null) continue;
      const count = line.listingCount ?? line.count ?? 0;
      const cur = byName.get(line.name);
      if (!cur) byName.set(line.name, { chaos, listingCount: count });
      else {
        if (chaos < cur.chaos) cur.chaos = chaos;
        cur.listingCount += count;
      }
    }
  }

  const all = Array.from(byName.entries()).map(([name, v]) => ({ name, chaos: v.chaos, listingCount: v.listingCount }));
  const byMostSold = [...all].sort((a, b) => b.listingCount - a.listingCount).slice(0, 50);
  const byMostExpensive = [...all].sort((a, b) => b.chaos - a.chaos).slice(0, 50);

  const watchlistMap = new Map();
  for (const it of [...byMostSold, ...byMostExpensive]) watchlistMap.set(it.name, it.chaos);
  const watchlist = Array.from(watchlistMap.entries()).map(([name, chaos]) => ({ name, chaos }));

  let divineRate = null, exaltedRate = null;
  const curResp = await fetch(`${NINJA_BASE}/exchange/current/overview?${new URLSearchParams({ league, type: "Currency" })}`, { headers: { "User-Agent": UA } });
  const curData = await curResp.json();
  const idToMeta = {};
  for (const it of (curData.items || [])) if (it.id) idToMeta[it.id] = it;
  for (const line of (curData.lines || [])) {
    const nm = (idToMeta[line.id] || {}).name || line.currencyTypeName;
    if (nm === "Divine Orb") divineRate = line.primaryValue;
    if (nm === "Exalted Orb") exaltedRate = line.primaryValue;
  }

  return { watchlist, divineRate, exaltedRate };
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
