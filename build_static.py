#!/usr/bin/env python3
"""
build_static.py
----------------
Generates a static (no-backend) build of the Boss Farm Estimator dashboard.
boss.py's ENTITIES stays the single hand-maintained source of truth: this
script imports it directly and embeds it as JSON, and reuses boss.py's PAGE
(same CSS/HTML/rendering JS) unchanged except for one swap — the
data-fetching layer.

boss.py's build_index()/build_payload() run server-side because poe.ninja
sends no CORS header, so a browser can't call it directly from a
static-hosted page. This script instead points the generated page at a small
Cloudflare Worker (see worker/worker.js) that reverse-proxies poe.ninja and
poe.watch with CORS enabled, and ports build_index()/build_payload()'s exact
pricing logic into client-side JS (the FETCH_ENGINE block below) that calls
the Worker instead.

The whole deployment (this build's output + the proxy) is one Cloudflare
Worker: worker.js serves build/ as static assets (see [assets] in
worker/wrangler.toml) and only runs its own fetch() handler for the /ninja,
/watch proxy routes and the redirect-everything-else-to-/bosses behavior —
so the dashboard's real URL is host/bosses, written here to
build/bosses/index.html. build/ is generated output, kept separate from
docs/ (real documentation, e.g. the README screenshot).

Usage:
    python build_static.py
    python build_static.py --worker-url https://... --league Standard --poll 120 --out build
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import boss  # noqa: E402  (must follow sys.path tweak)

OLD_FETCH_DATA = """async function fetchData(){
  const qs = currentLeague ? ('?league='+encodeURIComponent(currentLeague)) : '';
  const r = await fetch('/api/data'+qs, {signal: AbortSignal.timeout(60000)});
  if(!r.ok) throw new Error('HTTP '+r.status);
  return r.json();
}"""

# Client-side port of boss.py's build_index()/build_payload(). Kept as close
# to a line-by-line translation of the Python as JS syntax allows, so the two
# are easy to diff against each other when boss.py's pricing logic changes.
# The index is a Map<name, Map<type, entry>> (mirrors Python's dict keyed by
# the (name, type) tuple) rather than a single string-keyed Map — item names
# routinely contain spaces (e.g. "The Rippling Thoughts"), so concatenating
# name+type into one string key isn't safely reversible.
FETCH_ENGINE = r"""const WORKER_URL = __WORKER_URL_JSON__;
const LEAGUE = __LEAGUE_JSON__;
const ENTITIES = __ENTITIES_JSON__;
const ITEM_CATEGORIES = __ITEM_CATEGORIES_JSON__;
const EXCHANGE_CATEGORIES = __EXCHANGE_CATEGORIES_JSON__;
const OVERVIEW_SLUG = __OVERVIEW_SLUG_JSON__;
const DEFAULT_CHANCE = __DEFAULT_CHANCE_JSON__;
const PRICE_OVERRIDE = __PRICE_OVERRIDE_JSON__;
const CACHE_TTL = __CACHE_TTL_JSON__;

function pnSlugify(name){
  let s = name.toLowerCase().split("'").join("");
  s = s.replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  return s;
}

async function getJsonSafe(url, league, typ){
  const qs = new URLSearchParams({league, type: typ});
  try{
    const r = await fetch(url+"?"+qs, {signal: AbortSignal.timeout(25000)});
    if(!r.ok) throw new Error('HTTP '+r.status);
    return await r.json();
  }catch(e){
    console.warn('[warning] '+typ+': '+e);
    return {lines: [], currencyDetails: [], items: []};
  }
}

const UNIDED_ILVL_SUFFIX = /\s+\d+\+?$/;

async function getWatchUnidentified(league){
  const result = {};
  try{
    const qs = new URLSearchParams({league});
    const r = await fetch(WORKER_URL+'/watch/compact?'+qs, {signal: AbortSignal.timeout(40000)});
    if(!r.ok) throw new Error('HTTP '+r.status);
    const data = await r.json();
    for(const it of (data.items||[])){
      const nm = it.name || '';
      if(!nm.startsWith('Unidentified ')) continue;
      const base = nm.slice('Unidentified '.length).replace(UNIDED_ILVL_SUFFIX, '');
      let chaos = it.min;
      if(chaos == null) chaos = it.mean;
      if(chaos == null) continue;
      if(!(base in result) || chaos < result[base]) result[base] = chaos;
    }
  }catch(e){ console.warn('[warning] poe.watch unidentified prices: '+e); }
  return result;
}

function getEntry(index, name, typ){
  let byType = index.get(name);
  if(!byType){ byType = new Map(); index.set(name, byType); }
  let e = byType.get(typ);
  if(!e){
    e = {chaos: null, chaos_max: null, chaos_n: 0, chaos_alt: null, divine: null,
         icon: null, detailsId: pnSlugify(name), type: typ, chaos_unided: null};
    byType.set(typ, e);
  }
  return e;
}

async function buildIndex(league){
  const EXCHANGE_URL = WORKER_URL+'/ninja/exchange/current/overview';
  const CURRENCY_URL = WORKER_URL+'/ninja/stash/current/currency/overview';
  const ITEM_URL = WORKER_URL+'/ninja/stash/current/item/overview';

  const exchangeTypes = EXCHANGE_CATEGORIES;
  const stashTypes = EXCHANGE_CATEGORIES;
  const itemTypes = ITEM_CATEGORIES;

  const [exchangeResults, stashResults, itemResults, watchUnided] = await Promise.all([
    Promise.all(exchangeTypes.map(t => getJsonSafe(EXCHANGE_URL, league, t))),
    Promise.all(stashTypes.map(t => getJsonSafe(CURRENCY_URL, league, t))),
    Promise.all(itemTypes.map(t => getJsonSafe(ITEM_URL, league, t))),
    getWatchUnidentified(league),
  ]);
  const exchangeData = {}; exchangeTypes.forEach((t,i) => exchangeData[t] = exchangeResults[i]);
  const stashData = {}; stashTypes.forEach((t,i) => stashData[t] = stashResults[i]);
  const itemData = {}; itemTypes.forEach((t,i) => itemData[t] = itemResults[i]);

  const index = new Map(); // name -> Map(type -> entry)

  function put(name, typ, chaos, divine, icon, details, field){
    field = field || 'chaos';
    const e = getEntry(index, name, typ);
    if(chaos != null){
      const cur = e[field];
      if(cur == null || chaos < cur){
        e[field] = chaos;
        if(field === 'chaos') e.divine = divine;
      }
      if(field === 'chaos'){
        e.chaos_max = e.chaos_max == null ? chaos : Math.max(e.chaos_max, chaos);
        e.chaos_n += 1;
      }
    } else if(divine != null && e.divine == null){
      e.divine = divine;
    }
    if(icon && !e.icon) e.icon = icon;
    if(details) e.detailsId = details;
  }

  for(const typ of exchangeTypes){
    const data = exchangeData[typ];
    const idToMeta = {};
    for(const d of (data.items||[])) if(d.id) idToMeta[d.id] = d;
    for(const line of (data.lines||[])){
      const itemId = line.id;
      const meta = idToMeta[itemId] || {};
      const nm = meta.name || line.currencyTypeName || line.name;
      if(!nm) continue;
      const chaos = line.primaryValue;
      const details = meta.detailsId || itemId;
      // Astrolabe has no stash feed data at all, so its exchange-feed image
      // (a relative path) is the only icon source; prefixed into a full
      // poecdn URL. Currency/Fragment still rely on stash's icon (below).
      const icon = (typ === 'Astrolabe' && meta.image) ? 'https://web.poecdn.com'+meta.image : null;
      put(nm, typ, chaos == null ? null : chaos, null, icon, details, 'chaos');
    }
  }

  for(const typ of stashTypes){
    const data = stashData[typ];
    const icons = {};
    for(const d of (data.currencyDetails||[])) if(d.name) icons[d.name] = d.icon;
    for(const nm in icons) put(nm, typ, null, null, icons[nm], null, 'chaos');
    for(const line of (data.lines||[])){
      const nm = line.currencyTypeName || line.name;
      if(!nm) continue;
      let chaos = line.chaosEquivalent;
      if(chaos == null) chaos = line.chaosValue;
      put(nm, typ, chaos == null ? null : chaos, line.divineValue == null ? null : line.divineValue,
          icons[nm] || null, line.detailsId || null, 'chaos_alt');
    }
  }

  for(const byType of index.values()){
    for(const e of byType.values()){
      if(e.chaos == null && e.chaos_alt != null){
        e.chaos = e.chaos_alt;
        e.chaos_max = e.chaos_alt;
        e.chaos_n = 1;
        e.chaos_alt = null;
      }
    }
  }

  for(const typ of itemTypes){
    const data = itemData[typ];
    for(const line of (data.lines||[])){
      const nm = line.name;
      if(!nm) continue;
      let chaos = line.chaosValue;
      if(chaos == null) chaos = line.chaosEquivalent;
      put(nm, typ, chaos == null ? null : chaos, line.divineValue == null ? null : line.divineValue,
          line.icon || null, line.detailsId || null, 'chaos');
    }
  }

  for(const nm in watchUnided){
    const chaos = watchUnided[nm];
    const byType = index.get(nm);
    if(byType){
      for(const typ of itemTypes){
        const e = byType.get(typ);
        if(e) e.chaos_unided = chaos;
      }
    }
  }

  for(const [nm, byType] of index.entries()){
    if(Object.prototype.hasOwnProperty.call(PRICE_OVERRIDE, nm)){
      for(const e of byType.values()){
        e.chaos = PRICE_OVERRIDE[nm];
        e.chaos_unided = null;
        e.override = true;
      }
    }
  }

  return index;
}

function wikiUrl(name){
  return 'https://www.poewiki.net/wiki/'+encodeURIComponent(name.split(' ').join('_'));
}

function lookup(index, name, typ){
  const byType = index.get(name);
  const e = byType ? byType.get(typ) : undefined;
  if(e && e.chaos != null) return [e, false];
  if(byType){
    for(const [t, v] of byType.entries()){
      if(v.chaos != null) return [v, t !== typ];
    }
  }
  return [e || {}, false];
}

function resItem(index, name, typ, divineRate, leagueSlug){
  const [info, mismatched] = lookup(index, name, typ);
  const rtype = info.type || typ;
  const chaosFloor = info.chaos == null ? null : info.chaos;
  const chaosUnided = info.chaos_unided == null ? null : info.chaos_unided;
  const chaosMaxRaw = info.chaos_max == null ? null : info.chaos_max;
  const variants = info.chaos_n || 0;
  let chaos, priceMode, divine;
  if(chaosUnided != null){
    chaos = chaosUnided; priceMode = 'unidentified'; divine = null;
  }else{
    chaos = chaosFloor;
    priceMode = variants > 1 ? 'variants' : 'single';
    divine = info.divine == null ? null : info.divine;
  }
  const alt = info.chaos_alt == null ? null : info.chaos_alt;
  let diverge = null;
  if(chaosFloor && alt && chaosFloor > 0){
    const d = Math.abs(chaosFloor - alt) / chaosFloor;
    if(d >= 0.10) diverge = {alt: alt, pct: Math.round(d*100)};
  }
  let url, linkSrc;
  if(chaos != null){
    const slug = OVERVIEW_SLUG[rtype] || OVERVIEW_SLUG[typ] || 'currency';
    const details = info.detailsId || pnSlugify(name);
    url = 'https://poe.ninja/poe1/economy/'+leagueSlug+'/'+slug+'/'+details;
    linkSrc = 'ninja';
  }else{
    url = wikiUrl(name);
    linkSrc = 'wiki';
  }
  const hasUpside = chaosMaxRaw != null && chaos && chaosMaxRaw > chaos * 1.05;
  return {
    name: name, type: rtype, declared_type: typ,
    chaos: chaos == null ? null : chaos, divine: divine == null ? null : divine,
    chaosMax: hasUpside ? chaosMaxRaw : null,
    priceMode: priceMode,
    icon: info.icon == null ? null : info.icon, url: url, link_src: linkSrc,
    diverge: diverge, type_mismatch: mismatched,
    override: !!info.override,
  };
}

async function buildPayload(league){
  const leagueSlug = league.toLowerCase().split(' ').join('-');
  const index = await buildIndex(league);
  const [dv] = lookup(index, 'Divine Orb', 'Currency');
  const divineRate = dv.chaos == null ? null : dv.chaos;

  const res = (name, typ) => resItem(index, name, typ, divineRate, leagueSlug);

  const ents = [];
  for(const e of ENTITIES){
    const eitems = []; let ecost = 0.0; let ehas = false;
    for(const entryLine of e.entry){
      const name = entryLine[0], typ = entryLine[1], qty = entryLine[2];
      const r = res(name, typ);
      r.qty = qty;
      eitems.push(r);
      if(r.chaos != null){ ecost += r.chaos * qty; ehas = true; }
    }
    const entry = {items: eitems, total_chaos: ehas ? ecost : null};

    const ditems = []; let ev = 0.0; let bestItem = 0.0;
    for(const drop of e.drops){
      const name = drop[0], typ = drop[1], chance = drop[2], src = drop[3];
      const qty = drop.length > 4 ? drop[4] : 1;
      const r = res(name, typ);
      const ch = chance != null ? chance : DEFAULT_CHANCE;
      r.chance = ch; r.chance_src = src; r.qty = qty;
      ditems.push(r);
      if(r.chaos != null){
        const value = r.chaos * qty;
        ev += ch * value;
        bestItem = Math.max(bestItem, value);
      }
    }
    const drops = {items: ditems, ev_chaos: ev};

    const hasCost = entry.total_chaos != null;
    const net = hasCost ? ev - entry.total_chaos : null;
    const worst = hasCost ? -entry.total_chaos : null;
    const best = hasCost ? bestItem - entry.total_chaos : null;
    ents.push({
      name: e.name, tier: e.tier, entry: entry, drops: drops, ev_chaos: ev,
      net: net, worst: worst, best: best,
      quantMod: !!e.quant_mod, access: e.access || null, invuln: e.invuln || null,
      wikiUrl: wikiUrl(e.wiki || e.name),
    });
  }

  return {
    league: league, leagueSlug: leagueSlug, divineRate: divineRate,
    source: 'exchange→stash', updated: Math.floor(Date.now()/1000),
    cacheTtl: CACHE_TTL, bosses: ents,
  };
}

async function fetchData(){
  const data = await Promise.race([
    buildPayload(currentLeague || LEAGUE),
    new Promise((_, rej) => setTimeout(
      () => rej(Object.assign(new Error('timeout'), {name: 'TimeoutError'})), 60000)),
  ]);
  if(data.divineRate == null) throw new Error('no pricing data reachable');
  return data;
}"""


def render_page(league, worker_url, poll_ms):
    engine = (FETCH_ENGINE
              .replace("__WORKER_URL_JSON__", json.dumps(worker_url.rstrip("/")))
              .replace("__LEAGUE_JSON__", json.dumps(league))
              .replace("__ENTITIES_JSON__", json.dumps(boss.ENTITIES))
              .replace("__ITEM_CATEGORIES_JSON__", json.dumps(boss.ITEM_CATEGORIES))
              .replace("__EXCHANGE_CATEGORIES_JSON__", json.dumps(boss.EXCHANGE_CATEGORIES))
              .replace("__OVERVIEW_SLUG_JSON__", json.dumps(boss.OVERVIEW_SLUG))
              .replace("__DEFAULT_CHANCE_JSON__", json.dumps(boss.DEFAULT_CHANCE))
              .replace("__PRICE_OVERRIDE_JSON__", json.dumps(boss.PRICE_OVERRIDE))
              .replace("__CACHE_TTL_JSON__", json.dumps(boss.CACHE_TTL)))

    canonical_url = worker_url.rstrip("/") + "/bosses"
    html = (boss.render_bosses_page().replace("__POLL_MS__", str(poll_ms))
                     .replace("__CANONICAL_URL__", canonical_url))
    if OLD_FETCH_DATA not in html:
        raise RuntimeError("fetchData() block not found in boss.render_bosses_page() — boss.py's "
                            "frontend JS changed shape; update OLD_FETCH_DATA in build_static.py to match.")
    html = html.replace(OLD_FETCH_DATA, engine, 1)
    return html


def render_snipe_page(league, worker_url, poll_ms):
    # No FETCH_ENGINE swap needed — the Trade Sniper page talks to /snipe/*
    # on the same origin (the Worker itself, see worker/worker.js's
    # SnipeSession routes), which works unchanged whether that origin is the
    # custom domain or the *.workers.dev URL. Only __LEAGUE_JSON__ (used for
    # the league picker default) needs substituting, same as boss.py's own
    # local dev server does in main().
    canonical_url = worker_url.rstrip("/") + "/snipe"
    return (boss.render_snipe_page().replace("__POLL_MS__", str(poll_ms))
                    .replace("__LEAGUE_JSON__", json.dumps(league))
                    .replace("__CANONICAL_URL__", canonical_url))


# boss.py's HOME_JS hits the local dev server's own /api/patchnotes relatively;
# the static build instead needs the Worker's /forum/patch-notes proxy
# (absolute URL, same defensive reasoning as FETCH_ENGINE's WORKER_URL above —
# works even if the built HTML is previewed from somewhere other than the
# live worker origin). Small single-line swap, not a full FETCH_ENGINE-style
# port, since the actual fetch/render logic doesn't otherwise differ between
# deployments — only the URL prefix does.
OLD_PATCH_NOTES_BASE = "const PATCH_NOTES_BASE = '/api/patchnotes';"


def render_home_page(league, worker_url, poll_ms):
    canonical_url = worker_url.rstrip("/") + "/home"
    html = (boss.render_home_page().replace("__POLL_MS__", str(poll_ms))
                    .replace("__LEAGUE_JSON__", json.dumps(league))
                    .replace("__CANONICAL_URL__", canonical_url))
    if OLD_PATCH_NOTES_BASE not in html:
        raise RuntimeError("PATCH_NOTES_BASE line not found in boss.render_home_page() — "
                            "boss.py's HOME_JS changed shape; update OLD_PATCH_NOTES_BASE in build_static.py to match.")
    new_base = "const PATCH_NOTES_BASE = " + json.dumps(worker_url.rstrip("/") + "/forum/patch-notes") + ";"
    html = html.replace(OLD_PATCH_NOTES_BASE, new_base, 1)
    return html


def render_poe2_campaign_page(league, worker_url, poll_ms):
    canonical_url = worker_url.rstrip("/") + "/poe2-campaign"
    return (boss.render_poe2_campaign_page().replace("__POLL_MS__", str(poll_ms))
                    .replace("__LEAGUE_JSON__", json.dumps(league))
                    .replace("__CANONICAL_URL__", canonical_url))


# Same reasoning/pattern as OLD_PATCH_NOTES_BASE above: boss.py's FLIP_ADVISOR_JS
# hits the local dev server's own /api/flipadvisor relatively; the static build
# needs the Worker's /currency-exchange proxy instead (absolute URL).
OLD_FLIP_ADVISOR_BASE = "const FLIP_ADVISOR_BASE = '/api/flipadvisor';"


def render_flip_advisor_page(league, worker_url, poll_ms):
    canonical_url = worker_url.rstrip("/") + "/flip-advisor"
    html = (boss.render_flip_advisor_page().replace("__POLL_MS__", str(poll_ms))
                    .replace("__LEAGUE_JSON__", json.dumps(league))
                    .replace("__CANONICAL_URL__", canonical_url))
    if OLD_FLIP_ADVISOR_BASE not in html:
        raise RuntimeError("FLIP_ADVISOR_BASE line not found in boss.render_flip_advisor_page() — "
                            "boss.py's FLIP_ADVISOR_JS changed shape; update OLD_FLIP_ADVISOR_BASE in build_static.py to match.")
    new_base = "const FLIP_ADVISOR_BASE = " + json.dumps(worker_url.rstrip("/") + "/currency-exchange") + ";"
    html = html.replace(OLD_FLIP_ADVISOR_BASE, new_base, 1)
    return html


# Cloudflare's static-asset layer does an implicit "look for index.html" check
# at the bare root that returns its own 404 without ever invoking worker.js's
# fetch() handler (confirmed live — every other unmatched path correctly
# falls through to the Worker's redirect-to-/home logic, only "/" doesn't).
# A real index.html file sidesteps that entirely.
ROOT_REDIRECT = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="google-adsense-account" content="ca-pub-7517572231491496">
<meta http-equiv="refresh" content="0; url=/home">
<script>location.replace('/home');</script>
<title>Boss Farm Estimator</title>
</head><body>Redirecting to <a href="/home">/home</a>...</body></html>
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--worker-url", default="https://poe-farm-helper.com",
                    help="Public site URL (custom domain if you have one, otherwise the "
                    "Cloudflare Worker's *.workers.dev URL)")
    ap.add_argument("--league", default="Allflame")
    ap.add_argument("--poll", type=int, default=120,
                    help="browser auto-refresh interval, in seconds")
    ap.add_argument("--out", default="build", help="output directory (default: build — the same "
                    "directory worker/wrangler.toml's [assets] block points at)")
    ap.add_argument("--no-minify", action="store_true",
                    help="skip minifying the output HTML/CSS/JS via `npx esbuild` (minified by "
                    "default here — this workflow already requires Node/Wrangler)")
    args = ap.parse_args()

    out_dir = Path(__file__).resolve().parent / args.out
    home_dir = out_dir / "home"
    bosses_dir = out_dir / "bosses"
    snipe_dir = out_dir / "snipe"
    poe2_dir = out_dir / "poe2-campaign"
    flip_dir = out_dir / "flip-advisor"
    for d in (home_dir, bosses_dir, snipe_dir, poe2_dir, flip_dir):
        d.mkdir(parents=True, exist_ok=True)

    home_html = render_home_page(args.league, args.worker_url, args.poll * 1000)
    html = render_page(args.league, args.worker_url, args.poll * 1000)
    snipe_html = render_snipe_page(args.league, args.worker_url, args.poll * 1000)
    poe2_html = render_poe2_campaign_page(args.league, args.worker_url, args.poll * 1000)
    flip_html = render_flip_advisor_page(args.league, args.worker_url, args.poll * 1000)
    if not args.no_minify:
        home_html = boss.minify_page(home_html)
        html = boss.minify_page(html)
        snipe_html = boss.minify_page(snipe_html)
        poe2_html = boss.minify_page(poe2_html)
        flip_html = boss.minify_page(flip_html)
    (home_dir / "index.html").write_text(home_html, encoding="utf-8")
    (bosses_dir / "index.html").write_text(html, encoding="utf-8")
    (snipe_dir / "index.html").write_text(snipe_html, encoding="utf-8")
    (poe2_dir / "index.html").write_text(poe2_html, encoding="utf-8")
    (flip_dir / "index.html").write_text(flip_html, encoding="utf-8")
    (out_dir / "index.html").write_text(ROOT_REDIRECT, encoding="utf-8")

    base = args.worker_url.rstrip("/")
    (out_dir / "robots.txt").write_text(
        f"User-agent: *\nAllow: /\nSitemap: {base}/sitemap.xml\n", encoding="utf-8")
    (out_dir / "sitemap.xml").write_text(
        # /snipe, /poe2-campaign, and /flip-advisor deliberately left out of the sitemap
        # while they're admin-only/hidden from the site menu (see PAGES in boss.py) — all
        # three still build and work at their URLs for direct/manual access, just aren't
        # advertised for search engines to crawl yet.
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"  <url><loc>{base}/home</loc></url>\n"
        f"  <url><loc>{base}/bosses</loc></url>\n"
        "</urlset>\n", encoding="utf-8")

    print(f"Wrote {home_dir / 'index.html'} ({len(home_html):,} bytes)")
    print(f"Wrote {bosses_dir / 'index.html'} ({len(html):,} bytes)")
    print(f"Wrote {snipe_dir / 'index.html'} ({len(snipe_html):,} bytes)")
    print(f"Wrote {poe2_dir / 'index.html'} ({len(poe2_html):,} bytes)")
    print(f"Wrote {flip_dir / 'index.html'} ({len(flip_html):,} bytes)")
    print(f"Wrote {out_dir / 'index.html'} (redirect -> /home)")
    print(f"Wrote {out_dir / 'robots.txt'} and {out_dir / 'sitemap.xml'}")
    print(f"  league={args.league}  worker={args.worker_url}  poll={args.poll}s")
    print(f"  {len(boss.ENTITIES)} boss entities embedded")
    print("Redeploy: cd worker && wrangler deploy   (or push, if Git auto-deploy is set up)")


if __name__ == "__main__":
    main()
