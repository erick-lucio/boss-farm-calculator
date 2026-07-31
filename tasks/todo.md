# Task Backlog

## Done

- [x] **Currency Exchange flip advisor (PoE 1 only)** — shipped as `/flip-advisor`
      (admin-only). Turned out the original scoping note was wrong: `web.poecdn.com/api/
      currency-exchange/<hour>` is fully public, no OAuth needed (confirmed live). Ranks
      currency pairs by historical hourly spread% (a volatility signal, not a live-orderbook
      guarantee — the page says so explicitly). Currency ID -> display name resolved via
      RePoE's `base_items.min.json` (live-fetched, long-TTL cached); pairs with an unmapped
      ID are skipped rather than guessed. See `ARCHITECTURE.md` for the full writeup.

## Pending

- [ ] **Atlas Tree / Scarab popularity page (PoE 1 only)** — new page showing the most-used
      atlas trees and scarabs, sourced from poe.ninja
      (https://poe.ninja/poe1/atlas-trees/allflame). Needs research into whether poe.ninja
      exposes this as a stable JSON API (like the existing economy/exchange endpoints this
      repo already scrapes) or only as rendered HTML — that determines whether it's a simple
      proxy like the existing `/ninja/*` routes or needs different handling.
