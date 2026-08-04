# 💀 Boss Farm Estimator

A single-file, zero-dependency web dashboard that estimates farming profitability for **Path of Exile 1** pinnacle, Uber, and Tier 17 Nightmare-map bosses — live prices, real drop-chance sourcing where it exists, and an honest worst/average/best range instead of one misleading number.

> **Unofficial fan tool.** Not affiliated with or endorsed by Grinding Gear Games. Prices come from [poe.ninja](https://poe.ninja) / [poe.watch](https://poe.watch), drop data from [poewiki.net](https://www.poewiki.net) and community guides.

![Boss Farm Estimator](docs/initial_screenshot.png)

## Why this exists

Most "which boss should I farm" answers collapse everything into a single expected-value number. That number is real, but it's also not what any *one* run looks like — most kills return nothing from the loot pool and you just eat the entry cost, and occasionally one item pays for the next fifty runs. This tool shows all three: the **worst** case (nothing drops), the **average** (EV across many runs), and the **best** case (the single most valuable item in the pool drops — not the whole pool at once, since several bosses can only ever yield one of a handful of items per kill).

## Quick start

```bash
python boss.py
# or customize:
python boss.py --league Standard --port 8000 --poll 120
```

No `pip install` — it's stdlib Python 3 only. Opens `http://localhost:8000/bosses` in your browser automatically (`/` redirects there too). `--poll` sets the browser's auto-refresh interval in seconds; prices are cached server-side for 5 minutes regardless.

## Deploying a static version (Cloudflare Workers)

`boss.py` needs a running Python process because poe.ninja sends no CORS header, so a browser
can't call it directly from a static-hosted page — `boss.py`'s server proxies it. The static
deploy moves the pricing math (`build_index()`/`build_payload()`) into client-side JS, and uses
one Cloudflare Worker (`worker/worker.js`) for two jobs at once:

- Serves the built dashboard as static assets (`build/`, via `[assets]` in `worker/wrangler.toml`)
  at `host/bosses` — anything else (root, typos, old links) gets a 302 redirect to `/bosses`.
- Reverse-proxies poe.ninja/poe.watch with CORS enabled (`/ninja/*`, `/watch/compact`), since
  Cloudflare only invokes `worker.js`'s own logic for requests that don't match a static asset.

1. **Deploy the Worker** (one-time, needs a free [Cloudflare](https://dash.cloudflare.com/sign-up)
   account and the [`wrangler`](https://developers.cloudflare.com/workers/wrangler/) CLI):
   ```bash
   cd worker
   wrangler deploy
   ```
   This prints the Worker's `*.workers.dev` URL — this deployment also has a custom domain,
   [poe-farm-helper.com](https://poe-farm-helper.com), bound via `routes` in `wrangler.toml`
   (`workers_dev = true` there keeps the `.workers.dev` URL alive too — useful for things like
   AdSense site verification that were set up against it before the custom domain existed).

2. **Generate the static build** — `ENTITIES` in `boss.py` stays the single source of truth;
   this reads it directly and embeds it, it isn't duplicated anywhere:
   ```bash
   python build_static.py --worker-url https://poe-farm-helper.com --league Allflame --poll 120 --out build
   ```
   Writes `build/bosses/index.html` (the dashboard) and `build/index.html` (a redirect stub —
   Cloudflare's static-asset layer does its own "look for index.html" check at the bare root that
   bypasses `worker.js` entirely, so a real file there is needed). `build/` is generated output,
   kept separate from `docs/` (real documentation, e.g. the screenshot above).

3. **Redeploy** so the Worker picks up the new build: `cd worker && wrangler deploy` (or push, if
   Git-connected auto-deploy is set up in the Cloudflare dashboard — see `worker/wrangler.toml`'s
   `[build]` block).

Re-run step 2 (and redeploy) whenever `ENTITIES` changes — there's no CI/auto-rebuild of the
*build itself*, this is a manual regenerate step by design.

## Features

- **27 boss encounters** across three independently-ranked categories:
  - **Pinnacle** — Shaper, Elder, Sirus, Maven, Searing Exarch, Eater of Worlds, Venarius (Cortex), Atziri, and the three Secrets-of-the-Atlas Incarnations (Fear/Dread/Neglect)
  - **Uber** — the Uber Pinnacle version of every fight above (4-fragment entry)
  - **Nightmare** — all 5 Tier 17 Nightmare map bosses (Abomination, Citadel, Fortress, Sanctuary, Ziggurat)
- **Worst / average / best profit range** per boss, not a single collapsed number — see [why this exists](#why-this-exists).
- **Sortable rankings** — order each category by worst, average, or best (default: best).
- **×1 / ×10 / ×100 run multiplier** — see totals over multiple runs instead of one, recalculated instantly from already-loaded data.
- **Adjustable quantity controls** on fights where loot scales with a player choice instead of a fixed rate: Eldritch Altars (normal Searing Exarch / Eater of Worlds only) and map IIQ (all 5 Nightmare maps).
- **Editable time-per-run + live ≈chaos/hour rate** — GGG doesn't publish kill times any more than they publish drop rates, so this is a plain input with sensible defaults (60s direct-spawn, 120s if you have to navigate to the boss), not a guess baked into the code.
- **Access-type and invulnerability-phase badges** (🗺️ map-navigation required / ⚡ direct spawn, 🛡️ has an invulnerable phase) — shown only where actually verified, never a guessed default.
- **Realistic pricing, not lucky-roll fantasy numbers**: for items whose value depends on a random roll (corrupted-implicit weapons, jewel affix combos), the EV uses the realistic floor price — [poe.watch](https://poe.watch)'s dedicated "Unidentified" price when published, otherwise the lowest of poe.ninja's per-roll listings — with the identified ceiling shown separately (`↑850c`) rather than baked into the average.
- **EN / PT-BR UI**, dropdown in the header. Only interface labels and explanations translate — boss names, item names, and every number are exactly what poe.ninja/poe.watch/poewiki report, in either language.
- **League switcher** in the header — reprices everything against any league (defaults to the server's/build's configured league, plus Standard/Hardcore/current-Hardcore as quick picks) without restarting or rebuilding.
- **Dark / light theme toggle** in the header, persisted across reloads (defaults to your OS's light/dark preference the first time).
- **Rich hover explanations** on every stat and control — not a one-line browser tooltip, an actual explanation of the mechanic and the math behind the number.
- Left sidebar with a live-updating summary of every boss's worst/avg/best, click to jump straight to its card.

## How it works

One Python file (`boss.py`, stdlib only — `http.server`, `urllib`, `concurrent.futures`, no `pip install`):

1. **`ENTITIES`** — the hand-maintained boss/drop data: entry cost, loot pool with chances, and metadata (access type, invulnerability mechanic, whether quantity can be boosted). See the schema comment directly above it in the source.
2. **`build_index()`** — fetches live prices from poe.ninja (Exchange + Stash feeds for currency/fragments, Item feed for uniques/maps/invitations) and poe.watch (for the "Unidentified" price case), all in parallel via a thread pool.
3. **`build_payload()`** — resolves every entry/drop against live prices and computes the worst/average/best profit numbers per boss.
4. A single embedded HTML page (inline CSS + vanilla JS, no build step, no framework) renders it and polls `/api/data` on the interval you set.

## Data sources

| Source | Used for |
|---|---|
| [poe.ninja](https://poe.ninja/poe1/economy) | Live currency/fragment/unique prices |
| [poe.watch](https://poe.watch) | "Unidentified" floor price for roll-dependent uniques (e.g. [Watcher's Eye](https://poe.watch/detailed/159)) |
| [poewiki.net](https://www.poewiki.net) | Drop chances where officially documented (e.g. [The Eater of Worlds](https://www.poewiki.net/wiki/The_Eater_of_Worlds)), and every boss card links to its wiki page for verification |
| [poedb.tw](https://poedb.tw/us/) | Cross-checking boss mechanics and item sources |
| [maxroll.gg](https://maxroll.gg/poe) | Boss guides, drop pool cross-referencing |

Example: [Uber Atziri's card links to poewiki](https://www.poewiki.net/wiki/Uber_Atziri); its drops link to their live [poe.ninja unique-weapons](https://poe.ninja/poe1/economy/allflame/unique-weapons) / [unique-armours](https://poe.ninja/poe1/economy/allflame/unique-armours) listings.

## A note on accuracy

GGG does not publish official drop rates, kill times, or exact quantity-scaling formulas for any of this content. Every `"est"`-tagged chance in `ENTITIES` is a curated estimate, not an official number — `"wiki"`-tagged chances (currently: Eater of Worlds) come from a real published sample size on poewiki. This tool has gone through several correction passes (see `CLAUDE.md` for the full history) catching real errors: wrong item-to-boss attribution, misspelled names, wrong item types, and missing chase uniques — all fixed only after confirming the item exists as a real, currently-traded price on poe.ninja, never guessed. If you spot something wrong, it's a config change away: edit the 4-5 fields of the relevant line in `ENTITIES`.

## Admin mode (personal use, via a Chrome extension)

There's a second page, **Trade Sniper** (`/snipe`), that watches Path of Exile's official trade
site for listings priced below the current market floor and alerts you the instant one appears —
see the section below for how it works. It's deliberately **not linked anywhere in the public
site menu** (still a work in progress), but it's still reachable by anyone who knows the URL.

To see it in the menu (plus a small `ADMIN` badge in the header) on your own browser without
exposing it to everyone else, this repo includes a personal, unpublished Chrome extension:
**PoE Helper Admin** (`admin-extension/`).

### How it works

This is **not real authentication** — there's no login, no account, no database anywhere in this
project (see [A note on accuracy](#a-note-on-accuracy) and `CLAUDE.md`'s Security section). It's a
client-side convenience flag with nothing sensitive behind it, built so that only the site's owner
sees a couple of extra UI elements in their own browser:

1. The extension's content script runs on `poe-farm-helper.com` (and `localhost:8000` for local
   testing) and holds a random secret token.
2. On page load, the site dispatches a `poe-helper-admin-request` browser event. If the extension
   is installed, its content script answers with a `poe-helper-admin-response` event carrying the
   token (a `CustomEvent` on `window` is the one thing a content script's isolated JS world and the
   page's own JS can both see — a content script can't just set a variable on the page's `window`
   directly).
3. The page hashes whatever token it receives with SHA-256 (`crypto.subtle.digest`, no libraries)
   and compares it against a hardcoded expected hash. If it matches, the page reveals the Trade
   Sniper link in the site menu and shows an `ADMIN` chip in the header. If nothing answers (every
   visitor without the extension), nothing happens — indistinguishable from a normal visit.

The page's own source only ever contains the **hash** of the token, never the token itself — so
reading this repo's public JS doesn't hand anyone a working token to fake admin with via devtools.

### Setting it up

```
admin-extension/
├── manifest.json   # Manifest V3, content script matches poe-farm-helper.com + localhost:8000
├── content.js      # holds the secret token, answers the page's handshake request
└── popup.html      # small "active" confirmation when you click the extension's icon
```

1. Generate your own random token + its hash (don't reuse the one in this README):
   ```bash
   python -c "import secrets, hashlib; t = secrets.token_hex(32); print('TOKEN:', t); print('HASH:', hashlib.sha256(t.encode()).hexdigest())"
   ```
2. Paste the `TOKEN` value into `admin-extension/content.js` (the `ADMIN_TOKEN` constant).
3. Paste the `HASH` value into `boss.py`'s `SHARED_JS_CHROME` (the `EXPECTED_ADMIN_HASH` constant),
   then rebuild the static site (`python build_static.py ...`) and redeploy if you're using the
   Cloudflare Worker deployment.
4. Load the extension in Chrome: `chrome://extensions` → enable **Developer mode** → **Load
   unpacked** → select the `admin-extension/` folder.
5. Reload the site — the `ADMIN` chip and the Trade Sniper menu link should appear.

### Why `admin-extension/` isn't in this repo's history

It's git-ignored (see `.gitignore`). This repo's remote is public, and `content.js` holds the
plaintext token — if it were committed, anyone could read it straight off GitHub and fake admin
instantly in devtools (`window.dispatchEvent(new CustomEvent('poe-helper-admin-response',
{detail: '<token>'}))`), no extension required, defeating the entire point of hashing it on the
site side. Keep your own backup of that folder outside of git; if you ever suspect the token
leaked, just regenerate a new one (step 1 above) and update both sides.

## Author

Built by **Erick Lúcio** — [ericklucio.suv@gmail.com](mailto:ericklucio.suv@gmail.com)
