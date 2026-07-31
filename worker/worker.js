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
//   GET /forum/patch-notes?game=poe1|poe2 -> pathofexile.com forum (see fetchPatchNotes)
//   POST /snipe/start, GET /snipe/poll, POST /snipe/stop -> SnipeSession DO
//   anything else (root, typos, old links) -> 302 redirect to /home

const NINJA_BASE = "https://poe.ninja/poe1/api/economy";
const WATCH_BASE = "https://api.poe.watch";
const UA = "boss-dashboard-static-proxy/1.0 (contact: you@example.com)";
const CACHE_TTL = 300; // seconds; matches boss.py's CACHE_TTL

// Both games' patch notes live on the same classic server-rendered
// pathofexile.com forum software (confirmed live) — pathofexile2.com itself
// is a client-rendered SPA with no scrapeable HTML, a dead end ruled out
// before picking these URLs. PoE2's forum is a sub-forum ("Early Access Patch
// Notes") of the same pathofexile.com domain, id 2212 — found via the forum
// index page, not guessed. Mirrors boss.py's _get_patch_notes()/
// PATCH_NOTES_URLS — same "two independent implementations, must be kept in
// sync" caveat this repo already documents for FETCH_ENGINE/OLD_FETCH_DATA.
const PATCH_NOTES_URLS = {
  poe1: "https://www.pathofexile.com/forum/view-forum/patch-notes",
  poe2: "https://www.pathofexile.com/forum/view-forum/2212",
};
// The forum's WAF treats requests without a real browser User-Agent
// differently than the plain JSON APIs do — confirmed live, so this uses a
// real Chrome UA string rather than the plain UA constant above.
const FORUM_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36";

async function fetchPatchNotes(game) {
  const url = PATCH_NOTES_URLS[game];
  const resp = await fetch(url, { headers: { "User-Agent": FORUM_UA }, cf: { cacheTtl: CACHE_TTL, cacheEverything: true } });
  const html = await resp.text();
  const items = [];
  const titleRe = /<div class="title">\s*<a href="(\/forum\/view-thread\/\d+)">\s*([^<]+?)\s*<\/a>/gs;
  let m;
  while ((m = titleRe.exec(html)) !== null && items.length < 8) {
    const threadPath = m[1];
    const title = m[2].trim();
    if (!title || !/^\d/.test(title)) continue; // skip pinned non-patch threads (e.g. "Code of Conduct")
    // A wide window — the "postBy" block (which holds post_date, a <span> not
    // a <div>) can sit far past the title on a heavily-paginated thread (each
    // page-number link adds ~60 chars; an 8-page thread pushed it past 600
    // chars in testing, so 3000 is a safe margin).
    const window = html.slice(titleRe.lastIndex, titleRe.lastIndex + 3000);
    const dateM = /class="post_date">([^<]*)<\/span>/.exec(window);
    const date = dateM ? dateM[1].trim().replace(/^,\s*/, "") : "";
    items.push({ title, url: "https://www.pathofexile.com" + threadPath, date });
  }
  // One extra request per item to fetch its own thread page for a real
  // snippet — fired concurrently via Promise.all (mirrors boss.py's
  // ThreadPoolExecutor for the same reason: 8 sequential forum fetches would
  // make the whole page wait several seconds for no benefit).
  const snippets = await Promise.all(items.map((it) => fetchPatchSnippet(it.url)));
  items.forEach((it, i) => { it.snippet = snippets[i]; });
  return items;
}

const PATCH_BODY_RE = /<div class="contentStart"><\/div>\s*<div class="content">([\s\S]*?)(?:<div class="signature"|<\/td>)/;
const SNIPPET_LEN = 400;

// Plain-text excerpt of a patch note thread's own first post — a real
// (truncated) excerpt of the actual patch text, not a generated summary (no
// LLM available here). Best-effort: any failure just means that one item's
// popup has no snippet, never blocks the rest of the listing.
async function fetchPatchSnippet(threadUrl) {
  try {
    const resp = await fetch(threadUrl, { headers: { "User-Agent": FORUM_UA }, cf: { cacheTtl: CACHE_TTL, cacheEverything: true } });
    const html = await resp.text();
    const m = PATCH_BODY_RE.exec(html);
    if (!m) return "";
    // The real post body can be 10,000+ chars — only look at a generous
    // prefix, since we're truncating to SNIPPET_LEN anyway.
    let text = m[1].slice(0, SNIPPET_LEN * 4).replace(/<[^>]+>/g, " ");
    text = text.replace(/&#(\d+);/g, (_, n) => String.fromCodePoint(Number(n)))
               .replace(/&amp;/g, "&").replace(/&quot;/g, '"').replace(/&#039;/g, "'")
               .replace(/&lt;/g, "<").replace(/&gt;/g, ">");
    text = text.replace(/\s+/g, " ").trim();
    if (text.length > SNIPPET_LEN) {
      const cut = text.slice(0, SNIPPET_LEN);
      text = cut.slice(0, cut.lastIndexOf(" ")) + "…";
    }
    return text;
  } catch (e) {
    return "";
  }
}

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
// `alarm()` re-fires every ~6s, checks the single next item in the
// watchlist — two search+fetch pairs, see checkOneItem's general +
// unidentified-only passes — and moves on — a full lap through a 100-item
// list takes ~10 minutes. That's comfortably inside GGG's documented rate
// limits (search 5/12s, fetch 12/6s — this uses two of each per 6s), and
// plain trade search + fetch work fully unauthenticated (confirmed live) —
// so a credential was never required. Trades instant push for a
// ~10-minute-average staleness, which is the deliberate, user-approved
// choice here over the two risky alternatives above. POESESSID is still
// optional (see `poesessid` below): under heavy personal use GGG's
// anonymous rate-limit bucket can get exhausted (a real 429 seen in
// testing), and supplying your own account's session cookie may use a
// separate, more generous bucket — never required, never persisted to
// storage, only held in this instance's memory for the life of the watch.
// `rotationIntervalMs` starts at 6s but grows (capped, see applyRateLimit())
// every time a 429 happens — GGG's own bans escalate on repeat violations
// shortly after a previous one clears, so resuming at the original fast pace
// right when a ban expires reliably re-triggers a longer one; this session's
// pace permanently backs off instead of oscillating back into that loop.
// Fields whose loss breaks the rotation, persisted via persist()/rehydrated
// via blockConcurrencyWhile below. Deliberately excludes: `poesessid` (must
// never survive to storage at all, see its own comment), `buffer`/
// `checkLog`/`currentItem` (transient display-only queues capped at 200/50
// entries and drained on every poll — losing a few isn't a correctness bug,
// and persisting them on every single check would add needless storage
// writes to the hot path).
const PERSISTED_KEYS = [
  "watchlist", "rotationIndex", "league", "divineRate", "exaltedRate",
  "thresholdPct", "lastPolledAt", "rateLimitedUntil", "consecutiveBans", "rotationIntervalMs",
];

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
    this.consecutiveBans = 0;   // resets to 0 on any check that doesn't itself get 429'd
    this.rotationIntervalMs = 6000; // grows (capped) after each ban — see checkOneItem's 429 handling

    // Durable Objects can be evicted from memory between requests when idle
    // — Cloudflare's own hibernation, not something this code controls. This
    // was a real, reproduced bug: a session sitting on a long rate-limit
    // cooldown (its alarm just re-checks every ~30s during the wait, see
    // alarm() below) got silently wiped mid-cooldown; the constructor
    // re-ran with the defaults above, and since alarm()'s first line
    // no-ops when watchlist is empty, the rotation died permanently with
    // no error anywhere — `running` just silently went false. This makes
    // every fetch()/alarm() wait for storage-backed state to be restored
    // first, so no request can observe a half-evicted session.
    this.state.blockConcurrencyWhile(async () => {
      const stored = await this.state.storage.get(PERSISTED_KEYS);
      for (const key of PERSISTED_KEYS) {
        if (stored.has(key)) this[key] = stored.get(key);
      }
    });
  }

  async persist() {
    const snapshot = {};
    for (const key of PERSISTED_KEYS) snapshot[key] = this[key];
    await this.state.storage.put(snapshot);
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
    this.consecutiveBans = 0;
    this.rotationIntervalMs = 6000;
    this.lastPolledAt = Date.now();

    await this.persist();
    await this.state.storage.setAlarm(Date.now() + 1000);
    return new Response(JSON.stringify({ ok: true, watchlistSize: this.watchlist.length }), { status: 200 });
  }

  // Checks one watchlist item and appends one entry to `checkLog` regardless
  // of outcome (found nothing, found listings but none underpriced, or found
  // an underpriced hit) — this is what lets the frontend show "which item is
  // being checked, its live price vs. the poe.ninja reference price" instead
  // of only ever surfacing actual hits.
  // Runs two trade queries per item, not one: the general pool (any
  // identification state) plus a second, unidentified-only pass. An
  // unidentified item hasn't had its random mod rolls revealed yet, so its
  // price reflects the base item alone rather than whatever specific roll a
  // seller happened to get — a much more stable/"solid" reference than the
  // general pool, which mixes in every possible roll from far below to far
  // above a typical listing. The general query's cheapest-5 cutoff can also
  // bury a cheap unidentified listing behind pricier identified ones, so
  // querying unidentified-only surfaces those directly. This doubles the
  // request count per item (2 searches + 2 fetches instead of 1+1), so
  // rotationIntervalMs's base was doubled (3s -> 6s, see the constructor)
  // to keep the real per-second request rate roughly where it was.
  async checkOneItem(item) {
    const logEntry = {
      name: item.name, icon: item.icon || null,
      referenceChaos: item.chaos, referenceWatchChaos: item.watchChaos, checkedAt: Date.now(),
      listingsSeen: 0, cheapestAmount: null, cheapestCurrency: null, cheapestChaosEquiv: null, cheapestIdentified: null,
      underpriced: false, debug: null,
      variantCount: item.variants ? item.variants.length : null,
    };
    const tradeHeaders = { "User-Agent": TRADE_UA };
    if (this.poesessid) tradeHeaders["Cookie"] = `POESESSID=${this.poesessid}`;
    const seenIds = new Set(); // dedupes hits the two passes both surface

    const bannedOnGeneralPass = await this.runTradeQuery(item, tradeHeaders, logEntry, seenIds, false);
    if (!bannedOnGeneralPass) {
      await this.runTradeQuery(item, tradeHeaders, logEntry, seenIds, true);
    }

    if (this.buffer.length > 200) this.buffer = this.buffer.slice(-200);
    if (!logEntry.debug && logEntry.listingsSeen === 0) {
      logEntry.debug = "no usable listings found across either pass";
    }
    this.pushLog(logEntry);
  }

  // One search+fetch round trip for `item`. `unidentifiedOnly` picks which
  // of the two passes described above this is; mutates `logEntry` in place
  // and pushes any underpriced hit straight into `this.buffer` (skipping ids
  // already in `seenIds`, since the general pass and the unidentified-only
  // pass can both surface the same listing). Returns true if this call got
  // 429'd, so checkOneItem can skip firing the second pass into the same ban.
  async runTradeQuery(item, tradeHeaders, logEntry, seenIds, unidentifiedOnly) {
    const query = { status: { option: "securable" }, name: item.name, stats: [{ type: "and", filters: [] }] };
    if (unidentifiedOnly) query.filters = { misc_filters: { filters: { identified: { option: "false" } } } };

    let searchResp;
    try {
      searchResp = await fetch(`https://www.pathofexile.com/api/trade/search/${encodeURIComponent(this.league)}`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...tradeHeaders },
        body: JSON.stringify({ query, sort: { price: "asc" } }),
      });
    } catch (e) { logEntry.debug = logEntry.debug || `search request threw: ${e}`; return false; }
    if (!searchResp.ok) {
      let snippet = "";
      try { snippet = (await searchResp.text()).slice(0, 200); } catch (e) {}
      if (searchResp.status === 429) {
        const waitSec = parseRetrySeconds(searchResp, snippet);
        const paddedWaitSec = this.applyRateLimit(waitSec);
        logEntry.debug = `rate limited by pathofexile.com/trade (ban #${this.consecutiveBans} in a row) — server said wait ${waitSec}s, pausing ${paddedWaitSec}s with a safety margin, rotation slowed to one check every ${Math.round(this.rotationIntervalMs / 1000)}s`;
        return true;
      }
      logEntry.debug = logEntry.debug || `search HTTP ${searchResp.status} ${searchResp.statusText}: ${snippet}`;
      return false;
    }
    const searchData = await searchResp.json();
    if (!searchData.id || !Array.isArray(searchData.result) || !searchData.result.length) {
      if (!logEntry.debug) {
        logEntry.debug = searchData.error
          ? `search error: ${JSON.stringify(searchData.error)}`
          : `search OK, 0 ${unidentifiedOnly ? "unidentified " : ""}listings currently online for this item`;
      }
      return false;
    }

    const ids = searchData.result.slice(0, 5).join(",");
    let fetchResp;
    try {
      fetchResp = await fetch(`https://www.pathofexile.com/api/trade/fetch/${ids}?query=${searchData.id}`, {
        headers: tradeHeaders,
      });
    } catch (e) { logEntry.debug = logEntry.debug || `fetch request threw: ${e}`; return false; }
    if (!fetchResp.ok) {
      let snippet = "";
      try { snippet = (await fetchResp.text()).slice(0, 200); } catch (e) {}
      if (fetchResp.status === 429) {
        const waitSec = parseRetrySeconds(fetchResp, snippet);
        const paddedWaitSec = this.applyRateLimit(waitSec);
        logEntry.debug = `rate limited by pathofexile.com/trade (ban #${this.consecutiveBans} in a row) — server said wait ${waitSec}s, pausing ${paddedWaitSec}s with a safety margin, rotation slowed to one check every ${Math.round(this.rotationIntervalMs / 1000)}s`;
        return true;
      }
      logEntry.debug = logEntry.debug || `fetch HTTP ${fetchResp.status} ${fetchResp.statusText}: ${snippet}`;
      return false;
    }
    // Both calls succeeded without a 429 — this streak of bans is over.
    this.consecutiveBans = 0;
    const fetchData = await fetchResp.json();
    if (fetchData.error && !logEntry.debug) logEntry.debug = `fetch error: ${JSON.stringify(fetchData.error)}`;

    // Same searchId reopens this exact query on the real trade site — since
    // the query only ever matches this one item name (plus the identified
    // filter on the second pass), the page it lands on shows just those
    // listings, not a generic browse.
    const tradeUrl = `https://www.pathofexile.com/trade/search/${encodeURIComponent(this.league)}/${searchData.id}`;

    for (const r of (fetchData.result || [])) {
      if (!r || !r.listing || !r.listing.price || seenIds.has(r.id)) continue;
      const { amount, currency } = r.listing.price;
      if (amount == null || !currency) continue;
      const chaosEquiv = currency === "chaos" ? amount
        : (currency === "divine" && this.divineRate) ? amount * this.divineRate
        : (currency === "exalted" && this.exaltedRate) ? amount * this.exaltedRate
        : null;
      if (chaosEquiv == null) continue; // unsupported currency for comparison, skip
      seenIds.add(r.id);
      logEntry.listingsSeen++;
      if (logEntry.cheapestChaosEquiv == null || chaosEquiv < logEntry.cheapestChaosEquiv) {
        logEntry.cheapestChaosEquiv = Math.round(chaosEquiv * 100) / 100;
        logEntry.cheapestAmount = amount;
        logEntry.cheapestCurrency = currency;
        logEntry.cheapestIdentified = (r.item && typeof r.item.identified === "boolean") ? r.item.identified : null;
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
        unidentified: (r.item && typeof r.item.identified === "boolean") ? !r.item.identified : !!unidentifiedOnly,
        account: (r.listing.account && r.listing.account.name) || "?",
        tradeUrl,
        seenAt: Date.now(),
      });
    }
    return false;
  }

  pushLog(entry) {
    this.checkLog.push(entry);
    if (this.checkLog.length > 50) this.checkLog = this.checkLog.slice(-50);
  }

  // PoE's trade API escalates ban length on repeat violations shortly after
  // a previous ban clears (confirmed in testing: resuming right at the old
  // ban's expiry and retrying at the normal pace re-triggered a fresh ban
  // immediately). So a 429 here does three things instead of just trusting
  // the server's stated wait verbatim: pads it with a growing safety margin
  // that scales with how many bans have happened back-to-back, slows the
  // whole rotation's normal per-item pace (permanently for this session —
  // reverting it after one clean check risks oscillating straight back into
  // the same escalation), and tracks the streak so `debug` can show the user
  // it's actually adapting rather than looping silently.
  applyRateLimit(waitSec) {
    this.consecutiveBans++;
    const paddedWaitSec = Math.round(waitSec * (1 + 0.5 * Math.min(this.consecutiveBans - 1, 6)) + 10);
    this.rateLimitedUntil = Date.now() + paddedWaitSec * 1000;
    this.rotationIntervalMs = Math.min(this.rotationIntervalMs * 1.5, 20000);
    return paddedWaitSec;
  }

  async handlePoll() {
    this.lastPolledAt = Date.now();
    await this.persist(); // lastPolledAt drives the 10-min idle-abandon check — must survive eviction
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
    await this.persist();
    await this.state.storage.deleteAlarm();
    return new Response(JSON.stringify({ ok: true }), { status: 200 });
  }

  async alarm() {
    if (!this.watchlist.length) return;
    if (Date.now() - this.lastPolledAt > 10 * 60 * 1000) {
      this.watchlist = []; // no poll in 10+ min — stop rotating, matches handleStop's end state
      this.currentItem = null;
      this.poesessid = null;
      await this.persist();
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
      await this.persist();
      await this.state.storage.setAlarm(Math.min(this.rateLimitedUntil + 500, Date.now() + 30000));
      return;
    }
    this.rotationIndex = (this.rotationIndex + 1) % this.watchlist.length;
    await this.persist();
    await this.state.storage.setAlarm(Date.now() + this.rotationIntervalMs);
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
  const [results, watchUnided] = await Promise.all([
    Promise.all(categories.map(async (cat) => {
      const qs = new URLSearchParams({ league, type: cat });
      const resp = await fetch(`${NINJA_BASE}/stash/current/item/overview?${qs}`, { headers: { "User-Agent": UA } });
      return resp.json();
    })),
    fetchWatchUnidentifiedPrices(league),
  ]);

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
    const watchChaos = watchUnided.has(name) ? watchUnided.get(name) : null;
    all.push({ name, chaos: floorChaos, listingCount, icon, variants, watchChaos });
  }

  if (minChaos != null) all = all.filter((it) => it.chaos >= minChaos);
  if (maxChaos != null) all = all.filter((it) => it.chaos <= maxChaos);

  const byMostSold = [...all].sort((a, b) => b.listingCount - a.listingCount).slice(0, 50);
  const byMostExpensive = [...all].sort((a, b) => b.chaos - a.chaos).slice(0, 50);

  const watchlistMap = new Map();
  for (const it of [...byMostSold, ...byMostExpensive]) {
    watchlistMap.set(it.name, { chaos: it.chaos, icon: it.icon, variants: it.variants, watchChaos: it.watchChaos });
  }
  const watchlist = Array.from(watchlistMap.entries())
    .map(([name, v]) => ({ name, chaos: v.chaos, icon: v.icon, variants: v.variants, watchChaos: v.watchChaos }));

  return { watchlist, divineRate, exaltedRate };
}

// base item name -> lowest poe.watch "Unidentified <name> [ilvl]" price — the
// real floor value an as-dropped, unrolled item sells for, before anyone
// rolls/identifies it (mirrors boss.py's _get_watch_unidentified(): same
// source, same reasoning — an unidentified copy's price reflects the base
// item alone, not whatever specific roll a seller happened to get). Purely
// best-effort: poe.watch being slow/unreachable never blocks the watchlist,
// it just means those items show no poe.watch reference price.
async function fetchWatchUnidentifiedPrices(league) {
  const result = new Map();
  try {
    const qs = new URLSearchParams({ league });
    const resp = await fetch(`${WATCH_BASE}/compact?${qs}`, { headers: { "User-Agent": UA } });
    const data = await resp.json();
    for (const it of (data.items || [])) {
      const nm = it.name || "";
      if (!nm.startsWith("Unidentified ")) continue;
      const base = nm.slice("Unidentified ".length).replace(/\s+\d+\+?$/, "");
      const chaos = it.min ?? it.mean;
      if (chaos == null) continue;
      if (!result.has(base) || chaos < result.get(base)) result.set(base, chaos);
    }
  } catch (e) { /* best-effort — see comment above */ }
  return result;
}

// SnipeSession is intentionally a SINGLETON: pathofexile.com/trade's rate
// limit is keyed by the shared Cloudflare Workers egress IP, not by whatever
// "session" string a browser holds — so multiple independently-routed
// rotations (e.g. a page reload that starts a new watch without stopping the
// old one, or several tabs) would each poll the trade API on their own
// schedule, blind to each other, and collectively blow past the real shared
// limit even though each one paces itself conservatively in isolation. That
// was a real, reproduced bug: routing every /snipe/start to a fresh
// crypto.randomUUID()-derived DO id meant handleStart's own
// "session already running" 409 guard could never fire (the DO it checked
// was always brand new), so unlimited concurrent rotations could pile up and
// perpetually re-trigger each other's bans. Fixed by always routing to one
// fixed id, so that guard is finally live and only one rotation ever runs —
// correct for this feature anyway, since /snipe is a hidden admin-only page
// (see CLAUDE.md's admin-detection section), not a multi-tenant one.
const SNIPE_SESSION_NAME = "singleton";

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === "/snipe/start" || url.pathname === "/snipe/stop") {
      if (request.method === "OPTIONS") return withCors(null, 204);
      if (request.method !== "POST") return withCors(JSON.stringify({ ok: false, error: "method not allowed" }), 405);
      let payload;
      try { payload = await request.json(); } catch (e) { return withCors(JSON.stringify({ ok: false, error: "bad JSON" }), 400); }

      const id = env.SNIPE_SESSION.idFromName(SNIPE_SESSION_NAME);
      const stub = env.SNIPE_SESSION.get(id);
      if (url.pathname === "/snipe/start") {
        const doResp = await stub.fetch("https://do/start", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
        });
        const data = await doResp.json();
        if (!data.ok) return withCors(JSON.stringify(data), doResp.status);
        return withCors(JSON.stringify({ ok: true, session: SNIPE_SESSION_NAME, watchlistSize: data.watchlistSize }), 200);
      } else {
        const doResp = await stub.fetch("https://do/stop", { method: "POST" });
        return withCors(await doResp.text(), doResp.status, "application/json");
      }
    }

    if (url.pathname === "/snipe/poll") {
      if (request.method === "OPTIONS") return withCors(null, 204);
      if (request.method !== "GET") return withCors(JSON.stringify({ ok: false, error: "method not allowed" }), 405);
      const id = env.SNIPE_SESSION.idFromName(SNIPE_SESSION_NAME);
      const stub = env.SNIPE_SESSION.get(id);
      const doResp = await stub.fetch("https://do/poll");
      return withCors(await doResp.text(), doResp.status, "application/json");
    }

    if (!url.pathname.startsWith("/ninja/") && url.pathname !== "/watch/compact" && url.pathname !== "/forum/patch-notes") {
      // Not an API-proxy path, and no static asset matched (Cloudflare tries
      // that first) — send the visitor to the dashboard's real landing page.
      return Response.redirect(url.origin + "/home", 302);
    }

    if (request.method === "OPTIONS") return withCors(null, 204);
    if (request.method !== "GET") return withCors("method not allowed", 405, "text/plain");

    if (url.pathname === "/forum/patch-notes") {
      const game = url.searchParams.get("game") || "poe1";
      if (!PATCH_NOTES_URLS[game]) return withCors(JSON.stringify({ ok: false, error: "unknown game" }), 400);
      try {
        const items = await fetchPatchNotes(game);
        return withCors(JSON.stringify({ ok: true, items }), 200);
      } catch (e) {
        return withCors(JSON.stringify({ ok: false, error: String(e) }), 502);
      }
    }

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
