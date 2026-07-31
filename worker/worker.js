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
// **rotation polling** instead: no WebSocket, no live-search at all.
// `alarm()` re-fires every ~3s, checks the single next item in the
// watchlist (one `search` + one `fetch` call), and moves on — a full lap
// through a 100-item list takes ~5 minutes. That's comfortably inside GGG's
// documented rate limits (search 5/12s, fetch 12/6s — this uses one of each
// per 3s, an order of magnitude under both), and plain trade search + fetch
// work fully unauthenticated (confirmed live) — so a credential was never
// required. Trades instant push for a ~5-minute-average staleness, which is
// the deliberate, user-approved choice here over the two risky alternatives
// above. POESESSID is still optional (see `poesessid` below): under heavy
// personal use GGG's anonymous rate-limit bucket can get exhausted (a real
// 429 seen in testing), and supplying your own account's session cookie may
// use a separate, more generous bucket — never required, never persisted to
// storage, only held in this instance's memory for the life of the watch.
export class SnipeSession {
  constructor(state, env) {
    this.state = state;
    this.env = env;
    this.watchlist = [];       // [{name, chaos, icon}] — chaos is the poe.ninja floor reference price
    this.rotationIndex = 0;
    this.buffer = [];
    this.checkLog = [];        // rolling log of every item checked this rotation — see checkOneItem
    this.currentItem = null;   // {name, icon} of the item the rotation is checking right now, else null
    this.league = null;
    this.divineRate = null;
    this.exaltedRate = null;
    this.thresholdPct = 20;
    this.lastPolledAt = 0;
    this.rateLimitedUntil = 0;  // ms epoch; while in the future, alarm() makes zero trade-API calls
    this.poesessid = null;      // optional; never written to storage, never echoed in any response
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
    const { league, thresholdPct, minPrice, maxPrice, priceUnit, poesessid } = body;
    if (!league) {
      return new Response(JSON.stringify({ ok: false, error: "need league" }), { status: 400 });
    }
    this.league = league;
    this.thresholdPct = Math.min(90, Math.max(1, Number(thresholdPct) || 20));
    // In-memory only for this DO instance's lifetime — never storage.put(),
    // never included in any /poll or /start response, never logged.
    this.poesessid = (typeof poesessid === "string" && poesessid.trim()) ? poesessid.trim() : null;

    let data;
    try {
      data = await fetchTopUniqueWatchlist(league, {
        minPrice: minPrice != null && minPrice !== "" ? Number(minPrice) : null,
        maxPrice: maxPrice != null && maxPrice !== "" ? Number(maxPrice) : null,
        priceUnit: priceUnit === "divine" ? "divine" : "chaos",
      });
    } catch (e) {
      return new Response(JSON.stringify({ ok: false, error: "poe.ninja lookup failed: " + e }), { status: 502 });
    }
    if (!data.watchlist.length) {
      return new Response(JSON.stringify({ ok: false, error: "no poe.ninja unique items matched (check the league name, or widen the price range)" }), { status: 400 });
    }
    this.watchlist = data.watchlist;
    this.divineRate = data.divineRate;
    this.exaltedRate = data.exaltedRate;
    this.rotationIndex = 0;
    this.buffer = [];
    this.checkLog = [];
    this.currentItem = null;
    this.rateLimitedUntil = 0;
    this.lastPolledAt = Date.now();

    await this.state.storage.setAlarm(Date.now() + 1000);
    return new Response(JSON.stringify({ ok: true, watchlistSize: this.watchlist.length }), { status: 200 });
  }

  // Checks one watchlist item and appends one entry to `checkLog` regardless
  // of outcome (found nothing, found listings but none underpriced, or found
  // an underpriced hit) — this is what lets the frontend show "which item is
  // being checked, its live price vs. the poe.ninja reference price" instead
  // of only ever surfacing actual hits.
  async checkOneItem(item) {
    const logEntry = {
      name: item.name, icon: item.icon || null,
      referenceChaos: item.chaos, checkedAt: Date.now(),
      listingsSeen: 0, cheapestAmount: null, cheapestCurrency: null, cheapestChaosEquiv: null,
      underpriced: false, debug: null,
      variantCount: item.variants ? item.variants.length : null,
    };
    const tradeHeaders = { "User-Agent": TRADE_UA };
    if (this.poesessid) tradeHeaders["Cookie"] = `POESESSID=${this.poesessid}`;
    let searchResp;
    try {
      searchResp = await fetch(`https://www.pathofexile.com/api/trade/search/${encodeURIComponent(this.league)}`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...tradeHeaders },
        body: JSON.stringify({ query: { status: { option: "online" }, name: item.name }, sort: { price: "asc" } }),
      });
    } catch (e) { logEntry.debug = `search request threw: ${e}`; this.pushLog(logEntry); return; }
    if (!searchResp.ok) {
      let snippet = "";
      try { snippet = (await searchResp.text()).slice(0, 200); } catch (e) {}
      if (searchResp.status === 429) {
        const waitSec = parseRetrySeconds(searchResp, snippet);
        this.rateLimitedUntil = Date.now() + waitSec * 1000;
        logEntry.debug = `rate limited by pathofexile.com/trade — pausing the whole rotation for ${waitSec}s (no more requests until then)`;
      } else {
        logEntry.debug = `search HTTP ${searchResp.status} ${searchResp.statusText}: ${snippet}`;
      }
      this.pushLog(logEntry); return;
    }
    const searchData = await searchResp.json();
    if (!searchData.id || !Array.isArray(searchData.result) || !searchData.result.length) {
      logEntry.debug = searchData.error
        ? `search error: ${JSON.stringify(searchData.error)}`
        : "search OK, 0 listings currently online for this item";
      this.pushLog(logEntry); return;
    }

    const ids = searchData.result.slice(0, 5).join(",");
    let fetchResp;
    try {
      fetchResp = await fetch(`https://www.pathofexile.com/api/trade/fetch/${ids}?query=${searchData.id}`, {
        headers: tradeHeaders,
      });
    } catch (e) { logEntry.debug = `fetch request threw: ${e}`; this.pushLog(logEntry); return; }
    if (!fetchResp.ok) {
      let snippet = "";
      try { snippet = (await fetchResp.text()).slice(0, 200); } catch (e) {}
      if (fetchResp.status === 429) {
        const waitSec = parseRetrySeconds(fetchResp, snippet);
        this.rateLimitedUntil = Date.now() + waitSec * 1000;
        logEntry.debug = `rate limited by pathofexile.com/trade — pausing the whole rotation for ${waitSec}s (no more requests until then)`;
      } else {
        logEntry.debug = `fetch HTTP ${fetchResp.status} ${fetchResp.statusText}: ${snippet}`;
      }
      this.pushLog(logEntry); return;
    }
    const fetchData = await fetchResp.json();
    if (fetchData.error) logEntry.debug = `fetch error: ${JSON.stringify(fetchData.error)}`;

    // Same searchId reopens this exact name-filtered query on the real trade
    // site — since the query only ever matches this one item name, the page
    // it lands on shows just this item's listings, not a generic browse.
    const tradeUrl = `https://www.pathofexile.com/trade/search/${encodeURIComponent(this.league)}/${searchData.id}`;

    for (const r of (fetchData.result || [])) {
      if (!r || !r.listing || !r.listing.price) continue;
      const { amount, currency } = r.listing.price;
      if (amount == null || !currency) continue;
      const chaosEquiv = currency === "chaos" ? amount
        : (currency === "divine" && this.divineRate) ? amount * this.divineRate
        : (currency === "exalted" && this.exaltedRate) ? amount * this.exaltedRate
        : null;
      if (chaosEquiv == null) continue; // unsupported currency for comparison, skip
      logEntry.listingsSeen++;
      if (logEntry.cheapestChaosEquiv == null || chaosEquiv < logEntry.cheapestChaosEquiv) {
        logEntry.cheapestChaosEquiv = Math.round(chaosEquiv * 100) / 100;
        logEntry.cheapestAmount = amount;
        logEntry.cheapestCurrency = currency;
      }

      // item.chaos is the FLOOR across every poe.ninja-priced variant of this
      // name (e.g. Mageblood's cheapest "2 Flasks" tier) — safe as a
      // fallback (never produces a false "underpriced" claim) but blind to
      // real deals on pricier tiers. When this name has known variants, try
      // to identify which one THIS listing actually is by matching its real
      // mod text against each variant's discriminator (see
      // findDiscriminator), and compare against that variant's own price.
      let referenceChaos = item.chaos;
      let variantLabel = null, variantUncertain = false;
      if (item.variants) {
        const modTexts = [
          ...((r.item && r.item.implicitMods) || []),
          ...((r.item && r.item.explicitMods) || []),
        ].map((m) => m.description);
        const matches = item.variants.filter((v) => v.discriminator && modTexts.includes(v.discriminator));
        if (matches.length === 1) {
          referenceChaos = matches[0].chaos;
          variantLabel = matches[0].label;
        } else {
          variantUncertain = true; // no reliable discriminator, or ambiguous — fall back to the safe floor above
        }
      }

      if (chaosEquiv > referenceChaos * (1 - this.thresholdPct / 100)) continue; // not underpriced enough
      logEntry.underpriced = true;
      this.buffer.push({
        id: r.id,
        itemName: (r.item && (r.item.name || r.item.typeLine || r.item.baseType)) || item.name,
        icon: (r.item && r.item.icon) || item.icon || null,
        amount, currency,
        chaosEquiv: Math.round(chaosEquiv * 100) / 100,
        referenceChaos,
        variantLabel,
        variantUncertain,
        account: (r.listing.account && r.listing.account.name) || "?",
        tradeUrl,
        seenAt: Date.now(),
      });
    }
    if (this.buffer.length > 200) this.buffer = this.buffer.slice(-200);
    if (!logEntry.debug && logEntry.listingsSeen === 0) {
      logEntry.debug = `fetch returned ${(fetchData.result || []).length} result(s), none had a usable price/currency`;
    }
    this.pushLog(logEntry);
  }

  pushLog(entry) {
    this.checkLog.push(entry);
    if (this.checkLog.length > 50) this.checkLog = this.checkLog.slice(-50);
  }

  async handlePoll() {
    this.lastPolledAt = Date.now();
    const running = !!this.watchlist.length;
    const listings = this.buffer;
    this.buffer = [];
    const checks = this.checkLog;
    this.checkLog = [];
    return new Response(JSON.stringify({
      ok: true, running, listings, checks,
      checking: this.currentItem,
      rateLimitedUntil: this.rateLimitedUntil > Date.now() ? this.rateLimitedUntil : null,
      progress: running ? { index: this.rotationIndex, total: this.watchlist.length } : null,
    }), { status: 200 });
  }

  async handleStop() {
    this.watchlist = [];
    this.currentItem = null;
    this.rateLimitedUntil = 0;
    this.poesessid = null;
    await this.state.storage.deleteAlarm();
    return new Response(JSON.stringify({ ok: true }), { status: 200 });
  }

  async alarm() {
    if (!this.watchlist.length) return;
    if (Date.now() - this.lastPolledAt > 10 * 60 * 1000) {
      this.watchlist = []; // no poll in 10+ min — stop rotating, matches handleStop's end state
      this.currentItem = null;
      this.poesessid = null;
      return;
    }
    // While rate-limited, make ZERO trade-API calls until the cooldown clears
    // — retrying every 3s during the ban window would just keep re-triggering
    // it. Check back periodically (capped at 30s) only to keep the
    // 10-minute-idle abandonment check above alive.
    if (Date.now() < this.rateLimitedUntil) {
      this.currentItem = null;
      await this.state.storage.setAlarm(Math.min(this.rateLimitedUntil + 500, Date.now() + 30000));
      return;
    }
    const item = this.watchlist[this.rotationIndex];
    this.currentItem = { name: item.name, icon: item.icon || null };
    try {
      await this.checkOneItem(item);
    } catch (e) { /* transient failure on this one item — keep the rotation going */ }
    this.currentItem = null;
    if (Date.now() < this.rateLimitedUntil) {
      // just entered a rate-limit ban from the check above — don't advance
      // rotationIndex, so the same item gets re-checked first once cleared
      await this.state.storage.setAlarm(Math.min(this.rateLimitedUntil + 500, Date.now() + 30000));
      return;
    }
    this.rotationIndex = (this.rotationIndex + 1) % this.watchlist.length;
    await this.state.storage.setAlarm(Date.now() + 3000);
  }
}

function parseRetrySeconds(resp, bodyText) {
  const header = resp.headers.get("Retry-After");
  if (header && !isNaN(Number(header))) return Number(header);
  const m = bodyText && bodyText.match(/wait (\d+) seconds/i);
  if (m) return Number(m[1]);
  return 60; // conservative fallback if PoE didn't tell us how long
}

const TRADE_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36";

// A discriminator is a mod line (explicit preferred, then implicit) present
// on `row` but on NONE of the other poe.ninja rows for the same item name —
// the one line that reliably identifies this specific variant. Returns null
// (unmatchable) when no such line exists, or when the only candidate is a
// "(min-max)" rolled-range template: a live trade listing shows the
// concrete rolled number ("+37 to Strength"), which won't literal-match the
// template text, so that can't be used as a reliable discriminator.
function findDiscriminator(row, allRows) {
  const otherLines = new Set();
  for (const o of allRows) {
    if (o === row) continue;
    for (const m of [...(o.explicitModifiers || []), ...(o.implicitModifiers || [])]) otherLines.add(m.text);
  }
  const ownLines = [...(row.explicitModifiers || []), ...(row.implicitModifiers || [])].map((m) => m.text);
  for (const text of ownLines) {
    if (otherLines.has(text)) continue;
    if (/\(\d+(\.\d+)?-\d+(\.\d+)?\)/.test(text)) continue; // rolled range, not reliably matchable
    return text;
  }
  return null;
}

// Builds the curated watchlist: top 50 uniques by poe.ninja listingCount
// ("most sold" proxy — GGG publishes no real sales data, listing count is
// the best available liquidity signal) plus top 50 by chaos value ("most
// expensive"), deduped by name, optionally restricted to a chaos price
// range via `priceFilter` (directly shrinks the watchlist — the main lever
// against trade-API rate limits/timeouts under heavy use).
//
// Reference price per name is still the FLOOR chaos value across all its
// poe.ninja rows (matches this project's established floor-price philosophy
// — see boss.py's build_index()), used as the safe fallback. But for names
// poe.ninja splits into multiple *priced* variants (e.g. "Mageblood" has a
// "2/3/4/5 Flasks" row each, worth wildly different amounts — see
// findDiscriminator's docs above), the floor alone is misleading: a live
// listing could be any tier. `variants` captures each tier's own price plus
// its discriminator, so checkOneItem() can identify which tier a specific
// live listing actually is and compare against ITS price instead.
async function fetchTopUniqueWatchlist(league, priceFilter) {
  const categories = ["UniqueWeapon", "UniqueArmour", "UniqueAccessory", "UniqueJewel", "UniqueFlask"];
  const results = await Promise.all(categories.map(async (cat) => {
    const qs = new URLSearchParams({ league, type: cat });
    const resp = await fetch(`${NINJA_BASE}/stash/current/item/overview?${qs}`, { headers: { "User-Agent": UA } });
    return resp.json();
  }));

  // Currency rates are needed up front now — a "divine" priceFilter has to
  // be converted to chaos before it can filter poe.ninja's chaos-valued rows.
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

  let minChaos = null, maxChaos = null;
  if (priceFilter) {
    const unitRate = priceFilter.priceUnit === "divine" ? divineRate : 1;
    if (unitRate) {
      if (priceFilter.minPrice != null && !isNaN(priceFilter.minPrice)) minChaos = priceFilter.minPrice * unitRate;
      if (priceFilter.maxPrice != null && !isNaN(priceFilter.maxPrice)) maxChaos = priceFilter.maxPrice * unitRate;
    }
  }

  const byName = new Map(); // name -> raw poe.ninja rows for that name (kept whole, not pre-collapsed)
  for (const data of results) {
    for (const line of (data.lines || [])) {
      if (!line.name) continue;
      const chaos = line.chaosValue ?? line.chaosEquivalent;
      if (chaos == null) continue;
      if (!byName.has(line.name)) byName.set(line.name, []);
      byName.get(line.name).push(line);
    }
  }

  let all = [];
  for (const [name, rows] of byName) {
    const chaosOf = (l) => l.chaosValue ?? l.chaosEquivalent;
    const floorChaos = Math.min(...rows.map(chaosOf));
    const listingCount = rows.reduce((s, l) => s + (l.listingCount ?? l.count ?? 0), 0);
    const icon = (rows.find((l) => l.icon) || {}).icon || null;
    const variants = rows.length > 1
      ? rows.map((row) => ({ label: row.variant || null, chaos: chaosOf(row), discriminator: findDiscriminator(row, rows) }))
      : null;
    all.push({ name, chaos: floorChaos, listingCount, icon, variants });
  }

  if (minChaos != null) all = all.filter((it) => it.chaos >= minChaos);
  if (maxChaos != null) all = all.filter((it) => it.chaos <= maxChaos);

  const byMostSold = [...all].sort((a, b) => b.listingCount - a.listingCount).slice(0, 50);
  const byMostExpensive = [...all].sort((a, b) => b.chaos - a.chaos).slice(0, 50);

  const watchlistMap = new Map();
  for (const it of [...byMostSold, ...byMostExpensive]) {
    watchlistMap.set(it.name, { chaos: it.chaos, icon: it.icon, variants: it.variants });
  }
  const watchlist = Array.from(watchlistMap.entries())
    .map(([name, v]) => ({ name, chaos: v.chaos, icon: v.icon, variants: v.variants }));

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
        return withCors(JSON.stringify({ ok: true, session: token, watchlistSize: data.watchlistSize }), 200);
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
