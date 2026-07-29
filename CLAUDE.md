​# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## Workflow Orchestration

### 1. Plan Node Default
- Enter plan mode for ANY non-trivial task (3+ steps or architectural decisions)
- If something goes sideways, STOP and re-plan immediately – don't keep pushing
- Use plan mode for verification steps, not just building
- Write detailed specs upfront to reduce ambiguity

### 2. Subagent Strategy
- Use subagents liberally to keep main context window clean
- Offload research, exploration, and parallel analysis to subagents
- For complex problems, throw more compute at it via subagents
- One task per subagent for focused execution
- **Model selection**: Use the cheapest model (`haiku`) for code reading, exploration, and research subagents — reserve `sonnet`/`opus` only for tasks that require writing or complex reasoning

### 3. Self-Improvement Loop
- After ANY correction from the user: update `tasks/lessons.md` with the pattern
- Write rules for yourself that prevent the same mistake
- Ruthlessly iterate on these lessons until mistake rate drops
- Review lessons at session start for relevant project

### 3b. Session Init — Pending Task Review
- At the **start of every session**, read `tasks/todo.md` (if it exists)
- Identify all **pending/incomplete** items
- For each pending item, assess whether you have enough context to begin analysis (codebase knowledge, DB access, etc.)
- Present a concise summary to the user: list the pending items, mark which ones you can start on, and ask: **"Should I start analyzing any of these?"**
- Do NOT begin work on any item until the user confirms

### 4. Verification Before Done
- Never mark a task complete without proving it works
- Diff behavior between main and your changes when relevant
- Ask yourself: "Would a staff engineer approve this?"
- Run tests, check logs, demonstrate correctness
- **After every edit, explicitly list which services/projects were changed** (e.g. `credit-limit-service`, `lbd-cmn-tkl-get`, `web/`)

### 5. Demand Elegance (Balanced)
- For non-trivial changes: pause and ask "is there a more elegant way?"
- If a fix feels hacky: "Knowing everything I know now, implement the elegant solution"
- Skip this for simple, obvious fixes – don't over-engineer
- Challenge your own work before presenting it

### 6. Autonomous Bug Fixing
- When given a bug report: just fix it. Don't ask for hand-holding
- Point at logs, errors, failing tests – then resolve them
- Zero context switching required from the user
- Go fix failing CI tests without being told how

### 7. Database Access Rules
- **NEVER** execute DDL (`CREATE`, `ALTER`, `DROP`, `TRUNCATE`) or write operations (`INSERT`, `UPDATE`, `DELETE`, `MERGE`) against any database under any circumstance
- **Only `SELECT` statements are permitted** — read-only, always
- **Always ask the user for explicit confirmation before running any `SELECT`** — describe what query you intend to run and wait for approval before executing it
- If the task seems to require a write, stop and tell the user — never attempt a workaround
- **Both databases (`petronas-postgres` and `petronas-sqlserver`) are on a VPN** — if any connection fails, immediately tell the user: "Connection failed — please check your VPN connection and try again"
- `petronas-sqlserver` (`Infocenter4`) relates to the **Infocenter monolith and console applications**
- **IMPORTANT:** `petronas-sqlserver` always connects to `master` by default (dbhub ignores the DSN `database` param) — **every SQL Server query must be prefixed with `USE Infocenter4;`**
- `petronas-postgres` (`myInfocentre`) relates to the **MyInfocentre microservices and Lambda functions**

---

## Task Management

1. **Plan First**: Write plan to `tasks/todo.md` with checkable items
2. **Verify Plan**: Check in before starting implementation
3. **Track Progress**: Mark items complete as you go
4. **Explain Changes**: High-level summary at each step
5. **Document Results**: Add review section to `tasks/todo.md`
6. **Capture Lessons**: Update `tasks/lessons.md` after corrections

---

## Core Principles

- **Simplicity First**: Make every change as simple as possible. Impact minimal code.
- **No Laziness**: Find root causes. No temporary fixes. Senior developer standards.
- **Minimal Impact**: Changes should only touch what's necessary. Avoid introducing bugs.

## Code Quality Standards

Apply these principles on every change, **always respecting and following the existing architecture of the project being modified**. Never restructure or refactor the project's architectural style — work within it.

- **DRY (Don't Repeat Yourself)**: Extract shared logic; never duplicate behavior, constants, or queries.
- **Single Responsibility**: Each class, method, and module does one thing only.
- **Open/Closed**: Extend behavior without modifying existing stable code.
- **Dependency Inversion**: Depend on abstractions (interfaces), not concrete implementations.
- **Clean Architecture layers**: Respect the existing layer boundaries (Api → Domain → Infra). Never let Infra leak into Domain or Domain into Api.
- **Meaningful names**: Variables, methods, and classes must be self-explanatory — no abbreviations, no cryptic names.
- **Small functions**: Keep methods short and focused; if a function needs a comment to explain what it does, it needs to be broken down.
- **Fail fast**: Validate inputs at boundaries; throw specific, meaningful exceptions early.
- **No magic numbers/strings**: Use named constants or enums.
- **Consistent patterns**: Match the existing patterns in the file/service being modified — don't introduce a new style in isolation.

## Git Rules

- **Branches**: Create branches locally only — NEVER commit or push unless explicitly asked by the user.
- **Pull before branch**: Always `git pull origin master` before creating a new branch.

## Code Style

- **No comments**: Never add comments to code. Code must be self-explanatory.

---

## Task Backlog

All pending implementation tasks are tracked in **[`tasks/todo.md`](tasks/todo.md)**.
Read this at the start of every session (§3b above). Never lose this reference.

---

## Change History

All change history is tracked in [`change.md`](change.md) at the repo root.
After every implemented change, append a new row to the relevant service table in that file.

---

## Codebase Context

### What this is

`boss.py` is a single-file, zero-dependency (stdlib-only) Python 3 web dashboard that estimates farming profitability for Path of Exile 1 pinnacle/uber/nightmare-map bosses. It runs a local `ThreadingHTTPServer`, proxies live prices from poe.ninja (and poe.watch for one specific case — see Pricing below), and serves a single self-contained HTML page (inline CSS + vanilla JS, no build step, no frontend framework, no external JS libraries).

This repo has **no database** — §7 "Database Access Rules" above is boilerplate from an unrelated project template and does not apply here.

Run it with:
```
python boss.py                                  # defaults: league=Allflame, port=8000, poll=120s
python boss.py --league Standard --port 8000 --poll 120
```
`--poll` controls the browser's auto-refresh interval in seconds; the server itself caches upstream API responses for `CACHE_TTL` (300s) regardless of poll rate.

### File layout

- `boss.py` — the entire application (backend + embedded frontend).
- `.gitignore` — ignores `__pycache__/`, venvs, OS cruft, and `.claude/settings.local.json` + `.claude/scheduled_tasks.lock` (machine-local state, not shared).
- `CLAUDE.md` — this file.

### Backend architecture (`boss.py`)

1. **`ENTITIES`** (module-level list of dicts) — the only hand-maintained data in the file. Each dict is one boss *encounter* (normal and Uber versions are always separate entries, since they have different entry costs and loot pools). Schema, see the comment block directly above `ENTITIES` in the source for the authoritative version:
   - `name`, `tier` (`"normal"` | `"uber"` | `"nightmare"` — nightmare = T17 map bosses)
   - `entry`: list of `(item_name, poe.ninja type, quantity)` consumed to open the fight
   - `drops`: list of `(item_name, poe.ninja type, chance, source[, qty])` — `qty` (default 1) is units received per drop (e.g. stacked currency); `source` is `"wiki"` (real, sourced from poewiki with a sample size noted) or `"est"` (curated estimate — GGG does not publish official drop rates)
   - `quant_mod` (bool, optional): true if the fight's loot can be boosted by a player-chosen "increased quantity" source (Eldritch Altars for normal Searing Exarch/Eater of Worlds only — their Ubers can't be buffed; map IIQ for all 5 T17 Nightmare maps). Drives an adjustable frontend multiplier instead of a hardcoded percentage.
   - `access` (`"direct"` | `"map"` | omitted): whether you spawn straight into the boss room or have to navigate to it. Omitted where not verified — the frontend shows no badge rather than guessing.
   - `invuln` (string | omitted): short description of an invulnerability/untargetable phase. Omitted if none, or not verified.
   - `wiki` (string, optional): overrides the auto-generated poewiki.net page title when it doesn't match `name` (e.g. `"The Shaper & Elder"` → wiki page `"The Elder"`).

2. **`build_index(league)`** — fetches and merges pricing from poe.ninja (Exchange + Stash feeds, for Currency/Fragment) and the Item overview feed (for Unique*/Invitation/Map), plus poe.watch's compact feed for "Unidentified X" prices. All 11+ external HTTP calls are fired concurrently via `concurrent.futures.ThreadPoolExecutor` (cold-cache load ≈1.9s; this used to be fully serial and was the root cause of an intermittent "Failed to fetch" — see Pricing/Reliability notes below). Cached per-league in `_index_cache` for `CACHE_TTL` seconds.

   Key subtlety: poe.ninja returns **multiple price rows per item name** for anything with a random roll (corrupted-implicit weapons/armour, jewel affix combos, discrete variants like Doryani's Invitation's 4 elemental versions) — `put()` tracks the **minimum** across all same-name rows as the floor price (plus `chaos_max`/`chaos_n` for the ceiling and variant count), instead of the old behavior of just overwriting on whichever row loaded last (a real bug — it could silently pick up a multi-million-chaos jackpot outlier).

3. **`build_payload(league)`** — resolves each `ENTITIES` drop/entry against the price index, computes EV (`Σ chance × price × qty`, scaled by any `quant_mod` the user has dialed in), and three profit numbers per boss:
   - `net` — the average across many runs (EV − entry cost)
   - `worst` — nothing from the pool drops, −entry cost
   - `best` — the single most valuable pool item drops (**not** the sum of the whole pool at once — some pools can only ever yield one of several items per kill, e.g. Eater of Worlds' guaranteed one-of-three; summing everything would overstate the real ceiling)

   Also emits `wikiUrl` per boss (built via `wiki_url()`, reusing the same slug logic as the no-price item-link fallback) so every card links to a verifiable source regardless of whether its drop chances are `"wiki"` or `"est"`.

### Pricing methodology / reliability notes

- **Floor price, not average**: see `build_index` above. Additionally, `_get_watch_unidentified()` pulls poe.watch's dedicated `"Unidentified <name> [ilvl]"` rows (e.g. "Unidentified Watcher's Eye 86+") when they exist — that's the *actual* floor value an unidentified drop sells for, and wins over the poe.ninja floor when available. `res()` in `build_payload` only surfaces `chaosMax` (the "↑850c, rolls higher" UI marker) when it's a genuine ceiling above the chosen price — poe.watch's unidentified price can legitimately sit *above* poe.ninja's single identified listing in a young/volatile league, which would otherwise render as a nonsensical "ceiling below the floor."
- **poewiki.net is blocked to automated fetching** (an "Anubis" anti-bot challenge) as of 2026-07-29. Links to it in the UI are still correct and useful for a human clicking them in a real browser — don't remove them. But don't trust a research pass that claims to have "fetched poewiki and confirmed X" without checking whether it actually got content back or just the Anubis block page. Fallbacks that have worked: WebSearch (snippets, not full fetch), poedb.tw, maxroll.gg boss guides, vhpg.com, poe.watch's own data.
- **The "Failed to fetch" bug**: root cause was the old fully-serial fetch chain (11 poe.ninja calls each with a courtesy sleep, plus poe.watch's 12MB feed) taking long enough to occasionally drop the connection. Fixed by parallelizing (see `build_index`). Client-side (`load()`/`fetchData()`) also has a 60s `AbortSignal.timeout`, one silent background retry, and — if the dashboard already has data on screen — a failed sync now shows a small warning chip (`#warn`) instead of wiping the page.

### Frontend architecture (the `PAGE` string / embedded `<script>`)

- Bosses render as three independently-ranked sections (`GROUPS` const: Pinnacle / Uber / Nightmare), each sorted by `sortKey()` — **worst / avg / best**, selectable via the `#sortby` header control (`sortBy`, default `'best'`). `avg` uses `adjustedNet()` (accounts for `quant_mod` %); `worst`/`best` deliberately don't (a quantity buff raises the average, not what the single best/worst outcome is worth — see `profitBanner`). A left sidebar (`#sidenav`) mirrors the same groups as a clickable summary — worst/avg/best per boss, color-coded — with anchor links (`#group-<tier>`, `#b-<slug>`) that smooth-scroll via native `scroll-behavior:smooth` + `scroll-margin-top` (no manual scroll math), plus a brief highlight flash on the target card via a `hashchange` listener. (There used to be a small `.navlegend` header above the sidebar groups explaining worst/avg/best — removed per user request; the legend row up top and each number's hover popover cover it instead.)
- **×1/×10/×100 run multiplier** (`runMult`, header control): rescales all displayed profit/EV/worst/best numbers client-side from the last-loaded payload — no refetch.
- **Quantity control** (`quantctl`, per-card, only on `quantMod` bosses): +0/50/100/150/200% buttons, labeled "Eldritch Altar qty" or "Map IIQ" depending on `b.tier`. Stored in `quantState[bossName]`, read via `quantOf()`/`adjustedEv()`/`adjustedNet()`.
- **Time/rate control** (`timectl`, per-card): editable seconds-per-run input (`timeState[bossName]`, smart defaults 60s direct / 120s map / 90s unconfirmed access), drives a `≈Xc/hr` rate. Uses the `change` event (not `input`) so a full re-render doesn't steal focus mid-keystroke. This is intentionally a user input, not a hardcoded estimate — actual kill time is entirely gear/build dependent and GGG doesn't publish it either. (Note: its local variable is named `secs`, not `t` — `t` is the global translation function, see i18n below; a real shadowing bug happened here once.)
- **Access/invuln badges** (`metaBadges`): 🗺️ map / ⚡ direct / 🛡️ invuln (hover for the mechanic description, via the popover system below). No badge at all where the fact isn't verified — never a guessed default.
- **Rich hover popovers** (`#popover`, `showPopover()`/`hidePopover()`): a single `position:fixed` div positioned via `getBoundingClientRect()` (flips below if it'd clip the top of the viewport), driven by `data-info="KEY"` (static concept lookup into `I18N[lang][KEY]`) or `data-info-text="..."` (pre-baked per-instance text, e.g. a specific boss's invuln mechanic) on the trigger element. One delegated `mouseover`/`mouseout`/`focusin`/`focusout` listener set on `document` handles every trigger regardless of re-renders — don't add per-element listeners, they'd need rebinding on every `render()`.
- **EN / PT-BR translation** (`I18N` dict, `lang`, `t(key)`): covers every static label, control, and popover explanation — **never** boss/item names or anything from the API (poe.ninja/poe.watch/poewiki data is passed through untouched in every language). Static header/legend/note text is tagged `data-i18n="key"` and patched via `applyStaticI18n()` (`el.innerHTML = t(key)` for every match); dynamic content (cards, sidebar, controls, popovers) calls `t()` directly inside its own template function, so a language switch just calls `applyStaticI18n()` + `render(lastData)` again — no refetch. Two things that are easy to get wrong here if you extend this:
  - Icon glyphs must sit **outside** the translatable span (e.g. `&#128506; <span data-i18n="word_map">map</span>`, not inside it) or the innerHTML swap wipes them.
  - Per-boss dynamic text from the API (currently just `b.invuln`) isn't in `I18N` — it's translated via a separate exact-string lookup table, `INVULN_TR` (English text → Portuguese), applied through `tInvuln()`. If you add another API-sourced dynamic string that needs translating, follow that pattern rather than trying to route it through `I18N`.
  - Anything shown async after the initial render (the sync-failure warning chip/full-page error) must store a translation *key*, not pre-formatted text, or it goes stale when the user switches language before the next sync attempt. See `lastErrKey` + `updateWarnUI()`.
- **Footer**: disclaimer + data-source credit + LinkedIn link, both translated via `footer_disclaimer`/`footer_made_by` keys.
- All per-boss adjustable state (`quantState`, `timeState`, `quantState`'s sibling `sortBy`/`runMult`/`lang`) lives in plain JS variables/objects, persists across re-renders within a page session; `lang` additionally persists across reloads via `localStorage` (key `bossFarmLang`). Everything else resets on refresh — intentional, not a bug.

### Verification habits that have paid off in this repo

- After any JS-in-Python-string edit, actually validate the JS: extract the `<script>...</script>` block with a regex, substitute `__POLL_MS__`, and run `node --check` on it. `python -m py_compile` only proves the *Python* is valid — it says nothing about syntax errors inside the embedded JS string, which is most of this file's real logic.
- After touching `I18N`, cross-check key parity programmatically (every `data-i18n=`/`data-info=`/`t('...')` reference exists in **both** `en` and `pt` objects) rather than eyeballing a 70-key dictionary — it's caught real gaps every time it's been run.
- Before trusting any new item/boss-drop claim, check it exists on live poe.ninja (`curl` the relevant `type=` category from `ITEM_URL`/`EXCHANGE_URL` and grep the name) — a name that resolves to a real, currently-traded price is strong evidence against hallucination; a name that doesn't show up anywhere is a reason to hold off adding it.
- After any `ENTITIES` edit, sanity-check structure before trusting it (a manual edit once silently dropped an `"entry"` key while merging lines): `python -c "...; assert all('entry' in e and 'drops' in e for e in ENTITIES)"` style check, or just load the module and inspect.

### Lessons learned (patterns to keep applying)

- **Trust the user's firsthand game knowledge over web research when they conflict.** Multiple independent searches said Uber pinnacle fights need 5 fragments; the user said 4 and was right. Don't re-litigate a correction, don't hedge — just fix it.
- **Verify before asserting, especially for game mechanics.** Several confirmed mistakes this session (Original Sin's actual source, the fragment count above, Voidforge/The Balance of Terror boss attribution, "Betrayal's String" vs the real "Betrayal's Sting") came from trusting stale/wrong content or an earlier low-confidence guess. When research is genuinely unavailable or contradictory (see the T17 Ziggurat/Blazing Fragment pool question), ask the user rather than guessing — they're the domain authority for their own game.
- **Research subagents (esp. `haiku`) can give up too easily, hallucinate confidently, or genuinely conflict with each other.** One agent returned "COULD NOT VERIFY" for everything after only trying direct `WebFetch` on blocked pages — the same facts were findable seconds later via plain `WebSearch` (indexed snippets route around anti-bot blocks). Independent agents have also given contradictory answers about the same boss (e.g. whether Orb of Dominance belongs to The Maven) — when that happens, cross-check the disputed item directly (poe.ninja existence check, or a targeted `WebFetch` on poedb.tw, which — unlike poewiki.net — has not been blocked) rather than trusting whichever agent sounded more confident.
- **`WebSearch` has a hard per-session cap** (this session hit "200 of 200" mid-audit). When it's exhausted, fall back to `WebFetch` on non-poewiki sources (poedb.tw has worked reliably) and direct `curl`/poe.ninja API checks instead of stalling — most "does this item exist" questions don't need a search at all, just a live pricing-API lookup.
- **Don't hardcode values that are inherently player/build-dependent** (Eldritch Altar %, T17 IIQ, kill time) — expose an adjustable control with a sensible default instead of asserting a number that will be wrong for most readers.
- **A single research pass isn't the end state — re-auditing later catches real errors the first pass missed or introduced.** The full 27-entity re-audit (prompted by the user spot-checking one item) found and fixed: two items attributed to the wrong boss (Voidforge, The Balance of Terror), one boss's pool that had another encounter's drops mixed in (Sirus's normal pool had Conqueror-exclusive currency and an Uber-only unique), a wrong fragment in a T17 pool (Fortress: Synthesising → Lonely), a misspelled item name, a wrong item type, and several genuinely missing chase uniques across four bosses (Sirus, Uber Maven, Uber Searing Exarch, Uber Cortex) — all confirmed real via live poe.ninja data before being added, none guessed.
