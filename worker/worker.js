// Cloudflare Worker: CORS-friendly reverse proxy for the two upstreams
// boss.py's build_index()/build_payload() otherwise fetch server-side.
//
// poe.ninja sends no Access-Control-Allow-Origin header, so a browser
// calling it directly (as the static build in ../docs must, since GitHub
// Pages has no backend) gets blocked by CORS. poe.watch already sends
// `Access-Control-Allow-Origin: *`, but its /compact feed is ~12MB — routed
// through here too so Cloudflare's edge cache absorbs it once for every
// visitor instead of every browser refetching it on each poll.
//
// Routes (mirror boss.py's CURRENCY_URL/EXCHANGE_URL/ITEM_URL/WATCH_URL path
// shapes 1:1, so the mapping is easy to eyeball against boss.py):
//   GET /ninja/<path>?<query>  -> https://poe.ninja/poe1/api/economy/<path>?<query>
//   GET /watch/compact?<query> -> https://api.poe.watch/compact?<query>

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
    if (request.method === "OPTIONS") return withCors(null, 204);
    if (request.method !== "GET") return withCors("method not allowed", 405, "text/plain");

    const url = new URL(request.url);
    let upstream;
    if (url.pathname.startsWith("/ninja/")) {
      upstream = NINJA_BASE + url.pathname.slice("/ninja".length) + url.search;
    } else if (url.pathname === "/watch/compact") {
      upstream = WATCH_BASE + "/compact" + url.search;
    } else {
      return withCors("not found", 404, "text/plain");
    }

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
