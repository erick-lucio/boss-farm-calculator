# Task Backlog

## Done

- [x] **Currency Exchange flip advisor (PoE 1 only)** — shipped as `/flip-advisor`, later
      un-gated to fully public (see below). Turned out the original scoping note was wrong:
      `web.poecdn.com/api/currency-exchange/<hour>` is fully public, no OAuth needed
      (confirmed live). Ranks currency pairs by historical hourly spread% (a volatility
      signal, not a live-orderbook guarantee — the page says so explicitly). Currency ID ->
      display name resolved via RePoE's `base_items.min.json` (live-fetched, long-TTL
      cached); pairs with an unmapped ID are skipped rather than guessed. See
      `ARCHITECTURE.md` for the full writeup.
- [x] **Removed the admin gate from `/flip-advisor`** — now public: listed in `PAGES`, static
      site-menu entry, unhidden `/home` card, included in `sitemap.xml`.
- [x] **Campaign Guide pages** — `/campaign` (new, PoE1, 10 acts) and `/poe2-campaign`
      (rewritten from its old admin-only stub into a full public page, 4 acts + interludes,
      Early Access). Rush route, league-start/second-character item lists, and every
      quest/encounter granting a permanent bonus. Content generated from Python act data
      (`POE1_CAMPAIGN_ACTS`/`POE2_CAMPAIGN_ACTS`) via shared helpers next to
      `_favicon_data_uri()` — see the "Campaign Guide pages" section in `ARCHITECTURE.md` for
      the full sourcing/accuracy policy and the per-game league dropdown split
      (`--league-poe2`, separate `bossFarmLeaguePoe2` localStorage key). PoE1's route/quest
      data was rewritten from the open-source exile-leveling project's route dataset
      (github.com/HeartofPhos/exile-leveling, MIT) instead of guessed from memory — fixed real
      mistakes that guessing produced (e.g. "The Way Forward" is actually an Act 1 quest handed
      in at Lioneye's Watch, not an Act 2 Western Forest objective). PoE1's map images
      (`imgs/poe1/`) are shown exactly as supplied, no annotation — an auto-numbering attempt
      (color-detecting route nodes, connecting them by distance or hand-traced edges) was tried
      and dropped after repeated rounds still didn't match the real in-game path; branching
      routes have no reliable way to tell the intended path from an optional detour just from
      the image. PoE2 still uses the generated schematic SVG (`_campaign_act_svg()`, fixed to
      draw a real directional arrowhead toward each next node instead of a fixed up/down shape)
      since it has no images yet. Each PoE1 act also lists its Trial of Ascendancy zone(s), where
      present (Acts 4/5 have none) — sourced from the same exile-leveling dataset's `Complete
      {trial}` markers rather than poewiki (`Trial_of_Ascendancy`, Anubis-blocked again this
      session), with a `campaign_trial_note` explaining the Labyrinth/Ascendancy-points mechanic.
      **Update:** `/poe2-campaign` was deactivated back to admin-only per explicit request after
      being public for a while — `PAGE_REQUIRES_ADMIN = true` again, removed from `PAGES`, home
      card re-hidden with the "Admin only" badge, excluded from `sitemap.xml`, site-menu link
      back to client-side injection via `enableAdminUI()`'s `poe2Group` block. Its content/route
      data is unchanged, only the gating. `/campaign` (PoE1) is unaffected, still fully public.
- [x] **Swapped LinkedIn for email in the site footer** — `footer_made_by`/`footer_dm` +
      `SHARED_FOOTER_HTML` now show a byline (no link) and a `mailto:ericklucio.suv@gmail.com`
      contact link instead of the LinkedIn profile link, across every page.
- [x] **Added Google Analytics (gtag.js, `G-VNJJSYPYEQ`)** to `SHARED_HEAD_TEMPLATE` — live on
      every page.

## Pending

- [ ] **Atlas Tree / Scarab popularity page (PoE 1 only)** — new page showing the most-used
      atlas trees and scarabs, sourced from poe.ninja
      (https://poe.ninja/poe1/atlas-trees/allflame). Needs research into whether poe.ninja
      exposes this as a stable JSON API (like the existing economy/exchange endpoints this
      repo already scrapes) or only as rendered HTML — that determines whether it's a simple
      proxy like the existing `/ninja/*` routes or needs different handling.
- [ ] **Site-wide layout pass for Google Auto ads** — give Auto ads more natural
      whitespace/content breaks across `SHARED_CSS`/`SHARED_HEADER_HTML` (every page). No
      manual ad slots exist today (placement is fully automatic) — requested mid-session,
      deliberately deferred to its own focused pass rather than folded into the Campaign
      Guide work above.
