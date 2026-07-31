# Task Backlog

## Pending

- [ ] **Currency Exchange flip advisor (PoE 1 only)** — new page/feature that uses the
      official Currency Exchange API (https://www.pathofexile.com/developer/docs/reference#currencyexchange)
      to surface profitable currency-flipping opportunities. Needs an OAuth client
      (currency-exchange endpoints require authentication, unlike the plain trade
      search/fetch endpoints this repo already uses) — scope out the auth flow before
      implementation. PoE 1 only, not PoE 2.

- [ ] **Atlas Tree / Scarab popularity page (PoE 1 only)** — new page showing the most-used
      atlas trees and scarabs, sourced from poe.ninja
      (https://poe.ninja/poe1/atlas-trees/allflame). Needs research into whether poe.ninja
      exposes this as a stable JSON API (like the existing economy/exchange endpoints this
      repo already scrapes) or only as rendered HTML — that determines whether it's a simple
      proxy like the existing `/ninja/*` routes or needs different handling.
