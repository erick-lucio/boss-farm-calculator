#!/usr/bin/env python3
"""
poe_boss_dashboard.py
---------------------
LIVE web dashboard for pinnacle boss economy (PoE 1).

Each boss and its UBER version are separate entities (own card), since they
have different entry items and loot pools. Runs a local server that proxies
the poe.ninja API (avoids CORS) and serves a page with:
  - item icons (web.poecdn.com) + direct link to poe.ninja
  - price in chaos + divine conversion, using the realistic floor price for
    roll-dependent uniques (poe.watch's "Unidentified" price when published,
    else the lowest of poe.ninja's per-roll listings) instead of an arbitrary
    high-roll outlier
  - entry cost (with quantity; uber uses 4 fragments)
  - loot pool with chance per item (real, from poewiki, when available; else est.)
  - estimated profit per run = drop EV - entry cost
  - ranking by profit + AUTO-REFRESH

Zero dependencies (stdlib Python 3).

Usage:
    python poe_boss_dashboard.py
    python poe_boss_dashboard.py --league Standard --port 8000 --poll 120
"""

import argparse
import concurrent.futures
import json
import re
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
BASE = "https://poe.ninja/poe1/api/economy"
# poe.ninja has TWO price feeds for the same items, and they diverge:
#   stash    = listing prices (stash/web) — what the site usually shows
#   exchange = bulk currency-exchange prices — usually cheaper
# A divergence of tens of chaos on a fragment is normal between the two.
CURRENCY_URL = BASE + "/stash/current/currency/overview"
EXCHANGE_URL = BASE + "/exchange/current/overview"
ITEM_URL = BASE + "/stash/current/item/overview"

# poe.ninja lists ONE row per (name, variant) — items whose value depends on a
# random roll (corrupted implicits, jewel affix combos) get several rows under
# the same name at wildly different prices. poe.watch additionally publishes a
# dedicated "Unidentified <name> <ilvl>" row for some of those — the actual
# floor price the item sells for as-dropped, before anyone rolls/identifies it.
WATCH_URL = "https://api.poe.watch/compact"

# Manual price fix (chaos) — overrides everything, e.g. "Devouring Fragment": 120.0
PRICE_OVERRIDE = {}

UA = "boss-dashboard/1.0 (contact: you@example.com)"
CACHE_TTL = 300

ITEM_CATEGORIES = ["UniqueWeapon", "UniqueArmour", "UniqueAccessory",
                   "UniqueJewel", "UniqueFlask", "Invitation", "Map"]

# Currency/Fragment go through the Exchange+Stash feeds (see build_index); so
# does Astrolabe — poe.ninja only prices it via Exchange, but the shape is
# identical (items[]+lines[] with a primaryValue), so it merges the same way.
EXCHANGE_CATEGORIES = ["Currency", "Fragment", "Astrolabe"]

OVERVIEW_SLUG = {
    "Currency": "currency", "Fragment": "fragments",
    "UniqueWeapon": "unique-weapons", "UniqueArmour": "unique-armours",
    "UniqueAccessory": "unique-accessories", "UniqueJewel": "unique-jewels",
    "UniqueFlask": "unique-flasks", "Invitation": "invitations",
    "Map": "maps", "Astrolabe": "astrolabes",
}

UBER_FRAG_QTY = 4
DEFAULT_CHANCE = 0.05

# Astrolabes (Threads of the Originator / Originator Voidstone, added in this
# tool alongside the 3.28 mechanic): 10 named types, one per Atlas region
# encounter type. Requires completing "Threads of the Originator" and
# socketing the Originator Voidstone into the Atlas; doesn't drop in areas
# already affected by a Shaped Region (not modeled here — same simplification
# as every other prerequisite-gated unlock in this tool, e.g. reaching a
# pinnacle fight at all). Memory bosses (the 3 Incarnations) guarantee
# exactly one of these 10 per kill; other Atlas/map bosses only have a
# chance. Confirmed real, currently-traded poe.ninja category (type=Astrolabe,
# Exchange feed only) — not to be confused with "Venarius' Astrolabe", an
# unrelated pre-existing UniqueAccessory already in this file's Incarnation
# of Neglect pools.
ASTROLABE_TYPES = ["Fruiting Astrolabe", "Deceptive Astrolabe", "Templar Astrolabe",
                   "Lightless Astrolabe", "Runic Astrolabe", "Grasping Astrolabe",
                   "Fungal Astrolabe", "Nameless Astrolabe", "Chaotic Astrolabe",
                   "Timeless Astrolabe"]
# Sums to 1.0 — memory bosses guarantee exactly one Astrolabe per kill.
ASTROLABE_GUARANTEED = [(nm, "Astrolabe", 0.10, "est") for nm in ASTROLABE_TYPES]
# Curated low estimate (GGG publishes no rate) for the map-boss chance source.
ASTROLABE_MAP_CHANCE = [(nm, "Astrolabe", 0.005, "est") for nm in ASTROLABE_TYPES]

# --------------------------------------------------------------------------- #
# ENTITIES: each boss and its uber version are SEPARATE entries.
#   entry: list of (name, type, quantity) consumed to open the fight
#   drops: list of (name, type, chance, source[, qty]) — qty is the number of
#     units received when it drops (default 1; e.g. Eldritch Embers/Ichors
#     commonly drop 2-3 at once), factored into EV as chance * price * qty.
#     source: "wiki" (real) | "est"
# "wiki" chances come from poewiki (with sample size/patch noted in-line where
# relevant); "est" is a curated estimate — GGG does not publish official drop %.
#   quant_mod: True for fights whose loot can be boosted by a player-chosen
#     "increased quantity of items found" source — Eldritch Altars (normal
#     Searing Exarch / Eater of Worlds only, their Ubers open with pure
#     fragments and can't be buffed) or map IIQ (T17 Nightmare maps). The
#     frontend exposes an adjustable multiplier instead of a hardcoded
#     percentage, since the actual buff stacked varies per run.
#   access: "direct" (spawn straight in the boss room) or "map" (some
#     navigation/traversal needed first) — omitted where not verified.
#   invuln: short description of an invulnerability/untargetable phase, or
#     omitted if none / not verified. Sourced from poewiki/poedb/maxroll
#     boss guides; see change history for per-boss verification notes.
# --------------------------------------------------------------------------- #
ENTITIES = [
    # ------------------------------- Shaper -------------------------------- #
    {"name": "The Shaper", "tier": "normal", "access": "map",
     "entry": [("Fragment of the Phoenix", "Fragment", 1),
               ("Fragment of the Minotaur", "Fragment", 1),
               ("Fragment of the Hydra", "Fragment", 1),
               ("Fragment of the Chimera", "Fragment", 1)],
     "drops": [("Shaper's Exalted Orb", "Currency", 0.12, "est"),
               ("Starforge", "UniqueWeapon", 0.06, "est"),
               ("Disintegrator", "UniqueWeapon", 0.05, "est"),
               ("The Rippling Thoughts", "UniqueArmour", 0.05, "est")]},
    {"name": "Uber Shaper", "tier": "uber", "access": "map",
     "entry": [("Cosmic Fragment", "Fragment", UBER_FRAG_QTY)],
     "drops": [("Cosmic Reliquary Key", "Fragment", 0.05, "est"),
               ("Shaper's Exalted Orb", "Currency", 0.12, "est"),
               ("The Rippling Thoughts", "UniqueArmour", 0.06, "est")]},

    # ------------------------------- Elder --------------------------------- #
    {"name": "The Shaper & Elder", "tier": "normal", "wiki": "The Elder", "access": "map",
     "entry": [("Fragment of Knowledge", "Fragment", 1),
               ("Fragment of Terror", "Fragment", 1),
               ("Fragment of Emptiness", "Fragment", 1),
               ("Fragment of Shape", "Fragment", 1)],
     "drops": [("Watcher's Eye", "UniqueJewel", 0.30, "est"),
               ("Elder's Exalted Orb", "Currency", 0.12, "est"),
               ("Shaper's Exalted Orb", "Currency", 0.10, "est")]},
    {"name": "Uber Elder", "tier": "uber", "access": "map",
     "entry": [("Decaying Fragment", "Fragment", UBER_FRAG_QTY)],
     "drops": [("Decaying Reliquary Key", "Fragment", 0.05, "est"),
               ("Watcher's Eye", "UniqueJewel", 0.40, "est"),
               ("Elder's Exalted Orb", "Currency", 0.12, "est")]},

    # ------------------------------- Sirus --------------------------------- #
    {"name": "Sirus, Awakener", "tier": "normal", "wiki": "Sirus, Awakener of Worlds",
     "invuln": "splits into 4 illusions during Die Beam — only the real Sirus is attackable, the rest aren't",
     "entry": [("Al-Hezmin's Crest", "Fragment", 1),
               ("Baran's Crest", "Fragment", 1),
               ("Drox's Crest", "Fragment", 1),
               ("Veritania's Crest", "Fragment", 1)],
     "drops": [("Awakener's Orb", "Currency", 0.15, "est"),
               ("The Burden of Truth", "UniqueAccessory", 0.05, "est"),
               ("Crown of the Inward Eye", "UniqueArmour", 0.05, "est"),
               ("Hands of the High Templar", "UniqueArmour", 0.05, "est")]},
    {"name": "Uber Sirus", "tier": "uber",
     "invuln": "splits into 4 illusions during Die Beam — only the real Sirus is attackable, the rest aren't",
     "entry": [("Awakening Fragment", "Fragment", UBER_FRAG_QTY)],
     "drops": [("Oubliette Reliquary Key", "Fragment", 0.05, "est"),
               ("Awakener's Orb", "Currency", 0.15, "est"),
               ("The Saviour", "UniqueWeapon", 0.03, "est")]},

    # ------------------------------- Maven --------------------------------- #
    {"name": "The Maven", "tier": "normal",
     "invuln": "Memory Game — invulnerable while you repeat a lit pattern; a wrong step speeds up a lethal channel",
     "entry": [("The Maven's Writ", "Fragment", 1)],
     "drops": [("Maven's Orb", "Currency", 0.12, "est"),
               ("Orb of Dominance", "Currency", 0.05, "est")]},
    {"name": "Uber Maven", "tier": "uber",
     "invuln": "Memory Game — invulnerable while you repeat a lit pattern; a wrong step speeds up a lethal channel",
     "entry": [("Reality Fragment", "Fragment", UBER_FRAG_QTY)],
     "drops": [("Shiny Reliquary Key", "Fragment", 0.05, "est"),
               ("Maven's Orb", "Currency", 0.12, "est"),
               ("Orb of Dominance", "Currency", 0.08, "est"),
               ("Viridi's Veil", "UniqueArmour", 0.04, "est"),
               ("Impossible Escape", "UniqueJewel", 0.04, "est"),
               ("Progenesis", "UniqueFlask", 0.02, "est"),
               ("Grace of the Goddess", "UniqueWeapon", 0.03, "est"),
               ("Curio of Potential", "UniqueAccessory", 0.05, "est")]},

    # --------------------------- Searing Exarch ---------------------------- #
    {"name": "The Searing Exarch", "tier": "normal", "quant_mod": True, "access": "direct",
     "invuln": "Meteor Wall at ≤50% HP — invulnerable while unleashing meteors; destroy them to open a gap (1x)",
     "entry": [("Incandescent Invitation", "Invitation", 1)],
     "drops": [("Grand Eldritch Ember", "Currency", 0.30, "est"),
               ("Exceptional Eldritch Ember", "Currency", 0.15, "est"),
               ("Eldritch Exalted Orb", "Currency", 0.05, "est"),
               ("Eldritch Orb of Annulment", "Currency", 0.05, "est"),
               ("Eldritch Chaos Orb", "Currency", 0.05, "est"),
               ("Orb of Conflict", "Currency", 0.10, "est"),
               ("Forbidden Flame", "UniqueJewel", 0.05, "est")]},
    {"name": "Uber Searing Exarch", "tier": "uber", "access": "direct",
     "invuln": "Meteor Wall at ≤50% HP — invulnerable while unleashing meteors; destroy them to open a gap (1x, harsher)",
     "entry": [("Blazing Fragment", "Fragment", UBER_FRAG_QTY)],
     "drops": [("Archive Reliquary Key", "Fragment", 0.05, "est"),
               ("Forbidden Flame", "UniqueJewel", 0.05, "est"),
               ("Annihilation's Approach", "UniqueArmour", 0.04, "est"),
               ("Crystallised Omniscience", "UniqueAccessory", 0.03, "est"),
               ("Curio of Absorption", "UniqueAccessory", 0.05, "est"),
               ("The Annihilating Light", "UniqueWeapon", 0.03, "est")]},

    # ------------------- Eater of Worlds (poewiki, real) ------------------- #
    # https://www.poewiki.net/wiki/The_Eater_of_Worlds  (v3.26-3.27, n~625r/695u)
    {"name": "The Eater of Worlds", "tier": "normal", "quant_mod": True, "access": "map",
     "invuln": "Inescapable Doom at 75% HP — invulnerable while channeling a lethal blast; activate spheres to interrupt (1x)",
     "entry": [("Screaming Invitation", "Invitation", 1)],
     "drops": [("Exceptional Eldritch Ichor", "Currency", 0.15, "wiki"),
               ("Eldritch Exalted Orb", "Currency", 0.05, "wiki"),
               ("Eldritch Orb of Annulment", "Currency", 0.05, "wiki"),
               ("Eldritch Chaos Orb", "Currency", 0.05, "wiki"),
               ("Forbidden Flesh", "UniqueJewel", 0.05, "wiki")]},
    {"name": "Uber Eater of Worlds", "tier": "uber", "access": "map",
     "invuln": "Inescapable Doom at 75% HP — invulnerable while channeling a lethal blast; activate spheres to interrupt (1x, harsher)",
     "entry": [("Devouring Fragment", "Fragment", UBER_FRAG_QTY)],
     "drops": [("Ravenous Passion", "UniqueArmour", 0.68, "wiki"),
               ("Ashes of the Stars", "UniqueAccessory", 0.30, "wiki"),
               ("Nimis", "UniqueAccessory", 0.02, "wiki"),
               ("Curio of Consumption", "Fragment", 0.05, "wiki"),
               ("Visceral Reliquary Key", "Fragment", 0.01, "wiki"),
               ("Forbidden Flesh", "UniqueJewel", 0.05, "wiki")]},

    # --------------------------- Venarius / Cortex ------------------------- #
    {"name": "Venarius (Cortex)", "tier": "normal", "wiki": "Cortex (map)", "access": "direct",
     "invuln": "Venarius is never the actual target — you fight summoned Synthete adds for the whole encounter",
     "entry": [],
     "drops": [("Garb of the Ephemeral", "UniqueArmour", 0.05, "est")]},
    {"name": "Uber Cortex", "tier": "uber", "access": "direct",
     "invuln": "Venarius is never the actual target — you fight summoned Synthete adds for the whole encounter",
     "entry": [("Synthesising Fragment", "Fragment", UBER_FRAG_QTY)],
     "drops": [("Forgotten Reliquary Key", "Fragment", 0.05, "est"),
               ("Mask of the Tribunal", "UniqueArmour", 0.04, "est"),
               ("Nebulis", "UniqueWeapon", 0.04, "est"),
               ("Rational Doctrine", "UniqueJewel", 0.02, "est"),
               ("Circle of Ambition", "UniqueAccessory", 0.04, "est"),
               ("The Apostate", "UniqueArmour", 0.04, "est")]},

    # ------------------------------- Atziri -------------------------------- #
    {"name": "Atziri, Queen of the Vaal", "tier": "normal", "access": "map",
     "invuln": "Minion Phase — shields herself and becomes invulnerable while spawning health-draining monsters (1x)",
     "entry": [("Sacrifice at Dusk", "Fragment", 1),
               ("Sacrifice at Midnight", "Fragment", 1),
               ("Sacrifice at Dawn", "Fragment", 1),
               ("Sacrifice at Noon", "Fragment", 1)],
     "drops": [("Pledge of Hands", "UniqueWeapon", 0.05, "est"),
               ("Atziri's Promise", "UniqueFlask", 0.10, "est"),
               ("Atziri's Step", "UniqueArmour", 0.08, "est"),
               ("Doryani's Invitation", "UniqueAccessory", 0.06, "est")]},
    {"name": "Uber Atziri", "tier": "uber", "access": "map",
     "invuln": "Minion Phase (1x) + splits into 4 copies at 75/50/25% HP — only one is the real target (up to 4x total)",
     "entry": [("Mortal Grief", "Fragment", 1), ("Mortal Rage", "Fragment", 1),
               ("Mortal Ignorance", "Fragment", 1), ("Mortal Hope", "Fragment", 1)],
     "drops": [("Atziri's Disfavour", "UniqueWeapon", 0.06, "est"),
               ("Atziri's Acuity", "UniqueAccessory", 0.06, "est"),
               ("Atziri's Splendour", "UniqueArmour", 0.10, "est"),
               ("The Vertex", "UniqueArmour", 0.05, "est")]},

    # ----------------- Incarnations (Secrets of the Atlas) ----------------- #
    # Pools/tiers from Maxroll; % still estimated (adjust against poewiki).
    # Confirmed direct spawn, no invulnerability phase, for all 3 (normal+uber).
    {"name": "Incarnation of Fear", "tier": "normal", "access": "direct",
     "entry": [("Echo of Trauma", "Currency", 1)],
     "drops": [("Orb of Intention", "Currency", 0.12, "est"),
               ("Starcaller", "UniqueWeapon", 0.35, "est"),
               ("Coiling Whisper", "UniqueAccessory", 0.30, "est"),
               ("Servant of Decay", "UniqueAccessory", 0.20, "est"),
               ("Enmity's Embrace", "UniqueAccessory", 0.08, "est"),
               ("Bound By Destiny", "UniqueJewel", 0.05, "est"),
               *ASTROLABE_GUARANTEED]},
    {"name": "Uber Incarnation of Fear", "tier": "uber", "access": "direct",
     "entry": [("Traumatic Fragment", "Fragment", UBER_FRAG_QTY)],
     "drops": [("Traumatic Reliquary Key", "Fragment", 0.05, "est"),
               ("Orb of Intention", "Currency", 0.12, "est"),
               ("Starcaller", "UniqueWeapon", 0.35, "est"),
               ("Coiling Whisper", "UniqueAccessory", 0.30, "est"),
               ("Servant of Decay", "UniqueAccessory", 0.20, "est"),
               ("Enmity's Embrace", "UniqueAccessory", 0.08, "est"),
               ("Bound By Destiny", "UniqueJewel", 0.05, "est"),
               *ASTROLABE_GUARANTEED]},
    {"name": "Incarnation of Dread", "tier": "normal", "access": "direct",
     "entry": [("Echo of Reverence", "Currency", 1)],
     "drops": [("Orb of Unravelling", "Currency", 0.12, "est"),
               ("Whispers of Infinity", "UniqueAccessory", 0.45, "est"),
               ("The Dark Monarch", "UniqueArmour", 0.45, "est"),
               ("Seven Teachings", "UniqueAccessory", 0.08, "est"),
               ("Wine of the Prophet", "UniqueFlask", 0.02, "est"),
               ("Bound By Destiny", "UniqueJewel", 0.05, "est"),
               *ASTROLABE_GUARANTEED]},
    {"name": "Uber Incarnation of Dread", "tier": "uber", "access": "direct",
     "entry": [("Reverent Fragment", "Fragment", UBER_FRAG_QTY)],
     "drops": [("Reverent Reliquary Key", "Fragment", 0.05, "est"),
               ("Orb of Unravelling", "Currency", 0.12, "est"),
               ("Whispers of Infinity", "UniqueAccessory", 0.45, "est"),
               ("The Dark Monarch", "UniqueArmour", 0.45, "est"),
               ("Seven Teachings", "UniqueAccessory", 0.08, "est"),
               ("Wine of the Prophet", "UniqueFlask", 0.02, "est"),
               ("Bound By Destiny", "UniqueJewel", 0.05, "est"),
               *ASTROLABE_GUARANTEED]},
    {"name": "Incarnation of Neglect", "tier": "normal", "access": "direct",
     "entry": [("Echo of Loneliness", "Currency", 1)],
     "drops": [("Orb of Remembrance", "Currency", 0.12, "est"),
               ("Betrayal's Sting", "UniqueAccessory", 0.45, "est"),
               ("Arkhon's Tools", "UniqueAccessory", 0.45, "est"),
               ("Venarius' Astrolabe", "UniqueAccessory", 0.08, "est"),
               ("Legacy of the Rose", "UniqueWeapon", 0.02, "est"),
               ("Bound By Destiny", "UniqueJewel", 0.05, "est"),
               *ASTROLABE_GUARANTEED]},
    {"name": "Uber Incarnation of Neglect", "tier": "uber", "access": "direct",
     "entry": [("Lonely Fragment", "Fragment", UBER_FRAG_QTY)],
     "drops": [("Lonely Reliquary Key", "Fragment", 0.05, "est"),
               ("Orb of Remembrance", "Currency", 0.12, "est"),
               ("Betrayal's Sting", "UniqueAccessory", 0.45, "est"),
               ("Arkhon's Tools", "UniqueAccessory", 0.45, "est"),
               ("Venarius' Astrolabe", "UniqueAccessory", 0.08, "est"),
               ("Legacy of the Rose", "UniqueWeapon", 0.02, "est"),
               ("Bound By Destiny", "UniqueJewel", 0.05, "est"),
               *ASTROLABE_GUARANTEED]},

    # -------------------- T17 Nightmare Maps (area level 84) ----------------- #
    # Each T17 map has a Nightmare boss (nightmare version of the original boss).
    # Drops: uber fragments exclusive to that map's pool + 1 exclusive unique.
    # Entry: the T17 map itself (no tradeable fragment modeled in this tool).
    # Pool sources: poedb.tw + maxroll.gg/poe/currency/tier-17-boss-rushing
    # quant_mod: T17 fragment yield scales with map IIQ — base drop (no IIQ
    # affix) is always 1-3 fragments/kill; 2-3 at 235-250% IIQ; 2-4 at 250%+
    # (averaging ~2.5/kill at 235%+ IIQ). Multiple fragment TYPES can also
    # drop simultaneously, each in qty >1 (real example: one Ziggurat run
    # dropped 1x Blazing + 3x Devouring). No official per-fragment %, so —
    # like the Eldritch Altar control — this is exposed as a user-adjustable
    # multiplier rather than a hardcoded number.

    # -------------- Abomination — Nightmare of the Depraved Trinity ---------- #
    {"name": "Abomination - Depraved Trinity", "tier": "nightmare", "wiki": "Abomination Map",
     "access": "map", "quant_mod": True,
     "entry": [("Nightmare Map", "Map", 1)],
     "drops": [("Reality Fragment", "Fragment", 0.40, "est"),
               ("Blazing Fragment", "Fragment", 0.25, "est"),
               ("Synthesising Fragment", "Fragment", 0.25, "est"),
               ("Traumatic Fragment", "Fragment", 0.20, "est"),
               ("Malachai's Mark", "UniqueAccessory", 0.04, "est"),
               *ASTROLABE_MAP_CHANCE]},

    # ------------------ Citadel — Nightmare of Uhtred ----------------------- #
    {"name": "Citadel - Uhtred", "tier": "nightmare", "wiki": "Citadel Map",
     "access": "map", "quant_mod": True,
     "invuln": "boss submerges and becomes untargetable, then re-emerges — repeats through the fight",
     "entry": [("Nightmare Map", "Map", 1)],
     "drops": [("Cosmic Fragment", "Fragment", 0.35, "est"),
               ("Decaying Fragment", "Fragment", 0.30, "est"),
               ("Traumatic Fragment", "Fragment", 0.25, "est"),
               ("Lonely Fragment", "Fragment", 0.20, "est"),
               ("Manastorm", "UniqueWeapon", 0.04, "est"),
               *ASTROLABE_MAP_CHANCE]},

    # ----------------- Fortress — Nightmare of the Unbreakable -------------- #
    {"name": "Fortress - The Unbreakable", "tier": "nightmare", "wiki": "Fortress Map",
     "access": "map", "quant_mod": True,
     "entry": [("Nightmare Map", "Map", 1)],
     "drops": [("Lonely Fragment", "Fragment", 0.35, "est"),
               ("Decaying Fragment", "Fragment", 0.30, "est"),
               ("Cosmic Fragment", "Fragment", 0.25, "est"),
               ("Awakening Fragment", "Fragment", 0.20, "est"),
               ("Yoke of Suffering", "UniqueAccessory", 0.04, "est"),
               *ASTROLABE_MAP_CHANCE]},

    # ----------------- Sanctuary — Nightmare of Lycia ----------------------- #
    {"name": "Sanctuary - Lycia", "tier": "nightmare", "wiki": "Sanctuary Map",
     "access": "map", "quant_mod": True,
     "entry": [("Nightmare Map", "Map", 1)],
     "drops": [("Devouring Fragment", "Fragment", 0.40, "est"),
               ("Blazing Fragment", "Fragment", 0.30, "est"),
               ("Awakening Fragment", "Fragment", 0.25, "est"),
               ("Reverent Fragment", "Fragment", 0.20, "est"),
               ("The Dark Seer", "UniqueWeapon", 0.04, "est"),
               *ASTROLABE_MAP_CHANCE]},

    # ----------------- Ziggurat — Nightmare of Catarina --------------------- #
    {"name": "Ziggurat - Catarina", "tier": "nightmare", "wiki": "Ziggurat Map",
     "access": "map", "quant_mod": True,
     "invuln": "reportedly has invincibility phases per community guides, exact mechanic undocumented",
     "entry": [("Nightmare Map", "Map", 1)],
     "drops": [("Reality Fragment", "Fragment", 0.35, "est"),
               ("Devouring Fragment", "Fragment", 0.35, "est"),
               ("Synthesising Fragment", "Fragment", 0.25, "est"),
               ("Reverent Fragment", "Fragment", 0.20, "est"),
               ("Wraithlord", "UniqueArmour", 0.04, "est"),
               *ASTROLABE_MAP_CHANCE]},
]

# --------------------------------------------------------------------------- #
# Data collection (with cache)
# --------------------------------------------------------------------------- #
_cache = {}
_index_cache = {}
_lock = threading.Lock()


def slugify(name):
    s = name.lower().replace("'", "")
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s


def _get_json(url, league, typ):
    now = time.time()
    key = (url, league, typ)
    with _lock:
        c = _cache.get(key)
        if c and now - c[0] < CACHE_TTL:
            return c[1]
    qs = urllib.parse.urlencode({"league": league, "type": typ})
    req = urllib.request.Request(url + "?" + qs, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"  [warning] {typ}: {e}")
        data = {"lines": [], "currencyDetails": []}
    with _lock:
        _cache[key] = (now, data)
    time.sleep(0.4)
    return data


_UNIDED_ILVL_SUFFIX = re.compile(r"\s+\d+\+?$")


def _get_watch_unidentified(league):
    """base item name -> lowest poe.watch "Unidentified <name> [ilvl]" price.

    poe.watch splits some roll-dependent uniques (Watcher's Eye, Voices, ...)
    into separate rows per item-level tier once unidentified, e.g.
    "Unidentified Watcher's Eye 86+". That's the real floor value the item
    sells for as-dropped, before anyone rolls/identifies it.
    """
    now = time.time()
    key = ("watch-unided", league)
    with _lock:
        c = _cache.get(key)
        if c and now - c[0] < CACHE_TTL:
            return c[1]
    result = {}
    try:
        qs = urllib.parse.urlencode({"league": league})
        req = urllib.request.Request(WATCH_URL + "?" + qs, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=40) as r:
            data = json.loads(r.read().decode("utf-8"))
        for it in data.get("items", []):
            nm = it.get("name") or ""
            if not nm.startswith("Unidentified "):
                continue
            base = _UNIDED_ILVL_SUFFIX.sub("", nm[len("Unidentified "):])
            chaos = it.get("min")
            if chaos is None:
                chaos = it.get("mean")
            if chaos is None:
                continue
            if base not in result or chaos < result[base]:
                result[base] = chaos
    except Exception as e:
        print(f"  [warning] poe.watch unidentified prices: {e}")
    with _lock:
        _cache[key] = (now, result)
    return result


def build_index(league):
    """(name, type) -> {chaos, chaos_max, chaos_n, chaos_alt, divine, icon,
    detailsId, type, chaos_unided}.

    Keyed by (name, type) because the SAME name appears in different
    categories on poe.ninja — keying by name alone let one category
    overwrite another and swap the item's price (mapping bug).
    chaos     = lowest price seen for this (name, type); with roll-dependent
                items (corrupted implicits, jewel affixes) poe.ninja returns
                several rows per name at very different prices, so taking the
                floor avoids the EV silently picking up a jackpot-roll price.
    chaos_max = highest price seen — the identified/best-roll ceiling.
    chaos_n   = how many priced rows contributed (>1 means "value varies").
    chaos_alt = stash price (shown as a divergence when exchange also exists)
    chaos_unided = poe.watch's dedicated "Unidentified <name>" price, when
                published — the real floor value for an as-dropped item.
    """
    now = time.time()
    with _lock:
        c = _index_cache.get(league)
        if c and now - c[0] < CACHE_TTL:
            return c[1]

    # Fire every external request concurrently instead of one-at-a-time —
    # 13 poe.ninja calls plus poe.watch's heavy compact feed used to run
    # fully serial (each with its own timeout + courtesy sleep), so a single
    # slow/unreachable request could stall the whole page for a long time.
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        exchange_fut = {typ: ex.submit(_get_json, EXCHANGE_URL, league, typ)
                         for typ in EXCHANGE_CATEGORIES}
        stash_fut = {typ: ex.submit(_get_json, CURRENCY_URL, league, typ)
                      for typ in EXCHANGE_CATEGORIES}
        item_fut = {typ: ex.submit(_get_json, ITEM_URL, league, typ)
                    for typ in ITEM_CATEGORIES}
        watch_fut = ex.submit(_get_watch_unidentified, league)

        exchange_data = {typ: f.result() for typ, f in exchange_fut.items()}
        stash_data = {typ: f.result() for typ, f in stash_fut.items()}
        item_data = {typ: f.result() for typ, f in item_fut.items()}
        watch_unided = watch_fut.result()

    index = {}

    def put(name, typ, chaos, divine, icon, details, field="chaos"):
        key = (name, typ)
        e = index.setdefault(key, {"chaos": None, "chaos_max": None, "chaos_n": 0,
                                   "chaos_alt": None, "divine": None, "icon": None,
                                   "detailsId": slugify(name), "type": typ,
                                   "chaos_unided": None})
        if chaos is not None:
            cur = e.get(field)
            if cur is None or chaos < cur:
                e[field] = chaos
                if field == "chaos":
                    e["divine"] = divine
            if field == "chaos":
                e["chaos_max"] = chaos if e["chaos_max"] is None else max(e["chaos_max"], chaos)
                e["chaos_n"] += 1
        elif divine is not None and e.get("divine") is None:
            e["divine"] = divine
        if icon and not e.get("icon"):
            e["icon"] = icon
        if details:
            e["detailsId"] = details

    # 1) Exchange — primary price (the "chaos" field)
    # Different structure from the stash API:
    #   metadata is in "items" (not "currencyDetails"): id, name, detailsId
    #   price is in "lines": id + primaryValue (no chaosEquivalent/chaosValue)
    #   "items.image" is a relative path (/gen/image/...) — ignored for Currency/
    #   Fragment (icon comes from stash there, see section 2 below), but
    #   Astrolabe has no stash feed data at all (confirmed empty on poe.ninja),
    #   so its exchange-feed image — prefixed into a full poecdn URL — is the
    #   only icon source available for it.
    for typ in EXCHANGE_CATEGORIES:
        data = exchange_data[typ]
        id_to_meta = {d["id"]: d for d in data.get("items", []) if d.get("id")}
        for line in data.get("lines", []):
            item_id = line.get("id")
            meta = id_to_meta.get(item_id, {})
            nm = meta.get("name") or line.get("currencyTypeName") or line.get("name")
            if not nm:
                continue
            chaos = line.get("primaryValue")
            details = meta.get("detailsId") or item_id
            icon = "https://web.poecdn.com" + meta["image"] if typ == "Astrolabe" and meta.get("image") else None
            put(nm, typ, chaos, None, icon, details, "chaos")

    # 2) Stash — secondary price (the "chaos_alt" field); becomes "chaos" if exchange has none
    for typ in EXCHANGE_CATEGORIES:
        data = stash_data[typ]
        icons = {d.get("name"): d.get("icon")
                 for d in data.get("currencyDetails", []) if d.get("name")}
        for nm, ic in icons.items():
            put(nm, typ, None, None, ic, None)
        for line in data.get("lines", []):
            nm = line.get("currencyTypeName") or line.get("name")
            if not nm:
                continue
            chaos = line.get("chaosEquivalent")
            if chaos is None:
                chaos = line.get("chaosValue")
            put(nm, typ, chaos, line.get("divineValue"), icons.get(nm),
                line.get("detailsId"), "chaos_alt")

    # Fallback: if exchange has no price, use stash (no divergence shown)
    for e in index.values():
        if e["chaos"] is None and e["chaos_alt"] is not None:
            e["chaos"] = e["chaos_alt"]
            e["chaos_max"] = e["chaos_alt"]
            e["chaos_n"] = 1
            e["chaos_alt"] = None

    # 3) Items (uniques, maps, invitations) — only stash has these feeds
    for typ in ITEM_CATEGORIES:
        data = item_data[typ]
        for line in data.get("lines", []):
            nm = line.get("name")
            if not nm:
                continue
            chaos = line.get("chaosValue")
            if chaos is None:
                chaos = line.get("chaosEquivalent")
            put(nm, typ, chaos, line.get("divineValue"), line.get("icon"),
                line.get("detailsId"))

    # poe.watch's dedicated "Unidentified <name>" rows — the real as-dropped
    # floor price for roll-dependent uniques (e.g. Watcher's Eye), overriding
    # the poe.ninja floor above when available.
    for nm, chaos in watch_unided.items():
        for typ in ITEM_CATEGORIES:
            e = index.get((nm, typ))
            if e is not None:
                e["chaos_unided"] = chaos

    # Manual override wins over everything
    for (nm, typ), e in index.items():
        if nm in PRICE_OVERRIDE:
            e["chaos"] = PRICE_OVERRIDE[nm]
            e["chaos_unided"] = None
            e["override"] = True

    with _lock:
        _index_cache[league] = (now, index)
    return index


def wiki_url(name):
    return "https://www.poewiki.net/wiki/" + urllib.parse.quote(name.replace(" ", "_"))


def lookup(index, name, typ):
    """Look up by the declared type; if not found, try any category."""
    e = index.get((name, typ))
    if e and e.get("chaos") is not None:
        return e, False
    for (nm, t), v in index.items():
        if nm == name and v.get("chaos") is not None:
            # found under another category -> declared type in ENTITIES is wrong
            return v, (t != typ)
    return e or {}, False


def build_payload(league):
    league_slug = league.lower().replace(" ", "-")
    index = build_index(league)
    dv, _ = lookup(index, "Divine Orb", "Currency")
    divine_rate = dv.get("chaos")

    def res(name, typ):
        info, mismatched = lookup(index, name, typ)
        rtype = info.get("type", typ)
        chaos_floor = info.get("chaos")
        chaos_unided = info.get("chaos_unided")
        chaos_max = info.get("chaos_max")
        variants = info.get("chaos_n") or 0
        if chaos_unided is not None:
            # poe.watch's dedicated unidentified price beats the poe.ninja
            # floor — its "divine" belongs to a different priced row, so drop
            # it and let the frontend derive divine from the live rate.
            chaos, price_mode, divine = chaos_unided, "unidentified", None
        else:
            chaos = chaos_floor
            price_mode = "variants" if variants > 1 else "single"
            divine = info.get("divine")
        alt = info.get("chaos_alt")
        # divergence between the two poe.ninja feeds (stash vs exchange)
        diverge = None
        if chaos_floor and alt and chaos_floor > 0:
            d = abs(chaos_floor - alt) / chaos_floor
            if d >= 0.10:
                diverge = {"alt": alt, "pct": round(d * 100)}
        if chaos is not None:
            slug = OVERVIEW_SLUG.get(rtype) or OVERVIEW_SLUG.get(typ, "currency")
            details = info.get("detailsId") or slugify(name)
            url = f"https://poe.ninja/poe1/economy/{league_slug}/{slug}/{details}"
            link_src = "ninja"
        else:
            url = wiki_url(name)
            link_src = "wiki"
        # only surface chaos_max as an "upside" ceiling when it actually is one —
        # poe.watch's unidentified price can legitimately sit above poe.ninja's
        # single identified listing in a young/volatile league
        has_upside = chaos_max is not None and chaos and chaos_max > chaos * 1.05
        return {"name": name, "type": rtype, "declared_type": typ,
                "chaos": chaos, "divine": divine,
                "chaosMax": chaos_max if has_upside else None,
                "priceMode": price_mode,
                "icon": info.get("icon"), "url": url, "link_src": link_src,
                "diverge": diverge, "type_mismatch": mismatched,
                "override": bool(info.get("override"))}

    ents = []
    for e in ENTITIES:
        eitems, ecost, ehas = [], 0.0, False
        for name, typ, qty in e["entry"]:
            r = res(name, typ)
            r["qty"] = qty
            eitems.append(r)
            if r["chaos"] is not None:
                ecost += r["chaos"] * qty
                ehas = True
        entry = {"items": eitems, "total_chaos": ecost if ehas else None}

        ditems, ev, best_item = [], 0.0, 0.0
        for drop in e["drops"]:
            name, typ, chance, src = drop[:4]
            qty = drop[4] if len(drop) > 4 else 1
            r = res(name, typ)
            ch = chance if chance is not None else DEFAULT_CHANCE
            r["chance"] = ch
            r["chance_src"] = src
            r["qty"] = qty
            ditems.append(r)
            if r["chaos"] is not None:
                value = r["chaos"] * qty
                ev += ch * value
                best_item = max(best_item, value)
        drops = {"items": ditems, "ev_chaos": ev}

        # A single kill is a lottery, not a guaranteed average: most runs get
        # nothing from the pool (worst case) or the single best item that
        # rolled (best case) — NOT every pool item at once, which overstates
        # pools where only one of several items can ever drop per kill (e.g.
        # Eater of Worlds' guaranteed one-of-three). "net"/EV is the average
        # across many runs, not what any single run actually looks like.
        has_cost = entry["total_chaos"] is not None
        net = (ev - entry["total_chaos"]) if has_cost else None
        worst = (-entry["total_chaos"]) if has_cost else None
        best = (best_item - entry["total_chaos"]) if has_cost else None
        ents.append({"name": e["name"], "tier": e["tier"], "entry": entry,
                     "drops": drops, "ev_chaos": ev, "net": net,
                     "worst": worst, "best": best,
                     "quantMod": bool(e.get("quant_mod")),
                     "access": e.get("access"), "invuln": e.get("invuln"),
                     "wikiUrl": wiki_url(e.get("wiki", e["name"]))})

    return {"league": league, "leagueSlug": league_slug, "divineRate": divine_rate,
            "source": "exchange→stash", "updated": int(time.time()),
            "cacheTtl": CACHE_TTL, "bosses": ents}


# --------------------------------------------------------------------------- #
# Frontend
# --------------------------------------------------------------------------- #
PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Boss Farm Estimator</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Ctext y='.9em' font-size='90'%3E%F0%9F%92%80%3C/text%3E%3C/svg%3E">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Cinzel:wght@500;700&family=Spectral:ital,wght@0,400;0,600;1,400&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#0b0b0f; --panel:#14141c; --panel2:#1b1b26; --line:#2a2a38;
  --ink:#cdc7ba; --ink-dim:#8a8477; --gold:#c9a24c; --gold-bright:#e6c264;
  --unique:#d18b3f; --uber:#7db6e6; --nightmare:#c1685a; --ok:#8fae6a; --shadow:rgba(0,0,0,.55);
  --bg-glow:#1a1622; --overlay:rgba(255,255,255,.03); --overlay-soft:rgba(255,255,255,.015);
  --neg:#d98b6a; --warn:#e0b050; --body-weight:400;
}
:root[data-theme="light"]{
  --bg:#f3ede0; --panel:#ffffff; --panel2:#faf6ec; --line:#ddd3ba;
  --ink:#2b2620; --ink-dim:#6c6151; --gold:#9c7527; --gold-bright:#7e5e1c;
  --unique:#a6591a; --uber:#2f6ea8; --nightmare:#a13d2c; --ok:#4c7a34; --shadow:rgba(90,80,60,.18);
  --bg-glow:#fff6df; --overlay:rgba(0,0,0,.035); --overlay-soft:rgba(0,0,0,.02);
  --neg:#b5462a; --warn:#8a5a12; --body-weight:600;
}
*{box-sizing:border-box}
html,body{margin:0}
html{scroll-behavior:smooth}
body{
  background:radial-gradient(1200px 600px at 50% -10%, var(--bg-glow) 0%, transparent 60%), var(--bg);
  color:var(--ink); font-family:"Spectral",Georgia,serif; font-size:15px; font-weight:var(--body-weight);
  line-height:1.4; padding:0 0 64px;
}
a{color:inherit; text-decoration:none}
.wrap{max-width:1240px; margin:0 auto; padding:0 20px}

header{border-bottom:1px solid var(--line);
  background:linear-gradient(180deg,var(--panel),var(--bg));
  position:sticky; top:0; z-index:5; backdrop-filter:blur(6px);}
.head{display:flex; align-items:center; gap:20px; padding:18px 20px; flex-wrap:wrap}
.brand-block{display:flex; flex-direction:column; gap:6px}
.brand{font-family:"Cinzel",serif; font-weight:700; letter-spacing:.14em; text-transform:uppercase}
.brand b{display:block; color:var(--gold-bright); font-weight:700; font-size:20px}
.brand span{color:var(--ink-dim); font-size:11px; letter-spacing:.28em}
.meta-left{display:flex; align-items:center; gap:10px; flex-wrap:wrap}
.chip-sm{display:flex; align-items:center; gap:6px; font-size:10.5px; color:var(--ink-dim);
  border:1px solid var(--line); border-radius:2px; padding:3px 8px; background:var(--panel);}
.chip-sm b{color:var(--gold-bright); font-family:ui-monospace,monospace; font-weight:600}
.meta{margin-left:auto; display:flex; align-items:center; gap:18px; flex-wrap:wrap}
.chip{display:flex; align-items:center; gap:8px; font-size:13px; color:var(--ink-dim);
  border:1px solid var(--line); border-radius:2px; padding:6px 12px; background:var(--panel);}
.chip[hidden]{display:none}
.chip b{color:var(--gold-bright); font-family:ui-monospace,monospace; font-weight:600}
.dot{width:7px; height:7px; border-radius:50%; background:var(--ok);
  box-shadow:0 0 0 0 rgba(143,174,106,.6); animation:pulse 2.4s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(143,174,106,.5)}
  70%{box-shadow:0 0 0 9px rgba(143,174,106,0)}100%{box-shadow:0 0 0 0 rgba(143,174,106,0)}}
button.sync{font-family:"Cinzel",serif; font-weight:700; font-size:11px; letter-spacing:.18em;
  text-transform:uppercase; color:var(--bg); background:var(--gold); border:0;
  padding:8px 16px; cursor:pointer; border-radius:2px;}
button.sync:hover{background:var(--gold-bright)}
button.sync:disabled{opacity:.5; cursor:default}

.langsel{font-family:ui-monospace,monospace; font-size:12px; color:var(--ink-dim);
  background:var(--panel); border:1px solid var(--line); border-radius:2px;
  padding:6px 8px; cursor:pointer}
.langsel:hover{color:var(--ink)}

.themetoggle{font-family:ui-monospace,monospace; font-size:14px; line-height:1;
  color:var(--ink-dim); background:var(--panel); border:1px solid var(--line);
  border-radius:2px; padding:6px 10px; cursor:pointer}
.themetoggle:hover{color:var(--ink)}

.runs{display:flex; border:1px solid var(--line); border-radius:2px; overflow:hidden}
.runs button{font-family:ui-monospace,monospace; font-size:12px; color:var(--ink-dim);
  background:var(--panel); border:0; border-right:1px solid var(--line);
  padding:6px 11px; cursor:pointer}
.runs button:last-child{border-right:0}
.runs button:hover{color:var(--ink)}
.runs button.active{background:var(--gold); color:var(--bg); font-weight:700}

.sortby{display:flex; align-items:center; gap:0; border:1px solid var(--line); border-radius:2px; overflow:hidden}
.sortby-lbl{font-family:"Cinzel",serif; font-weight:700; font-size:10px; letter-spacing:.1em; text-transform:uppercase;
  color:var(--ink-dim); padding:0 8px; white-space:nowrap}
.sortby button{font-family:ui-monospace,monospace; font-size:12px; color:var(--ink-dim);
  background:var(--panel); border:0; border-left:1px solid var(--line);
  padding:6px 10px; cursor:pointer; text-transform:capitalize}
.sortby button:hover{color:var(--ink)}
.sortby button.active{background:var(--gold); color:var(--bg); font-weight:700}

.legend{display:flex; gap:20px; padding:12px 0 4px; color:var(--ink-dim); font-size:12px; flex-wrap:wrap}
.legend i{display:inline-block; width:9px; height:9px; margin-right:6px; border-radius:2px; vertical-align:middle}
.legend-range{display:inline-flex; gap:10px; font-family:ui-monospace,monospace; text-transform:uppercase; letter-spacing:.04em}
.legend-badges{display:inline-flex; align-items:center; gap:6px}

[data-info], [data-info-text]{cursor:help; text-decoration:underline dotted; text-underline-offset:3px; text-decoration-color:currentColor; opacity:.9}
[data-info]:hover, [data-info]:focus, [data-info-text]:hover, [data-info-text]:focus{opacity:1}
.badge[data-info-text]{text-decoration:none}
.popover{display:none; position:fixed; z-index:50; max-width:280px;
  background:var(--panel2); border:1px solid var(--line); border-radius:4px; padding:10px 12px;
  box-shadow:0 14px 34px -14px var(--shadow); font-family:"Spectral",Georgia,serif;
  font-size:12.5px; line-height:1.55; color:var(--ink); pointer-events:none}
.popover b{color:var(--gold-bright)}
.popover.show{display:block}

.layout{display:flex; align-items:flex-start; gap:28px}
.main{flex:1; min-width:0}

.sidenav{flex:0 0 210px; width:210px; position:sticky; top:88px;
  max-height:calc(100vh - 104px); overflow-y:auto; padding:2px 10px 20px 0}
.sidenav::-webkit-scrollbar{width:6px}
.sidenav::-webkit-scrollbar-thumb{background:var(--line); border-radius:3px}
.navgroup{margin-top:18px}
.navgroup:first-child{margin-top:14px}
.navtitle{display:block; font-family:"Cinzel",serif; font-weight:700; font-size:11px; letter-spacing:.14em;
  text-transform:uppercase; color:var(--gold-bright); padding:0 0 6px; margin-bottom:6px;
  border-bottom:1px solid var(--line)}
.navgroup.uber .navtitle{color:var(--uber)}
.navgroup.nightmare .navtitle{color:var(--nightmare)}
.navitem{display:flex; flex-direction:column; gap:1px; font-size:12px;
  color:var(--ink-dim); padding:4px 2px; border-radius:3px}
.navitem:hover{color:var(--ink); background:var(--overlay)}
.navitem .nm{white-space:nowrap; overflow:hidden; text-overflow:ellipsis}
.navitem .nr{font-family:ui-monospace,monospace; font-size:9.5px; display:flex; gap:6px}
.navitem .nr.na{color:var(--ink-dim)}
@media (max-width:880px){
  .layout{flex-direction:column}
  .sidenav{position:static; width:100%; max-height:none; display:flex; flex-wrap:wrap;
    gap:4px 18px; padding:0 0 10px; border-bottom:1px solid var(--line)}
  .navgroup{margin-top:0; display:flex; flex-wrap:wrap; align-items:baseline; gap:0 10px}
  .navtitle{border-bottom:0; padding:0; margin-bottom:0; margin-right:4px}
  .navitem{padding:2px 0; flex-direction:row}
  .navitem .nr{display:none}
}

.group{padding-top:26px; scroll-margin-top:80px}
.group:first-of-type{padding-top:14px}
.group-head{display:flex; align-items:baseline; gap:10px; padding-bottom:12px;
  border-bottom:1px solid var(--line)}
.group-head h3{margin:0; font-family:"Cinzel",serif; font-weight:700; font-size:13px; letter-spacing:.18em;
  text-transform:uppercase; color:var(--gold-bright)}
.group.uber .group-head h3{color:var(--uber)}
.group.nightmare .group-head h3{color:var(--nightmare)}
.group-head .count{font-family:ui-monospace,monospace; font-size:11px; color:var(--ink-dim)}
.group-head .desc{margin-left:auto; font-size:11.5px; color:var(--ink-dim)}

.grid{display:grid; grid-template-columns:repeat(auto-fill,minmax(340px,1fr)); gap:18px; padding-top:14px}
.card{background:linear-gradient(180deg,var(--panel),var(--panel2));
  border:1px solid var(--line); border-radius:4px; overflow:hidden;
  box-shadow:0 10px 30px -18px var(--shadow); display:flex; flex-direction:column;
  scroll-margin-top:80px}
.card.flash{animation:flash 1.4s ease-out}
@keyframes flash{0%{box-shadow:0 0 0 2px var(--gold-bright), 0 10px 30px -18px var(--shadow)}
  100%{box-shadow:0 10px 30px -18px var(--shadow)}}
.card.uber{border-color:#33465a}
.card.nightmare{border-color:#4a3230}
.card h2{margin:0; font-family:"Cinzel",serif; font-weight:700; font-size:14px; letter-spacing:.04em;
  color:var(--gold-bright); padding:13px 15px; border-bottom:1px solid var(--line);
  background:linear-gradient(90deg,rgba(201,162,76,.09),transparent);
  display:flex; align-items:center; gap:9px}
.card.uber h2{color:var(--uber); background:linear-gradient(90deg,rgba(125,182,230,.10),transparent)}
.card.nightmare h2{color:var(--nightmare); background:linear-gradient(90deg,rgba(193,104,90,.10),transparent)}
.meta-badges{display:flex; gap:6px; padding:8px 15px 0; flex-wrap:wrap}
.badge{font-family:ui-monospace,monospace; font-size:10px; color:var(--ink-dim);
  border:1px solid var(--line); border-radius:2px; padding:2px 6px; cursor:help}
.badge.invuln{color:var(--nightmare); border-color:#4a3230}

.quantctl, .timectl{display:flex; align-items:center; gap:6px; padding:8px 15px;
  border-bottom:1px solid var(--line); background:var(--overlay-soft); flex-wrap:wrap}
.quantctl .qlbl, .timectl .qlbl{font-family:"Cinzel",serif; font-weight:700; font-size:9px; letter-spacing:.14em;
  text-transform:uppercase; color:var(--ink-dim); margin-right:2px}
.quantctl button{font-family:ui-monospace,monospace; font-size:10.5px; color:var(--ink-dim);
  background:var(--panel); border:1px solid var(--line); border-radius:2px;
  padding:2px 7px; cursor:pointer}
.quantctl button:hover{color:var(--ink)}
.quantctl button.active{background:var(--gold); color:var(--bg); border-color:var(--gold); font-weight:700}
.timeinput{width:52px; font-family:ui-monospace,monospace; font-size:11px; color:var(--ink);
  background:var(--panel); border:1px solid var(--line); border-radius:2px; padding:2px 5px}
.timectl span:not(.qlbl){font-family:ui-monospace,monospace; font-size:10.5px; color:var(--ink-dim)}
.timectl .rate{margin-left:auto; font-weight:700}
.rank{font-family:ui-monospace,monospace; font-size:12px; color:var(--bg);
  background:var(--gold); border-radius:2px; padding:1px 7px; font-weight:700}
.rank.dim{background:var(--line); color:var(--ink-dim)}
.hname{flex:1; min-width:0; white-space:nowrap; overflow:hidden; text-overflow:ellipsis}
.tier{font-family:"Cinzel",serif; font-weight:700; font-size:9px; letter-spacing:.16em; text-transform:uppercase;
  padding:2px 7px; border-radius:2px; border:1px solid var(--line); color:var(--ink-dim)}
.tier.uber{color:var(--uber); border-color:#33465a}
.tier.nightmare{color:var(--nightmare); border-color:#4a3230}

.profit{display:flex; justify-content:space-between; align-items:baseline; gap:8px;
  padding:11px 15px 6px; background:var(--overlay-soft)}
.profit .lbl{font-family:"Cinzel",serif; font-weight:700; font-size:10px; letter-spacing:.2em;
  text-transform:uppercase; color:var(--ink-dim)}
.profit .val{font-family:ui-monospace,monospace; font-size:17px; font-weight:600}
.profit .val .div{font-size:12px; opacity:.8}
.pos{color:var(--ok)} .neg{color:var(--neg)} .na{color:var(--ink-dim)} .ink{color:var(--ink)}
.range{display:flex; justify-content:space-between; gap:10px; padding:0 15px 10px;
  background:var(--overlay-soft); font-family:ui-monospace,monospace; font-size:11px}
.range .rw, .range .rb{cursor:help; opacity:.85}
.evline{font-family:ui-monospace,monospace; font-size:11px; color:var(--ink-dim);
  padding:2px 15px 9px; border-bottom:1px solid var(--line)}

.section{padding:11px 15px}
.section+.section{border-top:1px dashed var(--line)}
.slabel{display:flex; justify-content:space-between; align-items:baseline;
  font-family:"Cinzel",serif; font-weight:700; font-size:10px; letter-spacing:.2em; text-transform:uppercase;
  color:var(--ink-dim); margin-bottom:7px}
.total{font-family:ui-monospace,monospace; color:var(--gold-bright); letter-spacing:0}
.total .div{color:var(--uber); font-size:12px}

.item{display:flex; align-items:center; gap:10px; padding:5px 6px; border-radius:3px; transition:background .12s}
.item:hover{background:var(--overlay)}
.item img{width:26px; height:26px; object-fit:contain; flex:0 0 26px;
  filter:drop-shadow(0 1px 2px rgba(0,0,0,.6))}
.item .nm{flex:1; min-width:0; white-space:nowrap; overflow:hidden; text-overflow:ellipsis}
.item .nm.currency,.item .nm.fragment,.item .nm.invitation{color:var(--gold)}
.item .nm.uniqueweapon,.item .nm.uniquearmour,.item .nm.uniqueaccessory,
.item .nm.uniquejewel,.item .nm.uniqueflask{color:var(--unique)}
.item .qty{font-family:ui-monospace,monospace; font-size:11px; color:var(--gold-bright);
  flex:0 0 auto}
.item .ch{font-family:ui-monospace,monospace; font-size:10px; flex:0 0 46px; text-align:right}
.item .ch.real{color:var(--gold)} .item .ch.est{color:var(--ink-dim); font-style:italic}
.item .px{font-family:ui-monospace,monospace; font-size:13px; color:var(--ink);
  text-align:right; flex:0 0 auto; min-width:56px}
.item .px .div{display:block; color:var(--uber); font-size:11px; opacity:.85}
.item .px .nd{color:var(--ink-dim)}
.warn{color:var(--warn); font-size:11px; cursor:help}
.item .px .alt{display:block; color:var(--warn); font-size:10px; opacity:.9}
.item .px .roll{display:block; color:var(--unique); font-size:10px; opacity:.85; cursor:help}
.ext{opacity:0; font-size:11px; color:var(--ink-dim); transition:opacity .12s}
.item:hover .ext{opacity:1}
.empty{color:var(--ink-dim); font-size:12px; padding:2px 0}

.note{color:var(--ink-dim); font-size:12.5px; padding:18px 0; line-height:1.6}
.err{color:var(--neg); padding:16px 0}

footer{border-top:1px solid var(--line); margin-top:24px;
  background:linear-gradient(0deg,var(--panel),var(--bg))}
.foot{max-width:1240px; margin:0 auto; padding:16px 20px; display:flex;
  justify-content:space-between; align-items:center; gap:20px; flex-wrap:wrap;
  color:var(--ink-dim); font-size:11.5px; line-height:1.5}
.foot-linkedin{flex:0 0 auto; color:var(--gold); white-space:nowrap; font-weight:600}
.foot-linkedin:hover{color:var(--gold-bright)}
@media (prefers-reduced-motion:reduce){*{animation:none!important}}
</style>
</head>
<body>
<header>
  <div class="head">
    <div class="brand-block">
      <div class="brand"><b>&#128128; Boss Farm Estimator</b><span data-i18n="tagline">PATH OF EXILE · BOSS ECONOMY</span></div>
      <div class="meta-left">
        <span class="chip-sm"><span data-i18n="chip_price">Price</span> <b id="src">—</b></span>
        <span class="chip-sm"><span data-i18n="chip_sync">Sync</span> <b id="ago">—</b> · <span data-i18n="chip_next">next</span> <b id="next">—</b></span>
      </div>
    </div>
    <div class="meta">
      <div class="chip"><span class="dot"></span><span data-i18n="chip_league">League</span> <b id="league">—</b></div>
      <select class="langsel" id="leaguesel" aria-label="league / liga"></select>
      <div class="chip">1 Divine <b id="divine">—</b> chaos</div>
      <div class="chip warn" id="warn" hidden></div>
      <select class="langsel" id="langsel" aria-label="language / idioma">
        <option value="en">EN</option>
        <option value="pt">PT-BR</option>
      </select>
      <button class="themetoggle" id="themetoggle" title="dark/light · escuro/claro" aria-label="toggle theme">&#127769;</button>
      <div class="sortby" id="sortby" role="group" aria-label="sort order">
        <span class="sortby-lbl" data-i18n="sortby_label">Order by</span>
        <button data-sort="best" class="active" data-i18n="word_best">best</button>
        <button data-sort="avg" data-i18n="word_avg">avg</button>
        <button data-sort="worst" data-i18n="word_worst">worst</button>
      </div>
      <div class="runs" id="runs" role="group" aria-label="runs multiplier">
        <button data-mult="1" class="active">×1</button>
        <button data-mult="10">×10</button>
        <button data-mult="100">×100</button>
      </div>
      <button class="sync" id="sync" data-i18n="btn_refresh">Refresh</button>
    </div>
  </div>
</header>

<div class="wrap">
  <div class="legend">
    <span><i style="background:var(--gold)"></i><span data-i18n="legend_currency">currency / fragment</span></span>
    <span><i style="background:var(--unique)"></i><span data-i18n="legend_unique">unique</span></span>
    <span><span class="tier uber">uber</span> <span class="tier nightmare">nightmare</span> <span data-i18n="legend_tiers">separate entities (own entry + loot)</span></span>
    <span><span style="color:var(--gold)">30%</span> <span data-i18n="legend_chance_confirmed">confirmed chance (poewiki)</span> · <span style="color:var(--ink-dim);font-style:italic">~5%</span> <span data-i18n="legend_chance_est">estimated</span></span>
    <span><span style="color:var(--unique)">&#8593;850c</span> <span data-i18n="legend_roll">value varies by roll — price shown is the floor</span></span>
    <span class="legend-range">
      <span class="neg" data-info="worst" data-i18n="word_worst">worst</span>
      <span class="ink" data-info="avg" data-i18n="word_avg">avg</span>
      <span class="pos" data-info="best" data-i18n="word_best">best</span>
    </span>
    <span class="legend-badges">
      <span class="badge" data-info="access_map">&#128506; <span data-i18n="word_map">map</span></span>/<span class="badge" data-info="access_direct">&#9889; <span data-i18n="word_direct">direct</span></span> <span data-i18n="legend_badges_access">access</span>
      · <span class="badge invuln" data-info="invuln">&#128737; <span data-i18n="word_invuln">invuln</span></span> <span data-i18n="legend_badges_phases">phase(s)</span>
    </span>
    <span><span class="pos">#1</span> <span data-i18n="legend_rank">ranked by average profit/run, within each category</span></span>
  </div>
  <div class="layout">
    <nav class="sidenav" id="sidenav"><div class="note">—</div></nav>
    <div class="main">
      <div id="root"><div class="note" data-i18n="loading">Loading data from poe.ninja…</div></div>
      <div class="note" data-i18n="note">
        Bosses are grouped into three categories — <b>Pinnacle</b>, <b>Uber</b>
        (opens with <b>4 fragments</b>), and <b>Nightmare</b> (T17 map bosses) —
        each ranked separately since their entry cost and loot pool aren't
        comparable across categories. Use the menu on the left to jump straight
        to a category or a specific boss, and the <b>×1/×10/×100</b> control up
        top to see totals over multiple runs instead of a single one.
        A single kill isn't its average — most runs return nothing from the
        loot pool and you eat the entry cost, occasionally one item hits.
        <b>Avg. profit/run</b> is the EV across many runs (EV = Σ(chance ×
        price × qty), qty = units per drop, shown as <span class="qty">×N</span>
        when &gt;1, e.g. stacked currency), bracketed by <span class="neg">worst</span>
        (nothing drops, −entry cost) and <span class="pos">best</span> (the single
        most valuable pool item drops — not the whole pool at once, since some
        pools can only ever drop one of several items per kill). The
        normal Searing Exarch and Eater of Worlds invitations can be buffed
        with "increased quantity of items found" via Eldritch Altars while
        mapping (their Uber versions can't be), and T17 Nightmare map
        fragment yield scales hard with map IIQ (confirmed 1-2 fragments/kill
        at low IIQ up to 3-4 at 400%+, multiple types at once) — set that
        card's quantity control to match what you're actually running; it
        scales that card's EV and ranking accordingly. <b>Time/run</b> is
        editable per boss (defaults: 60s direct-access, 120s if you have to
        navigate to the boss, 90s unconfirmed) and drives the <b>≈c/hr</b>
        rate shown next to it — GGG doesn't publish kill times either, and
        they're entirely gear/build dependent, so plug in your own. The
        <span class="badge" style="cursor:default">&#128506;</span>/<span class="badge" style="cursor:default">&#9889;</span>
        badge shows whether you spawn straight into the fight or have to
        reach it first, and <span class="badge invuln" style="cursor:default">&#128737;</span>
        flags a phase where the boss can't be damaged — hover it for what
        triggers it. Both are sourced from poewiki/community guides same as
        drop chances, and some bosses simply don't have a confirmed answer
        yet (no badge shown rather than a guess).
        Chances in <span style="color:var(--gold)">gold</span> come from
        <a href="https://www.poewiki.net" target="_blank" style="color:var(--gold)">poewiki</a>
        (currently: Eater of Worlds); the <span style="color:var(--ink-dim);font-style:italic">~gray</span>
        ones are still estimates — GGG doesn't publish official drop rates, so edit the
        4 fields of each drop in <code>ENTITIES</code> (name, type, chance, source) to plug
        in real % as they're confirmed per boss. Each card title links to its poewiki page
        for verification. Live prices from poe.ninja (~1×/hour). Fragments and most
        uniques have one fixed price, but some drops (jewels with random affix rolls,
        corrupted-implicit weapons/armour) list many prices for the same name — the EV
        always uses the realistic floor, not a lucky-roll outlier: poe.watch's dedicated
        "Unidentified" price when published (e.g. Watcher's Eye), otherwise the lowest
        price poe.ninja lists for that name. A <span style="color:var(--unique)">&#8593;850c</span>
        marker next to a price means it can roll higher once identified — hover it for
        the ceiling. No price → <span class="px"><span class="nd">n/a</span></span>,
        excluded from EV, and the link goes to <b>poewiki</b> instead (avoids a 404). An
        item with a price links to its real detail page on poe.ninja.
      </div>
    </div>
  </div>
</div>

<footer>
  <div class="foot">
    <span data-i18n="footer_disclaimer">Unofficial fan tool — not affiliated with or endorsed by Grinding Gear Games. Prices from poe.ninja/poe.watch, drop data from poewiki/community guides — see the note above for sourcing and caveats.</span>
    <a class="foot-linkedin" href="https://www.linkedin.com/in/erick-lucioo/" target="_blank" rel="noopener">
      <span data-i18n="footer_made_by">Built by Erick Lúcio</span> — LinkedIn &#8599;
    </a>
  </div>
</footer>

<div class="popover" id="popover" role="tooltip"></div>

<script>
const POLL_MS = __POLL_MS__;
let lastUpdated = 0;
let runMult = 1;

let theme = (function(){
  try { return localStorage.getItem('bossFarmTheme') || 'dark'; } catch(e) { return 'dark'; }
})();
function applyTheme(){
  document.documentElement.setAttribute('data-theme', theme);
  const btn = document.getElementById('themetoggle');
  if(btn) btn.innerHTML = theme === 'dark' ? '&#127769;' : '&#9728;&#65039;';
}

let currentLeague = (function(){
  try { return localStorage.getItem('bossFarmLeague') || null; } catch(e) { return null; }
})();
function populateLeagueOptions(data){
  const sel = document.getElementById('leaguesel');
  if(sel.options.length) return; // populate once — data.league doesn't change mid-session
  const opts = [data.league, 'Standard', 'Hardcore', 'Hardcore ' + data.league];
  const seen = new Set();
  const cur = currentLeague || data.league;
  sel.innerHTML = opts.filter(o => o && !seen.has(o) && seen.add(o))
    .map(o => `<option value="${o}" ${o===cur?'selected':''}>${o}</option>`).join('');
}

// UI-only translation dictionary. Never applied to boss/item names, API
// query params, or any data fetched from poe.ninja/poe.watch/poewiki —
// only to static labels/copy and the info popovers below.
const I18N = {
en: {
  tagline: 'PATH OF EXILE · BOSS ECONOMY',
  chip_league: 'League', chip_price: 'Price', chip_sync: 'Sync', chip_next: 'next',
  btn_refresh: 'Refresh', btn_syncing: 'Syncing…',
  legend_currency: 'currency / fragment', legend_unique: 'unique',
  legend_tiers: 'separate entities (own entry + loot)',
  legend_chance_confirmed: 'confirmed chance (poewiki)', legend_chance_est: 'estimated',
  legend_roll: 'value varies by roll — price shown is the floor',
  legend_badges_access: 'access', legend_badges_phases: 'phase(s)',
  legend_rank: 'ranked by average profit/run, within each category',
  word_worst: 'worst', word_avg: 'avg', word_best: 'best',
  word_map: 'map', word_direct: 'direct', word_invuln: 'invuln',
  word_normal: 'normal', word_uber: 'uber', word_nightmare: 'nightmare',
  loading: 'Loading data from poe.ninja…',
  group_normal_title: 'Pinnacle Bosses', group_normal_desc: 'base fights',
  group_uber_title: 'Uber Bosses', group_uber_desc: 'uber pinnacle fights · 4 fragments',
  group_nightmare_title: 'Nightmare Maps', group_nightmare_desc: 'T17 nightmare map bosses',
  group_ranked_suffix: ' · ranked by {word}',
  sortby_label: 'Order by',
  entry_label: 'Entry', entry_empty: 'via map / mechanic (no tradeable fragment)',
  loot_label: 'Loot pool · chance',
  profit_avg_run: 'Avg. profit/run', profit_avg_mult: 'Avg. profit · {n} runs',
  profit_entry_na: 'entry n/a', ev_drops: 'EV drops', entry_word: 'entry',
  quant_altar_label: 'Eldritch Altar qty', quant_iiq_label: 'Map IIQ',
  time_label: 'Time/run',
  tt_no_price: 'no price · poewiki', tt_ninja: 'poe.ninja',
  tt_estimate: 'estimate', tt_wiki: 'poewiki', tt_qty_factored: '{n} units per drop, factored into EV',
  err_timeout: 'poe.ninja/poe.watch took too long to respond',
  err_fetch: 'failed to reach local server or a network hiccup',
  err_load_suffix: '. Check the league name and whether poe.ninja is reachable.',
  warn_sync_failed: 'sync failed: ',
  warn_retry_title: 'last good data shown above; will retry in {s}s',
  footer_disclaimer: 'Unofficial fan tool — not affiliated with or endorsed by Grinding Gear Games. Prices from poe.ninja/poe.watch, drop data from poewiki/community guides — see the note above for sourcing and caveats.',
  footer_made_by: 'Built by Erick Lúcio',
  worst: 'Nothing from the loot pool drops this run — every independent chance roll misses, so you just lose the entry cost. Not a hypothetical: most bosses have no guaranteed drop, so this happens often.',
  avg: '<b>Expected value across many runs</b>, not what any single run looks like: EV = Σ(chance × price × qty) summed over every item in the loot pool, minus entry cost. Over enough repeats your real results converge to this number — over a handful of runs they usually don’t.',
  best: 'The single most valuable item in the loot pool drops — not the whole pool at once. Some pools can only ever yield one of several items per kill (e.g. Eater of Worlds’ guaranteed one-of-three pick), so summing every item would overstate the real ceiling.',
  access_map: 'You don’t spawn directly on the boss — some navigation is needed first: a short walk across the arena, or a full map/dungeon to clear. Factor the extra time into <b>Time/run</b>.',
  access_direct: 'You spawn straight into the boss room, no navigation needed.',
  invuln: 'This boss has a phase where it can’t be damaged. Hover the badge on its own card for the specific mechanic and how many times it triggers — more/longer invulnerable phases mean a longer run, so factor that into <b>Time/run</b> too.',
  quant_altar: 'This fight’s loot can be boosted by a player-chosen "increased quantity of items found" source: <b>Eldritch Altars</b> while mapping (normal Searing Exarch / Eater of Worlds only — their Ubers open with pure fragments and can’t be buffed). Since what you actually stack varies every run, this scales the card’s EV/average/ranking by the % you pick here, rather than assuming one fixed number for everyone.',
  quant_iiq: 'T17 Nightmare map fragment yield scales with map <b>Item Quantity (IIQ)</b>: base drop (no IIQ affix) is always 1-3 fragments; at 235-250% IIQ it’s 2-3; at 250%+ it’s 2-4, averaging ~2.5 fragments/kill at 235%+. Set this to roughly match your map’s IIQ — it scales this card’s EV/average/ranking accordingly, since GGG doesn’t publish exact per-fragment odds.',
  time: 'Editable seconds per run, including any map navigation and the fight itself. Defaults: 60s for a direct spawn, 120s if you have to navigate to the boss, 90s where that isn’t confirmed yet. GGG doesn’t publish kill times — they’re entirely gear/build dependent — so this is deliberately a plain input you set, not a calculated guess. It drives the <b>≈c/hr</b> rate: average profit per run ÷ time per run × 3600.',
  rate: 'Average profit per run ÷ time per run, expressed per hour. Uses the <b>average</b> (not worst/best), and ignores the ×1/×10/×100 control since it’s already a rate — running the boss 10× doesn’t change your chaos-per-hour.',
  ev: '<b>Expected value of the loot pool</b>: Σ(chance × price × qty) for every drop — the price used is the realistic floor (poe.watch’s unidentified price when available, otherwise the lowest poe.ninja listing), not a lucky-roll outlier. Multiplied by the quantity control if this boss has one. This is the same number behind "avg" above; entry cost is subtracted separately to get profit.',
  note: `Bosses are grouped into three categories — <b>Pinnacle</b>, <b>Uber</b>
    (opens with <b>4 fragments</b>), and <b>Nightmare</b> (T17 map bosses) —
    each ranked separately since their entry cost and loot pool aren't
    comparable across categories. Use the menu on the left to jump straight
    to a category or a specific boss, and the <b>×1/×10/×100</b> control up
    top to see totals over multiple runs instead of a single one.
    A single kill isn't its average — most runs return nothing from the
    loot pool and you eat the entry cost, occasionally one item hits.
    <b>Avg. profit/run</b> is the EV across many runs (EV = Σ(chance ×
    price × qty), qty = units per drop, shown as <span class="qty">×N</span>
    when &gt;1, e.g. stacked currency), bracketed by <span class="neg">worst</span>
    (nothing drops, −entry cost) and <span class="pos">best</span> (the single
    most valuable pool item drops — not the whole pool at once, since some
    pools can only ever drop one of several items per kill). The
    normal Searing Exarch and Eater of Worlds invitations can be buffed
    with "increased quantity of items found" via Eldritch Altars while
    mapping (their Uber versions can't be), and T17 Nightmare map
    fragment yield scales with map IIQ (always 1-3 fragments/kill at
    base, 2-3 at 235-250% IIQ, 2-4 at 250%+, averaging ~2.5/kill at
    235%+) — set that card's quantity control to match what you're
    actually running; it scales that card's EV and ranking accordingly. <b>Time/run</b> is
    editable per boss (defaults: 60s direct-access, 120s if you have to
    navigate to the boss, 90s unconfirmed) and drives the <b>≈c/hr</b>
    rate shown next to it — GGG doesn't publish kill times either, and
    they're entirely gear/build dependent, so plug in your own. The
    <span class="badge" style="cursor:default">&#128506;</span>/<span class="badge" style="cursor:default">&#9889;</span>
    badge shows whether you spawn straight into the fight or have to
    reach it first, and <span class="badge invuln" style="cursor:default">&#128737;</span>
    flags a phase where the boss can't be damaged — hover it for what
    triggers it. Both are sourced from poewiki/community guides same as
    drop chances, and some bosses simply don't have a confirmed answer
    yet (no badge shown rather than a guess).
    Chances in <span style="color:var(--gold)">gold</span> come from
    <a href="https://www.poewiki.net" target="_blank" style="color:var(--gold)">poewiki</a>
    (currently: Eater of Worlds); the <span style="color:var(--ink-dim);font-style:italic">~gray</span>
    ones are still estimates — GGG doesn't publish official drop rates, so edit the
    4 fields of each drop in <code>ENTITIES</code> (name, type, chance, source) to plug
    in real % as they're confirmed per boss. Each card title links to its poewiki page
    for verification. Live prices from poe.ninja (~1×/hour). Fragments and most
    uniques have one fixed price, but some drops (jewels with random affix rolls,
    corrupted-implicit weapons/armour) list many prices for the same name — the EV
    always uses the realistic floor, not a lucky-roll outlier: poe.watch's dedicated
    "Unidentified" price when published (e.g. Watcher's Eye), otherwise the lowest
    price poe.ninja lists for that name. A <span style="color:var(--unique)">&#8593;850c</span>
    marker next to a price means it can roll higher once identified — hover it for
    the ceiling. No price → <span class="px"><span class="nd">n/a</span></span>,
    excluded from EV, and the link goes to <b>poewiki</b> instead (avoids a 404). An
    item with a price links to its real detail page on poe.ninja.`,
},
pt: {
  tagline: 'PATH OF EXILE · ECONOMIA DE BOSS',
  chip_league: 'Liga', chip_price: 'Preço', chip_sync: 'Sincronia', chip_next: 'próximo',
  btn_refresh: 'Atualizar', btn_syncing: 'Sincronizando…',
  legend_currency: 'moeda / fragmento', legend_unique: 'único',
  legend_tiers: 'entidades separadas (entrada + loot próprios)',
  legend_chance_confirmed: 'chance confirmada (poewiki)', legend_chance_est: 'estimada',
  legend_roll: 'valor varia pela rolagem — preço mostrado é o piso',
  legend_badges_access: 'acesso', legend_badges_phases: 'fase(s)',
  legend_rank: 'classificado pelo lucro médio/run, dentro de cada categoria',
  word_worst: 'pior', word_avg: 'média', word_best: 'melhor',
  word_map: 'mapa', word_direct: 'direto', word_invuln: 'invul',
  word_normal: 'normal', word_uber: 'uber', word_nightmare: 'nightmare',
  loading: 'Carregando dados do poe.ninja…',
  group_normal_title: 'Bosses Pinnacle', group_normal_desc: 'lutas base',
  group_uber_title: 'Bosses Uber', group_uber_desc: 'lutas uber pinnacle · 4 fragmentos',
  group_nightmare_title: 'Mapas Nightmare', group_nightmare_desc: 'bosses de mapa T17 nightmare',
  group_ranked_suffix: ' · classificado por {word}',
  sortby_label: 'Ordenar por',
  entry_label: 'Entrada', entry_empty: 'via mapa / mecânica (sem fragmento negociável)',
  loot_label: 'Pool de loot · chance',
  profit_avg_run: 'Lucro médio/run', profit_avg_mult: 'Lucro médio · {n} runs',
  profit_entry_na: 'entrada n/d', ev_drops: 'EV dos drops', entry_word: 'entrada',
  quant_altar_label: 'Qtd. Altar Eldritch', quant_iiq_label: 'IIQ do Mapa',
  time_label: 'Tempo/run',
  tt_no_price: 'sem preço · poewiki', tt_ninja: 'poe.ninja',
  tt_estimate: 'estimativa', tt_wiki: 'poewiki', tt_qty_factored: '{n} unidades por drop, considerado no EV',
  err_timeout: 'poe.ninja/poe.watch demorou demais pra responder',
  err_fetch: 'falha ao alcançar o servidor local ou instabilidade de rede',
  err_load_suffix: '. Confira o nome da liga e se o poe.ninja está acessível.',
  warn_sync_failed: 'sincronização falhou: ',
  warn_retry_title: 'últimos dados bons mostrados acima; tentará de novo em {s}s',
  footer_disclaimer: 'Ferramenta de fã não-oficial — sem afiliação ou endosso da Grinding Gear Games. Preços do poe.ninja/poe.watch, dados de drop do poewiki/guias da comunidade — veja a nota acima pras fontes e ressalvas.',
  footer_made_by: 'Feito por Erick Lúcio',
  worst: 'Nada do loot pool dropa nesse run — todas as chances independentes falham, então você só perde o custo de entrada. Não é hipotético: a maioria dos bosses não tem drop garantido, então isso acontece com frequência.',
  avg: '<b>Valor esperado ao longo de muitos runs</b>, não o que um único run parece: EV = Σ(chance × preço × qtd) somado por todo item do loot pool, menos o custo de entrada. Com repetições suficientes seus resultados reais convergem pra esse número — em poucos runs, geralmente não.',
  best: 'O item mais valioso do loot pool dropa sozinho — não o pool inteiro de uma vez. Alguns pools só permitem um entre vários itens por kill (ex: a escolha garantida de um-entre-três do Eater of Worlds), então somar todos os itens superestimaria o teto real.',
  access_map: 'Você não nasce direto no boss — é preciso navegar antes: uma caminhada curta pela arena, ou um mapa/masmorra inteiro pra limpar. Considere esse tempo extra no <b>Tempo/run</b>.',
  access_direct: 'Você nasce direto na sala do boss, sem precisar navegar.',
  invuln: 'Esse boss tem uma fase em que não pode receber dano. Passe o mouse no badge do card dele pra ver o mecanismo específico e quantas vezes ativa — fases invulneráveis mais longas/frequentes significam um run mais demorado, então considere isso no <b>Tempo/run</b> também.',
  quant_altar: 'O loot dessa luta pode ser aumentado por uma fonte de "quantidade aumentada de itens encontrados" escolhida pelo jogador: <b>Altares Eldritch</b> durante o mapa (apenas Searing Exarch / Eater of Worlds normais — as versões Uber abrem só com fragmentos e não podem ser bufadas). Como o que você realmente acumula varia a cada run, isso escala o EV/média/ranking do card pela % escolhida aqui, em vez de assumir um número fixo pra todo mundo.',
  quant_iiq: 'O rendimento de fragmentos dos mapas T17 Nightmare escala com o <b>Item Quantity (IIQ)</b> do mapa: o drop base (sem afixo de IIQ) é sempre 1-3 fragmentos; em 235-250% de IIQ fica 2-3; em 250%+ fica 2-4, com média de ~2,5 fragmentos/kill em 235%+. Ajuste isso pra bater aproximadamente com o IIQ do seu mapa — isso escala o EV/média/ranking desse card de acordo, já que a GGG não publica as odds exatas por fragmento.',
  time: 'Segundos por run, editável, incluindo qualquer navegação de mapa e a luta em si. Padrões: 60s pra spawn direto, 120s se precisar navegar até o boss, 90s onde isso ainda não foi confirmado. A GGG não publica tempos de kill — dependem totalmente do build/equipamento — então esse é propositalmente um campo que você mesmo preenche, não uma estimativa calculada. Ele alimenta a taxa <b>≈c/h</b>: lucro médio por run ÷ tempo por run × 3600.',
  rate: 'Lucro médio por run ÷ tempo por run, expresso por hora. Usa a <b>média</b> (não pior/melhor), e ignora o controle ×1/×10/×100 já que isso já é uma taxa — rodar o boss 10× não muda seu chaos-por-hora.',
  ev: '<b>Valor esperado do loot pool</b>: Σ(chance × preço × qtd) de cada drop — o preço usado é o piso realista (o preço "não identificado" do poe.watch quando disponível, senão a listagem mais baixa do poe.ninja), não um outlier de sorte. Multiplicado pelo controle de quantidade se esse boss tiver um. É o mesmo número por trás da "média" acima; o custo de entrada é subtraído separadamente pra chegar no lucro.',
  note: `Bosses são agrupados em três categorias — <b>Pinnacle</b>, <b>Uber</b>
    (abre com <b>4 fragmentos</b>), e <b>Nightmare</b> (bosses de mapa T17) —
    cada uma classificada separadamente já que o custo de entrada e o loot
    pool não são comparáveis entre categorias. Use o menu à esquerda pra
    pular direto pra uma categoria ou um boss específico, e o controle
    <b>×1/×10/×100</b> no topo pra ver totais de múltiplos runs em vez de
    um único. Um kill não é a sua média — a maioria dos runs não retorna
    nada do loot pool e você só perde o custo de entrada, ocasionalmente
    um item acerta. <b>Lucro médio/run</b> é o EV ao longo de muitos runs
    (EV = Σ(chance × preço × qtd), qtd = unidades por drop, mostrado como
    <span class="qty">×N</span> quando &gt;1, ex: moeda empilhada), balizado
    por <span class="neg">pior</span> (nada dropa, −custo de entrada) e
    <span class="pos">melhor</span> (o item mais valioso do pool dropa
    sozinho — não o pool inteiro de uma vez, já que alguns pools só
    conseguem dropar um entre vários itens por kill). Os convites normais
    de Searing Exarch e Eater of Worlds podem ser bufados com "quantidade
    aumentada de itens encontrados" via Altares Eldritch durante o mapa
    (suas versões Uber não podem), e o rendimento de fragmentos dos mapas
    T17 Nightmare escala com o IIQ do mapa (sempre 1-3 fragmentos/kill no
    base, 2-3 em 235-250% de IIQ, 2-4 em 250%+, com média de ~2,5/kill em
    235%+) — ajuste o controle de quantidade daquele card pra bater com o
    que você realmente está rodando; isso escala o EV e o ranking daquele
    card de acordo. <b>Tempo/run</b> é editável por boss (padrões: 60s
    acesso direto, 120s se precisar navegar até o boss, 90s não
    confirmado) e alimenta a taxa <b>≈c/h</b> mostrada ao lado — a GGG
    também não publica tempos de kill, e eles dependem totalmente do
    build/equipamento, então preencha com o seu. O badge
    <span class="badge" style="cursor:default">&#128506;</span>/<span class="badge" style="cursor:default">&#9889;</span>
    mostra se você nasce direto na luta ou precisa chegar até ela, e
    <span class="badge invuln" style="cursor:default">&#128737;</span>
    sinaliza uma fase em que o boss não pode receber dano — passe o mouse
    pra ver o que ativa. Ambos vêm do poewiki/guias da comunidade, igual
    as chances de drop, e alguns bosses simplesmente não têm uma resposta
    confirmada ainda (nenhum badge mostrado em vez de um chute).
    Chances em <span style="color:var(--gold)">dourado</span> vêm do
    <a href="https://www.poewiki.net" target="_blank" style="color:var(--gold)">poewiki</a>
    (atualmente: Eater of Worlds); as <span style="color:var(--ink-dim);font-style:italic">~cinza</span>
    ainda são estimativas — a GGG não publica taxas de drop oficiais, então
    edite os 4 campos de cada drop em <code>ENTITIES</code> (nome, tipo,
    chance, fonte) pra colocar os % reais conforme forem confirmados por
    boss. O título de cada card linka pra página do poewiki pra
    verificação. Preços ao vivo do poe.ninja (~1×/hora). Fragmentos e a
    maioria dos únicos têm um preço fixo, mas alguns drops (joias com
    rolagem de afixo aleatória, armas/armaduras com implícito corrompido)
    listam vários preços pro mesmo nome — o EV sempre usa o piso realista,
    não um outlier de sorte: o preço "não identificado" dedicado do
    poe.watch quando publicado (ex: Watcher's Eye), senão o preço mais
    baixo que o poe.ninja lista pra aquele nome. Um marcador
    <span style="color:var(--unique)">&#8593;850c</span> ao lado de um
    preço significa que pode rolar mais alto depois de identificado —
    passe o mouse pra ver o teto. Sem preço → <span class="px"><span class="nd">n/d</span></span>,
    excluído do EV, e o link vai pro <b>poewiki</b> em vez disso (evita um
    404). Um item com preço linka pra sua página de detalhe real no
    poe.ninja.`,
},
};

let lang = (function(){
  try { return localStorage.getItem('bossFarmLang') || 'en'; } catch(e) { return 'en'; }
})();
function t(key){ return (I18N[lang] && I18N[lang][key]) || I18N.en[key] || key; }

function showPopover(target){
  const pop = document.getElementById('popover');
  const html = target.dataset.infoText || t(target.dataset.info);
  if(!html) return;
  pop.innerHTML = html;
  pop.classList.add('show');
  const r = target.getBoundingClientRect();
  const pw = pop.offsetWidth, ph = pop.offsetHeight;
  let left = r.left + r.width/2 - pw/2;
  left = Math.max(8, Math.min(left, window.innerWidth - pw - 8));
  let top = r.top - ph - 8;
  if(top < 8) top = r.bottom + 8;
  pop.style.left = left+'px';
  pop.style.top = top+'px';
}
function hidePopover(){ document.getElementById('popover').classList.remove('show'); }
const INFO_SEL = '[data-info], [data-info-text]';
document.addEventListener('mouseover', e => {
  const el = e.target.closest(INFO_SEL);
  if(el) showPopover(el);
});
document.addEventListener('mouseout', e => {
  const el = e.target.closest(INFO_SEL);
  if(el && !(e.relatedTarget && e.relatedTarget.closest(INFO_SEL) === el)) hidePopover();
});
document.addEventListener('focusin', e => {
  const el = e.target.closest(INFO_SEL);
  if(el) showPopover(el);
});
document.addEventListener('focusout', e => {
  if(e.target.closest(INFO_SEL)) hidePopover();
});

const fmtChaos = c => c == null ? null :
  (Math.abs(c) >= 1000 ? Math.round(c).toLocaleString('en-US') :
   Math.abs(c) >= 10 ? c.toFixed(0) : c.toFixed(1));

function price(it, dr){
  if(it.chaos == null) return '<span class="nd">n/a</span>';
  let html = fmtChaos(it.chaos)+'c';
  const div = it.divine != null ? it.divine : (dr ? it.chaos/dr : null);
  if(div != null && div >= 0.2) html += '<span class="div">'+div.toFixed(1)+' div</span>';
  if(it.diverge) html += '<span class="alt" title="other poe.ninja feed">≠ '+
                          fmtChaos(it.diverge.alt)+'c</span>';
  if(it.chaosMax != null){
    const tt = it.priceMode==='unidentified'
      ? 'EV uses poe.watch\'s unidentified price (real as-dropped floor) — a lucky affix roll can sell for up to '+fmtChaos(it.chaosMax)+'c identified'
      : 'value depends on a random roll (corrupted implicit / affix) — EV uses the lowest listed price to avoid overstating it; best known roll sells for up to '+fmtChaos(it.chaosMax)+'c';
    html += '<span class="roll" title="'+tt+'">&#8593;'+fmtChaos(it.chaosMax)+'c</span>';
  }
  return html;
}
function chaosDiv(c, dr){
  if(c == null) return '';
  let s = fmtChaos(c)+'c';
  if(dr && Math.abs(c) >= dr) s += ' <span class="div">('+(c/dr).toFixed(1)+' div)</span>';
  return s;
}
function nmeClass(typ){ return (typ||'').toLowerCase(); }
function iconTag(it){ return it.icon ? `<img src="${it.icon}" alt="" loading="lazy">`
                                     : `<span style="width:26px"></span>`; }

function entryRow(it, dr){
  const qty = it.qty > 1 ? `<span class="qty">×${it.qty}</span>` : '';
  const tt = it.link_src==='wiki' ? t('tt_no_price') : t('tt_ninja');
  return `<a class="item" href="${it.url}" target="_blank" rel="noopener" title="${tt}">
    ${iconTag(it)}<span class="nm ${nmeClass(it.type)}">${it.name}</span>
    ${qty}<span class="ext">&#8599;</span>
    <span class="px">${price(it, dr)}</span></a>`;
}
function dropRow(it, dr){
  const pct = it.chance!=null
    ? (it.chance*100>=1 ? (it.chance*100).toFixed(0) : (it.chance*100).toFixed(1))+'%' : '';
  const real = it.chance_src === 'wiki';
  const ch = pct ? `<span class="ch ${real?'real':'est'}" title="${real?t('tt_wiki'):t('tt_estimate')}">${real?'':'~'}${pct}</span>` : '';
  const qty = it.qty > 1 ? `<span class="qty" data-info-text="${t('tt_qty_factored').replace('{n}', it.qty)}">×${it.qty}</span>` : '';
  const tt = it.link_src==='wiki' ? t('tt_no_price') : t('tt_ninja');
  return `<a class="item" href="${it.url}" target="_blank" rel="noopener" title="${tt}">
    ${iconTag(it)}<span class="nm ${nmeClass(it.type)}">${it.name}</span>
    ${qty}<span class="ext">&#8599;</span>${ch}
    <span class="px">${price(it, dr)}</span></a>`;
}

function entrySection(entry, dr){
  if(!entry.items.length)
    return `<div class="section"><div class="slabel"><span>${t('entry_label')}</span></div>
      <div class="empty">${t('entry_empty')}</div></div>`;
  const total = entry.total_chaos==null ? '' :
    `<span class="total">${chaosDiv(entry.total_chaos, dr)}</span>`;
  return `<div class="section"><div class="slabel"><span>${t('entry_label')}</span>${total}</div>
    ${entry.items.map(it=>entryRow(it, dr)).join('')}</div>`;
}
function dropSection(drops, dr){
  const ev = `<span class="total">${t('ev_drops')} ${chaosDiv(drops.ev_chaos, dr)}</span>`;
  const body = drops.items.length
    ? drops.items.map(it=>dropRow(it, dr)).join('')
    : `<div class="empty">—</div>`;
  return `<div class="section"><div class="slabel"><span>${t('loot_label')}</span>${ev}</div>
    ${body}</div>`;
}
const QUANT_PRESETS = [0, 50, 100, 150, 200];
let quantState = {};

function quantOf(b){ return b.quantMod ? (quantState[b.name] || 0) : 0; }
function adjustedEv(b){ return b.ev_chaos * (1 + quantOf(b)/100); }
function adjustedNet(b){
  return b.entry.total_chaos == null ? null : adjustedEv(b) - b.entry.total_chaos;
}

function quantCtl(b){
  if(!b.quantMod) return '';
  const cur = quantOf(b);
  const isIiq = b.tier === 'nightmare';
  const lbl = t(isIiq ? 'quant_iiq_label' : 'quant_altar_label');
  const info = isIiq ? 'quant_iiq' : 'quant_altar';
  return `<div class="quantctl"><span class="qlbl" data-info="${info}">${lbl}</span>
    ${QUANT_PRESETS.map(p => `<button data-boss="${b.name}" data-q="${p}" class="${p===cur?'active':''}">+${p}%</button>`).join('')}
  </div>`;
}

// Per-boss mechanic descriptions come from the API as fixed English text
// (ENTITIES data, not a UI label) — translated here by exact-string lookup
// rather than a key, so ENTITIES doesn't need a translation field per boss.
const INVULN_TR = {
  "splits into 4 illusions during Die Beam — only the real Sirus is attackable, the rest aren't":
    'se divide em 4 ilusões durante o Die Beam — só o Sirus real pode ser atingido, os outros não',
  'Memory Game — invulnerable while you repeat a lit pattern; a wrong step speeds up a lethal channel':
    'Memory Game — invulnerável enquanto você repete um padrão aceso; um passo errado acelera uma canalização letal',
  'Meteor Wall at ≤50% HP — invulnerable while unleashing meteors; destroy them to open a gap (1x)':
    'Meteor Wall em ≤50% de vida — invulnerável enquanto solta meteoros; destrua-os pra abrir uma brecha (1x)',
  'Meteor Wall at ≤50% HP — invulnerable while unleashing meteors; destroy them to open a gap (1x, harsher)':
    'Meteor Wall em ≤50% de vida — invulnerável enquanto solta meteoros; destrua-os pra abrir uma brecha (1x, mais forte)',
  'Inescapable Doom at 75% HP — invulnerable while channeling a lethal blast; activate spheres to interrupt (1x)':
    'Inescapable Doom em 75% de vida — invulnerável enquanto canaliza uma explosão letal; ative as esferas pra interromper (1x)',
  'Inescapable Doom at 75% HP — invulnerable while channeling a lethal blast; activate spheres to interrupt (1x, harsher)':
    'Inescapable Doom em 75% de vida — invulnerável enquanto canaliza uma explosão letal; ative as esferas pra interromper (1x, mais forte)',
  'Venarius is never the actual target — you fight summoned Synthete adds for the whole encounter':
    'Venarius nunca é o alvo real — você luta contra os Synthetes invocados durante todo o encontro',
  'Minion Phase — shields herself and becomes invulnerable while spawning health-draining monsters (1x)':
    'Minion Phase — ela se protege e fica invulnerável enquanto invoca monstros que drenam vida (1x)',
  'Minion Phase (1x) + splits into 4 copies at 75/50/25% HP — only one is the real target (up to 4x total)':
    'Minion Phase (1x) + se divide em 4 cópias em 75/50/25% de vida — só uma é o alvo real (até 4x no total)',
  'boss submerges and becomes untargetable, then re-emerges — repeats through the fight':
    'o boss submerge e fica sem poder ser alvejado, depois reaparece — se repete ao longo da luta',
  'reportedly has invincibility phases per community guides, exact mechanic undocumented':
    'segundo guias da comunidade tem fases de invencibilidade, mecânica exata não documentada',
};
function tInvuln(text){ return lang === 'pt' ? (INVULN_TR[text] || text) : text; }

function metaBadges(b){
  const parts = [];
  if(b.access === 'map')
    parts.push(`<span class="badge map" data-info="access_map">&#128506; ${t('word_map')}</span>`);
  else if(b.access === 'direct')
    parts.push(`<span class="badge direct" data-info="access_direct">&#9889; ${t('word_direct')}</span>`);
  if(b.invuln)
    parts.push(`<span class="badge invuln" data-info-text="${tInvuln(b.invuln)}">&#128737; ${t('word_invuln')}</span>`);
  return parts.length ? `<div class="meta-badges">${parts.join('')}</div>` : '';
}

const DEFAULT_TIME = {map: 120, direct: 60};
let timeState = {};
function timeOf(b){ return timeState[b.name] ?? (DEFAULT_TIME[b.access] || 90); }

function timeCtl(b){
  const secs = timeOf(b);
  const net = adjustedNet(b);
  const rate = net != null ? net / (secs/3600) : null;
  const rateTxt = rate != null
    ? `<span class="rate ${rate>=0?'pos':'neg'}" data-info="rate">≈ ${fmtSigned(rate)}/hr</span>`
    : '';
  return `<div class="timectl"><span class="qlbl" data-info="time">${t('time_label')}</span>
    <input type="number" min="1" step="5" value="${secs}" data-boss="${b.name}" class="timeinput"><span>s</span>
    ${rateTxt}
  </div>`;
}

function fmtSigned(v){ return (v>=0?'+':'−') + fmtChaos(Math.abs(v)) + 'c'; }

function profitBanner(b, dr, mult){
  const lbl = mult === 1 ? t('profit_avg_run') : t('profit_avg_mult').replace('{n}', mult);
  const ev = adjustedEv(b) * mult;
  if(b.entry.total_chaos == null)
    return `<div class="profit"><span class="lbl" data-info="avg">${lbl}</span>
      <span class="val na">${t('profit_entry_na')}</span></div>
      <div class="evline"><span data-info="ev">${t('ev_drops')}</span> ${fmtChaos(ev)}c</div>`;
  const entryTotal = b.entry.total_chaos * mult;
  const net = ev - entryTotal;
  const worst = b.worst * mult;
  const best = b.best * mult;
  const cls = net >= 0 ? 'pos' : 'neg';
  const sign = net >= 0 ? '+' : '−';
  return `<div class="profit"><span class="lbl" data-info="avg">${lbl}</span>
      <span class="val ${cls}">${sign}${chaosDiv(Math.abs(net), dr)}</span></div>
    <div class="range">
      <span class="rw neg" data-info="worst">${t('word_worst')} ${fmtSigned(worst)}</span>
      <span class="rb pos" data-info="best">${t('word_best')} ${fmtSigned(best)}</span>
    </div>
    <div class="evline"><span data-info="ev">${t('ev_drops')}</span> ${fmtChaos(ev)}c − ${t('entry_word')} ${fmtChaos(entryTotal)}c</div>`;
}

const GROUPS = [
  {tier: 'normal', titleKey: 'group_normal_title', descKey: 'group_normal_desc'},
  {tier: 'uber', titleKey: 'group_uber_title', descKey: 'group_uber_desc'},
  {tier: 'nightmare', titleKey: 'group_nightmare_title', descKey: 'group_nightmare_desc'},
];

function slug(s){
  return s.toLowerCase().replace(/'/g,'').replace(/[^a-z0-9]+/g,'-').replace(/^-+|-+$/g,'');
}
let sortBy = 'best'; // 'best' | 'avg' | 'worst' — worst/best ignore any quant_mod %,
                      // same as displayed (see profitBanner): a quantity buff raises the
                      // average, not what the single best/worst outcome is worth
function sortKey(b){
  if(sortBy === 'worst') return b.worst;
  if(sortBy === 'best') return b.best;
  return adjustedNet(b);
}
function rankSort(list){
  return list.slice().sort((a,b)=>{
    const ak = sortKey(a), bk = sortKey(b);
    if(ak==null && bk==null) return 0;
    if(ak==null) return 1;
    if(bk==null) return -1;
    return bk - ak;
  });
}

function tierWord(tier){ return t('word_'+tier); }

function bossCard(b, i, dr, mult){
  const has = adjustedNet(b) != null;
  return `<div class="card ${b.tier}" id="b-${slug(b.name)}">
    <h2><span class="rank ${has?'':'dim'}">${has?('#'+(i+1)):'—'}</span>
        <a class="hname" href="${b.wikiUrl}" target="_blank" rel="noopener" title="poewiki source">${b.name}</a>
        <span class="tier ${b.tier}">${tierWord(b.tier)}</span></h2>
    ${metaBadges(b)}
    ${quantCtl(b)}
    ${timeCtl(b)}
    ${profitBanner(b, dr, mult)}
    ${entrySection(b.entry, dr)}
    ${dropSection(b.drops, dr)}
  </div>`;
}

function navItem(b, mult){
  const avgRaw = adjustedNet(b);
  const has = avgRaw != null;
  const avg = has ? avgRaw * mult : null;
  const worst = has ? b.worst * mult : null;
  const best = has ? b.best * mult : null;
  const cls = has ? (avg>=0?'pos':'neg') : 'na';
  const range = has
    ? `<span class="nr" data-info-text="${t('word_worst')} / ${t('word_avg')} / ${t('word_best')}">
         <span class="neg">${fmtSigned(worst)}</span><span class="${cls}">${fmtSigned(avg)}</span><span class="pos">${fmtSigned(best)}</span>
       </span>`
    : `<span class="nr na">—</span>`;
  return `<a class="navitem" href="#b-${slug(b.name)}" title="${b.name}">
    <span class="nm">${b.name}</span>${range}</a>`;
}

let lastData = null;

function render(data){
  lastData = data;
  document.getElementById('league').textContent = data.league;
  populateLeagueOptions(data);
  document.getElementById('divine').textContent =
    data.divineRate ? Math.round(data.divineRate).toLocaleString('en-US') : '—';
  document.getElementById('src').textContent = data.source || 'stash';
  const dr = data.divineRate;
  const mult = runMult;
  const grouped = GROUPS.map(g => ({...g, items: rankSort(data.bosses.filter(b=>b.tier===g.tier))}))
                         .filter(g => g.items.length);

  const rankedSuffix = t('group_ranked_suffix').replace('{word}', t('word_'+sortBy));
  document.getElementById('root').innerHTML = grouped.map(g => `
    <div class="group ${g.tier}" id="group-${g.tier}">
      <div class="group-head"><h3>${t(g.titleKey)}</h3><span class="count">${g.items.length}</span>
        <span class="desc">${t(g.descKey)}${rankedSuffix}</span></div>
      <div class="grid">${g.items.map((b,i)=>bossCard(b, i, dr, mult)).join('')}</div>
    </div>`).join('');

  document.getElementById('sidenav').innerHTML = grouped.map(g => `
    <div class="navgroup ${g.tier}">
      <a class="navtitle" href="#group-${g.tier}">${t(g.titleKey)}</a>
      ${g.items.map(b => navItem(b, mult)).join('')}
    </div>`).join('');
}

window.addEventListener('hashchange', () => {
  const el = document.getElementById(location.hash.slice(1));
  if(!el || !el.classList.contains('card')) return;
  el.classList.remove('flash'); void el.offsetWidth; el.classList.add('flash');
});

async function fetchData(){
  const qs = currentLeague ? ('?league='+encodeURIComponent(currentLeague)) : '';
  const r = await fetch('/api/data'+qs, {signal: AbortSignal.timeout(60000)});
  if(!r.ok) throw new Error('HTTP '+r.status);
  return r.json();
}

let lastErrKey = null; // 'err_timeout' | 'err_fetch' | null — re-rendered in the
                        // current language by updateWarnUI(), not baked in at fetch time

function updateWarnUI(){
  const warn = document.getElementById('warn');
  if(!lastErrKey){ warn.hidden = true; return; }
  if(lastUpdated){
    // we already have a working dashboard on screen — don't nuke it,
    // just flag the sync failure and let the next auto-poll try again
    warn.hidden = false;
    warn.textContent = t('warn_sync_failed')+t(lastErrKey);
    warn.title = t('warn_retry_title').replace('{s}', Math.round(POLL_MS/1000));
  }else{
    document.getElementById('root').innerHTML =
      '<div class="err">'+t(lastErrKey)+t('err_load_suffix')+'</div>';
  }
}

async function load(retrying){
  const btn = document.getElementById('sync');
  btn.disabled = true; btn.textContent = t('btn_syncing');
  try{
    render(await fetchData());
    lastUpdated = Date.now();
    lastErrKey = null;
    document.getElementById('warn').hidden = true;
  }catch(e){
    lastErrKey = e.name === 'TimeoutError' ? 'err_timeout' : 'err_fetch';
    if(!retrying){
      // transient — retry once in the background before bothering the user
      setTimeout(() => load(true), 3000);
    }else{
      updateWarnUI();
    }
  }finally{
    btn.disabled = false; btn.textContent = t('btn_refresh');
  }
}
function tick(){
  if(!lastUpdated) return;
  document.getElementById('ago').textContent = Math.round((Date.now()-lastUpdated)/1000)+'s';
  document.getElementById('next').textContent =
    Math.max(0, Math.round((POLL_MS-(Date.now()-lastUpdated))/1000))+'s';
}
function applyStaticI18n(){
  document.querySelectorAll('[data-i18n]').forEach(el => { el.innerHTML = t(el.dataset.i18n); });
  document.documentElement.lang = lang === 'pt' ? 'pt-BR' : 'en';
}
const langSel = document.getElementById('langsel');
langSel.value = lang;
applyStaticI18n();
applyTheme();
langSel.addEventListener('change', () => {
  lang = langSel.value;
  try { localStorage.setItem('bossFarmLang', lang); } catch(e) {}
  applyStaticI18n();
  if(lastData) render(lastData);
  updateWarnUI();
});
document.getElementById('themetoggle').addEventListener('click', () => {
  theme = theme === 'dark' ? 'light' : 'dark';
  try { localStorage.setItem('bossFarmTheme', theme); } catch(e) {}
  applyTheme();
});
document.getElementById('leaguesel').addEventListener('change', e => {
  currentLeague = e.target.value;
  try { localStorage.setItem('bossFarmLeague', currentLeague); } catch(e) {}
  load();
});
document.getElementById('sync').addEventListener('click', () => load());
document.getElementById('runs').addEventListener('click', e => {
  const btn = e.target.closest('button[data-mult]');
  if(!btn) return;
  runMult = Number(btn.dataset.mult);
  document.querySelectorAll('#runs button').forEach(b => b.classList.toggle('active', b===btn));
  if(lastData) render(lastData);
});
document.getElementById('sortby').addEventListener('click', e => {
  const btn = e.target.closest('button[data-sort]');
  if(!btn) return;
  sortBy = btn.dataset.sort;
  document.querySelectorAll('#sortby button').forEach(b => b.classList.toggle('active', b===btn));
  if(lastData) render(lastData);
});
document.getElementById('root').addEventListener('click', e => {
  const btn = e.target.closest('.quantctl button[data-boss]');
  if(!btn) return;
  quantState[btn.dataset.boss] = Number(btn.dataset.q);
  if(lastData) render(lastData);
});
document.getElementById('root').addEventListener('change', e => {
  const inp = e.target.closest('.timeinput');
  if(!inp) return;
  timeState[inp.dataset.boss] = Math.max(1, Number(inp.value) || 60);
  if(lastData) render(lastData);
});
load();
setInterval(load, POLL_MS);
setInterval(tick, 1000);
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# Server
# --------------------------------------------------------------------------- #
def make_handler(league, poll_ms):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            if path == "/":
                html = PAGE.replace("__POLL_MS__", str(poll_ms))
                self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/api/data":
                qs = urllib.parse.parse_qs(parsed.query)
                req_league = (qs.get("league") or [league])[0].strip() or league
                try:
                    body = json.dumps(build_payload(req_league)).encode("utf-8")
                    self._send(200, body, "application/json")
                except Exception as e:
                    self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")
            else:
                self._send(404, b"not found", "text/plain")
    return H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--league", default="Allflame")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--poll", type=int, default=120,
                    help="browser auto-refresh interval, in seconds")
    args = ap.parse_args()

    srv = ThreadingHTTPServer(("127.0.0.1", args.port),
                              make_handler(args.league, args.poll * 1000))
    url = f"http://localhost:{args.port}"
    print(f"Boss Farm Estimator at {url}  (league: {args.league}, price: exchange->stash)"
          f"  --  Ctrl+C to stop")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down…")
        srv.shutdown()


if __name__ == "__main__":
    main()