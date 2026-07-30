// Cloudflare Worker serving the whole boss-farm-calculator deployment:
// static assets (the built dashboard, see ../docs, configured via the
// [assets] block in wrangler.toml) plus a CORS-friendly reverse proxy for
// the two upstreams boss.py's build_index()/build_payload() otherwise fetch
// server-side.
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
// something that should redirect to /ladder (the dashboard's real URL; see
// build_static.py, which writes it to docs/ladder/index.html).
//
// Routes (mirror boss.py's CURRENCY_URL/EXCHANGE_URL/ITEM_URL/WATCH_URL path
// shapes 1:1, so the mapping is easy to eyeball against boss.py):
//   GET /ninja/<path>?<query>  -> https://poe.ninja/poe1/api/economy/<path>?<query>
//   GET /watch/compact?<query> -> https://api.poe.watch/compact?<query>
//   anything else (root, typos, old links) -> 302 redirect to /ladder

const NINJA_BASE = "https://poe.ninja/poe1/api/economy";
const WATCH_BASE = "https://api.poe.watch";
const UA = "boss-dashboard-static-proxy/1.0 (contact: you@example.com)";
const CACHE_TTL = 300; // seconds; matches boss.py's CACHE_TTL

function withCors(body, status, contentType) {
  return new Response(body, {
    status,
    headers: {
      "Content-Type": contentType || "application/json",
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET, OPTIONS",
      "Access-Control-Allow-Headers": "*",
    },
  });
}

export default {
  async fetch(request) {
    const url = new URL(request.url);

    if (!url.pathname.startsWith("/ninja/") && url.pathname !== "/watch/compact") {
      // Not an API-proxy path, and no static asset matched (Cloudflare tries
      // that first) — send the visitor to the dashboard's real URL.
      return Response.redirect(url.origin + "/ladder", 302);
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
