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
import html as html_module  # aliased — this file uses `html` as a local var name everywhere
import json
import mimetypes
import os
import re
import subprocess
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
                   "UniqueJewel", "UniqueFlask", "Invitation", "Map", "SkillGem"]

# Currency/Fragment go through the Exchange+Stash feeds (see build_index); so
# does Astrolabe — poe.ninja only prices it via Exchange, but the shape is
# identical (items[]+lines[] with a primaryValue), so it merges the same way.
EXCHANGE_CATEGORIES = ["Currency", "Fragment", "Astrolabe"]

OVERVIEW_SLUG = {
    "Currency": "currency", "Fragment": "fragments",
    "UniqueWeapon": "unique-weapons", "UniqueArmour": "unique-armours",
    "UniqueAccessory": "unique-accessories", "UniqueJewel": "unique-jewels",
    "UniqueFlask": "unique-flasks", "Invitation": "invitations",
    "Map": "maps", "Astrolabe": "astrolabes", "SkillGem": "skill-gems",
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
# Sums to ~1.0 — memory bosses guarantee exactly one Astrolabe per kill.
# Not an even 10% split: poewiki (Incarnation of Fear page) confirms Templar
# Astrolabe is weighted at 33%, with the other 9 types splitting the rest
# evenly at 7.5% each — Templar is the simplest/most basic type.
ASTROLABE_GUARANTEED = [(nm, "Astrolabe", 0.33 if nm == "Templar Astrolabe" else 0.075, "wiki")
                        for nm in ASTROLABE_TYPES]
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
     "drops": [("Servant of Decay", "UniqueArmour", 0.50, "wiki"),
               ("The Unseen Hue", "UniqueAccessory", 0.40, "wiki"),
               ("Enmity's Embrace", "UniqueAccessory", 0.08, "wiki"),
               ("Starcaller", "UniqueWeapon", 0.02, "wiki"),
               ("Bound By Destiny", "UniqueJewel", 0.10, "wiki"),
               ("Greater Devour Support", "SkillGem", 0.05, "wiki"),
               ("Orb of Intention", "Currency", 0.50, "wiki"),
               *ASTROLABE_GUARANTEED]},
    {"name": "Uber Incarnation of Fear", "tier": "uber", "access": "direct",
     "entry": [("Traumatic Fragment", "Fragment", UBER_FRAG_QTY)],
     "drops": [("The Caged Mammoth", "UniqueArmour", 0.60, "wiki"),
               ("Coiling Whisper", "UniqueAccessory", 0.36, "wiki"),
               ("Wing of the Wyvern", "UniqueWeapon", 0.02, "wiki"),
               ("Woespike", "UniqueAccessory", 0.02, "wiki"),
               ("Traumatic Reliquary Key", "Fragment", 0.01, "wiki"),
               ("Bound By Destiny", "UniqueJewel", 0.10, "wiki"),
               ("Greater Devour Support", "SkillGem", 0.05, "wiki"),
               ("Orb of Intention", "Currency", 0.50, "wiki"),
               *ASTROLABE_GUARANTEED]},
    {"name": "Incarnation of Dread", "tier": "normal", "access": "direct",
     "entry": [("Echo of Reverence", "Currency", 1)],
     "drops": [("Bonemeld", "UniqueAccessory", 0.55, "wiki"),
               ("The Dark Monarch", "UniqueArmour", 0.35, "wiki"),
               ("Seven Teachings", "UniqueAccessory", 0.08, "wiki"),
               ("Wine of the Prophet", "UniqueFlask", 0.02, "wiki"),
               ("Bound By Destiny", "UniqueJewel", 0.10, "wiki"),
               ("Congregation Support", "SkillGem", 0.05, "est"),
               ("Orb of Unravelling", "Currency", 0.33, "wiki"),
               *ASTROLABE_GUARANTEED]},
    {"name": "Uber Incarnation of Dread", "tier": "uber", "access": "direct",
     "entry": [("Reverent Fragment", "Fragment", UBER_FRAG_QTY)],
     "drops": [("The Hallowed Monarch", "UniqueArmour", 0.54, "wiki"),
               ("Whispers of Infinity", "UniqueAccessory", 0.30, "wiki"),
               ("Wellwater Phylactery", "UniqueFlask", 0.14, "wiki"),
               ("The Golden Charlatan", "UniqueWeapon", 0.02, "wiki"),
               ("Reverent Reliquary Key", "Fragment", 0.01, "wiki"),
               ("Bound By Destiny", "UniqueJewel", 0.10, "wiki"),
               ("Congregation Support", "SkillGem", 0.05, "est"),
               ("Orb of Unravelling", "Currency", 0.33, "wiki"),
               *ASTROLABE_GUARANTEED]},
    {"name": "Incarnation of Neglect", "tier": "normal", "access": "direct",
     "entry": [("Echo of Loneliness", "Currency", 1)],
     "drops": [("Betrayal's Sting", "UniqueAccessory", 0.50, "wiki"),
               ("The Arkhon's Tools", "UniqueAccessory", 0.38, "wiki"),
               ("Venarius' Astrolabe", "UniqueAccessory", 0.10, "wiki"),
               ("Legacy of the Rose", "UniqueWeapon", 0.02, "wiki"),
               ("Bound By Destiny", "UniqueJewel", 0.10, "wiki"),
               ("Frostmage Support", "SkillGem", 0.05, "wiki"),
               ("Orb of Remembrance", "Currency", 0.33, "wiki"),
               *ASTROLABE_GUARANTEED]},
    {"name": "Uber Incarnation of Neglect", "tier": "uber", "access": "direct",
     "entry": [("Lonely Fragment", "Fragment", UBER_FRAG_QTY)],
     "drops": [("Refuge in Isolation", "UniqueArmour", 0.55, "wiki"),
               ("Bitter Instinct", "UniqueArmour", 0.30, "wiki"),
               ("Haunting Memories", "UniqueAccessory", 0.13, "wiki"),
               ("Festering Resentment", "UniqueWeapon", 0.02, "wiki"),
               ("Lonely Reliquary Key", "Fragment", 0.01, "wiki"),
               ("Bound By Destiny", "UniqueJewel", 0.10, "wiki"),
               ("Frostmage Support", "SkillGem", 0.05, "wiki"),
               ("Orb of Remembrance", "Currency", 0.33, "wiki"),
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


# Both games' patch notes live on the same classic server-rendered
# pathofexile.com forum software (confirmed live) — pathofexile2.com itself is
# a client-rendered SPA (just a <div id="app"> plus a JS bundle import) with no
# scrapeable HTML, a dead end ruled out before picking these URLs. PoE2's forum
# is a sub-forum ("Early Access Patch Notes") of the SAME pathofexile.com
# domain, id 2212 — found via the forum index page, not guessed.
PATCH_NOTES_URLS = {
    "poe1": "https://www.pathofexile.com/forum/view-forum/patch-notes",
    "poe2": "https://www.pathofexile.com/forum/view-forum/2212",
}
# The forum's WAF treats requests without a real browser User-Agent
# differently than the plain JSON APIs do — confirmed live, a generic/blank UA
# risked a different (bot-challenge) response, so this uses a real Chrome UA
# string rather than boss.py's own UA constant.
_FORUM_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
_PATCH_TITLE_RE = re.compile(r'<div class="title">\s*<a href="(/forum/view-thread/\d+)">\s*([^<]+?)\s*</a>', re.DOTALL)
_PATCH_DATE_RE = re.compile(r'class="post_date">([^<]*)</span>')
# The OP's own post body starts right after this marker on a thread page
# (confirmed live) — every reply after it also has its own "content" div, but
# this is the only one preceded by "contentStart", which only the first post
# in the whole thread has.
_PATCH_BODY_RE = re.compile(r'<div class="contentStart"></div>\s*<div class="content">(.*?)(?:<div class="signature"|</td>)', re.DOTALL)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_SNIPPET_LEN = 400


def _snippet_from_thread(thread_url):
    """Plain-text excerpt of a patch note thread's own first post — a real
    (truncated) excerpt of the actual patch text, not a generated summary
    (this project has no LLM to summarize with). Best-effort: any failure
    here just means that one item's popup has no snippet, never blocks the
    rest of the listing.
    """
    try:
        req = urllib.request.Request(thread_url, headers={"User-Agent": _FORUM_UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            html = r.read().decode("utf-8", errors="replace")
        m = _PATCH_BODY_RE.search(html)
        if not m:
            return ""
        # The real post body can be 10,000+ chars (long bulleted category
        # lists) — only look at a generous prefix, since we're truncating to
        # _SNIPPET_LEN anyway; avoids stripping tags across the whole thing.
        raw = m.group(1)[:_SNIPPET_LEN * 4]
        text = _HTML_TAG_RE.sub(" ", raw)
        text = html_module.unescape(text)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) > _SNIPPET_LEN:
            text = text[:_SNIPPET_LEN].rsplit(" ", 1)[0] + "…"
        return text
    except Exception as e:
        print(f"  [warning] patch note snippet ({thread_url}): {e}")
        return ""


def _get_patch_notes(game):
    """Live patch-note listing for `game` ("poe1"/"poe2"), newest first, top 8,
    each with a real (truncated) excerpt of its own thread body for the
    hover-popup preview.

    Filtered to titles starting with a digit — patch titles are consistently
    version-numbered (e.g. "3.29.1 Maintenance", "0.5.4e Maintenance",
    confirmed on both real listings) — to skip pinned non-patch threads like
    "Code of Conduct" that the forum always shows first. The listing's own
    row order is already newest-first (the forum's default sort), so no
    separate date-based sort is needed; the parsed date is display-only.
    """
    now = time.time()
    key = ("patchnotes", game)
    with _lock:
        c = _cache.get(key)
        if c and now - c[0] < CACHE_TTL:
            return c[1]
    result = []
    try:
        url = PATCH_NOTES_URLS[game]
        req = urllib.request.Request(url, headers={"User-Agent": _FORUM_UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            html = r.read().decode("utf-8", errors="replace")
        for m in _PATCH_TITLE_RE.finditer(html):
            thread_path, title = m.group(1), m.group(2).strip()
            if not title or not title[0].isdigit():
                continue
            # A wide window — the "postBy" block (which holds post_date) can sit
            # far past the title on a heavily-paginated thread (each page-number
            # link adds ~60 chars; an 8-page thread pushed it past 600 chars in
            # testing, so 3000 is a safe margin).
            window = html[m.end():m.end() + 3000]
            date_m = _PATCH_DATE_RE.search(window)
            date_text = date_m.group(1).strip().lstrip(",").strip() if date_m else ""
            result.append({
                "title": title,
                "url": "https://www.pathofexile.com" + thread_path,
                "date": date_text,
            })
            if len(result) >= 8:
                break
        # One extra request per item to fetch its own thread page for a real
        # snippet — fired concurrently (matches build_index()'s own pattern
        # for the same reason: 8 sequential ~1-2s forum fetches would make
        # the whole page wait 8-16s for no benefit).
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            snippets = list(ex.map(_snippet_from_thread, [it["url"] for it in result]))
        for it, snippet in zip(result, snippets):
            it["snippet"] = snippet
    except Exception as e:
        print(f"  [warning] patch notes ({game}): {e}")
    with _lock:
        _cache[key] = (now, result)
    return result


# --------------------------------------------------------------------------- #
# Currency Exchange flip advisor (/flip-advisor, PoE1 only)
# --------------------------------------------------------------------------- #
# https://web.poecdn.com/api/currency-exchange/<hour> is the automated
# bulk-currency-exchange market's hourly aggregate feed — fully public, no
# OAuth (confirmed live; the official docs page implies auth might be needed,
# it isn't). `id` is a PATH segment, not a query param despite how the docs
# phrase it, and must be an already-completed hour — the current in-progress
# hour returns an empty markets list (confirmed live).
CURRENCY_EXCHANGE_URL = "https://web.poecdn.com/api/currency-exchange"
REPOE_BASE_ITEMS_URL = "https://raw.githubusercontent.com/brather1ng/RePoE/master/RePoE/data/base_items.min.json"
# RePoE only updates when GGG ships new item types (roughly per-league) —
# nowhere near CACHE_TTL's 5-minute freshness need, and it's a ~2MB fetch, so
# it gets its own much longer cache lifetime.
_REPOE_CACHE_TTL = 6 * 3600
# Liquidity is filtered in CHAOS-EQUIVALENT VALUE, not raw per-currency unit
# counts — a real bug found during development: raw volume isn't comparable
# across currencies (1000 Portal Scrolls and 1000 Divine Orbs are wildly
# different amounts of real economic value), which is exactly how a
# near-worthless, barely-traded pair could look "liquid" by raw count alone.
# `_compute_chaos_values()` prices every reachable currency in Chaos-Orb
# terms via the trade graph itself; each pair's liquidity is then
# min(volume_A * chaosValue_A, volume_B * chaosValue_B) — the LESSER side,
# not summed (a cheap, high-volume currency can otherwise mask a nearly-dead
# partner). The threshold is user-adjustable (`min_liquidity` query param,
# chaos-equivalent) rather than a fixed constant — confirmed live that a
# strict bar (few results, all plausible: Chaos<->Divine, Chaos<->Exalted
# etc.) and a loose one (many results, but reintroducing thin-liquidity/
# bad-mapping noise like the "Clear Oil" case below) are both legitimate
# depending on how much the user wants to trade off count vs. confidence —
# not this code's call to make silently.
_FLIP_DEFAULT_MIN_LIQUIDITY_CHAOS = 100
_FLIP_MAX_RESULTS = 30


def _get_currency_names():
    """internal item id ("Metadata/Items/Currency/CurrencyRerollRare") ->
    display name ("Chaos Orb"), sourced from RePoE (a community-maintained,
    unauthenticated data-mining export — confirmed live and accurate against
    several known currencies before relying on it). Best-effort: on failure,
    returns whatever's cached (even if stale) rather than nothing, since
    losing this mapping shouldn't take down a working flip-advisor result
    just because RePoE happened to be unreachable this cycle.
    """
    now = time.time()
    key = ("currencynames",)
    with _lock:
        c = _cache.get(key)
        if c and now - c[0] < _REPOE_CACHE_TTL:
            return c[1]
    try:
        req = urllib.request.Request(REPOE_BASE_ITEMS_URL, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=40) as r:
            data = json.loads(r.read().decode("utf-8"))
        names = {k: v["name"] for k, v in data.items()
                 if k.startswith("Metadata/Items/Currency/") and v.get("name")}
        with _lock:
            _cache[key] = (now, names)
        return names
    except Exception as e:
        print(f"  [warning] RePoE currency names: {e}")
        with _lock:
            c = _cache.get(key)
        return c[1] if c else {}


def _fetch_currency_exchange_hour(hour_ts):
    req = urllib.request.Request(f"{CURRENCY_EXCHANGE_URL}/{hour_ts}", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def _compute_chaos_values(full_graph, base="Chaos Orb"):
    """currency name -> how many Chaos Orbs 1 unit of it is worth, derived
    from the trade graph itself via BFS from `base` (rather than needing a
    separate price source). BFS visits `base`'s DIRECT neighbors first, so a
    currency that trades directly against Chaos always gets that direct rate
    rather than a noisier multi-hop derived one; only currencies with no
    direct Chaos pair fall back to a value derived through whatever
    intermediate currency connects them. `full_graph` must be built from
    EVERY resolved pair regardless of liquidity — a currency's own trading
    volume being too thin to trust as a *result* doesn't mean its rate can't
    still usefully help price a DIFFERENT currency it connects to.
    """
    if base not in full_graph:
        return {}
    values = {base: 1.0}
    queue = [base]
    while queue:
        nxt_queue = []
        for node in queue:
            for neighbor, rate in full_graph.get(node, {}).items():
                if neighbor in values or rate <= 0:
                    continue
                values[neighbor] = values[node] / rate
                nxt_queue.append(neighbor)
        queue = nxt_queue
    return values


# Multi-hop cycle search — see _find_flip_cycles()'s own docstring for the
# full reasoning. Every currency-exchange pair yields TWO graph edges (A->B
# and B->A); B->A is approximated as the exact mathematical inverse of A->B
# (this feed has no separate buy/sell price, only one pool ratio per pair —
# see the rate-averaging note in the main loop below), which means a
# 2-currency round trip (A->B->A) is *always* exactly break-even by
# construction and can never appear as "profitable" — genuine profit can
# only come from a cycle through 3+ distinct currencies (real triangular+
# arbitrage), so the search only looks at cycle lengths 3-10.
_FLIP_CYCLE_START = "Chaos Orb"  # the de facto base/reference currency for trading
_FLIP_CYCLE_MIN_STEPS = 3
_FLIP_CYCLE_MAX_STEPS = 10
_FLIP_CYCLE_MAX_RESULTS = 20
_FLIP_CYCLE_MAX_VISITS = 300000  # safety valve against pathological graph blowup
# A pair can be genuinely liquid (high chaos-equivalent volume on both sides)
# and STILL have an extreme intra-hour price swing — confirmed live: Chaos<->
# Orb of Fusing moved from an 11:1 ratio to 1:1 within one hour (spread_pct
# 1000%) despite six-figure volume on both sides. Liquidity and price
# stability are different things; compounding several such edges across a
# multi-hop cycle is what produced clearly-implausible 1000%+ "profit"
# strategies even after the liquidity fix above. Capping which pairs are
# trusted as CYCLE-GRAPH edges (independent of the liquidity filter) is what
# actually fixes it — the single-pair spread list below still shows a
# high-spread pair's real number regardless, since that's honestly
# informative there; it just isn't trusted to compound through a cycle.
_FLIP_CYCLE_MAX_EDGE_SPREAD_PCT = 50


def _find_flip_cycles(graph, start):
    """Simple-cycle DFS from `start` back to `start`, 3-10 hops, keeping
    every cycle whose compounded rate product is profitable (> 1.0) —
    "compounded" meaning literally: start with 1 unit of `start`, multiply by
    each hop's rate in sequence, see if you end up with more than 1.

    Deliberately exhaustive-but-bounded rather than a shortest-path/Bellman-
    Ford style algorithm: the ask here is "show me the top 20 distinct
    strategies", not just the single best cycle, and this repo's currency
    graphs are small/sparse enough (tens of nodes, a few hundred edges at
    most, after the min-volume filter) for plain DFS with a node-revisit
    guard (simple cycles only) and a hard visit-count safety valve to stay
    fast — confirmed live at well under a second for a real league's graph.
    """
    if start not in graph:
        return []
    results = []
    visits = [0]
    path = [start]
    path_set = {start}

    def dfs(current, rate_so_far, depth):
        visits[0] += 1
        if visits[0] > _FLIP_CYCLE_MAX_VISITS:
            return
        if depth >= _FLIP_CYCLE_MIN_STEPS and start in graph.get(current, {}):
            final_rate = rate_so_far * graph[current][start]
            if final_rate > 1.0:
                results.append({"path": list(path) + [start], "rate": final_rate})
        if depth >= _FLIP_CYCLE_MAX_STEPS:
            return
        for nxt, rate in graph.get(current, {}).items():
            if nxt == start or nxt in path_set:
                continue
            path.append(nxt)
            path_set.add(nxt)
            dfs(nxt, rate_so_far * rate, depth + 1)
            path.pop()
            path_set.discard(nxt)

    dfs(start, 1.0, 0)
    results.sort(key=lambda r: -r["rate"])
    return results[:_FLIP_CYCLE_MAX_RESULTS]


def _get_flip_opportunities(league, min_liquidity_chaos=None):
    """Top currency-exchange pairs for `league` (ranked by historical spread%
    over the last completed hour) plus multi-hop "flip strategy" cycles
    (see _find_flip_cycles()), both filtered by a user-adjustable
    chaos-equivalent liquidity floor (see the constant's own comment for why
    that's the right unit — raw per-currency volume isn't comparable).

    This is HOURLY AGGREGATE, DELAYED data, not a live orderbook — there is
    no way to retroactively act on an intra-hour price swing. Both
    `spread_pct` and a strategy's profit% are volatility/inefficiency
    SIGNALS from the last completed hour, never a guaranteed live profit —
    each edge's rate is the AVERAGE of that hour's lowest_ratio/
    highest_ratio-derived rate (not the optimistic extreme — using the
    optimistic edge would compound across a multi-hop cycle into wildly
    overstated "profit", the same trap the single-pair spread% metric
    itself avoids by showing the true range instead of one cherry-picked
    number). Pairs whose pool stock hit zero on either side during the hour
    are excluded entirely (see the lowest_stock check below) — volume_traded
    alone can look healthy for a pair that was actually untradeable for
    part of the hour. The frontend must present both as signals to verify
    live, not precise numbers to trust.
    """
    min_liquidity_chaos = (
        _FLIP_DEFAULT_MIN_LIQUIDITY_CHAOS if min_liquidity_chaos is None
        else max(0.0, min_liquidity_chaos)
    )
    now = time.time()
    key = ("flipadvisor", league, min_liquidity_chaos)
    with _lock:
        c = _cache.get(key)
        if c and now - c[0] < CACHE_TTL:
            return c[1]
    result = {"items": [], "strategies": [], "hourTimestamp": None,
              "minLiquidityChaos": min_liquidity_chaos, "divineRateChaos": None}
    try:
        names = _get_currency_names()
        current_hour = int(now) - (int(now) % 3600)
        data = None
        hour_ts = None
        # The in-progress hour is always empty; older ones can occasionally
        # be too (e.g. right after a server restart with no traffic logged
        # yet) — try a few completed hours back before giving up.
        for hours_back in (1, 2, 3, 4):
            candidate_ts = current_hour - hours_back * 3600
            candidate = _fetch_currency_exchange_hour(candidate_ts)
            if candidate.get("markets"):
                data, hour_ts = candidate, candidate_ts
                break
        if data:
            # Pass 1: build the FULL rate graph (every name-resolved pair,
            # no liquidity filtering yet) and price every reachable currency
            # in Chaos terms from it — see _compute_chaos_values()'s
            # docstring for why this needs the unfiltered graph.
            league_markets = [m for m in data["markets"] if m.get("league") == league]
            full_graph = {}
            pair_cache = []
            for m in league_markets:
                pair = m.get("market_pair") or []
                if len(pair) != 2:
                    continue
                a, b = pair
                name_a, name_b = names.get(a), names.get(b)
                if not name_a or not name_b:
                    continue  # never guess a name — see _get_currency_names()
                lo, hi = m.get("lowest_ratio", {}), m.get("highest_ratio", {})
                if not (lo.get(a) and lo.get(b) and hi.get(a) and hi.get(b)):
                    continue
                # Skip pairs whose pool stock hit zero on either side during
                # the hour — a hard "this trade could not actually execute"
                # signal that volume_traded (past activity only) misses.
                # Confirmed live: Orb of Fusing<->Orb of Alteration had real
                # volume_traded but lowest_stock of 0 for Alteration, i.e.
                # the pool was drained dry for part of the hour — 27% of
                # this hour's pairs showed this, so it's common, not an edge
                # case worth ignoring.
                stock_lo = m.get("lowest_stock", {})
                if stock_lo.get(a, 0) <= 0 or stock_lo.get(b, 0) <= 0:
                    continue
                rate_low = lo[b] / lo[a]
                rate_high = hi[b] / hi[a]
                if rate_low <= 0:
                    continue
                lo_rate, hi_rate = min(rate_low, rate_high), max(rate_low, rate_high)
                avg_rate = (lo_rate + hi_rate) / 2  # see docstring: avoid the optimistic-extreme trap
                if avg_rate <= 0:
                    continue
                full_graph.setdefault(name_a, {})[name_b] = avg_rate
                full_graph.setdefault(name_b, {})[name_a] = 1 / avg_rate
                vol = m.get("volume_traded", {})
                pair_cache.append((name_a, name_b, lo_rate, hi_rate, vol.get(a) or 0, vol.get(b) or 0))

            chaos_values = _compute_chaos_values(full_graph)
            result["divineRateChaos"] = chaos_values.get("Divine Orb")

            # Pass 2: now filter by chaos-equivalent liquidity, using the
            # prices just derived.
            items = []
            graph = {}
            edge_liquidity = {}  # (from, to) -> chaos-equivalent liquidity, for strategy steps below
            for name_a, name_b, lo_rate, hi_rate, vol_a, vol_b in pair_cache:
                cv_a, cv_b = chaos_values.get(name_a), chaos_values.get(name_b)
                if cv_a is None or cv_b is None:
                    continue  # unreachable from Chaos Orb in this hour's graph
                liquidity_chaos = min(vol_a * cv_a, vol_b * cv_b)
                if liquidity_chaos < min_liquidity_chaos:
                    continue
                spread_pct = (hi_rate - lo_rate) / lo_rate * 100
                items.append({
                    "nameA": name_a, "nameB": name_b,
                    "rateLow": round(lo_rate, 4), "rateHigh": round(hi_rate, 4),
                    "spreadPct": round(spread_pct, 1),
                    "liquidityChaos": round(liquidity_chaos, 1),
                })
                # Liquid but volatile pairs are shown above regardless — just
                # not trusted as a cycle-graph edge (see
                # _FLIP_CYCLE_MAX_EDGE_SPREAD_PCT's comment on why).
                if spread_pct <= _FLIP_CYCLE_MAX_EDGE_SPREAD_PCT:
                    avg_rate = (lo_rate + hi_rate) / 2
                    graph.setdefault(name_a, {})[name_b] = avg_rate
                    graph.setdefault(name_b, {})[name_a] = 1 / avg_rate
                    rounded_liq = round(liquidity_chaos, 1)
                    edge_liquidity[(name_a, name_b)] = rounded_liq
                    edge_liquidity[(name_b, name_a)] = rounded_liq
            items.sort(key=lambda it: -it["spreadPct"])

            strategies = []
            for cyc in _find_flip_cycles(graph, _FLIP_CYCLE_START):
                path = cyc["path"]
                steps = []
                for i in range(len(path) - 1):
                    frm, to = path[i], path[i + 1]
                    steps.append({
                        "sell": frm, "buy": to, "rate": round(graph[frm][to], 4),
                        "liquidityChaos": edge_liquidity.get((frm, to)),
                    })
                strategies.append({
                    "steps": steps,
                    "profitPct": round((cyc["rate"] - 1) * 100, 2),
                    "endAmount": round(cyc["rate"], 4),
                    "minStepLiquidityChaos": min(s["liquidityChaos"] for s in steps),
                })
            result["items"] = items[:_FLIP_MAX_RESULTS]
            result["strategies"] = strategies
            result["hourTimestamp"] = hour_ts
    except Exception as e:
        print(f"  [warning] flip advisor ({league}): {e}")
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
# --------------------------------------------------------------------------- #
# Frontend — shared chrome + per-page templates
# --------------------------------------------------------------------------- #
# Every page shares: head boilerplate (SEO/AdSense/fonts, SHARED_HEAD_TEMPLATE),
# CSS (SHARED_CSS), header chrome (SHARED_HEADER_HTML, with an
# __EXTRA_CONTROLS__ slot for page-specific header controls), the site-menu
# drawer (PAGES registry + render_sitemenu()), the footer (SHARED_FOOTER_HTML),
# and a small set of page-agnostic JS (SHARED_JS_CHROME: escAttr, theme
# toggle, menu open/close, the popover system, and the t()/applyStaticI18n()
# i18n plumbing). This is a plain string-composition pattern (same
# __PLACEHOLDER__ substitution already used for __POLL_MS__/__CANONICAL_URL__)
# — no build step, no bundler, no ES modules, consistent with this file's
# zero-dependency character.
#
# Each page keeps its OWN complete I18N object (deliberately NOT auto-merged
# from a shared dict — I18N is one large literal with nested template-literal
# HTML, too risky to split/merge programmatically for the modest DRY win).
# When adding a new page, copy these shared UI-chrome keys into its own I18N
# as a starting point: tagline, chip_price, chip_sync, chip_next, btn_refresh,
# btn_syncing, menu_title, footer_disclaimer, footer_made_by, footer_dm, plus
# its own menu_<slug> label (see PAGES below).
#
# To add a new page: add one entry to PAGES, write its own <PAGE>_BODY/
# <PAGE>_JS content (and __EXTRA_CONTROLS__ content if it needs header
# controls beyond the shared ones), and a render_<page>_page() function
# following render_bosses_page() below. Then add one do_GET branch and one
# build_static.py build call — mechanical and explicit, not automatic (with
# only one real page today, generic "loop over all pages" machinery would be
# speculative).

PAGES = [
    # "game" is None for game-neutral pages (rendered as plain top-level links, always
    # above both game groups) or "poe1"/"poe2" for game-specific ones — those render
    # inside a dedicated <div class="sitemenu-group" data-game="..."> container (see
    # render_sitemenu() below), and it's the two CONTAINERS that get reordered by
    # reorderSiteMenuByGame() (in SHARED_JS_CHROME) based on the visitor's stored
    # bossFarmGame preference — not the individual links inside them. A group with no
    # links (e.g. "poe2" for a non-admin visitor, before enableAdminUI() ever injects
    # anything into it) hides itself entirely via CSS (:has() — see SHARED_CSS).
    {"slug": "home", "icon": "&#129517;", "menu_key": "menu_home", "label_en": "Home", "game": None},
    {"slug": "bosses", "icon": "&#128128;", "menu_key": "menu_bosses", "label_en": "Boss Farm", "game": "poe1"},
    {"slug": "flip-advisor", "icon": "&#128177;", "menu_key": "menu_flip_advisor", "label_en": "Flip Advisor", "game": "poe1"},
    {"slug": "campaign", "icon": "&#128506;", "menu_key": "menu_campaign", "label_en": "Campaign Guide", "game": "poe1"},
    {"slug": "poe2-campaign", "icon": "&#9876;&#65039;", "menu_key": "menu_poe2_campaign", "label_en": "PoE2 Campaign", "game": "poe2"},
    # Admin-only pages (Trade Sniper) intentionally not listed here —
    # hidden from the site menu for everyone else, injected client-side (into the
    # matching game's <div class="sitemenu-group">) by enableAdminUI() only once the
    # admin handshake confirms. Their /slug routes still render and work for anyone
    # with the direct URL; only the nav link is suppressed.
]

GAME_GROUP_LABELS = {"poe1": "Path of Exile 1", "poe2": "Path of Exile 2"}


def _sitemenu_link(pg, current_slug):
    cls = "sitemenu-link active" if pg["slug"] == current_slug else "sitemenu-link"
    aria = ' aria-current="page"' if pg["slug"] == current_slug else ""
    return (f'  <a class="{cls}" href="/{pg["slug"]}"{aria}>{pg["icon"]} '
            f'<span data-i18n="{pg["menu_key"]}">{pg["label_en"]}</span></a>')


def render_sitemenu(current_slug):
    neutral = [_sitemenu_link(pg, current_slug) for pg in PAGES if not pg["game"]]
    groups = []
    for game, label in GAME_GROUP_LABELS.items():
        links = [_sitemenu_link(pg, current_slug) for pg in PAGES if pg["game"] == game]
        groups.append(f'  <div class="sitemenu-group" data-game="{game}">\n'
                       f'    <div class="sitemenu-group-label">{label}</div>\n'
                       + "\n".join(links) + ("\n" if links else "") +
                       '  </div>')
    return ('<div class="siteoverlay" id="siteoverlay" hidden></div>\n'
            '<nav class="sitemenu" id="sitemenu" aria-label="site menu">\n'
            '  <div class="sitemenu-head">\n'
            '    <span class="sitemenu-title" data-i18n="menu_title">Menu</span>\n'
            '    <button class="sitemenu-close" id="sitemenu-close" aria-label="close menu">&#10005;</button>\n'
            '  </div>\n'
            + "\n".join(neutral) + "\n"
            + "\n".join(groups) + "\n"
            "</nav>\n")


def _favicon_data_uri(emoji):
    """Builds an inline SVG favicon from a raw emoji character — each page
    substitutes its own via __FAVICON_URL__ so the browser tab icon matches
    that page's own brand icon (__BRAND_ICON__) instead of one hardcoded
    icon (a skull) for every page site-wide, which is what this used to be
    before the multi-page split."""
    svg = f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>{emoji}</text></svg>"
    return "data:image/svg+xml," + urllib.parse.quote(svg)


# --------------------------------------------------------------------------- #
# Campaign Guide pages (/campaign PoE1, /poe2-campaign PoE2) — shared helpers
# --------------------------------------------------------------------------- #
# Both games' campaign act data (POE1_CAMPAIGN_ACTS / POE2_CAMPAIGN_ACTS,
# defined further down near their respective pages) feeds these two
# generators. This is the one place in the file where a page's body/I18N is
# built by looping over Python data instead of hand-written per-section HTML
# (unlike BOSSES_BODY, which is empty chrome hydrated by a live JSON payload
# client-side) — justified because this content is fully static/known at
# build time, and hand-duplicating ~10 (PoE1) / ~7 (PoE2) nearly-identical
# act sections twice each (EN+PT) would be pure repetition. Each act still
# ultimately renders through the same data-i18n + applyStaticI18n() mechanism
# every other page uses (same precedent as I18N.note's large prose block) —
# only the per-act SVG stays outside that, since it's language-neutral
# (numbers/letters only, no embedded prose, so it never needs re-rendering
# on a language switch).
#
# Route "waypoints" and pin positions are deliberately schematic, not
# to-scale real map coordinates or a literal turn-by-turn speedrun path —
# zone sub-connections can shift slightly between patches even when the
# major zone names don't, so this shows visit ORDER of the major named
# zones/quest objectives, not exact geometry. Quest-giver NPC names are
# intentionally omitted (several community sources disagree with each other
# on exactly who hands out which of these quests) in favor of the objective
# zone, which every source agrees on — same "don't state what isn't
# confidently known" policy as the RePoE/divination-card precedent elsewhere
# in this file.
def _campaign_act_svg(n_steps, quest_pins):
    """n_steps: number of route waypoints. quest_pins: list of
    (waypoint_index, letter) for each must-do quest, letter matches the
    lettered quest list rendered beside it. Inline SVG (no separate asset
    file, same precedent as _favicon_data_uri), using var(--...) CSS custom
    properties for fill/stroke so it re-themes with the rest of the page.

    Route arrows point at the actual next waypoint (computed from the real
    vector between the two node centers via an auto-oriented SVG marker),
    not a fixed direction — a zigzag route has diagonal segments, so a
    hardcoded up/down arrow would point at nothing. Quest pins are plain
    (non-directional) dots on a leader line, precisely because they are NOT
    route-direction indicators — only the route line itself carries an
    arrowhead, so there's no longer any arrow that could point the wrong way."""
    step_w = 90
    pad_x = 30
    row_y = (46, 108)
    height = 150
    pts = [(pad_x + i * step_w, row_y[i % 2]) for i in range(max(n_steps, 1))]
    width = pad_x * 2 + max(n_steps - 1, 0) * step_w
    parts = [f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" '
             f'role="img" aria-label="route map">']
    parts.append('<defs><marker id="campaign-arrow" viewBox="0 0 10 10" refX="8" refY="5" '
                 'markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
                 '<path d="M0,0 L10,5 L0,10 z" fill="var(--gold-bright)"/></marker></defs>')
    for i in range(len(pts) - 1):
        x1, y1 = pts[i]
        x2, y2 = pts[i + 1]
        dx, dy = x2 - x1, y2 - y1
        dist = (dx * dx + dy * dy) ** 0.5 or 1
        ux, uy = dx / dist, dy / dist
        # stop short of the destination circle (r=12) so the arrowhead sits
        # right at its edge instead of hiding underneath it
        ex, ey = x2 - ux * 14, y2 - uy * 14
        sx, sy = x1 + ux * 13, y1 + uy * 13
        parts.append(f'<line x1="{sx:.1f}" y1="{sy:.1f}" x2="{ex:.1f}" y2="{ey:.1f}" '
                     f'stroke="var(--line)" stroke-width="2" marker-end="url(#campaign-arrow)"/>')
    for i, (x, y) in enumerate(pts):
        parts.append(f'<circle cx="{x}" cy="{y}" r="12" fill="var(--panel2)" '
                     f'stroke="var(--gold-bright)" stroke-width="2"/>')
        parts.append(f'<text x="{x}" y="{y+4}" text-anchor="middle" font-size="11" '
                     f'font-weight="700" fill="var(--gold-bright)">{i+1}</text>')
    for wp_idx, letter in quest_pins:
        x, y = pts[wp_idx]
        up = y < height / 2
        py = y - 28 if up else y + 28
        parts.append(f'<line x1="{x}" y1="{y}" x2="{x}" y2="{py}" stroke="var(--ok)" '
                     f'stroke-width="1.5" stroke-dasharray="2,2"/>')
        parts.append(f'<circle cx="{x}" cy="{py}" r="8" fill="var(--ok)"/>')
        parts.append(f'<text x="{x}" y="{py+3.5}" text-anchor="middle" font-size="9" '
                     f'font-weight="700" fill="var(--panel)">{letter}</text>')
    parts.append('</svg>')
    return "".join(parts)


def _campaign_act_content_html(act, lang):
    """Builds one act's full inner HTML for one language — becomes an
    I18N[lang]['campaign_<id>'] value, swapped in via data-i18n like every
    other large-prose block in this file (see I18N.note)."""
    def pick(en, pt):
        return en if lang == "en" else pt

    title = pick(act["title_en"], act["title_pt"])
    route = act["route_en"] if lang == "en" else act["route_pt"]
    parts = [f'<h3 class="campaign-act-title">{title}</h3>']
    parts.append('<div class="campaign-section-label">' + pick("Route", "Rota") + '</div>')
    parts.append('<div class="campaign-route-list">')
    for i, wp in enumerate(route):
        parts.append(f'<div class="rstep"><span class="rnum">{i+1}</span><span>{wp}</span></div>')
    parts.append('</div>')
    if act["quests"]:
        parts.append('<div class="campaign-section-label">'
                      + pick("Do these — passive skill points", "Faça estas — pontos de passiva") + '</div>')
        for i, q in enumerate(act["quests"]):
            letter = chr(65 + i)
            name = pick(q["name_en"], q["name_pt"])
            zone = pick(q["zone_en"], q["zone_pt"])
            note = pick(q["note_en"], q["note_pt"])
            parts.append(
                f'<div class="campaign-quest"><div class="cq-head">'
                f'<span class="cq-pin">{letter}</span> {name} '
                f'<span class="cq-reward">{q["reward"]}</span></div>'
                f'<div class="cq-zone">{zone}</div><div>{note}</div></div>')
    if act.get("trials_en"):
        trials = act["trials_en"] if lang == "en" else act["trials_pt"]
        parts.append('<div class="campaign-section-label">' + pick(
            "Trial of Ascendancy", "Julgamento de Ascendência") + '</div>')
        parts.append('<div class="campaign-route-list">')
        for zone in trials:
            parts.append(f'<div class="rstep"><span class="rnum">&#9884;</span><span>{zone}</span></div>')
        parts.append('</div>')
    if act.get("boss_en"):
        boss = pick(act["boss_en"], act["boss_pt"])
        parts.append('<div class="campaign-section-label">' + pick("Act boss", "Chefe do ato") + '</div>'
                      + f'<div>{boss}</div>')
    return "".join(parts)


def _render_campaign_acts(acts, img_dir=None):
    """Returns (body_html, en_i18n_js_entries, pt_i18n_js_entries) for a full
    campaign page's act-by-act section. The two *_i18n_js_entries strings are
    raw JS object-literal source (one `key: "...",` line per act) meant to be
    embedded directly inside that page's I18N.en/I18N.pt blocks — each value
    is produced via json.dumps() rather than a hand-escaped template
    literal, since JSON string syntax is valid JS string syntax and safely
    handles any apostrophes/quotes in the generated HTML (e.g. quest names
    like "Dweller's Deep") without manual escaping.

    img_dir: if given (e.g. "/imgs/poe1"), each act shows the real supplied
    map screenshot ("<img_dir>/act<N>.png", N from the act's "a<N>" id) as
    its route visual instead of the generated schematic SVG — these images
    already show the actual route nodes, branches, and quest markers (from a
    real leveling-route tool), which is strictly more accurate than our own
    simplified schematic, so when a real image exists it takes over
    entirely rather than sitting as a background behind our SVG. None (the
    default) falls back to `_campaign_act_svg()` — used for games/acts with
    no map image yet."""
    body_chunks = []
    en_entries = []
    pt_entries = []
    for act in acts:
        key = "campaign_" + act["id"]
        if img_dir:
            act_num = act["id"][1:]
            img_url = f"{img_dir}/act{act_num}.png"
            visual = (f'<div class="campaign-map-wrap"><img class="campaign-map-img" '
                      f'src="{img_url}" alt="Act {act_num} route map" loading="lazy"></div>')
        else:
            quest_pins = [(q["attach"], chr(65 + i)) for i, q in enumerate(act["quests"])]
            svg = _campaign_act_svg(len(act["route_en"]), quest_pins)
            visual = f'<div class="campaign-svg-wrap">{svg}</div>'
        body_chunks.append(
            f'<div class="campaign-act">\n{visual}\n'
            f'<div data-i18n="{key}"></div>\n</div>')
        en_entries.append(f'  {key}: {json.dumps(_campaign_act_content_html(act, "en"))},')
        pt_entries.append(f'  {key}: {json.dumps(_campaign_act_content_html(act, "pt"))},')
    body_html = '<div class="campaign-guide">\n' + "\n".join(body_chunks) + '\n</div>'
    return body_html, "\n".join(en_entries), "\n".join(pt_entries)


def _render_campaign_items(items):
    """Builds one language-neutral-container .campaign-items grid (item name
    + why, both en/pt versions rendered as sibling data-lang blocks toggled
    by the same I18N mechanism)... """
    # Items render through the same per-act I18N key mechanism — see
    # _render_campaign_page_body() below, which calls this once per language.
    parts = ['<div class="campaign-items">']
    for it in items:
        parts.append(
            '<div class="campaign-item"><b>{item}</b><div class="ci-why">{why}</div></div>'
            .format(item=it["item"], why=it["why"]))
    parts.append('</div>')
    return "".join(parts)


SHARED_HEAD_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta name="google-adsense-account" content="ca-pub-7517572231491496">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__PAGE_TITLE__</title>
<meta name="description" content="__PAGE_DESCRIPTION__">
<meta name="robots" content="index, follow">
<link rel="canonical" href="__CANONICAL_URL__">
<meta property="og:type" content="website">
<meta property="og:title" content="__PAGE_SOCIAL_TITLE__">
<meta property="og:description" content="__PAGE_SOCIAL_DESCRIPTION__">
<meta property="og:url" content="__CANONICAL_URL__">
<meta name="twitter:card" content="summary">
<meta name="twitter:title" content="__PAGE_SOCIAL_TITLE__">
<meta name="twitter:description" content="__PAGE_SOCIAL_DESCRIPTION__">
<link rel="icon" href="__FAVICON_URL__">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Cinzel:wght@500;700&family=Spectral:ital,wght@0,400;0,600;1,400&display=swap" rel="stylesheet">
<!-- Google tag (gtag.js) -->
<script async src="https://www.googletagmanager.com/gtag/js?id=G-VNJJSYPYEQ"></script>
<script>
  window.dataLayer = window.dataLayer || [];
  function gtag(){dataLayer.push(arguments);}
  gtag('js', new Date());

  gtag('config', 'G-VNJJSYPYEQ');
</script>
<script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-7517572231491496"
     crossorigin="anonymous"></script>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"WebApplication","name":"__PAGE_APP_NAME__","description":"__PAGE_JSONLD_DESCRIPTION__","applicationCategory":"UtilitiesApplication","operatingSystem":"Any","url":"__CANONICAL_URL__","offers":{"@type":"Offer","price":"0","priceCurrency":"USD"}}
</script>"""

SHARED_CSS = r"""<style>
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
/* Any element toggled via the plain `hidden` attribute/property must stay
   hidden regardless of its own class's `display` value — an author rule like
   `.foo{display:flex}` normally beats the UA stylesheet's `[hidden]{display:
   none}` outright (author rules always win over UA rules at equal
   importance, independent of specificity), which silently broke
   .page-card[hidden] (admin-only home cards showing for everyone),
   .snipe-current[hidden], and .snipe-row[hidden] — real bugs, not just this
   one. One global !important rule here fixes the whole class of bug instead
   of patching each conflicting class individually. */
[hidden]{display:none!important}
html,body{margin:0}
html{scroll-behavior:smooth}
body{
  background:radial-gradient(1200px 600px at 50% -10%, var(--bg-glow) 0%, transparent 60%), var(--bg);
  color:var(--ink); font-family:"Spectral",Georgia,serif; font-size:15px; font-weight:var(--body-weight);
  line-height:1.4; display:flex; flex-direction:column; min-height:100vh;
}
a{color:inherit; text-decoration:none}
.wrap{max-width:1240px; margin:0 auto; padding:0 20px}

header{border-bottom:1px solid var(--line);
  background:linear-gradient(180deg,var(--panel),var(--bg));
  position:sticky; top:0; z-index:5; backdrop-filter:blur(6px);}
.head{display:flex; align-items:center; gap:20px; padding:18px 20px; flex-wrap:wrap}
.brand-block{display:flex; flex-direction:column; gap:6px}
.brand{font-family:"Cinzel",serif; font-weight:700; letter-spacing:.14em; text-transform:uppercase}
.brand h1{display:block; margin:0; color:var(--gold-bright); font-weight:700; font-size:20px}
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
.timectl .rate .div{color:var(--uber); font-size:10px; opacity:.85; margin-left:5px; font-weight:400}
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

footer{border-top:1px solid var(--line); margin-top:auto; padding-top:24px;
  background:linear-gradient(0deg,var(--panel),var(--bg))}
.foot{max-width:1240px; margin:0 auto; padding:16px 20px; display:flex;
  justify-content:space-between; align-items:center; gap:20px; flex-wrap:wrap;
  color:var(--ink-dim); font-size:11.5px; line-height:1.5}
.foot-credits{display:flex; flex-direction:column; align-items:flex-end; gap:4px; flex:0 0 auto}
.foot-byline{color:var(--gold); white-space:nowrap; font-weight:600}
.foot-contact{color:var(--ink-dim); font-size:11px; white-space:nowrap}
.foot-contact:hover{color:var(--ink)}

.menutoggle{font-family:ui-monospace,monospace; font-size:16px; line-height:1; color:var(--ink-dim);
  background:var(--panel); border:1px solid var(--line); border-radius:2px;
  padding:6px 10px; cursor:pointer; flex:0 0 auto}
.menutoggle:hover{color:var(--ink)}
.siteoverlay{display:none; position:fixed; inset:0; background:rgba(0,0,0,.45); z-index:29}
.siteoverlay.show{display:block}
.sitemenu{position:fixed; top:0; left:0; height:100vh; width:240px; background:var(--panel);
  border-right:1px solid var(--line); z-index:30; transform:translateX(-100%);
  transition:transform .22s ease; padding:16px; overflow-y:auto}
.sitemenu.open{transform:translateX(0)}
.sitemenu-head{display:flex; align-items:center; justify-content:space-between; margin-bottom:16px}
.sitemenu-title{font-family:"Cinzel",serif; font-weight:700; font-size:12px; letter-spacing:.14em;
  text-transform:uppercase; color:var(--gold-bright)}
.sitemenu-close{font-family:ui-monospace,monospace; font-size:14px; color:var(--ink-dim);
  background:none; border:0; cursor:pointer; padding:2px 6px}
.sitemenu-close:hover{color:var(--ink)}
.sitemenu-link{display:flex; align-items:center; gap:8px; padding:8px 10px; border-radius:3px;
  color:var(--ink-dim); font-size:13px}
.sitemenu-link:hover{background:var(--overlay); color:var(--ink)}
.sitemenu-link.active{color:var(--gold-bright); background:var(--overlay-soft); font-weight:600}
.sitemenu-group{margin-top:14px; padding-top:10px; border-top:1px solid var(--line)}
.sitemenu-group:not(:has(.sitemenu-link)){display:none}
.sitemenu-group-label{font-family:"Cinzel",serif; font-weight:700; font-size:10.5px; letter-spacing:.1em;
  text-transform:uppercase; color:var(--ink-dim); padding:0 10px 6px}
@media (prefers-reduced-motion:reduce){*{animation:none!important} .sitemenu{transition:none}}

.meta-left[hidden]{display:none}

.chip.admin-badge{color:var(--bg); background:var(--gold); border-color:var(--gold); font-weight:700;
  font-family:"Cinzel",serif; letter-spacing:.12em; font-size:11px; text-transform:uppercase}

.snipe-form{background:linear-gradient(180deg,var(--panel),var(--panel2));
  border:1px solid var(--line); border-radius:4px; padding:16px 18px; margin-top:14px;
  display:flex; flex-direction:column; gap:12px}
.snipe-row{display:flex; align-items:center; gap:10px; flex-wrap:wrap}
.snipe-row label{display:flex; flex-direction:column; gap:4px; font-size:11px; color:var(--ink-dim);
  font-family:ui-monospace,monospace; text-transform:uppercase; letter-spacing:.05em; flex:1; min-width:160px}
.snipe-row input, .snipe-row select{font-family:"Spectral",Georgia,serif; font-size:13px; color:var(--ink);
  background:var(--panel); border:1px solid var(--line); border-radius:2px; padding:8px 10px}
.snipe-actions{display:flex; align-items:center; gap:10px}
.snipe-status{font-family:ui-monospace,monospace; font-size:12px; color:var(--ink-dim)}
.snipe-status.err{color:var(--neg)}
.snipe-status.ok{color:var(--ok)}
.snipe-status.warn{color:var(--warn)}
.snipe-poesessid-warn{padding:8px 10px; margin:0; font-size:11px; border-left:2px solid var(--warn);
  background:var(--overlay-soft)}
button.stop{font-family:"Cinzel",serif; font-weight:700; font-size:11px; letter-spacing:.18em;
  text-transform:uppercase; color:var(--ink); background:var(--panel); border:1px solid var(--line);
  padding:8px 16px; cursor:pointer; border-radius:2px}
button.stop:hover{border-color:var(--neg); color:var(--neg)}
button.sync:disabled, button.stop:disabled{opacity:.4; cursor:default}
.snipe-results{margin-top:18px}
.snipe-list{display:flex; flex-direction:column; gap:8px; margin-top:10px}
.snipe-hit{display:flex; align-items:center; gap:12px; background:var(--panel);
  border:1px solid var(--line); border-radius:3px; padding:10px 14px}
.snipe-hit img{width:32px; height:32px; object-fit:contain; flex:0 0 auto}
.snipe-hit .nm{flex:1; min-width:0; white-space:nowrap; overflow:hidden; text-overflow:ellipsis}
.snipe-hit .px{font-family:ui-monospace,monospace; color:var(--gold-bright); font-weight:700; white-space:nowrap}
.snipe-hit .ref{font-family:ui-monospace,monospace; color:var(--ink-dim); font-size:11px; white-space:nowrap}
.snipe-hit .variant{font-family:ui-monospace,monospace; font-size:11px; color:var(--ok)}
.snipe-hit .variant.unsure{color:var(--warn)}
.snipe-hit a.tradelink{font-family:ui-monospace,monospace; font-size:11px; color:var(--uber);
  border:1px solid var(--line); border-radius:2px; padding:4px 9px; white-space:nowrap}
.snipe-hit a.tradelink:hover{color:var(--ink); border-color:var(--uber)}

.snipe-current{display:flex; align-items:center; gap:12px; background:var(--panel2);
  border:1px dashed var(--line); border-radius:3px; padding:10px 14px; margin-top:14px}
.snipe-current img{width:28px; height:28px; object-fit:contain; flex:0 0 auto}
.snipe-current .dot{width:8px; height:8px; border-radius:50%; background:var(--ok);
  flex:0 0 auto; animation:snipe-pulse 1.2s ease-in-out infinite}
@keyframes snipe-pulse{0%,100%{opacity:1} 50%{opacity:.25}}
.snipe-current .nm{font-family:ui-monospace,monospace; font-size:12.5px; color:var(--ink)}
.snipe-current .lbl{font-family:ui-monospace,monospace; font-size:11px; color:var(--ink-dim);
  text-transform:uppercase; letter-spacing:.05em}

.slabel-row{display:flex; align-items:center; justify-content:space-between; gap:10px; flex-wrap:wrap}
.log-toggle{font-family:ui-monospace,monospace; font-size:11px; color:var(--ink-dim); cursor:pointer;
  border:1px solid var(--line); border-radius:2px; padding:3px 8px; background:none}
.log-toggle:hover{color:var(--ink); border-color:var(--uber)}
.snipe-log{display:grid; grid-template-columns:repeat(auto-fill,minmax(230px,1fr)); gap:10px;
  margin-top:10px; max-height:480px; overflow-y:auto}
.snipe-log.collapsed{display:none}
.snipe-log-card{display:flex; flex-direction:column; gap:7px; background:var(--panel);
  border:1px solid var(--line); border-radius:4px; padding:10px 12px; font-size:12px}
.snipe-log-card .card-head{display:flex; align-items:center; gap:8px}
.snipe-log-card img{width:28px; height:28px; object-fit:contain; flex:0 0 auto}
.snipe-log-card .nm{flex:1; min-width:0; font-family:"Spectral",Georgia,serif; font-size:12.5px;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis}
.snipe-log-card .idbadge{font-family:ui-monospace,monospace; font-size:9.5px; text-transform:uppercase;
  letter-spacing:.04em; padding:2px 5px; border-radius:2px; border:1px solid var(--line);
  color:var(--ink-dim); white-space:nowrap}
.snipe-log-card .idbadge.unid{color:var(--ok); border-color:var(--ok)}
.snipe-log-card .price-grid{display:grid; grid-template-columns:auto 1fr; gap:3px 8px;
  font-family:ui-monospace,monospace; font-size:11px}
.snipe-log-card .price-grid .lbl{color:var(--ink-dim)}
.snipe-log-card .price-grid .val{color:var(--ink); text-align:right; white-space:nowrap}
.snipe-log-card .price-grid .val.found{color:var(--ok); font-weight:600}
.snipe-log-card .price-grid .val.none{opacity:.6; font-weight:400; color:var(--ink-dim)}
.snipe-log-card .variants{opacity:.65; font-size:10px}
.snipe-log-card .dbg{font-family:ui-monospace,monospace; font-size:10px; color:var(--ink-dim); opacity:.7}
.snipe-log-card.hit{border-color:var(--ok)}

.game-toggle{display:flex; gap:10px; margin-top:14px; flex-wrap:wrap}
.game-toggle-btn{flex:1; min-width:200px; display:flex; align-items:center; gap:10px;
  font-family:"Cinzel",serif; font-weight:700; font-size:13px; letter-spacing:.06em;
  color:var(--ink-dim); background:var(--panel); border:1px solid var(--line);
  border-radius:3px; padding:14px 18px; cursor:pointer}
.game-toggle-btn .gt-icon{width:22px; height:22px; object-fit:contain; flex:0 0 auto}
.game-panel h2 .panel-icon{width:18px; height:18px; object-fit:contain; vertical-align:middle}
.game-toggle-btn:hover{color:var(--ink); border-color:var(--uber)}
.game-toggle-btn.active{color:var(--bg); background:var(--gold); border-color:var(--gold)}
.game-panels{display:grid; grid-template-columns:1fr 1fr; gap:18px; margin-top:18px}
.game-panels:has(.game-panel[hidden]){grid-template-columns:1fr}
@media (max-width:800px){.game-panels{grid-template-columns:1fr}}
.game-panel{background:linear-gradient(180deg,var(--panel),var(--panel2));
  border:1px solid var(--line); border-radius:3px; padding:16px 18px}
.game-panel h2{margin:0 0 8px; font-family:"Cinzel",serif; font-weight:700; font-size:15px;
  letter-spacing:.04em; display:flex; align-items:center; gap:8px}
.page-cards{display:flex; flex-direction:column; gap:10px; margin-top:14px}
.page-card{position:relative; display:block; background:var(--panel); border:1px solid var(--line);
  border-radius:3px; padding:12px 14px; color:var(--ink)}
.page-card:hover{border-color:var(--uber); background:var(--overlay)}
.page-card-head{display:flex; align-items:center; gap:8px; font-family:"Cinzel",serif; font-weight:700;
  font-size:13px; letter-spacing:.03em}
.page-card .pc-icon{font-size:16px}
.page-card-desc{margin-top:6px; font-size:12px; color:var(--ink-dim); line-height:1.5}
.page-card-badge{position:absolute; top:10px; right:12px; font-family:ui-monospace,monospace; font-size:9px;
  text-transform:uppercase; letter-spacing:.06em; color:var(--warn); border:1px solid var(--warn);
  border-radius:2px; padding:2px 6px}
.patchnotes{margin-top:14px}
.patchnotes-list{display:flex; flex-direction:column; gap:6px; margin-top:8px}
.patch-item{display:flex; align-items:baseline; gap:10px; font-size:12.5px;
  border-bottom:1px solid var(--line); padding-bottom:6px}
.patch-item:last-child{border-bottom:0}
.patch-item .patch-date{font-family:ui-monospace,monospace; font-size:10.5px;
  color:var(--ink-dim); white-space:nowrap}
.patch-item a{color:var(--ink); text-decoration:none; flex:1; min-width:0}
.patch-item a:hover{color:var(--gold-bright); text-decoration:underline}

.flip-grid{display:grid; grid-template-columns:repeat(auto-fill,minmax(240px,1fr)); gap:10px;
  margin-top:14px}
.flip-card{display:flex; flex-direction:column; gap:6px; background:var(--panel);
  border:1px solid var(--line); border-radius:4px; padding:12px 14px}
.flip-card .flip-pair{font-family:"Spectral",Georgia,serif; font-size:13px; color:var(--ink)}
.flip-card .flip-rate{font-family:ui-monospace,monospace; font-size:11px; color:var(--ink-dim)}
.flip-card .flip-spread{font-family:ui-monospace,monospace; font-size:18px; font-weight:700;
  color:var(--gold-bright)}
.flip-card .flip-volume{font-family:ui-monospace,monospace; font-size:10.5px; color:var(--ink-dim)}

.flip-strategies{display:flex; flex-direction:column; gap:10px; margin-top:14px}
.flip-strategy-card{background:linear-gradient(180deg,var(--panel),var(--panel2));
  border:1px solid var(--ok); border-radius:4px; padding:12px 14px}
.flip-strategy-head{display:flex; align-items:center; justify-content:space-between;
  flex-wrap:wrap; gap:8px; margin-bottom:8px}
.flip-strategy-profit{font-family:ui-monospace,monospace; font-size:16px; font-weight:700; color:var(--ok)}
.flip-strategy-meta{font-family:ui-monospace,monospace; font-size:10.5px; color:var(--ink-dim)}
.flip-steps{display:flex; flex-direction:column; gap:4px}
.flip-step{display:flex; align-items:center; gap:8px; font-size:12px; flex-wrap:wrap}
.flip-step .step-num{font-family:ui-monospace,monospace; font-size:10px; color:var(--ink-dim);
  width:16px; flex:0 0 auto}
.flip-step .step-op{font-family:ui-monospace,monospace; font-size:9.5px; text-transform:uppercase;
  letter-spacing:.05em; padding:2px 6px; border-radius:2px; flex:0 0 auto}
.flip-step .step-op.sell{color:var(--neg); border:1px solid var(--neg)}
.flip-step .step-op.buy{color:var(--ok); border:1px solid var(--ok)}
.flip-step .step-desc{font-family:"Spectral",Georgia,serif; flex:1; min-width:0}
.flip-step .step-liq{font-family:ui-monospace,monospace; font-size:9.5px; color:var(--ink-dim); white-space:nowrap}
.flip-step .step-guide{font-family:ui-monospace,monospace; font-size:11px; color:var(--gold-bright);
  background:var(--overlay-soft); border-radius:2px; padding:2px 6px; white-space:nowrap}

.campaign-guide{display:flex; flex-direction:column; gap:20px; margin-top:14px}
.campaign-act{background:linear-gradient(180deg,var(--panel),var(--panel2));
  border:1px solid var(--line); border-radius:4px; padding:16px 18px}
.campaign-act-title{margin:0 0 10px; font-family:"Cinzel",serif; font-weight:700; font-size:15px;
  letter-spacing:.03em; color:var(--ink)}
.campaign-svg-wrap{background:var(--overlay-soft); border-radius:4px; padding:10px 10px 4px;
  margin-bottom:12px; overflow-x:auto}
.campaign-svg-wrap svg{display:block; width:100%; height:auto; min-width:420px}
.campaign-map-wrap{border-radius:4px; overflow:hidden; margin-bottom:12px; border:1px solid var(--line);
  background:var(--overlay-soft)}
.campaign-map-img{display:block; width:100%; height:auto}
.campaign-section-label{font-family:"Cinzel",serif; font-weight:700; font-size:11px; letter-spacing:.08em;
  text-transform:uppercase; color:var(--ink-dim); margin:14px 0 6px}
.campaign-route-list{display:flex; flex-direction:column; gap:4px; font-size:12.5px}
.campaign-route-list .rstep{display:flex; gap:8px; align-items:baseline}
.campaign-route-list .rnum{font-family:ui-monospace,monospace; font-size:10px; color:var(--gold-bright);
  flex:0 0 auto; width:16px}
.campaign-quest{border-left:2px solid var(--ok); padding:6px 10px; margin-bottom:6px; font-size:12.5px}
.campaign-quest .cq-head{font-weight:700; color:var(--ink); display:flex; gap:8px; align-items:baseline;
  flex-wrap:wrap}
.campaign-quest .cq-pin{font-family:ui-monospace,monospace; font-size:10px; color:var(--ok);
  border:1px solid var(--ok); border-radius:2px; padding:0 4px}
.campaign-quest .cq-reward{font-family:ui-monospace,monospace; font-size:10.5px; color:var(--gold-bright)}
.campaign-quest .cq-zone{font-size:11px; color:var(--ink-dim)}
.campaign-items{display:grid; grid-template-columns:repeat(auto-fill,minmax(220px,1fr)); gap:8px;
  margin-top:8px; margin-bottom:20px}
.campaign-item{background:var(--panel); border:1px solid var(--line); border-radius:3px;
  padding:8px 10px; font-size:12px}
.campaign-item b{color:var(--ink)}
.campaign-item .ci-why{color:var(--ink-dim); font-size:11px; margin-top:3px}
</style>"""

SHARED_HEADER_HTML = r"""<header>
  <div class="head">
    <button class="menutoggle" id="menutoggle" title="menu" aria-label="open menu">&#9776;</button>
    <div class="brand-block">
      <div class="brand"><h1>__BRAND_ICON__ __BRAND_TITLE__</h1><span data-i18n="tagline">PATH OF EXILE · BOSS ECONOMY</span></div>
      <div class="meta-left" __PRICECHIPS_ATTR__>
        <span class="chip-sm"><span data-i18n="chip_price">Price</span> <b id="src">—</b></span>
        <span class="chip-sm"><span data-i18n="chip_sync">Sync</span> <b id="ago">—</b> · <span data-i18n="chip_next">next</span> <b id="next">—</b></span>
      </div>
    </div>
    <div class="meta">
      <div class="chip"><span class="dot"></span><span data-i18n="chip_league">League</span> <b id="league">—</b></div>
      <select class="langsel" id="leaguesel" aria-label="league / liga"></select>
      <div class="chip" __DIVINE_CHIP_ATTR__>1 Divine <b id="divine">—</b> chaos</div>
      <div class="chip warn" id="warn" hidden></div>
      <select class="langsel" id="langsel" aria-label="language / idioma">
        <option value="en">EN</option>
        <option value="pt">PT-BR</option>
      </select>
      <button class="themetoggle" id="themetoggle" title="dark/light · escuro/claro" aria-label="toggle theme">&#127769;</button>
__EXTRA_CONTROLS__    </div>
  </div>
</header>"""

SHARED_FOOTER_HTML = r"""<footer>
  <div class="foot">
    <span data-i18n="footer_disclaimer">Unofficial fan tool — not affiliated with or endorsed by Grinding Gear Games. Prices from poe.ninja/poe.watch, drop data from poewiki/community guides — see the note above for sourcing and caveats.</span>
    <div class="foot-credits">
      <span class="foot-byline" data-i18n="footer_made_by">Built by Erick Lúcio</span>
      <a class="foot-contact" href="mailto:ericklucio.suv@gmail.com">
        ericklucio.suv@gmail.com — <span data-i18n="footer_dm">send me an email there for comment or feedback</span>
      </a>
    </div>
  </div>
</footer>"""

SHARED_JS_CHROME = r"""function escAttr(s){
  return String(s==null?'':s).replace(/&/g,'&amp;').replace(/"/g,'&quot;')
                             .replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
let theme = (function(){
  try { return localStorage.getItem('bossFarmTheme') || 'dark'; } catch(e) { return 'dark'; }
})();
function applyTheme(){
  document.documentElement.setAttribute('data-theme', theme);
  const btn = document.getElementById('themetoggle');
  if(btn) btn.innerHTML = theme === 'dark' ? '&#127769;' : '&#9728;&#65039;';
}
function openMenu(){
  document.getElementById('sitemenu').classList.add('open');
  const overlay = document.getElementById('siteoverlay');
  overlay.hidden = false;
  requestAnimationFrame(() => overlay.classList.add('show'));
}
function closeMenu(){
  document.getElementById('sitemenu').classList.remove('open');
  const overlay = document.getElementById('siteoverlay');
  overlay.classList.remove('show');
  setTimeout(() => { if(!document.getElementById('sitemenu').classList.contains('open')) overlay.hidden = true; }, 220);
}
let lang = (function(){
  try { return localStorage.getItem('bossFarmLang') || 'pt'; } catch(e) { return 'pt'; }
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
function applyStaticI18n(){
  document.querySelectorAll('[data-i18n]').forEach(el => { el.innerHTML = t(el.dataset.i18n); });
  document.documentElement.lang = lang === 'pt' ? 'pt-BR' : 'en';
}
document.getElementById('themetoggle').addEventListener('click', () => {
  theme = theme === 'dark' ? 'light' : 'dark';
  try { localStorage.setItem('bossFarmTheme', theme); } catch(e) {}
  applyTheme();
});
document.getElementById('menutoggle').addEventListener('click', openMenu);
document.getElementById('sitemenu-close').addEventListener('click', closeMenu);
document.getElementById('siteoverlay').addEventListener('click', closeMenu);
document.addEventListener('keydown', e => { if(e.key === 'Escape') closeMenu(); });

// Reorders the site menu's two game CONTAINERS (<div class="sitemenu-group"
// data-game="poe1"/"poe2">, built by render_sitemenu() from PAGES' game key)
// by the visitor's stored preference — what moves is the whole group (label
// + all its links), not the individual links inside it; Home (game-neutral)
// stays pinned above both regardless. Re-appending a node moves it to the
// end without removing/re-adding it, so this works as an in-place reorder.
// Unlike applyStaticI18n()/applyTheme(), this has no per-page I18N dependency
// (it only reads localStorage and rearranges DOM nodes render_sitemenu()
// already put there), so — deliberately breaking from this file's usual
// "shared chrome only defines, each page calls" rule — it's safe to call
// once here, directly, so no future page can forget to wire it in.
// enableAdminUI() calls it again after injecting admin-only links into their
// group, in case that changes which group has content first.
function reorderSiteMenuByGame(){
  const nav = document.getElementById('sitemenu');
  if(!nav) return;
  let preferred;
  try { preferred = localStorage.getItem('bossFarmGame') || 'poe1'; } catch(e) { preferred = 'poe1'; }
  const other = preferred === 'poe1' ? 'poe2' : 'poe1';
  const mineGroup = nav.querySelector('.sitemenu-group[data-game="' + preferred + '"]');
  const theirsGroup = nav.querySelector('.sitemenu-group[data-game="' + other + '"]');
  if(mineGroup) nav.appendChild(mineGroup);
  if(theirsGroup) nav.appendChild(theirsGroup);
}
reorderSiteMenuByGame();

// Admin detection via the "PoE Helper Admin" Chrome extension (personal-use,
// unpublished — see admin-extension/). This is NOT real authentication: it's
// a client-side convenience flag with nothing sensitive behind it (this repo
// has no login/database/write endpoints — see the Security section of
// CLAUDE.md). The site only ever stores a SHA-256 hash of the extension's
// token, never the token itself, so reading this page's source alone doesn't
// hand anyone a working token — but this is still just a personal toggle to
// show/hide dev-only UI in your own browser, not an access-control boundary.
//
// The extension's content script runs in an isolated JS world and can't set
// a property directly on this page's own `window` — a CustomEvent dispatched
// on `window` is the one thing both worlds can observe, so that's the
// handshake: this page asks "are you there?", the extension (if installed)
// answers with its token. Whichever side finishes loading first, the answer
// always arrives once both exist — no race on injection timing. No response
// ever arrives for the vast majority of visitors (no extension installed),
// which is indistinguishable from "not admin" — there's nothing to time out,
// the admin-only UI just never appears.
async function sha256Hex(str){
  const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(str));
  return Array.from(new Uint8Array(buf)).map(b => b.toString(16).padStart(2, '0')).join('');
}
const EXPECTED_ADMIN_HASH = '32aeba3d9e6dd8c874802de0d20d1b619dbcf326d11d8ae4d58ab9e33aad3172';
function enableAdminUI(){
  const meta = document.querySelector('.meta');
  if(meta && !document.getElementById('adminbadge')){
    const badge = document.createElement('div');
    badge.className = 'chip admin-badge';
    badge.id = 'adminbadge';
    badge.textContent = 'ADMIN';
    meta.insertBefore(badge, meta.firstChild);
  }
  const menu = document.getElementById('sitemenu');
  const poe1Group = menu && menu.querySelector('.sitemenu-group[data-game="poe1"]');
  if(poe1Group && !poe1Group.querySelector('a[href="/snipe"]')){
    const isCurrent = location.pathname.startsWith('/snipe');
    const a = document.createElement('a');
    a.className = 'sitemenu-link' + (isCurrent ? ' active' : '');
    a.href = '/snipe';
    if(isCurrent) a.setAttribute('aria-current', 'page');
    a.innerHTML = '&#127919; <span>' + t('menu_snipe') + '</span>';
    poe1Group.appendChild(a);
  }
  // Generic marker for any admin-gated content beyond the sitemenu link
  // above (e.g. the /home page's Trade Sniper page-card) — just add hidden
  // data-admin-only to an element and it reveals itself here once admin
  // status is confirmed, no per-page wiring needed.
  document.querySelectorAll('[data-admin-only]').forEach(el => { el.hidden = false; });
  reorderSiteMenuByGame();
}
let isAdmin = false;
let adminGateTimer = null;
window.addEventListener('poe-helper-admin-response', async e => {
  const hash = await sha256Hex(e.detail || '');
  if(hash === EXPECTED_ADMIN_HASH){
    isAdmin = true;
    if(adminGateTimer){ clearTimeout(adminGateTimer); adminGateTimer = null; }
    enableAdminUI();
  }
});
window.dispatchEvent(new CustomEvent('poe-helper-admin-request'));

// Hidden pages (PAGE_REQUIRES_ADMIN, set per-page — see render_snipe_page())
// aren't secured server-side (nothing sensitive behind them, see CLAUDE.md's
// Security section), so this is a UX convenience gate, not real access
// control — it just sends a visitor who stumbles onto an unlisted page
// somewhere real instead of leaving them on unfinished/unadvertised UI. Give
// the admin-extension handshake a brief window to respond (content scripts
// normally answer near-instantly) before deciding "not admin" and sending
// them home.
if(typeof PAGE_REQUIRES_ADMIN !== 'undefined' && PAGE_REQUIRES_ADMIN){
  adminGateTimer = setTimeout(() => { if(!isAdmin) location.replace('/home'); }, 600);
}
"""

# --------------------------------------------------------------------------- #
# Boss Farm page (/bosses)
# --------------------------------------------------------------------------- #
BOSSES_EXTRA_CONTROLS = r"""      <div class="sortby" id="sortby" role="group" aria-label="sort order">
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
"""

BOSSES_BODY = r"""

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

"""

BOSSES_JS = r"""const POLL_MS = __POLL_MS__;
let lastUpdated = 0;
let runMult = 1;

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
  menu_title: 'Menu', menu_home: 'Home', menu_bosses: 'Boss Farm', menu_snipe: 'Trade Sniper', menu_poe2_campaign: 'PoE2 Campaign', menu_flip_advisor: 'Flip Advisor', menu_campaign: 'Campaign Guide',
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
  pchance_label: 'Chance of profit',
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
  footer_dm: 'Send me an email there for comment or feedback',
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
  pchance: 'Simulated probability that your <b>total</b> profit is positive after this many runs (×1/×10/×100), not whether any single run is profitable. Simulated 1,000 times from each item’s independent drop chance — a small pool with one high-value item can show a low chance even when the average is positive, since most runs miss and only a lucky one carries it.',
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
  menu_title: 'Menu', menu_home: 'Início', menu_bosses: 'Farm de Bosses', menu_snipe: 'Caçador de Ofertas', menu_poe2_campaign: 'Campanha PoE2', menu_flip_advisor: 'Conselheiro de Flip', menu_campaign: 'Guia de Campanha',
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
  pchance_label: 'Chance de lucro',
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
  footer_dm: 'Manda um e-mail lá para comentário ou feedback',
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
  pchance: 'Probabilidade simulada de que seu lucro <b>total</b> seja positivo depois desse número de runs (×1/×10/×100), não se um único run é lucrativo. Simulado 1.000 vezes a partir da chance de drop independente de cada item — um pool pequeno com um item de alto valor pode mostrar uma chance baixa mesmo com a média positiva, já que a maioria dos runs erra e só um run de sorte carrega o resultado.',
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
function nmeClass(typ){ return escAttr((typ||'').toLowerCase()); }
function iconTag(it){ return it.icon ? `<img src="${escAttr(it.icon)}" alt="${escAttr(it.name)}" loading="lazy">`
                                     : `<span style="width:26px"></span>`; }

function entryRow(it, dr){
  const qty = it.qty > 1 ? `<span class="qty">×${it.qty}</span>` : '';
  const tt = it.link_src==='wiki' ? t('tt_no_price') : t('tt_ninja');
  return `<a class="item" href="${escAttr(it.url)}" target="_blank" rel="noopener" title="${tt}">
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
  return `<a class="item" href="${escAttr(it.url)}" target="_blank" rel="noopener" title="${tt}">
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

function fmtRateDiv(rate, dr){
  if(dr == null || dr <= 0) return '';
  const div = rate / dr;
  if(Math.abs(div) < 0.005) return '';
  return `<span class="div">${div>=0?'+':'−'}${Math.abs(div).toFixed(2)} div/hr</span>`;
}
function timeCtl(b, dr){
  const secs = timeOf(b);
  const net = adjustedNet(b);
  const rate = net != null ? net / (secs/3600) : null;
  const rateTxt = rate != null
    ? `<span class="rate ${rate>=0?'pos':'neg'}" data-info="rate">≈ ${fmtSigned(rate)}/hr${fmtRateDiv(rate, dr)}</span>`
    : '';
  return `<div class="timectl"><span class="qlbl" data-info="time">${t('time_label')}</span>
    <input type="number" min="1" step="5" value="${secs}" data-boss="${b.name}" class="timeinput"><span>s</span>
    ${rateTxt}
  </div>`;
}

function fmtSigned(v){ return (v>=0?'+':'−') + fmtChaos(Math.abs(v)) + 'c'; }

const PROFIT_CHANCE_TRIALS = 1000;
// Monte Carlo, not a closed form — with dozens of low-probability items per
// pool, simulating is far simpler and safer than deriving a Poisson-binomial
// distribution by hand. Items are treated as independent per-run Bernoulli
// trials, same assumption the EV sum already makes (linearity of
// expectation holds either way) — this keeps "chance of profit" consistent
// with the "avg" number already shown, rather than introducing a second,
// stricter mutual-exclusivity model nothing else in this file uses.
function simulateProfitChance(b, mult){
  if(b.entry.total_chaos == null) return null;
  const entryCost = b.entry.total_chaos * mult;
  const scale = 1 + quantOf(b) / 100;
  const items = b.drops.items
    .filter(it => it.chaos != null && it.chance != null)
    .map(it => ({p: it.chance, v: it.chaos * it.qty * scale}));
  if(!items.length) return entryCost <= 0 ? 1 : 0;
  let wins = 0;
  for(let t = 0; t < PROFIT_CHANCE_TRIALS; t++){
    let total = 0;
    for(let r = 0; r < mult; r++){
      for(const it of items){
        if(Math.random() < it.p) total += it.v;
      }
    }
    if(total > entryCost) wins++;
  }
  return wins / PROFIT_CHANCE_TRIALS;
}

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
  const chance = simulateProfitChance(b, mult);
  const chanceTxt = chance != null
    ? ` · <span data-info="pchance">${t('pchance_label')}</span> <span class="${chance>=0.5?'pos':'neg'}">${Math.round(chance*100)}%</span>`
    : '';
  return `<div class="profit"><span class="lbl" data-info="avg">${lbl}</span>
      <span class="val ${cls}">${sign}${chaosDiv(Math.abs(net), dr)}</span></div>
    <div class="range">
      <span class="rw neg" data-info="worst">${t('word_worst')} ${fmtSigned(worst)}</span>
      <span class="rb pos" data-info="best">${t('word_best')} ${fmtSigned(best)}</span>
    </div>
    <div class="evline"><span data-info="ev">${t('ev_drops')}</span> ${fmtChaos(ev)}c − ${t('entry_word')} ${fmtChaos(entryTotal)}c${chanceTxt}</div>`;
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
    ${timeCtl(b, dr)}
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
setInterval(tick, 1000);"""


def render_bosses_page():
    head = (SHARED_HEAD_TEMPLATE
            .replace("__PAGE_TITLE__", 'Boss Farm Estimator — Path of Exile Pinnacle &amp; Uber Boss Farming Profit Calculator')
            .replace("__PAGE_DESCRIPTION__", 'Live Path of Exile 1 farming profit calculator for every pinnacle, Uber, and Tier 17 Nightmare map boss — real poe.ninja prices, honest worst/average/best profit ranges instead of one misleading number.')
            .replace("__PAGE_SOCIAL_TITLE__", 'Boss Farm Estimator — PoE Boss Farming Profit Calculator')
            .replace("__PAGE_SOCIAL_DESCRIPTION__", 'Live Path of Exile 1 farming profit calculator for every pinnacle, Uber, and Tier 17 Nightmare map boss — real poe.ninja prices, honest worst/average/best profit ranges.')
            .replace("__PAGE_APP_NAME__", 'Boss Farm Estimator')
            .replace("__PAGE_JSONLD_DESCRIPTION__", 'Live Path of Exile 1 farming profit calculator for every pinnacle, Uber, and Tier 17 Nightmare map boss, using real poe.ninja prices.')
            .replace("__FAVICON_URL__", _favicon_data_uri("\U0001F480")))
    header = (SHARED_HEADER_HTML.replace("__EXTRA_CONTROLS__", BOSSES_EXTRA_CONTROLS)
              .replace("__BRAND_ICON__", "&#128128;").replace("__BRAND_TITLE__", "Boss Farm Estimator")
              .replace("__PRICECHIPS_ATTR__", "").replace("__DIVINE_CHIP_ATTR__", ""))
    return (head + "\n" + SHARED_CSS + '\n</head>\n<body>' + "\n"
            + render_sitemenu("bosses") + header + BOSSES_BODY + SHARED_FOOTER_HTML
            + '\n\n<div class="popover" id="popover" role="tooltip"></div>\n\n'
            + "<script>\nconst PAGE_REQUIRES_ADMIN = false;\n" + SHARED_JS_CHROME + BOSSES_JS
            + '\n</script>\n</body>\n</html>')


# --------------------------------------------------------------------------- #
# Trade Sniper page (/snipe)
# --------------------------------------------------------------------------- #
# Watches a curated list of Unique items (top 50 poe.ninja listing-count +
# top 50 poe.ninja price, deduped — see worker.js's fetchTopUniqueWatchlist)
# for trade-site listings priced below the poe.ninja floor, via a Cloudflare
# Durable Object (worker/worker.js's SnipeSession) that rotates through the
# list roughly once every 10 minutes (two search+fetch pairs every ~6s — a
# general pass plus an unidentified-only pass, since an unidentified item's
# price is a more solid reference before its random rolls are revealed — see
# CLAUDE.md for the rate-limit reasoning behind rotation-polling instead of a
# live-search WebSocket). No POESESSID needed — plain trade search+fetch work
# fully unauthenticated. Unlike the Boss Farm page, this still needs a real
# backend (the Durable Object's alarm-driven rotation loop), so locally
# (`python boss.py`) this page renders but Start/Stop/Poll calls will fail —
# there's no local equivalent of the Durable Object. It only works fully once
# deployed behind the Worker (poe-farm-helper.com or the *.workers.dev URL).
SNIPE_EXTRA_CONTROLS = ""

SNIPE_BODY = r"""

<div class="wrap">
  <div class="note" data-i18n="snipe_intro">
    Watches a curated list of Path of Exile Unique items — the top 50 by poe.ninja listing count
    ("most sold" proxy) plus the top 50 by poe.ninja price ("most expensive"), deduped — for
    trade-site listings priced below the current market floor. Rotates through the whole list
    roughly once every 5 minutes, one item at a time, to stay well within Path of Exile's trade
    API rate limits. POESESSID is optional — see below.
  </div>
  <div class="note" data-i18n="snipe_scope_note">
    Uniques only — Currency, Fragments, Scarabs, and other stackable currency-type goods are
    intentionally left out (buy those from Faustus in-game instead, at a fixed Artifacts price).
    Also: every listing found here requires whispering the seller in-game to complete the trade —
    Path of Exile's Instant Buyout / Asynchronous Trade system has no public API, so this can't
    show or filter to instant-buyout-only listings.
  </div>

  <div class="snipe-form">
    <div class="snipe-row">
      <label><span data-i18n="snipe_threshold_label">Underpriced by at least</span>
        <input type="number" id="threshold" value="20" min="1" max="90" style="width:70px">
      </label>
      <label><span data-i18n="snipe_minprice_label">Min price</span>
        <input type="number" id="minPrice" min="0" style="width:90px" placeholder="0">
      </label>
      <label><span data-i18n="snipe_maxprice_label">Max price</span>
        <input type="number" id="maxPrice" min="0" style="width:90px" placeholder="&#8734;">
      </label>
      <label><span data-i18n="snipe_priceunit_label">Unit</span>
        <select id="priceUnit" style="width:90px">
          <option value="chaos" data-i18n="snipe_unit_chaos">Chaos</option>
          <option value="divine" data-i18n="snipe_unit_divine">Divine</option>
        </select>
      </label>
    </div>
    <div class="snipe-row">
      <label style="flex:2; min-width:260px"><span data-i18n="snipe_poesessid_label">POESESSID (optional)</span>
        <input type="password" id="poesessid" autocomplete="off" placeholder="&#8226;&#8226;&#8226;&#8226;&#8226;&#8226;&#8226;&#8226;">
      </label>
    </div>
    <div class="note snipe-poesessid-warn" data-i18n="snipe_poesessid_warn">
      Optional. This is your Path of Exile account's session cookie — treat it like a password.
      It's sent only to this Worker when you click Start, held in memory only for the life of this
      watch, never logged or written to storage, and cleared the moment you stop watching or the
      session times out. Leave it blank to use Trade Sniper fully anonymously (the default).
    </div>
    <div class="snipe-row">
      <div class="snipe-actions">
        <button class="sync" id="snipeStart" data-i18n="snipe_btn_start">Start watching</button>
        <button class="stop" id="snipeStop" data-i18n="snipe_btn_stop" disabled>Stop</button>
      </div>
      <span class="snipe-status" id="snipeStatus" data-i18n="snipe_status_idle">idle</span>
    </div>
    <div class="snipe-row" id="snipeProgressRow" hidden>
      <span class="snipe-status" id="snipeProgress"></span>
    </div>
  </div>

  <div class="snipe-current" id="snipeCurrent" hidden>
    <span class="dot"></span>
    <span class="lbl" data-i18n="snipe_checking_now">checking now</span>
    <img id="snipeCurrentIcon" src="" alt="" hidden>
    <span class="nm" id="snipeCurrentName"></span>
  </div>

  <div class="snipe-results">
    <div class="slabel"><span data-i18n="snipe_results_label">Underpriced listings found</span></div>
    <div class="snipe-list" id="snipeList"></div>
    <div class="empty" id="snipeEmpty" data-i18n="snipe_results_empty">Nothing yet — start watching above.</div>
  </div>

  <div class="snipe-results">
    <div class="slabel slabel-row">
      <span data-i18n="snipe_log_label">Live check log — reference price vs. cheapest live listing</span>
      <button type="button" class="log-toggle" id="snipeLogToggle">Hide</button>
    </div>
    <div class="snipe-log" id="snipeLog"></div>
    <div class="empty" id="snipeLogEmpty" data-i18n="snipe_log_empty">No items checked yet — start watching above.</div>
  </div>
</div>
"""

SNIPE_JS = r"""const LEAGUE = __LEAGUE_JSON__;
const POLL_MS = 3000;

function populateLeagueOptions(){
  const sel = document.getElementById('leaguesel');
  if(sel.options.length) return;
  let currentLeague;
  try { currentLeague = localStorage.getItem('bossFarmLeague'); } catch(e) { currentLeague = null; }
  const cur = currentLeague || LEAGUE;
  const opts = [LEAGUE, 'Standard', 'Hardcore', 'Hardcore ' + LEAGUE];
  const seen = new Set();
  sel.innerHTML = opts.filter(o => o && !seen.has(o) && seen.add(o))
    .map(o => `<option value="${o}" ${o===cur?'selected':''}>${o}</option>`).join('');
  document.getElementById('league').textContent = cur;
}
populateLeagueOptions();
document.getElementById('leaguesel').addEventListener('change', e => {
  try { localStorage.setItem('bossFarmLeague', e.target.value); } catch(err) {}
  document.getElementById('league').textContent = e.target.value;
});

const I18N = {
en: {
  tagline: 'PATH OF EXILE · TRADE SNIPER',
  menu_title: 'Menu', menu_home: 'Home', menu_bosses: 'Boss Farm', menu_snipe: 'Trade Sniper', menu_poe2_campaign: 'PoE2 Campaign', menu_flip_advisor: 'Flip Advisor', menu_campaign: 'Campaign Guide',
  chip_league: 'League', chip_price: 'Price', chip_sync: 'Sync', chip_next: 'next',
  footer_disclaimer: 'Unofficial fan tool — not affiliated with or endorsed by Grinding Gear Games. Uses Path of Exile’s official, unauthenticated trade API to check a curated list of items on a rotation — nothing is stored server-side beyond an active session.',
  footer_made_by: 'Built by Erick Lúcio',
  footer_dm: 'Send me an email there for comment or feedback',
  snipe_intro: 'Watches a curated list of Path of Exile Unique items — the top 50 by poe.ninja listing count ("most sold" proxy) plus the top 50 by poe.ninja price ("most expensive"), deduped — for trade-site listings priced below the current market floor. Checks both the general market and unidentified-only listings (a more solid price reference, since their random rolls aren\'t revealed yet). Rotates through the whole list roughly once every 10 minutes, one item at a time, to stay well within Path of Exile’s trade API rate limits. POESESSID is optional — see below.',
  snipe_scope_note: 'Uniques only — Currency, Fragments, Scarabs, and other stackable currency-type goods are intentionally left out (buy those from Faustus in-game instead, at a fixed Artifacts price). Also: every listing found here requires whispering the seller in-game to complete the trade — Path of Exile’s Instant Buyout / Asynchronous Trade system has no public API, so this can’t show or filter to instant-buyout-only listings.',
  snipe_threshold_label: 'Underpriced by at least',
  snipe_btn_start: 'Start watching',
  snipe_btn_stop: 'Stop',
  snipe_status_idle: 'idle',
  snipe_status_starting: 'starting…',
  snipe_status_running: 'watching…',
  snipe_status_stopped: 'stopped',
  snipe_progress: 'checked {i} / {n} — full lap ≈10 min',
  snipe_status_ratelimited: 'rate limited by pathofexile.com/trade — paused, resuming in {mins}m {secs}s',
  snipe_results_label: 'Underpriced listings found',
  snipe_results_empty: 'Nothing yet — start watching above.',
  snipe_err_generic: 'failed to start: ',
  snipe_checking_now: 'checking now',
  snipe_log_label: 'Live check log — reference price vs. cheapest live listing',
  snipe_log_empty: 'No items checked yet — start watching above.',
  snipe_log_none_found: 'no listings found',
  snipe_view_trade: 'view on trade ↗',
  snipe_minprice_label: 'Min price',
  snipe_maxprice_label: 'Max price',
  snipe_priceunit_label: 'Unit',
  snipe_unit_chaos: 'Chaos',
  snipe_unit_divine: 'Divine',
  snipe_poesessid_label: 'POESESSID (optional)',
  snipe_poesessid_warn: 'Optional. This is your Path of Exile account’s session cookie — treat it like a password. It’s sent only to this Worker when you click Start, held in memory only for the life of this watch, never logged or written to storage, and cleared the moment you stop watching or the session times out. Leave it blank to use Trade Sniper fully anonymously (the default).',
  snipe_variant_unsure: '⚠ variant unconfirmed',
  snipe_unidentified_tag: 'unidentified',
  snipe_log_variants: '{n} price tiers',
  snipe_log_hide: 'Hide',
  snipe_log_show: 'Show',
  snipe_price_ninja: 'poe.ninja',
  snipe_price_watch: 'poe.watch (unid.)',
  snipe_price_trade: 'trade',
  snipe_id_identified: 'identified',
  snipe_id_unidentified: 'unidentified',
  snipe_id_unknown: 'unknown',
  snipe_price_notfound: 'not found',
},
pt: {
  tagline: 'PATH OF EXILE · CAÇADOR DE OFERTAS',
  menu_title: 'Menu', menu_home: 'Início', menu_bosses: 'Farm de Chefes', menu_snipe: 'Caçador de Ofertas', menu_poe2_campaign: 'Campanha PoE2', menu_flip_advisor: 'Conselheiro de Flip', menu_campaign: 'Guia de Campanha',
  chip_league: 'Liga', chip_price: 'Preço', chip_sync: 'Sincronia', chip_next: 'próximo',
  footer_disclaimer: 'Ferramenta não-oficial feita por fã — sem afiliação com a Grinding Gear Games. Usa a API oficial e não-autenticada de troca do Path of Exile para checar uma lista selecionada de itens em rodízio — nada é guardado no servidor além de uma sessão ativa.',
  footer_made_by: 'Feito por Erick Lúcio',
  footer_dm: 'Manda um e-mail lá para comentário ou feedback',
  snipe_intro: 'Monitora uma lista selecionada de itens Únicos do Path of Exile — os 50 mais listados no poe.ninja (indicador de "mais vendidos") mais os 50 de maior preço no poe.ninja ("mais caros"), sem repetição — em busca de anúncios com preço abaixo do valor de mercado atual. Verifica tanto o mercado geral quanto anúncios não identificados (uma referência de preço mais sólida, já que as propriedades aleatórias ainda não foram reveladas). Passa pela lista inteira a cada ~10 minutos, um item de cada vez, para ficar bem dentro dos limites de requisição da API de troca do Path of Exile. POESESSID é opcional — veja abaixo.',
  snipe_scope_note: 'Apenas itens Únicos — Moedas, Fragmentos, Scarabs e outros itens empilháveis do tipo moeda ficam de fora de propósito (compre esses do Faustus dentro do jogo, por um preço fixo em Artefatos). Além disso: todo anúncio encontrado aqui exige sussurrar para o vendedor dentro do jogo para completar a troca — o sistema de Compra Instantânea / Troca Assíncrona do Path of Exile não tem API pública, então isso não consegue mostrar ou filtrar só os anúncios de compra instantânea.',
  snipe_threshold_label: 'Abaixo do preço em pelo menos',
  snipe_btn_start: 'Começar a monitorar',
  snipe_btn_stop: 'Parar',
  snipe_status_idle: 'parado',
  snipe_status_starting: 'iniciando…',
  snipe_status_running: 'monitorando…',
  snipe_status_stopped: 'parado',
  snipe_progress: 'checado {i} / {n} — volta completa ≈10 min',
  snipe_status_ratelimited: 'limite de taxa do pathofexile.com/trade — pausado, retomando em {mins}m {secs}s',
  snipe_results_label: 'Ofertas abaixo do preço encontradas',
  snipe_results_empty: 'Nada ainda — comece a monitorar acima.',
  snipe_err_generic: 'falha ao iniciar: ',
  snipe_checking_now: 'checando agora',
  snipe_log_label: 'Log de checagens ao vivo — preço de referência vs. menor anúncio ao vivo',
  snipe_log_empty: 'Nenhum item checado ainda — comece a monitorar acima.',
  snipe_log_none_found: 'nenhum anúncio encontrado',
  snipe_view_trade: 'ver na trade ↗',
  snipe_minprice_label: 'Preço mín.',
  snipe_maxprice_label: 'Preço máx.',
  snipe_priceunit_label: 'Unidade',
  snipe_unit_chaos: 'Caos',
  snipe_unit_divine: 'Divino',
  snipe_poesessid_label: 'POESESSID (opcional)',
  snipe_poesessid_warn: 'Opcional. Este é o cookie de sessão da sua conta do Path of Exile — trate como uma senha. Ele é enviado só para este Worker quando você clica em Começar, fica em memória apenas durante essa monitoração, nunca é registrado em log nem gravado em armazenamento, e é apagado assim que você parar ou a sessão expirar. Deixe em branco para usar o Caçador de Ofertas de forma totalmente anônima (o padrão).',
  snipe_variant_unsure: '⚠ variante não confirmada',
  snipe_unidentified_tag: 'não identificado',
  snipe_log_variants: '{n} faixas de preço',
  snipe_log_hide: 'Ocultar',
  snipe_log_show: 'Mostrar',
  snipe_price_ninja: 'poe.ninja',
  snipe_price_watch: 'poe.watch (não id.)',
  snipe_price_trade: 'trade',
  snipe_id_identified: 'identificado',
  snipe_id_unidentified: 'não identificado',
  snipe_id_unknown: 'desconhecido',
  snipe_price_notfound: 'não encontrado',
},
};

let snipeSession = null;
let pollTimer = null;
let rateLimitWarned = false;

// Tracked so a language switch can re-translate whatever's currently shown
// (see refreshDynamicI18n) — these three are set purely from JS state, not
// from data-i18n static markup, so applyStaticI18n() alone can't reach them.
let lastStatusKey = 'snipe_status_idle', lastStatusCls = null;
let lastProgressData = null;
let lastRateLimitedUntil = null;

function setStatus(key, cls){
  lastStatusKey = key; lastStatusCls = cls;
  const el = document.getElementById('snipeStatus');
  el.textContent = t(key);
  el.className = 'snipe-status' + (cls ? ' ' + cls : '');
}

function setProgress(progress){
  lastProgressData = progress; lastRateLimitedUntil = null;
  const row = document.getElementById('snipeProgressRow');
  if(!progress){ row.hidden = true; return; }
  row.hidden = false;
  document.getElementById('snipeProgress').textContent =
    t('snipe_progress').replace('{i}', progress.index + 1).replace('{n}', progress.total);
}

function setRateLimitedProgress(rateLimitedUntil){
  lastRateLimitedUntil = rateLimitedUntil;
  const remainMs = Math.max(0, rateLimitedUntil - Date.now());
  const mins = Math.floor(remainMs / 60000);
  const secs = Math.floor((remainMs % 60000) / 1000);
  document.getElementById('snipeProgressRow').hidden = false;
  document.getElementById('snipeProgress').textContent =
    t('snipe_status_ratelimited').replace('{mins}', mins).replace('{secs}', secs);
  setStatus('snipe_status_running', 'warn');
}

// Re-applies whatever dynamic (non data-i18n) text is currently on screen in
// the new language — status line, progress/rate-limit line, log-toggle
// label. Already-rendered log cards/hit rows keep their old-language text
// (this is a live incremental log, not a snapshot re-rendered from stored
// data, same limitation the Boss Farm page doesn't have since it re-renders
// from `lastData` instead) — only what's still driven by live state redraws.
function refreshDynamicI18n(){
  if(lastRateLimitedUntil) setRateLimitedProgress(lastRateLimitedUntil);
  else { setStatus(lastStatusKey, lastStatusCls); setProgress(lastProgressData); }
  applyLogVisibility();
}

function renderHit(hit){
  const div = document.createElement('div');
  div.className = 'snipe-hit';
  const img = hit.icon ? `<img src="${escAttr(hit.icon)}" alt="">` : '';
  const priceTxt = `${hit.amount} ${escAttr(hit.currency)}`;
  const variantTxt = hit.variantLabel
    ? ` <span class="variant">(${escAttr(hit.variantLabel)})</span>`
    : (hit.variantUncertain ? ` <span class="variant unsure">${t('snipe_variant_unsure')}</span>` : '');
  const unidTxt = hit.unidentified ? ` <span class="variant">${t('snipe_unidentified_tag')}</span>` : '';
  div.innerHTML = `${img}<span class="nm">${escAttr(hit.itemName)}${variantTxt}${unidTxt}</span>
    <span class="px">${priceTxt}</span>
    <span class="ref">ref ~${Math.round(hit.referenceChaos)}c</span>
    <a class="tradelink" href="${escAttr(hit.tradeUrl)}" target="_blank" rel="noopener noreferrer">${t('snipe_view_trade')}</a>`;
  return div;
}

function setCurrent(item){
  const box = document.getElementById('snipeCurrent');
  if(!item){ box.hidden = true; return; }
  box.hidden = false;
  const img = document.getElementById('snipeCurrentIcon');
  if(item.icon){ img.src = item.icon; img.hidden = false; } else { img.hidden = true; img.src = ''; }
  document.getElementById('snipeCurrentName').textContent = item.name;
}

function renderLogRow(chk){
  const div = document.createElement('div');
  div.className = 'snipe-log-card' + (chk.underpriced ? ' hit' : '');
  const img = chk.icon ? `<img src="${escAttr(chk.icon)}" alt="">` : '';
  const variantsTxt = chk.variantCount
    ? ` <span class="variants">(${t('snipe_log_variants').replace('{n}', chk.variantCount)})</span>` : '';

  const idLabel = chk.cheapestChaosEquiv == null ? null
    : chk.cheapestIdentified === true ? t('snipe_id_identified')
    : chk.cheapestIdentified === false ? t('snipe_id_unidentified')
    : t('snipe_id_unknown');
  const idBadge = idLabel
    ? `<span class="idbadge${chk.cheapestIdentified === false ? ' unid' : ''}">${escAttr(idLabel)}</span>` : '';

  const ninjaVal = `~${Math.round(chk.referenceChaos)}c`;
  const watchVal = chk.referenceWatchChaos != null
    ? `~${Math.round(chk.referenceWatchChaos)}c`
    : `<span class="none">${t('snipe_price_notfound')}</span>`;
  const tradeVal = chk.cheapestChaosEquiv != null
    ? `<span class="found">${chk.cheapestAmount} ${escAttr(chk.cheapestCurrency)} (~${chk.cheapestChaosEquiv}c)</span>`
    : `<span class="none">${t('snipe_log_none_found')}</span>`;

  const dbg = (chk.cheapestChaosEquiv == null && chk.debug)
    ? `<div class="dbg">${escAttr(chk.debug)}</div>` : '';
  div.innerHTML = `<div class="card-head">${img}<span class="nm">${escAttr(chk.name)}${variantsTxt}</span>${idBadge}</div>
    <div class="price-grid">
      <span class="lbl">${t('snipe_price_ninja')}</span><span class="val">${ninjaVal}</span>
      <span class="lbl">${t('snipe_price_watch')}</span><span class="val">${watchVal}</span>
      <span class="lbl">${t('snipe_price_trade')}</span><span class="val">${tradeVal}</span>
    </div>${dbg}`;
  return div;
}

// Every entry here is one real trade-API round-trip: `referenceChaos` is the
// poe.ninja market-floor price this watchlist item was queued with (the
// floor across all its price tiers when `variantCount` is set — e.g.
// Mageblood's cheapest flask-count tier), `cheapestChaosEquiv` is the actual
// cheapest currently-listed price found live on pathofexile.com/trade just
// now — logged so it's visible exactly which item was checked and how its
// live price compares to the reference. Individual underpriced HITS (below)
// use a more accurate per-tier reference when the specific listing's mods
// could be matched to one of poe.ninja's known tiers — this log-level
// reference stays floor-based since it's describing the whole check, not
// one listing. `debug` (set server-side whenever no price was found)
// explains *why* — a trade-API HTTP error, an empty search result, or an
// unparseable listing — instead of leaving an unexplained zero.
function logCheck(chk){
  console.log(
    `[Trade Sniper] checked "${chk.name}"${chk.variantCount ? ` (${chk.variantCount} price tiers)` : ''} — reference (poe.ninja floor): ~${Math.round(chk.referenceChaos)}c` +
    (chk.cheapestChaosEquiv != null
      ? ` | cheapest live listing: ${chk.cheapestAmount} ${chk.cheapestCurrency} (~${chk.cheapestChaosEquiv}c, ${chk.listingsSeen} listing(s) seen)` + (chk.underpriced ? ' — UNDERPRICED HIT (see hit list for per-tier reference price)' : '')
      : ` | no price found${chk.debug ? ' — ' + chk.debug : ''}`),
    chk
  );
  const list = document.getElementById('snipeLog');
  document.getElementById('snipeLogEmpty').hidden = true;
  list.prepend(renderLogRow(chk));
  while(list.children.length > 40) list.removeChild(list.lastChild);
}

async function pollLoop(){
  if(!snipeSession) return;
  const session = snipeSession;
  try{
    const r = await fetch('/snipe/poll?session=' + encodeURIComponent(session), {signal: AbortSignal.timeout(10000)});
    const data = await r.json();
    // The user may have clicked Stop (or Start again) while this request was
    // in flight — snipeSession would then no longer match what we sent this
    // poll for. Bail out before touching any DOM state: this response
    // reflects a moment before the stop took effect (e.g. still `running:
    // true` with a stale `checking` item), and applying it now would
    // re-show "checking now" right after stopSniper() already hid it, with
    // nothing left to clean it up afterward (this was the exact bug behind
    // the indicator getting stuck visible/green after Stop).
    if(session !== snipeSession) return;
    if(!data.ok){
      console.error('[Trade Sniper] /snipe/poll returned ok:false — stopping.', {httpStatus: r.status, data});
      stopSniper(); setStatus('snipe_status_stopped', 'err'); return;
    }
    if(data.checks && data.checks.length) for(const chk of data.checks) logCheck(chk);
    setCurrent(data.checking);
    if(data.listings && data.listings.length){
      const list = document.getElementById('snipeList');
      document.getElementById('snipeEmpty').hidden = true;
      for(const hit of data.listings){
        console.log(`[Trade Sniper] UNDERPRICED HIT: "${hit.itemName}" listed for ${hit.amount} ${hit.currency} (~${hit.chaosEquiv}c) vs. reference ~${Math.round(hit.referenceChaos)}c`, hit);
        list.prepend(renderHit(hit));
      }
    }
    if(data.rateLimitedUntil){
      setRateLimitedProgress(data.rateLimitedUntil);
      if(!rateLimitWarned){
        const remainMs = Math.max(0, data.rateLimitedUntil - Date.now());
        console.warn(`[Trade Sniper] pathofexile.com/trade rate-limited this session — pausing ALL requests until ${new Date(data.rateLimitedUntil).toLocaleTimeString()} (~${Math.floor(remainMs/60000)}m ${Math.floor((remainMs%60000)/1000)}s). The rotation resumes automatically once the cooldown clears.`);
        rateLimitWarned = true;
      }
    }else{
      rateLimitWarned = false;
      setProgress(data.progress);
    }
    if(!data.running){
      console.warn('[Trade Sniper] /snipe/poll reports running:false — session ended server-side (stopped, or the 10-min idle timeout hit). Stopping locally.', data);
      stopSniper(); setStatus('snipe_status_stopped'); return;
    }
  }catch(e){
    // Real diagnostic instead of silently swallowing this — a fetch that
    // throws here (network error, timeout, non-JSON response) previously
    // left no trace anywhere, which was exactly why "why did it just stop
    // working" was impossible to diagnose from the console.
    console.error('[Trade Sniper] /snipe/poll request failed — will retry next tick.', e);
  }
  if(session === snipeSession) pollTimer = setTimeout(pollLoop, POLL_MS);
}

function stopSniper(){
  if(pollTimer) clearTimeout(pollTimer);
  pollTimer = null;
  document.getElementById('snipeStart').disabled = false;
  document.getElementById('snipeStop').disabled = true;
  setProgress(null);
  setCurrent(null);
  rateLimitWarned = false;
}

document.getElementById('snipeStart').addEventListener('click', async () => {
  const thresholdPct = Number(document.getElementById('threshold').value) || 20;
  const league = document.getElementById('leaguesel').value || LEAGUE;
  const minRaw = document.getElementById('minPrice').value;
  const maxRaw = document.getElementById('maxPrice').value;
  const minPrice = minRaw !== '' ? Number(minRaw) : null;
  const maxPrice = maxRaw !== '' ? Number(maxRaw) : null;
  const priceUnit = document.getElementById('priceUnit').value || 'chaos';
  const poesessidEl = document.getElementById('poesessid');
  const poesessid = poesessidEl.value.trim() || null;
  poesessidEl.value = ''; // never keep it sitting in the DOM longer than needed to send it once

  document.getElementById('snipeStart').disabled = true;
  setStatus('snipe_status_starting');
  try{
    const r = await fetch('/snipe/start', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({league, thresholdPct, minPrice, maxPrice, priceUnit, poesessid}),
      signal: AbortSignal.timeout(20000),
    });
    const data = await r.json();
    if(!data.ok){
      console.error('[Trade Sniper] /snipe/start failed.', {httpStatus: r.status, data});
      document.getElementById('snipeStatus').textContent = t('snipe_err_generic') + (data.error || ('HTTP '+r.status));
      document.getElementById('snipeStatus').className = 'snipe-status err';
      document.getElementById('snipeStart').disabled = false;
      return;
    }
    snipeSession = data.session;
    document.getElementById('snipeStop').disabled = false;
    setStatus('snipe_status_running', 'ok');
    document.getElementById('snipeList').innerHTML = '';
    document.getElementById('snipeEmpty').hidden = false;
    document.getElementById('snipeLog').innerHTML = '';
    document.getElementById('snipeLogEmpty').hidden = false;
    const rangeTxt = (minPrice != null || maxPrice != null)
      ? `, price range=${minPrice != null ? minPrice : '0'}-${maxPrice != null ? maxPrice : '∞'} ${priceUnit}` : '';
    console.log(`[Trade Sniper] session started — league="${league}", threshold=${thresholdPct}% below reference${rangeTxt}, watchlist size=${data.watchlistSize} unique items, POESESSID=${poesessid ? 'provided (not logged)' : 'none — anonymous'}.`);
    console.log('[Trade Sniper] every ~3s the rotation checks the next watchlist item live on pathofexile.com/trade (one search + one fetch call) and compares its cheapest currently-listed price against the poe.ninja reference price for that item — the per-tier price when the listing\'s mods could be matched to a known variant (e.g. Mageblood\'s flask count), otherwise the floor price across all tiers. A full lap through the watchlist takes ~5 minutes. Every check — hit or not — is logged here.');
    pollLoop();
  }catch(e){
    console.error('[Trade Sniper] /snipe/start request threw.', e);
    document.getElementById('snipeStatus').textContent = t('snipe_err_generic') + e;
    document.getElementById('snipeStatus').className = 'snipe-status err';
    document.getElementById('snipeStart').disabled = false;
  }
});

document.getElementById('snipeStop').addEventListener('click', async () => {
  if(!snipeSession) return;
  const session = snipeSession;
  snipeSession = null;
  stopSniper();
  setStatus('snipe_status_stopped');
  try{
    await fetch('/snipe/stop', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({session}), signal: AbortSignal.timeout(10000),
    });
  }catch(e) {}
});

let logHidden = (function(){
  try { return localStorage.getItem('bossFarmSnipeLogHidden') === '1'; } catch(e) { return false; }
})();
function applyLogVisibility(){
  document.getElementById('snipeLog').classList.toggle('collapsed', logHidden);
  document.getElementById('snipeLogToggle').textContent = t(logHidden ? 'snipe_log_show' : 'snipe_log_hide');
}
document.getElementById('snipeLogToggle').addEventListener('click', () => {
  logHidden = !logHidden;
  try { localStorage.setItem('bossFarmSnipeLogHidden', logHidden ? '1' : '0'); } catch(e) {}
  applyLogVisibility();
});

// SHARED_HEADER_HTML's #langsel markup is shared across pages, but (like
// #leaguesel) each page has to wire its own change listener since each needs
// to refresh its own dynamic content afterward — BOSSES_JS calls
// render(lastData), this page calls refreshDynamicI18n() (see its own
// comment for what that can and can't reach). Without this wiring at all,
// the dropdown just changes its own selected option and nothing else
// updates — which was exactly the reported bug on this page.
const langSel = document.getElementById('langsel');
langSel.value = lang;
langSel.addEventListener('change', () => {
  lang = langSel.value;
  try { localStorage.setItem('bossFarmLang', lang); } catch(e) {}
  applyStaticI18n();
  refreshDynamicI18n();
});

applyStaticI18n();
applyLogVisibility();
setStatus('snipe_status_idle');
"""


def render_snipe_page():
    head = (SHARED_HEAD_TEMPLATE
            .replace("__PAGE_TITLE__", 'Trade Sniper — Path of Exile Trade API Underpriced Unique Watcher')
            .replace("__PAGE_DESCRIPTION__", 'Watches a curated list of top Path of Exile Unique items (most-listed + most-expensive on poe.ninja) for trade-site listings priced below the current market floor. No account credential needed.')
            .replace("__PAGE_SOCIAL_TITLE__", 'Trade Sniper — PoE Underpriced Unique Watcher')
            .replace("__PAGE_SOCIAL_DESCRIPTION__", 'Path of Exile trade-site watcher for underpriced Unique-item listings across a curated top-100 list, via the official unauthenticated trade API.')
            .replace("__PAGE_APP_NAME__", 'Trade Sniper')
            .replace("__PAGE_JSONLD_DESCRIPTION__", 'Watches a curated list of top Path of Exile Unique items for underpriced trade-site listings via the official trade API.')
            .replace("__FAVICON_URL__", _favicon_data_uri("\U0001F3AF")))
    header = (SHARED_HEADER_HTML.replace("__EXTRA_CONTROLS__", SNIPE_EXTRA_CONTROLS)
              .replace("__BRAND_ICON__", "&#127919;").replace("__BRAND_TITLE__", "Trade Sniper")
              .replace("__PRICECHIPS_ATTR__", "hidden").replace("__DIVINE_CHIP_ATTR__", "hidden"))
    return (head + "\n" + SHARED_CSS + '\n</head>\n<body>' + "\n"
            + render_sitemenu("snipe") + header + SNIPE_BODY + SHARED_FOOTER_HTML
            + '\n\n<div class="popover" id="popover" role="tooltip"></div>\n\n'
            + "<script>\nconst PAGE_REQUIRES_ADMIN = true;\n" + SHARED_JS_CHROME + SNIPE_JS
            + '\n</script>\n</body>\n</html>')


# --------------------------------------------------------------------------- #
# Home page (/home) — the site's landing page, PoE1/PoE2 split
# --------------------------------------------------------------------------- #
# The big PoE1/PoE2 toggle both reorders the site menu (via reorderSiteMenuByGame(),
# defined in SHARED_JS_CHROME) AND filters which game panel is shown below (only the
# selected game's panel — the toggle buttons themselves stay visible for both, so you
# can switch back). The toggle icons are the real official favicons GGG serves from
# web.poecdn.com (confirmed live: stable since 2024, CORS-open, safe to hotlink — same
# pattern this repo already uses for poe.ninja item icons, no local assets hosted).
POE1_ICON_URL = "https://web.poecdn.com/protected/image/favicon/favicon.png?key=Iu4RwgXxfRpzGkEV729D7Q"
POE2_ICON_URL = "https://web.poecdn.com/protected/image/favicon/poe2/favicon.png?key=rPkjGpdcBJcvx5P3el7BFg"

HOME_EXTRA_CONTROLS = ""

HOME_BODY = r"""

<div class="wrap">
  <div class="game-toggle" id="gameToggle" role="group" aria-label="game selector">
    <button type="button" class="game-toggle-btn" data-game="poe1" id="gameBtnPoe1">
      <img class="gt-icon" src="__POE1_ICON_URL__" alt="Path of Exile 1">
      <span data-i18n="home_toggle_poe1">Path of Exile 1</span>
    </button>
    <button type="button" class="game-toggle-btn" data-game="poe2" id="gameBtnPoe2">
      <img class="gt-icon" src="__POE2_ICON_URL__" alt="Path of Exile 2">
      <span data-i18n="home_toggle_poe2">Path of Exile 2</span>
    </button>
  </div>
  <div class="note" data-i18n="home_toggle_note">
    Pick your game — this only reorders the menu on the left. Both games' info stays visible below either way.
  </div>

  <div class="game-panels">
    <section class="game-panel" id="panelPoe1">
      <h2><img class="panel-icon" src="__POE1_ICON_URL__" alt=""> <span data-i18n="home_panel_poe1_title">Path of Exile 1</span></h2>
      <div class="note" data-i18n="home_poe1_blurb">
        Path of Exile 1 is deep into its endgame content cycle — pinnacle bosses, Uber fights, and
        Tier 17 Nightmare maps remain the most reliable currency sources late-league. Use the Boss
        Farm Estimator to compare live profit-per-run across every encounter, based on real
        poe.ninja prices.
      </div>
      <div class="page-cards">
        <a class="page-card" href="/bosses">
          <div class="page-card-head"><span class="pc-icon">&#128128;</span> <span data-i18n="home_card_bosses_title">Boss Farm Estimator</span></div>
          <div class="page-card-desc" data-i18n="home_card_bosses_desc">
            Compare live profit-per-run across every pinnacle, Uber, and Tier 17 Nightmare map
            boss encounter, using real poe.ninja prices — worst/average/best instead of one
            misleading number.
          </div>
        </a>
        <a class="page-card" href="/snipe" hidden data-admin-only>
          <div class="page-card-head"><span class="pc-icon">&#127919;</span> <span data-i18n="home_card_snipe_title">Trade Sniper</span></div>
          <div class="page-card-desc" data-i18n="home_card_snipe_desc">
            Watches a curated list of top Unique items for trade-site listings priced below the
            current market floor, checked live against the official trade API.
          </div>
          <span class="page-card-badge" data-i18n="home_card_admin_badge">Admin only</span>
        </a>
        <a class="page-card" href="/flip-advisor">
          <div class="page-card-head"><span class="pc-icon">&#128177;</span> <span data-i18n="home_card_flipadvisor_title">Flip Advisor</span></div>
          <div class="page-card-desc" data-i18n="home_card_flipadvisor_desc">
            Ranks Currency Exchange pairs by historical hourly spread%, plus multi-step buy/sell
            strategies with a whole-unit trade guide — a volatility signal to check live, not a
            guaranteed profit.
          </div>
        </a>
        <a class="page-card" href="/campaign">
          <div class="page-card-head"><span class="pc-icon">&#128506;</span> <span data-i18n="home_card_campaign_title">Campaign Guide</span></div>
          <div class="page-card-desc" data-i18n="home_card_campaign_desc">
            Fastest campaign route act by act, league-start and second-character item lists, and
            every quest that grants a bonus passive skill point — with a route map per act.
          </div>
        </a>
      </div>
      <div class="patchnotes" id="patchNotesPoe1">
        <div class="slabel" data-i18n="home_patchnotes_label">Recent patch notes</div>
        <div class="patchnotes-list" id="patchListPoe1"></div>
        <div class="empty" id="patchEmptyPoe1" data-i18n="home_patchnotes_loading">Loading&hellip;</div>
      </div>
    </section>

    <section class="game-panel" id="panelPoe2">
      <h2><img class="panel-icon" src="__POE2_ICON_URL__" alt=""> <span data-i18n="home_panel_poe2_title">Path of Exile 2</span></h2>
      <div class="note" data-i18n="home_poe2_blurb">
        Path of Exile 2 is in Early Access, with new content and balance changes shipping
        frequently. Farming tools for PoE2 are still on the roadmap here — check the patch notes
        below to stay current in the meantime.
      </div>
      <div class="page-cards">
        <a class="page-card" href="/poe2-campaign">
          <div class="page-card-head"><span class="pc-icon">&#9876;&#65039;</span> <span data-i18n="home_card_poe2campaign_title">PoE2 Campaign</span></div>
          <div class="page-card-desc" data-i18n="home_card_poe2campaign_desc">
            Fastest campaign route act by act, league-start and second-character item lists, and
            every permanent bonus available in Early Access so far.
          </div>
        </a>
      </div>
      <div class="patchnotes" id="patchNotesPoe2">
        <div class="slabel" data-i18n="home_patchnotes_label">Recent patch notes</div>
        <div class="patchnotes-list" id="patchListPoe2"></div>
        <div class="empty" id="patchEmptyPoe2" data-i18n="home_patchnotes_loading">Loading&hellip;</div>
      </div>
    </section>
  </div>
</div>
"""

HOME_JS = r"""const LEAGUE = __LEAGUE_JSON__;
const PATCH_NOTES_BASE = '/api/patchnotes';

function populateLeagueOptions(){
  const sel = document.getElementById('leaguesel');
  if(sel.options.length) return;
  let currentLeague;
  try { currentLeague = localStorage.getItem('bossFarmLeague'); } catch(e) { currentLeague = null; }
  const cur = currentLeague || LEAGUE;
  const opts = [LEAGUE, 'Standard', 'Hardcore', 'Hardcore ' + LEAGUE];
  const seen = new Set();
  sel.innerHTML = opts.filter(o => o && !seen.has(o) && seen.add(o))
    .map(o => `<option value="${o}" ${o===cur?'selected':''}>${o}</option>`).join('');
  document.getElementById('league').textContent = cur;
}
populateLeagueOptions();
document.getElementById('leaguesel').addEventListener('change', e => {
  try { localStorage.setItem('bossFarmLeague', e.target.value); } catch(err) {}
  document.getElementById('league').textContent = e.target.value;
});

const I18N = {
en: {
  tagline: 'PATH OF EXILE · HUB',
  menu_title: 'Menu', menu_home: 'Home', menu_bosses: 'Boss Farm', menu_snipe: 'Trade Sniper', menu_poe2_campaign: 'PoE2 Campaign', menu_flip_advisor: 'Flip Advisor', menu_campaign: 'Campaign Guide',
  chip_league: 'League', chip_price: 'Price', chip_sync: 'Sync', chip_next: 'next',
  btn_refresh: 'Refresh', btn_syncing: 'syncing…',
  footer_disclaimer: 'Unofficial fan tool — not affiliated with or endorsed by Grinding Gear Games.',
  footer_made_by: 'Built by Erick Lúcio',
  footer_dm: 'Send me an email there for comment or feedback',
  home_toggle_poe1: 'Path of Exile 1',
  home_toggle_poe2: 'Path of Exile 2',
  home_toggle_note: 'Pick your game — this only reorders the menu on the left. Both games\' info stays visible below either way.',
  home_panel_poe1_title: 'Path of Exile 1',
  home_panel_poe2_title: 'Path of Exile 2',
  home_poe1_blurb: 'Path of Exile 1 is deep into its endgame content cycle — pinnacle bosses, Uber fights, and Tier 17 Nightmare maps remain the most reliable currency sources late-league. Use the Boss Farm Estimator to compare live profit-per-run across every encounter, based on real poe.ninja prices.',
  home_poe2_blurb: 'Path of Exile 2 is in Early Access, with new content and balance changes shipping frequently. Farming tools for PoE2 are still on the roadmap here — check the patch notes below to stay current in the meantime.',
  home_go_bosses: 'Open Boss Farm Estimator →',
  home_card_bosses_title: 'Boss Farm Estimator',
  home_card_bosses_desc: 'Compare live profit-per-run across every pinnacle, Uber, and Tier 17 Nightmare map boss encounter, using real poe.ninja prices — worst/average/best instead of one misleading number.',
  home_card_snipe_title: 'Trade Sniper',
  home_card_snipe_desc: 'Watches a curated list of top Unique items for trade-site listings priced below the current market floor, checked live against the official trade API.',
  home_card_poe2campaign_title: 'PoE2 Campaign',
  home_card_poe2campaign_desc: 'Fastest campaign route act by act, league-start and second-character item lists, and every permanent bonus available in Early Access so far.',
  home_card_flipadvisor_title: 'Flip Advisor',
  home_card_flipadvisor_desc: 'Ranks Currency Exchange pairs by historical hourly spread%, plus multi-step buy/sell strategies with a whole-unit trade guide — a volatility signal to check live, not a guaranteed profit.',
  home_card_campaign_title: 'Campaign Guide',
  home_card_campaign_desc: 'Fastest campaign route act by act, league-start and second-character item lists, and every quest that grants a bonus passive skill point — with a route map per act.',
  home_card_admin_badge: 'Admin only',
  home_patchnotes_label: 'Recent patch notes',
  home_patchnotes_loading: 'Loading…',
  home_patchnotes_empty: 'No patch notes found.',
  home_patchnotes_error: 'Could not load patch notes right now.',
},
pt: {
  tagline: 'PATH OF EXILE · CENTRAL',
  menu_title: 'Menu', menu_home: 'Início', menu_bosses: 'Farm de Chefes', menu_snipe: 'Caçador de Ofertas', menu_poe2_campaign: 'Campanha PoE2', menu_flip_advisor: 'Conselheiro de Flip', menu_campaign: 'Guia de Campanha',
  chip_league: 'Liga', chip_price: 'Preço', chip_sync: 'Sincronia', chip_next: 'próximo',
  btn_refresh: 'Atualizar', btn_syncing: 'sincronizando…',
  footer_disclaimer: 'Ferramenta não-oficial feita por fã — sem afiliação com a Grinding Gear Games.',
  footer_made_by: 'Feito por Erick Lúcio',
  footer_dm: 'Manda um e-mail lá para comentário ou feedback',
  home_toggle_poe1: 'Path of Exile 1',
  home_toggle_poe2: 'Path of Exile 2',
  home_toggle_note: 'Escolha seu jogo — isso só reordena o menu à esquerda. As informações dos dois jogos continuam visíveis abaixo de qualquer forma.',
  home_panel_poe1_title: 'Path of Exile 1',
  home_panel_poe2_title: 'Path of Exile 2',
  home_poe1_blurb: 'Path of Exile 1 está no meio do seu ciclo de conteúdo de endgame — chefes pinnacle, lutas Uber e mapas Nightmare T17 continuam sendo as fontes de moeda mais confiáveis no fim de liga. Use o Boss Farm Estimator para comparar o lucro por run ao vivo entre todos os encontros, com preços reais do poe.ninja.',
  home_poe2_blurb: 'Path of Exile 2 está em Acesso Antecipado, com conteúdo novo e mudanças de balanceamento saindo com frequência. Ferramentas de farm para PoE2 ainda estão no roadmap aqui — confira as notas de atualização abaixo para se manter atualizado enquanto isso.',
  home_go_bosses: 'Abrir Boss Farm Estimator →',
  home_card_bosses_title: 'Boss Farm Estimator',
  home_card_bosses_desc: 'Compare o lucro por run ao vivo entre todos os encontros de chefes pinnacle, Uber e mapas Nightmare T17, com preços reais do poe.ninja — pior/médio/melhor em vez de um único número enganoso.',
  home_card_snipe_title: 'Caçador de Ofertas',
  home_card_snipe_desc: 'Monitora uma lista selecionada dos principais itens Únicos em busca de anúncios com preço abaixo do valor de mercado atual, checados ao vivo na API oficial de troca.',
  home_card_poe2campaign_title: 'Campanha PoE2',
  home_card_poe2campaign_desc: 'Rota mais rápida da campanha ato a ato, listas de itens de início de liga e de segundo personagem, e todo bônus permanente disponível no Early Access até agora.',
  home_card_flipadvisor_title: 'Conselheiro de Flip',
  home_card_flipadvisor_desc: 'Classifica pares da Currency Exchange pelo spread% histórico por hora, além de estratégias de compra/venda em múltiplas etapas com um guia de troca em unidades inteiras — um sinal de volatilidade para checar ao vivo, não um lucro garantido.',
  home_card_campaign_title: 'Guia de Campanha',
  home_card_campaign_desc: 'Rota mais rápida da campanha ato a ato, listas de itens de início de liga e de segundo personagem, e toda quest que dá um ponto de passiva bônus — com mapa de rota por ato.',
  home_card_admin_badge: 'Somente admin',
  home_patchnotes_label: 'Notas de atualização recentes',
  home_patchnotes_loading: 'Carregando…',
  home_patchnotes_empty: 'Nenhuma nota de atualização encontrada.',
  home_patchnotes_error: 'Não foi possível carregar as notas de atualização agora.',
},
};

let gamePref = (function(){
  try { return localStorage.getItem('bossFarmGame') || 'poe1'; } catch(e) { return 'poe1'; }
})();
function applyGameToggleUI(){
  document.getElementById('gameBtnPoe1').classList.toggle('active', gamePref === 'poe1');
  document.getElementById('gameBtnPoe2').classList.toggle('active', gamePref === 'poe2');
  document.getElementById('panelPoe1').hidden = gamePref !== 'poe1';
  document.getElementById('panelPoe2').hidden = gamePref !== 'poe2';
}
applyGameToggleUI();
document.getElementById('gameToggle').addEventListener('click', e => {
  const btn = e.target.closest('.game-toggle-btn');
  if(!btn) return;
  gamePref = btn.dataset.game;
  try { localStorage.setItem('bossFarmGame', gamePref); } catch(err) {}
  applyGameToggleUI();
  reorderSiteMenuByGame();
});

// Tracks which (if any) translated empty/error message is currently shown
// per patch list, so a language switch can re-translate it without
// re-fetching (the patch items themselves — title/date/url — aren't
// translated content, only these two status messages are).
const patchStatusKey = { patchEmptyPoe1: null, patchEmptyPoe2: null };

function renderPatchList(listId, emptyId, items){
  const list = document.getElementById(listId);
  const empty = document.getElementById(emptyId);
  if(!items || !items.length){
    patchStatusKey[emptyId] = 'home_patchnotes_empty';
    empty.textContent = t('home_patchnotes_empty');
    empty.hidden = false;
    list.innerHTML = '';
    return;
  }
  patchStatusKey[emptyId] = null;
  empty.hidden = true;
  list.innerHTML = items.map(it => {
    const infoAttr = it.snippet ? ` data-info-text="${escAttr(it.snippet)}"` : '';
    return `<div class="patch-item"><span class="patch-date">${escAttr(it.date || '')}</span>` +
      `<a href="${escAttr(it.url)}" target="_blank" rel="noopener noreferrer"${infoAttr}>${escAttr(it.title)}</a></div>`;
  }).join('');
}

async function loadPatchNotes(game, listId, emptyId){
  try{
    const r = await fetch(PATCH_NOTES_BASE + '?game=' + game, {signal: AbortSignal.timeout(20000)});
    const data = await r.json();
    if(!data.ok) throw new Error(data.error || 'failed');
    renderPatchList(listId, emptyId, data.items);
  }catch(e){
    patchStatusKey[emptyId] = 'home_patchnotes_error';
    const empty = document.getElementById(emptyId);
    empty.textContent = t('home_patchnotes_error');
    empty.hidden = false;
  }
}
loadPatchNotes('poe1', 'patchListPoe1', 'patchEmptyPoe1');
loadPatchNotes('poe2', 'patchListPoe2', 'patchEmptyPoe2');

// Re-applies whatever dynamic (non data-i18n) text is currently on screen in
// the new language — same pattern/reasoning as the Trade Sniper page's own
// refreshDynamicI18n(): patch-list empty/error messages are set from JS, not
// static markup, so applyStaticI18n() alone can't reach them.
function refreshDynamicI18n(){
  for(const emptyId of Object.keys(patchStatusKey)){
    const key = patchStatusKey[emptyId];
    if(key) document.getElementById(emptyId).textContent = t(key);
  }
}

// SHARED_HEADER_HTML's #langsel markup is shared across pages, but each page
// has to wire its own change listener since each needs to refresh its own
// dynamic content afterward (see refreshDynamicI18n() above) — without this
// wiring at all, the dropdown just changes its own selected option and
// nothing else updates, which was exactly the reported bug on this page.
const langSel = document.getElementById('langsel');
langSel.value = lang;
langSel.addEventListener('change', () => {
  lang = langSel.value;
  try { localStorage.setItem('bossFarmLang', lang); } catch(e) {}
  applyStaticI18n();
  refreshDynamicI18n();
});

applyStaticI18n();
"""


def render_home_page():
    head = (SHARED_HEAD_TEMPLATE
            .replace("__PAGE_TITLE__", 'Path of Exile Helper — Boss Farm, Trade Sniper &amp; Patch Notes Hub')
            .replace("__PAGE_DESCRIPTION__", 'Landing page for Path of Exile 1 and Path of Exile 2 tools — boss farming profit estimator, trade sniper, and live patch notes for both games.')
            .replace("__PAGE_SOCIAL_TITLE__", 'Path of Exile Helper — PoE1 / PoE2 Hub')
            .replace("__PAGE_SOCIAL_DESCRIPTION__", 'Boss farming profit estimator, trade sniper, and live patch notes for Path of Exile 1 and Path of Exile 2.')
            .replace("__PAGE_APP_NAME__", 'Path of Exile Helper')
            .replace("__PAGE_JSONLD_DESCRIPTION__", 'Landing page for Path of Exile 1 and Path of Exile 2 farming tools and patch notes.')
            .replace("__FAVICON_URL__", _favicon_data_uri("\U0001F9ED")))
    header = (SHARED_HEADER_HTML.replace("__EXTRA_CONTROLS__", HOME_EXTRA_CONTROLS)
              .replace("__BRAND_ICON__", "&#129517;").replace("__BRAND_TITLE__", "PoE Helper")
              .replace("__PRICECHIPS_ATTR__", "hidden").replace("__DIVINE_CHIP_ATTR__", "hidden"))
    body = (HOME_BODY.replace("__POE1_ICON_URL__", POE1_ICON_URL)
                      .replace("__POE2_ICON_URL__", POE2_ICON_URL))
    return (head + "\n" + SHARED_CSS + '\n</head>\n<body>' + "\n"
            + render_sitemenu("home") + header + body + SHARED_FOOTER_HTML
            + '\n\n<div class="popover" id="popover" role="tooltip"></div>\n\n'
            + "<script>\nconst PAGE_REQUIRES_ADMIN = false;\n" + SHARED_JS_CHROME + HOME_JS
            + '\n</script>\n</body>\n</html>')


# --------------------------------------------------------------------------- #
# Campaign Guide page (/campaign) — PoE1, public
# --------------------------------------------------------------------------- #
# Rush route, act-by-act tips, league-start/second-character item lists, and
# every quest that grants a bonus passive skill point (24 total across the
# 10-act campaign) — see _render_campaign_acts()'s docstring above for the
# sourcing/accuracy policy (objective zone, not NPC name; schematic route,
# not literal turn-by-turn geometry).
POE1_LEAGUE_START_ITEMS = [
    {"item_en": "Wanderlust (unique boots)", "item_pt": "Wanderlust (botas única)",
     "why_en": "Movement speed with no snare/chill/temporal-chains penalty — cheap, huge early QoL.",
     "why_pt": "Velocidade de movimento sem penalidade de lentidão/gelo/correntes temporais — barata, ótima qualidade de vida cedo."},
    {"item_en": "Goldrim (unique helmet)", "item_pt": "Goldrim (elmo único)",
     "why_en": "All resistances plus increased rarity for a few chaos — the single best early defensive upgrade.",
     "why_pt": "Todas as resistências mais raridade aumentada por poucos chaos — a melhor melhoria defensiva do início de liga."},
    {"item_en": "Tabula Rasa (unique body armour)", "item_pt": "Tabula Rasa (peitoral único)",
     "why_en": "A fully-linked 6-socket white chest — 6-links your leveling skill from level 1 instead of grinding currency for one.",
     "why_pt": "Peitoral branco com 6 conexões já prontas — dá 6-link na sua skill de leveling desde o nível 1, sem precisar farmar currency."},
    {"item_en": "Lifesprig (unique wand, for casters)", "item_pt": "Lifesprig (varinha única, para conjuradores)",
     "why_en": "Cast speed plus a free level-1 spell socketed in — a strong free starting weapon for spellcasters.",
     "why_pt": "Velocidade de conjuração e um feitiço nível 1 grátis já encaixado — ótima arma inicial de graça para conjuradores."},
    {"item_en": "Full rare-set vendor recipe", "item_pt": "Receita de vendedor com set raro completo",
     "why_en": "Selling a matching rare helmet + gloves + boots + chest (normal rarity, no quality/links) to any town vendor returns a random unique of that slot — a free shot at exactly the items above.",
     "why_pt": "Vender um set raro combinando (elmo + luvas + botas + peitoral, sem qualidade/conexões) para qualquer vendedor da cidade devolve um único aleatório daquele slot — uma chance grátis de conseguir os itens acima."},
]

POE1_SECOND_CHAR_ITEMS = [
    {"item_en": "Buy Wanderlust / Goldrim / Tabula Rasa outright", "item_pt": "Compre Wanderlust / Goldrim / Tabula Rasa direto",
     "why_en": "With currency already banked from your first character, just buy these instead of relying on the vendor recipe — a few chaos each right at league start.",
     "why_pt": "Com currency já guardada do seu primeiro personagem, compre esses itens direto em vez de depender da receita — poucos chaos cada logo no início da liga."},
    {"item_en": "20% quality gems", "item_pt": "Gemas com 20% de qualidade",
     "why_en": "Buy (or apply Gemcutter's Prisms to) your leveling skill and support gems immediately — noticeably smoother than leveling with 0%-quality gems.",
     "why_pt": "Compre (ou aplique Prismas de Cortador de Gemas em) sua skill de leveling e suportes desde já — bem mais suave que nivelar com gemas de 0% de qualidade."},
    {"item_en": "A build-appropriate leveling unique weapon", "item_pt": "Uma arma única de leveling adequada à build",
     "why_en": "Second characters can afford a real leveling weapon (a cheap low-level unique matching the new build's damage type) immediately instead of grinding to it.",
     "why_pt": "Personagens secundários já podem comprar uma arma de leveling de verdade (uma única barata e de nível baixo, compatível com o tipo de dano da build) em vez de precisar farmar até conseguir uma."},
    {"item_en": "Chaos and Alchemy orbs on hand", "item_pt": "Chaos e Orbes de Alquimia em mãos",
     "why_en": "Upgrade good rare drops into full rares as you go, instead of relying only on the unique-item vendor recipes above for gear.",
     "why_pt": "Melhore drops raros bons conforme joga, em vez de depender só das receitas de vendedor por itens únicos para equipamento."},
]

# Route/quest/boss data cross-checked against the exile-leveling project
# (github.com/HeartofPhos/exile-leveling, MIT licensed) — an actively
# maintained, open-source route dataset used by a real community leveling
# tool, not this file's own guesswork. "attach" is the 0-based index into
# route_en/route_pt the quest's pin renders next to (only used by the
# _campaign_act_svg() fallback — see img_dir in the _render_campaign_acts()
# call below; PoE1 currently always has a real map image, so this only
# matters if that image is ever removed for an act). Bandit choice assumed
# is "kill all three" (the standard leveling recommendation, +2 passive).
POE1_CAMPAIGN_ACTS = [
    {"id": "a1", "title_en": "Act 1 — The Twilight Strand", "title_pt": "Ato 1 — A Faixa do Crepúsculo",
     "route_en": ["Lioneye's Watch (town)", "The Coast", "The Mud Flats", "The Submerged Passage",
                  "The Tidal Island (side)", "The Flooded Depths", "The Ledge", "The Climb",
                  "The Lower Prison", "The Upper Prison", "Prisoner's Gate", "The Ship Graveyard",
                  "The Cavern of Wrath", "The Cavern of Anger", "Merveil's Lair"],
     "route_pt": ["Vigia de Lioneye (cidade)", "A Costa", "Os Pântanos Lodosos", "A Passagem Submersa",
                  "A Ilha das Marés (opcional)", "As Profundezas Alagadas", "A Saliência", "A Subida",
                  "A Prisão Inferior", "A Prisão Superior", "O Portão do Prisioneiro", "Cemitério de Navios",
                  "A Caverna da Ira", "A Caverna da Fúria", "Covil de Merveil"],
     "trials_en": ["The Lower Prison"], "trials_pt": ["A Prisão Inferior"],
     "quests": [
        {"attach": 5, "name_en": "The Dweller of the Deep", "name_pt": "O Habitante das Profundezas",
         "zone_en": "The Flooded Depths", "zone_pt": "As Profundezas Alagadas", "reward": "+1 passive",
         "note_en": "Kill the unique Dweller of the Deep — reached from The Submerged Passage.",
         "note_pt": "Mate o único Habitante das Profundezas — acessado a partir da Passagem Submersa."},
        {"attach": 11, "name_en": "The Marooned Mariner", "name_pt": "O Marinheiro Encalhado",
         "zone_en": "The Ship Graveyard", "zone_pt": "Cemitério de Navios", "reward": "+1 passive",
         "note_en": "Kill Captain Fairgraves in the Ship Graveyard on the way to the Cavern of Wrath.",
         "note_pt": "Mate o Capitão Fairgraves no Cemitério de Navios, a caminho da Caverna da Ira."},
     ],
     "boss_en": "Merveil, the Siren", "boss_pt": "Merveil, a Sereia"},

    {"id": "a2", "title_en": "Act 2 — The Forest Encampment", "title_pt": "Ato 2 — Acampamento da Floresta",
     "route_en": ["The Forest Encampment (town)", "The Old Fields", "The Crossroads", "The Den (side)",
                  "The Chamber of Sins (1-2)", "The Fellshrine Ruins", "The Crypt (1-2)", "The Riverways",
                  "The Western Forest", "The Weaver's Chambers", "The Broken Bridge", "The Wetlands",
                  "The Vaal Ruins", "The Northern Forest", "The Caverns", "The Ancient Pyramid"],
     "route_pt": ["Acampamento da Floresta (cidade)", "Os Campos Antigos", "A Encruzilhada", "A Toca (opcional)",
                  "Câmara dos Pecados (1-2)", "Ruínas de Fellshrine", "A Cripta (1-2)", "As Vias Fluviais",
                  "Floresta Oeste", "Câmaras da Tecelã", "A Ponte Quebrada", "Os Pântanos",
                  "Ruínas Vaal", "Floresta Norte", "As Cavernas", "A Pirâmide Antiga"],
     "trials_en": ["The Chamber of Sins, Level 2", "The Crypt, Level 1"],
     "trials_pt": ["Câmara dos Pecados, Nível 2", "A Cripta, Nível 1"],
     "quests": [
        {"attach": 6, "name_en": "Through Sacred Ground", "name_pt": "Através do Solo Sagrado",
         "zone_en": "The Crypt, Level 2", "zone_pt": "A Cripta, Nível 2", "reward": "+1 passive",
         "note_en": "Find the Altar and take the Golden Hand on Crypt Level 2.",
         "note_pt": "Encontre o Altar e pegue a Mão Dourada no Nível 2 da Cripta."},
        {"attach": 11, "name_en": "The Way Forward", "name_pt": "O Caminho à Frente",
         "zone_en": "hands in at Lioneye's Watch, after the bandit fight", "zone_pt": "entregue em Vigia de Lioneye, depois da luta com os bandidos", "reward": "+1 passive",
         "note_en": "Unlocked by killing all three bandits — hand in back at Lioneye's Watch (Act 1's town), not in Act 2 itself.",
         "note_pt": "Desbloqueada ao matar os três bandidos — entregue de volta em Vigia de Lioneye (cidade do Ato 1), não no Ato 2."},
        {"attach": 11, "name_en": "Deal with the Bandits", "name_pt": "Lide com os Bandidos",
         "zone_en": "Broken Bridge / Wetlands / Western Forest", "zone_pt": "Ponte Quebrada / Pântanos / Floresta Oeste", "reward": "+2 passive",
         "note_en": "Killing all three bandits (Kraityn, Oak, Alira) and reporting back is the standard leveling choice — the extra passive point outweighs any single bandit's unique reward long-term.",
         "note_pt": "Matar os três bandidos (Kraityn, Oak, Alira) e reportar é a escolha padrão de leveling — o ponto de passiva extra compensa mais a longo prazo do que a recompensa única de poupar um bandido."},
     ],
     "boss_en": "The Vaal Oversoul", "boss_pt": "A Alma Suprema Vaal"},

    {"id": "a3", "title_en": "Act 3 — The City of Sarn", "title_pt": "Ato 3 — A Cidade de Sarn",
     "route_en": ["The City of Sarn", "The Sarn Encampment (town)", "The Slums", "The Crematorium",
                  "The Sewers", "The Marketplace", "The Battlefront", "The Solaris Temple (1-2)",
                  "The Docks", "The Ebony Barracks", "The Lunaris Temple (1-2)", "The Imperial Gardens",
                  "The Sceptre of God"],
     "route_pt": ["A Cidade de Sarn", "Acampamento de Sarn (cidade)", "Os Cortiços", "O Crematório",
                  "Os Esgotos", "O Mercado", "Linha de Frente", "Templo de Solaris (1-2)",
                  "As Docas", "Os Quartéis de Ébano", "Templo de Lunaris (1-2)", "Os Jardins Imperiais",
                  "O Cetro de Deus"],
     "trials_en": ["The Crematorium", "The Catacombs", "The Imperial Gardens"],
     "trials_pt": ["O Crematório", "As Catacumbas", "Os Jardins Imperiais"],
     "quests": [
        {"attach": 4, "name_en": "Victario's Secrets", "name_pt": "Segredos de Victario",
         "zone_en": "The Sewers", "zone_pt": "Os Esgotos", "reward": "+1 passive",
         "note_en": "Find the hidden Platinum Busts inside the Sewers.",
         "note_pt": "Encontre os Bustos de Platina escondidos dentro dos Esgotos."},
        {"attach": 10, "name_en": "Piety's Pets", "name_pt": "Os Bichos de Piety",
         "zone_en": "The Lunaris Temple, Level 2", "zone_pt": "Templo de Lunaris, Nível 2", "reward": "+1 passive",
         "note_en": "Kill Piety and take the Tower Key on Lunaris Temple Level 2.",
         "note_pt": "Mate Piety e pegue a Chave da Torre no Nível 2 do Templo de Lunaris."},
     ],
     "boss_en": "Dominus, High Templar (Piety is also fought earlier, at The Crematorium)",
     "boss_pt": "Dominus, Alto Templário (Piety também é enfrentada antes, no Crematório)"},

    {"id": "a4", "title_en": "Act 4 — Highgate", "title_pt": "Ato 4 — Highgate",
     "route_en": ["The Aqueduct", "Highgate (town)", "The Dried Lake", "The Mines (1-2)",
                  "The Crystal Veins", "Daresso's Dream", "The Grand Arena", "Kaom's Dream",
                  "Kaom's Stronghold", "The Belly of the Beast (1-2)", "The Harvest", "The Ascent"],
     "route_pt": ["O Aqueduto", "Highgate (cidade)", "O Lago Seco", "As Minas (1-2)",
                  "As Veias de Cristal", "O Sonho de Daresso", "A Grande Arena", "O Sonho de Kaom",
                  "A Fortaleza de Kaom", "A Barriga da Fera (1-2)", "A Colheita", "A Ascensão"],
     "quests": [
        {"attach": 3, "name_en": "An Indomitable Spirit", "name_pt": "Um Espírito Indomável",
         "zone_en": "The Mines", "zone_pt": "As Minas", "reward": "+1 passive",
         "note_en": "Free Deshret in the Mines — the only bonus-point quest this act, quick to grab.",
         "note_pt": "Liberte Deshret nas Minas — a única quest de ponto bônus deste ato, rápida de pegar."},
     ],
     "boss_en": "Malachai, the Nightmare", "boss_pt": "Malachai, o Pesadelo"},

    {"id": "a5", "title_en": "Act 5 — The Slave Pens", "title_pt": "Ato 5 — Os Currais de Escravos",
     "route_en": ["The Slave Pens", "Overseer's Tower (town)", "The Control Blocks", "Oriath Square",
                  "The Templar Courts", "The Chamber of Innocence", "The Torched Courts", "The Ruined Square",
                  "The Ossuary", "The Reliquary", "The Cathedral Rooftop"],
     "route_pt": ["Os Currais de Escravos", "A Torre do Supervisor (cidade)", "Os Blocos de Controle", "Praça de Oriath",
                  "Os Tribunais Templários", "A Câmara da Inocência", "Os Tribunais Queimados", "A Praça em Ruínas",
                  "O Ossário", "O Relicário", "O Telhado da Catedral"],
     "quests": [
        {"attach": 2, "name_en": "In Service to Science", "name_pt": "A Serviço da Ciência",
         "zone_en": "The Control Blocks", "zone_pt": "Os Blocos de Controle", "reward": "+1 passive",
         "note_en": "Take the Miasmeter and kill Justicar Casticus inside the Control Blocks.",
         "note_pt": "Pegue o Miasmeter e mate o Justicar Casticus dentro dos Blocos de Controle."},
        {"attach": 9, "name_en": "Kitava's Torments", "name_pt": "Os Tormentos de Kitava",
         "zone_en": "The Reliquary", "zone_pt": "O Relicário", "reward": "+1 passive",
         "note_en": "Find all 3 Kitava's Torment items in the corners of the Reliquary — an optional side zone, short detour.",
         "note_pt": "Encontre os 3 itens Tormento de Kitava nos cantos do Relicário — uma zona opcional, desvio curto."},
     ],
     "boss_en": "Kitava, the Insatiable (first encounter)", "boss_pt": "Kitava, o Insaciável (primeiro encontro)"},

    {"id": "a6", "title_en": "Act 6 — Lioneye's Watch (return)", "title_pt": "Ato 6 — Vigia de Lioneye (retorno)",
     "route_en": ["Lioneye's Watch (town)", "The Twilight Strand", "The Coast", "The Mud Flats",
                  "The Karui Fortress", "The Ridge", "The Lower Prison", "Shavronne's Tower",
                  "Prisoner's Gate", "The Western Forest", "The Riverways", "The Wetlands",
                  "The Southern Forest", "The Cavern of Anger", "The Beacon", "The Brine King's Reef"],
     "route_pt": ["Vigia de Lioneye (cidade)", "A Faixa do Crepúsculo", "A Costa", "Os Pântanos Lodosos",
                  "A Fortaleza Karui", "A Crista", "A Prisão Inferior", "A Torre de Shavronne",
                  "O Portão do Prisioneiro", "Floresta Oeste", "As Vias Fluviais", "Os Pântanos",
                  "Floresta Sul", "A Caverna da Fúria", "O Farol", "Recife do Rei das Marés"],
     "trials_en": ["The Lower Prison"], "trials_pt": ["A Prisão Inferior"],
     "quests": [
        {"attach": 4, "name_en": "The Father of War", "name_pt": "O Pai da Guerra",
         "zone_en": "The Karui Fortress", "zone_pt": "A Fortaleza Karui", "reward": "+1 passive",
         "note_en": "Kill Tukohama, Karui God of War, inside the Karui Fortress.",
         "note_pt": "Mate Tukohama, Deus Karui da Guerra, dentro da Fortaleza Karui."},
        {"attach": 8, "name_en": "The Cloven One", "name_pt": "O Fendido",
         "zone_en": "Prisoner's Gate", "zone_pt": "O Portão do Prisioneiro", "reward": "+1 passive",
         "note_en": "Kill Abberath, the Cloven One, at Prisoner's Gate.",
         "note_pt": "Mate Abberath, o Fendido, no Portão do Prisioneiro."},
        {"attach": 11, "name_en": "The Puppet Mistress", "name_pt": "A Mestra das Marionetes",
         "zone_en": "The Wetlands", "zone_pt": "Os Pântanos", "reward": "+1 passive",
         "note_en": "Kill Ryslatha, the Puppet Mistress, in the Wetlands.",
         "note_pt": "Mate Ryslatha, a Mestra das Marionetes, nos Pântanos."},
     ],
     "boss_en": "Tsoagoth, the Brine King", "boss_pt": "Tsoagoth, o Rei das Marés"},

    {"id": "a7", "title_en": "Act 7 — The Bridge Encampment", "title_pt": "Ato 7 — Acampamento da Ponte",
     "route_en": ["The Bridge Encampment (town)", "The Broken Bridge", "The Crossroads", "The Fellshrine Ruins",
                  "The Crypt", "The Chamber of Sins (1-2)", "The Den", "The Ashen Fields",
                  "The Northern Forest", "The Causeway", "The Vaal City", "The Dread Thicket",
                  "The Temple of Decay (1-2)"],
     "route_pt": ["Acampamento da Ponte (cidade)", "A Ponte Quebrada", "A Encruzilhada", "Ruínas de Fellshrine",
                  "A Cripta", "Câmara dos Pecados (1-2)", "A Toca", "Os Campos de Cinzas",
                  "Floresta Norte", "A Calçada", "A Cidade Vaal", "Matagal do Pavor",
                  "Templo da Decadência (1-2)"],
     "trials_en": ["The Crypt", "The Chamber of Sins, Level 2"],
     "trials_pt": ["A Cripta", "Câmara dos Pecados, Nível 2"],
     "quests": [
        {"attach": 7, "name_en": "The Master of a Million Faces", "name_pt": "O Mestre de um Milhão de Faces",
         "zone_en": "The Ashen Fields", "zone_pt": "Os Campos de Cinzas", "reward": "+1 passive",
         "note_en": "Kill Greust, Lord of the Forest, in the Ashen Fields.",
         "note_pt": "Mate Greust, Senhor da Floresta, nos Campos de Cinzas."},
        {"attach": 11, "name_en": "Queen of Despair", "name_pt": "Rainha do Desespero",
         "zone_en": "The Dread Thicket", "zone_pt": "Matagal do Pavor", "reward": "+1 passive",
         "note_en": "Kill Gruthkul, Mother of Despair, inside the Dread Thicket.",
         "note_pt": "Mate Gruthkul, Mãe do Desespero, dentro do Matagal do Pavor."},
        {"attach": 9, "name_en": "Kishara's Star", "name_pt": "A Estrela de Kishara",
         "zone_en": "The Causeway", "zone_pt": "A Calçada", "reward": "+1 passive",
         "note_en": "Find and take Kishara's Star along the Causeway.",
         "note_pt": "Encontre e pegue a Estrela de Kishara ao longo da Calçada."},
     ],
     "boss_en": "Arakaali, Spinner of Shadows", "boss_pt": "Arakaali, Tecelã das Sombras"},

    {"id": "a8", "title_en": "Act 8 — The Sarn Ramparts", "title_pt": "Ato 8 — As Muralhas de Sarn",
     "route_en": ["The Sarn Ramparts", "The Sarn Encampment (town)", "The Toxic Conduits", "Doedre's Cesspool",
                  "The Quay", "The Grain Gate", "The Imperial Fields", "The Solaris Temple (1-2)",
                  "The Lunaris Concourse", "The Lunaris Temple (1-2)", "The Harbour Bridge",
                  "The Bath House", "The High Gardens"],
     "route_pt": ["As Muralhas de Sarn", "Acampamento de Sarn (cidade)", "Os Condutos Tóxicos", "O Poço de Doedre",
                  "O Cais", "O Portão de Grãos", "Os Campos Imperiais", "Templo de Solaris (1-2)",
                  "Terminal de Lunaris", "Templo de Lunaris (1-2)", "A Ponte do Porto",
                  "A Casa de Banhos", "Os Jardins Altos"],
     "trials_en": ["The Bath House"], "trials_pt": ["A Casa de Banhos"],
     "quests": [
        {"attach": 4, "name_en": "Love is Dead", "name_pt": "O Amor Está Morto",
         "zone_en": "The Quay", "zone_pt": "O Cais", "reward": "+1 passive",
         "note_en": "Talk to Clarissa and kill Tolman at the Quay.",
         "note_pt": "Fale com Clarissa e mate Tolman no Cais."},
        {"attach": 5, "name_en": "The Gemling Legion", "name_pt": "A Legião Gemulada",
         "zone_en": "The Grain Gate", "zone_pt": "O Portão de Grãos", "reward": "+1 passive",
         "note_en": "Find and kill the Gemling Legionnaires at the Grain Gate.",
         "note_pt": "Encontre e mate os Legionários Gemulados no Portão de Grãos."},
        {"attach": 12, "name_en": "Reflection of Terror", "name_pt": "Reflexo do Terror",
         "zone_en": "The High Gardens", "zone_pt": "Os Jardins Altos", "reward": "+1 passive",
         "note_en": "Kill Yugul, Reflection of Terror, in the High Gardens.",
         "note_pt": "Mate Yugul, Reflexo do Terror, nos Jardins Altos."},
     ],
     "boss_en": "Yugul, Reflection of Terror (Lunaris and Solaris are also fought earlier, at The Harbour Bridge)",
     "boss_pt": "Yugul, Reflexo do Terror (Lunaris e Solaris também são enfrentadas antes, na Ponte do Porto)"},

    {"id": "a9", "title_en": "Act 9 — Highgate (return)", "title_pt": "Ato 9 — Highgate (retorno)",
     "route_en": ["Highgate (town)", "The Descent", "The Vastiri Desert", "The Foothills",
                  "The Boiling Lake", "The Oasis", "The Tunnel", "The Quarry", "The Refinery",
                  "The Belly of the Beast", "The Rotting Core"],
     "route_pt": ["Highgate (cidade)", "A Descida", "O Deserto de Vastiri", "As Colinas",
                  "O Lago Fervente", "O Oásis", "O Túnel", "A Pedreira", "A Refinaria",
                  "A Barriga da Fera", "O Núcleo Podre"],
     "trials_en": ["The Tunnel"], "trials_pt": ["O Túnel"],
     "quests": [
        {"attach": 5, "name_en": "Queen of the Sands", "name_pt": "Rainha das Areias",
         "zone_en": "The Oasis", "zone_pt": "O Oásis", "reward": "+1 passive",
         "note_en": "Kill Shakari, Queen of the Sands, at the Oasis.",
         "note_pt": "Mate Shakari, Rainha das Areias, no Oásis."},
        {"attach": 7, "name_en": "The Ruler of Highgate", "name_pt": "O Governante de Highgate",
         "zone_en": "The Quarry", "zone_pt": "A Pedreira", "reward": "+1 passive",
         "note_en": "Kill Garukhan, Queen of the Winds, at the Quarry.",
         "note_pt": "Mate Garukhan, Rainha dos Ventos, na Pedreira."},
     ],
     "boss_en": "The Depraved Trinity (Doedre, Maligaro & Shavronne, fought together at The Rotting Core)",
     "boss_pt": "A Trindade Depravada (Doedre, Maligaro e Shavronne, enfrentados juntos no Núcleo Podre)"},

    {"id": "a10", "title_en": "Act 10 — Oriath Docks", "title_pt": "Ato 10 — Docas de Oriath",
     "route_en": ["Oriath Docks (town)", "The Cathedral Rooftop", "The Ravaged Square", "The Control Blocks",
                  "The Ossuary", "The Torched Courts", "The Desecrated Chambers", "The Canals",
                  "The Feeding Trough", "Karui Shores"],
     "route_pt": ["Docas de Oriath (cidade)", "O Telhado da Catedral", "A Praça Devastada", "Os Blocos de Controle",
                  "O Ossário", "Os Tribunais Queimados", "As Câmaras Profanadas", "Os Canais",
                  "O Cocho de Alimentação", "Praias Karui"],
     "trials_en": ["The Ossuary"], "trials_pt": ["O Ossário"],
     "quests": [
        {"attach": 3, "name_en": "Vilenta's Vengeance", "name_pt": "A Vingança de Vilenta",
         "zone_en": "The Control Blocks", "zone_pt": "Os Blocos de Controle", "reward": "+1 passive",
         "note_en": "Find and kill Vilenta inside the Control Blocks.",
         "note_pt": "Encontre e mate Vilenta dentro dos Blocos de Controle."},
        {"attach": 8, "name_en": "An End to Hunger", "name_pt": "Um Fim à Fome",
         "zone_en": "The Feeding Trough", "zone_pt": "O Cocho de Alimentação", "reward": "+2 passive",
         "note_en": "Worth +2 passive points on its own — the single highest-value optional quest in the campaign. Use /passives in-game to confirm you have all 24.",
         "note_pt": "Vale +2 pontos de passiva sozinha — a quest opcional de maior valor de toda a campanha. Use /passives no jogo para confirmar que você tem todos os 24."},
     ],
     "boss_en": "Kitava, the Insatiable (final campaign fight)", "boss_pt": "Kitava, o Insaciável (confronto final da campanha)"},
]

_poe1_acts_body, _poe1_acts_i18n_en, _poe1_acts_i18n_pt = _render_campaign_acts(
    POE1_CAMPAIGN_ACTS, img_dir="/imgs/poe1")
_poe1_items_league_en = _render_campaign_items(
    [{"item": it["item_en"], "why": it["why_en"]} for it in POE1_LEAGUE_START_ITEMS])
_poe1_items_league_pt = _render_campaign_items(
    [{"item": it["item_pt"], "why": it["why_pt"]} for it in POE1_LEAGUE_START_ITEMS])
_poe1_items_second_en = _render_campaign_items(
    [{"item": it["item_en"], "why": it["why_en"]} for it in POE1_SECOND_CHAR_ITEMS])
_poe1_items_second_pt = _render_campaign_items(
    [{"item": it["item_pt"], "why": it["why_pt"]} for it in POE1_SECOND_CHAR_ITEMS])

POE1_CAMPAIGN_EXTRA_CONTROLS = ""

POE1_CAMPAIGN_BODY = (r"""
<div class="wrap">
  <div class="note" data-i18n="campaign_intro">
    A community-sourced rush guide, not an official one — the zone route and quest list are
    cross-checked against the open-source exile-leveling project's route data, and
    quest-giver NPCs are left out where that doesn't specify one. The map image for each act
    is a real screenshot of that act's layout, not annotated by this site. Only the quests
    below grant a passive skill point — every other optional quest in the campaign is safe to
    skip if you're rushing; you can ignore this quest and come back for the loot on a slower
    playthrough.
  </div>
  <div class="note" data-i18n="campaign_trial_note">
    Each act's "Trial of Ascendancy" section (where present — Acts 4 and 5 have none) marks a
    zone with a Trial of Ascendancy: complete it once to help unlock the Labyrinth, a separate
    optional dungeon that grants Ascendancy points for your subclass. You only need 3 completed
    trials per Labyrinth difficulty (Normal/Cruel/Merciless) — some acts have more than one
    available, so treat extras as optional convenience, not a hard requirement.
  </div>
  <div class="campaign-section-label" data-i18n="campaign_items_league_label">League-start items</div>
  <div data-i18n="campaign_items_league"></div>
  <div class="campaign-section-label" data-i18n="campaign_items_second_label">Second-character items</div>
  <div data-i18n="campaign_items_second"></div>
""" + _poe1_acts_body + """
</div>
""")

POE1_CAMPAIGN_JS = (r"""const LEAGUE = __LEAGUE_JSON__;

function populateLeagueOptions(){
  const sel = document.getElementById('leaguesel');
  if(sel.options.length) return;
  let currentLeague;
  try { currentLeague = localStorage.getItem('bossFarmLeague'); } catch(e) { currentLeague = null; }
  const cur = currentLeague || LEAGUE;
  const opts = [LEAGUE, 'Standard', 'Hardcore', 'Hardcore ' + LEAGUE];
  const seen = new Set();
  sel.innerHTML = opts.filter(o => o && !seen.has(o) && seen.add(o))
    .map(o => `<option value="${o}" ${o===cur?'selected':''}>${o}</option>`).join('');
  document.getElementById('league').textContent = cur;
}
populateLeagueOptions();

const I18N = {
en: {
  tagline: 'PATH OF EXILE · CAMPAIGN GUIDE',
  menu_title: 'Menu', menu_home: 'Home', menu_bosses: 'Boss Farm', menu_snipe: 'Trade Sniper', menu_poe2_campaign: 'PoE2 Campaign', menu_flip_advisor: 'Flip Advisor', menu_campaign: 'Campaign Guide',
  chip_league: 'League', chip_price: 'Price', chip_sync: 'Sync', chip_next: 'next',
  btn_refresh: 'Refresh', btn_syncing: 'syncing…',
  footer_disclaimer: 'Unofficial fan tool — not affiliated with or endorsed by Grinding Gear Games.',
  footer_made_by: 'Built by Erick Lúcio',
  footer_dm: 'Send me an email there for comment or feedback',
  campaign_intro: 'A community-sourced rush guide, not an official one — the zone route and quest list are cross-checked against the open-source exile-leveling project\'s route data, and quest-giver NPCs are left out where that doesn\'t specify one. The map image for each act is a real screenshot of that act\'s layout, not annotated by this site. Only the quests below grant a passive skill point — every other optional quest in the campaign is safe to skip if you\'re rushing; you can ignore this quest and come back for the loot on a slower playthrough.',
  campaign_trial_note: 'Each act\'s "Trial of Ascendancy" section (where present — Acts 4 and 5 have none) marks a zone with a Trial of Ascendancy: complete it once to help unlock the Labyrinth, a separate optional dungeon that grants Ascendancy points for your subclass. You only need 3 completed trials per Labyrinth difficulty (Normal/Cruel/Merciless) — some acts have more than one available, so treat extras as optional convenience, not a hard requirement.',
  campaign_items_league_label: 'League-start items',
  campaign_items_second_label: 'Second-character items',
""" + _poe1_acts_i18n_en + r"""
  campaign_items_league: """ + json.dumps(_poe1_items_league_en) + r""",
  campaign_items_second: """ + json.dumps(_poe1_items_second_en) + r""",
},
pt: {
  tagline: 'PATH OF EXILE · GUIA DE CAMPANHA',
  menu_title: 'Menu', menu_home: 'Início', menu_bosses: 'Farm de Chefes', menu_snipe: 'Caçador de Ofertas', menu_poe2_campaign: 'Campanha PoE2', menu_flip_advisor: 'Conselheiro de Flip', menu_campaign: 'Guia de Campanha',
  chip_league: 'Liga', chip_price: 'Preço', chip_sync: 'Sincronia', chip_next: 'próximo',
  btn_refresh: 'Atualizar', btn_syncing: 'sincronizando…',
  footer_disclaimer: 'Ferramenta não-oficial feita por fã — sem afiliação com a Grinding Gear Games.',
  footer_made_by: 'Feito por Erick Lúcio',
  footer_dm: 'Manda um e-mail lá para comentário ou feedback',
  campaign_intro: 'Um guia feito com fontes da comunidade, não oficial — a rota de zonas e a lista de quests foram cruzadas com os dados de rota do projeto de código aberto exile-leveling, e os NPCs que dão as quests foram deixados de fora onde essa fonte não especifica um. A imagem do mapa de cada ato é uma captura de tela real do layout daquele ato, sem anotações deste site. Só as quests listadas abaixo dão ponto de passiva — qualquer outra quest opcional da campanha pode ser ignorada se você estiver correndo; pode ignorar essa quest e voltar pelo loot numa jogada mais tranquila depois.',
  campaign_trial_note: 'A seção "Julgamento de Ascendência" de cada ato (quando existe — os Atos 4 e 5 não têm nenhum) marca uma zona com um Julgamento de Ascendência: complete uma vez para ajudar a desbloquear o Labirinto, uma masmorra opcional separada que dá pontos de Ascendência para sua subclasse. Você só precisa de 3 julgamentos completos por dificuldade do Labirinto (Normal/Cruel/Implacável) — alguns atos têm mais de um disponível, então trate os extras como conveniência opcional, não uma exigência.',
  campaign_items_league_label: 'Itens de início de liga',
  campaign_items_second_label: 'Itens para o segundo personagem',
""" + _poe1_acts_i18n_pt + r"""
  campaign_items_league: """ + json.dumps(_poe1_items_league_pt) + r""",
  campaign_items_second: """ + json.dumps(_poe1_items_second_pt) + r""",
},
};

const langSel = document.getElementById('langsel');
langSel.value = lang;
langSel.addEventListener('change', () => {
  lang = langSel.value;
  try { localStorage.setItem('bossFarmLang', lang); } catch(e) {}
  applyStaticI18n();
});

applyStaticI18n();
""")


def render_campaign_page():
    head = (SHARED_HEAD_TEMPLATE
            .replace("__PAGE_TITLE__", 'Campaign Guide — Path of Exile 1 Leveling Route & Passive Quests')
            .replace("__PAGE_DESCRIPTION__", 'Path of Exile 1 campaign rush guide: fastest route, league-start and second-character item lists, and every quest that grants a bonus passive skill point, act by act.')
            .replace("__PAGE_SOCIAL_TITLE__", 'Campaign Guide — Path of Exile 1')
            .replace("__PAGE_SOCIAL_DESCRIPTION__", 'Fastest campaign route, league-start gear, and every bonus passive-point quest, act by act.')
            .replace("__PAGE_APP_NAME__", 'Campaign Guide')
            .replace("__PAGE_JSONLD_DESCRIPTION__", 'Path of Exile 1 campaign rush guide with route maps and passive skill point quests.')
            .replace("__FAVICON_URL__", _favicon_data_uri(chr(0x1F5FA))))
    header = (SHARED_HEADER_HTML.replace("__EXTRA_CONTROLS__", POE1_CAMPAIGN_EXTRA_CONTROLS)
              .replace("__BRAND_ICON__", "&#128506;").replace("__BRAND_TITLE__", "Campaign Guide")
              .replace("__PRICECHIPS_ATTR__", "hidden").replace("__DIVINE_CHIP_ATTR__", "hidden"))
    return (head + "\n" + SHARED_CSS + '\n</head>\n<body>' + "\n"
            + render_sitemenu("campaign") + header + POE1_CAMPAIGN_BODY + SHARED_FOOTER_HTML
            + '\n\n<div class="popover" id="popover" role="tooltip"></div>\n\n'
            + "<script>\nconst PAGE_REQUIRES_ADMIN = false;\n" + SHARED_JS_CHROME + POE1_CAMPAIGN_JS
            + '\n</script>\n</body>\n</html>')


# --------------------------------------------------------------------------- #
# PoE2 Campaign Guide page (/poe2-campaign) — public
# --------------------------------------------------------------------------- #
# PoE2 (0.5.x "Return of the Ancients") is still Early Access: 4 of a planned
# 6 acts exist today, each followed by an Interlude. Content here is sourced
# from Maxroll's league-start leveling guide for this patch rather than
# pre-cutoff training knowledge (PoE2 is new enough that guessing from memory
# is much riskier than for PoE1 — see the sourcing policy above
# _campaign_act_svg()). Route waypoints stay at the act/interlude level
# (no claimed sub-zone chains) for the same reason; the named encounters
# below are exactly as the source describes them, without asserting whether
# each is an NPC, a zone, or a mini-boss.
POE2_LEAGUE_START_ITEMS = [
    {"item_en": "Any 4-linked chest matching your main skill's colors", "item_pt": "Qualquer peitoral com 4 conexões nas cores da sua skill principal",
     "why_en": "PoE2's link system is per-item (no separate Orb of Fusing/Jeweller's step) — a decent rare or magic chest with the right sockets is a bigger early spike than chasing a specific unique.",
     "why_pt": "O sistema de conexões do PoE2 é por item (sem uma etapa separada de Orbe de Fusão/Joalheiro) — um peitoral raro ou mágico decente com os encaixes certos é um salto inicial maior do que perseguir um único específico."},
    {"item_en": "Resistance-capping rings/amulet", "item_pt": "Anéis/amuleto que fecham as resistências",
     "why_en": "Elemental resistances matter from the very first act — grab cheap rares/magics with any positive resistance rolls before pushing further.",
     "why_pt": "As resistências elementais importam desde o primeiro ato — pegue raros/mágicos baratos com qualquer resistência positiva antes de avançar mais."},
    {"item_en": "Spirit-boosting gear", "item_pt": "Equipamento que aumenta Spirit",
     "why_en": "Spirit gates how many persistent buffs/auras/minions you can run — an early piece with flat Spirit noticeably widens your build options.",
     "why_pt": "Spirit limita quantos buffs/auras/minions persistentes você consegue manter ativos — uma peça inicial com Spirit direto abre bastante as opções de build."},
    {"item_en": "Movement-speed boots", "item_pt": "Botas com velocidade de movimento",
     "why_en": "Basic movement speed on boots is one of the highest-value early upgrades per currency spent, same principle as PoE1's Wanderlust.",
     "why_pt": "Velocidade de movimento básica nas botas é uma das melhorias de maior valor por currency gasta no início, mesmo princípio do Wanderlust no PoE1."},
]

POE2_SECOND_CHAR_ITEMS = [
    {"item_en": "Buy a full resistance-capped rare set outright", "item_pt": "Compre um set raro completo com resistências fechadas direto",
     "why_en": "With currency from your first character, skip the early gear grind entirely and buy a capped set instead of slowly upgrading piece by piece.",
     "why_pt": "Com currency do seu primeiro personagem, pule totalmente o grind de equipamento inicial e compre um set com resistências fechadas em vez de melhorar peça por peça aos poucos."},
    {"item_en": "A build-appropriate 4-linked weapon", "item_pt": "Uma arma com 4 conexões adequada à build",
     "why_en": "Second characters can afford a real weapon matching the new build's skills immediately instead of leveling on whatever drops.",
     "why_pt": "Personagens secundários já podem comprar uma arma de verdade compatível com as skills da nova build imediatamente, em vez de nivelar com o que cair."},
    {"item_en": "Spare Waystones for early maps", "item_pt": "Waystones sobressalentes para mapas iniciais",
     "why_en": "Bank a few low-tier Waystones ahead of time so endgame starts the moment the campaign ends, no farming detour needed first.",
     "why_pt": "Guarde alguns Waystones de tier baixo com antecedência para o endgame começar assim que a campanha terminar, sem precisar de um desvio de farm antes."},
]

# "attach" = 0-based index into route_en/route_pt. Reward text is exactly as
# sourced (resistance/Spirit/life/passive-point bonuses — PoE2's Early Access
# campaign hands out a wider mix of permanent buffs than PoE1's "always a
# passive point" pattern), so the page's intro note explains this instead of
# assuming every bonus is a passive point.
POE2_CAMPAIGN_ACTS = [
    {"id": "a1", "title_en": "Act 1", "title_pt": "Ato 1",
     "route_en": ["Act 1 start", "Freythorn", "Act 1 end"],
     "route_pt": ["Início do Ato 1", "Freythorn", "Fim do Ato 1"],
     "quests": [
        {"attach": 0, "name_en": "Beira", "name_pt": "Beira",
         "zone_en": "Act 1", "zone_pt": "Ato 1", "reward": "+10% Cold Resistance",
         "note_en": "A permanent resistance bonus available during Act 1 — grab it on your first pass through.",
         "note_pt": "Um bônus permanente de resistência disponível durante o Ato 1 — pegue já na primeira passagem."},
        {"attach": 1, "name_en": "Crowbell and Una's Lute", "name_pt": "Crowbell e o Alaúde de Una",
         "zone_en": "Act 1", "zone_pt": "Ato 1", "reward": "+2 passive points",
         "note_en": "A permanent passive-point bonus available during Act 1.",
         "note_pt": "Um bônus permanente de pontos de passiva disponível durante o Ato 1."},
        {"attach": 1, "name_en": "Freythorn", "name_pt": "Freythorn",
         "zone_en": "Freythorn", "zone_pt": "Freythorn", "reward": "+30 Spirit",
         "note_en": "A permanent Spirit bonus tied to the Freythorn zone.",
         "note_pt": "Um bônus permanente de Spirit ligado à zona de Freythorn."},
     ]},

    {"id": "a2", "title_en": "Act 2", "title_pt": "Ato 2",
     "route_en": ["Act 2 start", "Act 2 end"],
     "route_pt": ["Início do Ato 2", "Fim do Ato 2"],
     "quests": [
        {"attach": 0, "name_en": "Kabala", "name_pt": "Kabala",
         "zone_en": "Act 2", "zone_pt": "Ato 2", "reward": "+2 passive points",
         "note_en": "A permanent passive-point bonus available during Act 2.",
         "note_pt": "Um bônus permanente de pontos de passiva disponível durante o Ato 2."},
        {"attach": 0, "name_en": "Sun and Kabala Clan relics", "name_pt": "Relíquias do Sol e do Clã Kabala",
         "zone_en": "Act 2", "zone_pt": "Ato 2", "reward": "Changeable Relic buff",
         "note_en": "Relic-slot buffs — unlike most other bonuses on this page, these can be swapped later, so don't overthink the first pick.",
         "note_pt": "Buffs de slot de relíquia — diferente da maioria dos outros bônus desta página, esses podem ser trocados depois, então não pense demais na primeira escolha."},
        {"attach": 1, "name_en": "Sisters of Garukhan", "name_pt": "Irmãs de Garukhan",
         "zone_en": "Act 2", "zone_pt": "Ato 2", "reward": "+10% Lightning Resistance",
         "note_en": "A permanent resistance bonus available during Act 2.",
         "note_pt": "Um bônus permanente de resistência disponível durante o Ato 2."},
     ]},

    {"id": "a3", "title_en": "Act 3", "title_pt": "Ato 3",
     "route_en": ["Act 3 start", "Venom Crypts", "Act 3 end"],
     "route_pt": ["Início do Ato 3", "Criptas de Veneno", "Fim do Ato 3"],
     "quests": [
        {"attach": 0, "name_en": "Silverfist", "name_pt": "Punho de Prata",
         "zone_en": "Act 3", "zone_pt": "Ato 3", "reward": "+2 passive points",
         "note_en": "A permanent passive-point bonus available during Act 3.",
         "note_pt": "Um bônus permanente de pontos de passiva disponível durante o Ato 3."},
        {"attach": 1, "name_en": "Blackjaw", "name_pt": "Mandíbula Negra",
         "zone_en": "Venom Crypts", "zone_pt": "Criptas de Veneno", "reward": "+10% Fire Resistance",
         "note_en": "One of the buff choices inside the Venom Crypts cannot be changed later — read the choice carefully before confirming.",
         "note_pt": "Uma das escolhas de buff dentro das Criptas de Veneno não pode ser trocada depois — leia a escolha com atenção antes de confirmar."},
        {"attach": 2, "name_en": "Ignagduk", "name_pt": "Ignagduk",
         "zone_en": "Act 3", "zone_pt": "Ato 3", "reward": "+30 Spirit",
         "note_en": "A permanent Spirit bonus available during Act 3.",
         "note_pt": "Um bônus permanente de Spirit disponível durante o Ato 3."},
     ]},

    {"id": "a4", "title_en": "Act 4 and Interludes", "title_pt": "Ato 4 e Interlúdios",
     "route_en": ["Interlude 1", "Interlude 2", "Interlude 3 (Doryani's Contingency)", "Act 4"],
     "route_pt": ["Interlúdio 1", "Interlúdio 2", "Interlúdio 3 (Contingência de Doryani)", "Ato 4"],
     "quests": [
        {"attach": 2, "name_en": "Tattoos", "name_pt": "Tatuagens",
         "zone_en": "Interludes", "zone_pt": "Interlúdios", "reward": "Attribute/resistance choice",
         "note_en": "Small permanent attribute or resistance bonuses picked up across the interludes.",
         "note_pt": "Pequenos bônus permanentes de atributo ou resistência obtidos ao longo dos interlúdios."},
        {"attach": 3, "name_en": "Orbala's Pillars", "name_pt": "Pilares de Orbala",
         "zone_en": "Act 4", "zone_pt": "Ato 4", "reward": "+5% Maximum Life and more passive points",
         "note_en": "Multiple permanent bonuses (including further passive points) available across Act 4 and its interludes.",
         "note_pt": "Vários bônus permanentes (incluindo mais pontos de passiva) disponíveis ao longo do Ato 4 e seus interlúdios."},
     ]},
]

_poe2_acts_body, _poe2_acts_i18n_en, _poe2_acts_i18n_pt = _render_campaign_acts(POE2_CAMPAIGN_ACTS)
_poe2_items_league_en = _render_campaign_items(
    [{"item": it["item_en"], "why": it["why_en"]} for it in POE2_LEAGUE_START_ITEMS])
_poe2_items_league_pt = _render_campaign_items(
    [{"item": it["item_pt"], "why": it["why_pt"]} for it in POE2_LEAGUE_START_ITEMS])
_poe2_items_second_en = _render_campaign_items(
    [{"item": it["item_en"], "why": it["why_en"]} for it in POE2_SECOND_CHAR_ITEMS])
_poe2_items_second_pt = _render_campaign_items(
    [{"item": it["item_pt"], "why": it["why_pt"]} for it in POE2_SECOND_CHAR_ITEMS])

POE2_CAMPAIGN_EXTRA_CONTROLS = ""

POE2_CAMPAIGN_BODY = (r"""

<div class="wrap">
  <div class="note" data-i18n="campaign_intro">
    A community-sourced rush guide, not an official one — Path of Exile 2 is still Early Access
    (0.5.x, "Return of the Ancients"), with 4 of a planned 6 acts released so far, so this guide
    covers Acts 1-4 and their interludes and will grow as GGG ships the rest. Unlike Path of
    Exile 1, not every permanent bonus below is a passive skill point — some are resistances,
    Spirit, or max life. Every other optional quest is safe to skip if you're rushing; you can
    ignore this quest and come back for the loot on a slower playthrough.
  </div>
  <div class="campaign-section-label" data-i18n="campaign_items_league_label">League-start items</div>
  <div data-i18n="campaign_items_league"></div>
  <div class="campaign-section-label" data-i18n="campaign_items_second_label">Second-character items</div>
  <div data-i18n="campaign_items_second"></div>
""" + _poe2_acts_body + """
</div>
""")

POE2_CAMPAIGN_JS = (r"""const LEAGUE = __LEAGUE_JSON__;

// Separate localStorage key from PoE1 pages' 'bossFarmLeague' — this is a
// PoE2 league name (e.g. "Return of the Ancients"), never mixed with PoE1's
// league list, and picking one here must never overwrite a PoE1 page's
// saved preference or vice versa.
function populateLeagueOptions(){
  const sel = document.getElementById('leaguesel');
  if(sel.options.length) return;
  let currentLeague;
  try { currentLeague = localStorage.getItem('bossFarmLeaguePoe2'); } catch(e) { currentLeague = null; }
  const cur = currentLeague || LEAGUE;
  const opts = [LEAGUE, 'Standard', 'Hardcore', 'Hardcore ' + LEAGUE];
  const seen = new Set();
  sel.innerHTML = opts.filter(o => o && !seen.has(o) && seen.add(o))
    .map(o => `<option value="${o}" ${o===cur?'selected':''}>${o}</option>`).join('');
  document.getElementById('league').textContent = cur;
  sel.addEventListener('change', () => {
    try { localStorage.setItem('bossFarmLeaguePoe2', sel.value); } catch(e) {}
    document.getElementById('league').textContent = sel.value;
  });
}
populateLeagueOptions();

const I18N = {
en: {
  tagline: 'PATH OF EXILE 2 · CAMPAIGN GUIDE',
  menu_title: 'Menu', menu_home: 'Home', menu_bosses: 'Boss Farm', menu_snipe: 'Trade Sniper', menu_poe2_campaign: 'PoE2 Campaign', menu_flip_advisor: 'Flip Advisor', menu_campaign: 'Campaign Guide',
  chip_league: 'League', chip_price: 'Price', chip_sync: 'Sync', chip_next: 'next',
  btn_refresh: 'Refresh', btn_syncing: 'syncing…',
  footer_disclaimer: 'Unofficial fan tool — not affiliated with or endorsed by Grinding Gear Games.',
  footer_made_by: 'Built by Erick Lúcio',
  footer_dm: 'Send me an email there for comment or feedback',
  campaign_intro: 'A community-sourced rush guide, not an official one — Path of Exile 2 is still Early Access (0.5.x, "Return of the Ancients"), with 4 of a planned 6 acts released so far, so this guide covers Acts 1-4 and their interludes and will grow as GGG ships the rest. Unlike Path of Exile 1, not every permanent bonus below is a passive skill point — some are resistances, Spirit, or max life. Every other optional quest is safe to skip if you\'re rushing; you can ignore this quest and come back for the loot on a slower playthrough.',
  campaign_items_league_label: 'League-start items',
  campaign_items_second_label: 'Second-character items',
""" + _poe2_acts_i18n_en + r"""
  campaign_items_league: """ + json.dumps(_poe2_items_league_en) + r""",
  campaign_items_second: """ + json.dumps(_poe2_items_second_en) + r""",
},
pt: {
  tagline: 'PATH OF EXILE 2 · GUIA DE CAMPANHA',
  menu_title: 'Menu', menu_home: 'Início', menu_bosses: 'Farm de Chefes', menu_snipe: 'Caçador de Ofertas', menu_poe2_campaign: 'Campanha PoE2', menu_flip_advisor: 'Conselheiro de Flip', menu_campaign: 'Guia de Campanha',
  chip_league: 'Liga', chip_price: 'Preço', chip_sync: 'Sincronia', chip_next: 'próximo',
  btn_refresh: 'Atualizar', btn_syncing: 'sincronizando…',
  footer_disclaimer: 'Ferramenta não-oficial feita por fã — sem afiliação com a Grinding Gear Games.',
  footer_made_by: 'Feito por Erick Lúcio',
  footer_dm: 'Manda um e-mail lá para comentário ou feedback',
  campaign_intro: 'Um guia feito com fontes da comunidade, não oficial — o Path of Exile 2 ainda está em Early Access (0.5.x, "Return of the Ancients"), com 4 de 6 atos planejados lançados até agora, então este guia cobre os Atos 1-4 e seus interlúdios, e vai crescer conforme a GGG lançar o resto. Diferente do Path of Exile 1, nem todo bônus permanente abaixo é um ponto de passiva — alguns são resistências, Spirit ou vida máxima. Qualquer outra quest opcional pode ser ignorada se você estiver correndo; pode ignorar essa quest e voltar pelo loot numa jogada mais tranquila depois.',
  campaign_items_league_label: 'Itens de início de liga',
  campaign_items_second_label: 'Itens para o segundo personagem',
""" + _poe2_acts_i18n_pt + r"""
  campaign_items_league: """ + json.dumps(_poe2_items_league_pt) + r""",
  campaign_items_second: """ + json.dumps(_poe2_items_second_pt) + r""",
},
};

const langSel = document.getElementById('langsel');
langSel.value = lang;
langSel.addEventListener('change', () => {
  lang = langSel.value;
  try { localStorage.setItem('bossFarmLang', lang); } catch(e) {}
  applyStaticI18n();
});

applyStaticI18n();
""")


def render_poe2_campaign_page():
    head = (SHARED_HEAD_TEMPLATE
            .replace("__PAGE_TITLE__", 'PoE2 Campaign Guide — Path of Exile 2 Leveling Route & Permanent Bonuses')
            .replace("__PAGE_DESCRIPTION__", 'Path of Exile 2 campaign rush guide: fastest route, league-start and second-character item lists, and every permanent bonus available per act and interlude.')
            .replace("__PAGE_SOCIAL_TITLE__", 'PoE2 Campaign Guide')
            .replace("__PAGE_SOCIAL_DESCRIPTION__", 'Fastest campaign route, league-start gear, and every permanent act/interlude bonus.')
            .replace("__PAGE_APP_NAME__", 'PoE2 Campaign Guide')
            .replace("__PAGE_JSONLD_DESCRIPTION__", 'Path of Exile 2 campaign rush guide with route maps and permanent act/interlude bonuses.')
            .replace("__FAVICON_URL__", _favicon_data_uri(chr(0x2694) + chr(0xFE0F))))
    header = (SHARED_HEADER_HTML.replace("__EXTRA_CONTROLS__", POE2_CAMPAIGN_EXTRA_CONTROLS)
              .replace("__BRAND_ICON__", "&#9876;&#65039;").replace("__BRAND_TITLE__", "PoE2 Campaign")
              .replace("__PRICECHIPS_ATTR__", "hidden").replace("__DIVINE_CHIP_ATTR__", "hidden"))
    return (head + "\n" + SHARED_CSS + '\n</head>\n<body>' + "\n"
            + render_sitemenu("poe2-campaign") + header + POE2_CAMPAIGN_BODY + SHARED_FOOTER_HTML
            + '\n\n<div class="popover" id="popover" role="tooltip"></div>\n\n'
            + "<script>\nconst PAGE_REQUIRES_ADMIN = false;\n" + SHARED_JS_CHROME + POE2_CAMPAIGN_JS
            + '\n</script>\n</body>\n</html>')


# --------------------------------------------------------------------------- #
# Flip Advisor page (/flip-advisor) — admin-only, PoE1 only
# --------------------------------------------------------------------------- #
# Surfaces currency-exchange pairs ranked by historical spread% over the last
# completed hour (see _get_flip_opportunities() for the full methodology and
# why this is a volatility SIGNAL, not a live-orderbook guarantee). Not
# listed in PAGES — admin-only, injected client-side, same precedent as
# /snipe and /poe2-campaign.
FLIP_ADVISOR_EXTRA_CONTROLS = ""

FLIP_ADVISOR_BODY = r"""

<div class="wrap">
  <div class="note" data-i18n="flip_disclaimer">
    This uses the official currency-exchange market's hourly aggregate data — delayed and
    historical, not a live orderbook. A high spread% means that pair's rate moved a lot within
    the last completed hour (a volatility signal worth checking), not a guaranteed profit.
    Always verify the live price on the Bulk Item Exchange before trading.
  </div>
  <div class="note" data-i18n="flip_liquidity_note">
    Liquidity is shown in chaos-equivalent value (volume × that currency's own chaos price), not
    raw unit counts — a lower number means fewer results but higher confidence; a higher number
    shows more results, some from thinner/more volatile markets.
  </div>
  <div class="snipe-row">
    <label><span data-i18n="flip_liquidity_label">Min. liquidity</span>
      <input type="number" id="flipMinLiquidity" min="0" step="1" value="100" style="width:100px">
    </label>
    <select id="flipLiquidityUnit" style="width:90px">
      <option value="chaos" data-i18n="snipe_unit_chaos">Chaos</option>
      <option value="divine" data-i18n="snipe_unit_divine">Divine</option>
    </select>
    <div class="runs" id="flipLiquidityPresets" role="group" aria-label="liquidity presets">
      <button data-liq="10">10</button>
      <button data-liq="100" class="active">100</button>
      <button data-liq="1000">1000</button>
    </div>
    <button class="sync" id="flipApply" data-i18n="flip_apply">Apply</button>
  </div>
  <div class="snipe-row">
    <span class="snipe-status" id="flipStatus" data-i18n="flip_status_loading">Loading…</span>
    <a class="sync" id="flipTradeLink" href="https://www.pathofexile.com/trade/exchange/Standard" target="_blank" rel="noopener noreferrer" data-i18n="flip_open_exchange">Open Bulk Item Exchange &rarr;</a>
  </div>

  <div class="snipe-results">
    <div class="slabel" data-i18n="flip_strategies_label">Multi-step flip strategies (start &amp; end with Chaos Orb)</div>
    <div class="flip-strategies" id="flipStrategies"></div>
    <div class="empty" id="flipStrategiesEmpty" data-i18n="flip_strategies_empty">No profitable multi-step cycle found at this liquidity level right now — try lowering the minimum liquidity, or check back next hour.</div>
  </div>

  <div class="snipe-results">
    <div class="slabel" data-i18n="flip_pairs_label">Single-pair spreads</div>
    <div class="flip-grid" id="flipList"></div>
    <div class="empty" id="flipEmpty" data-i18n="flip_empty">No opportunities found for this league right now.</div>
  </div>
</div>
"""

FLIP_ADVISOR_JS = r"""const LEAGUE = __LEAGUE_JSON__;
const FLIP_ADVISOR_BASE = '/api/flipadvisor';

function populateLeagueOptions(){
  const sel = document.getElementById('leaguesel');
  if(sel.options.length) return;
  let currentLeague;
  try { currentLeague = localStorage.getItem('bossFarmLeague'); } catch(e) { currentLeague = null; }
  const cur = currentLeague || LEAGUE;
  const opts = [LEAGUE, 'Standard', 'Hardcore', 'Hardcore ' + LEAGUE];
  const seen = new Set();
  sel.innerHTML = opts.filter(o => o && !seen.has(o) && seen.add(o))
    .map(o => `<option value="${o}" ${o===cur?'selected':''}>${o}</option>`).join('');
  document.getElementById('league').textContent = cur;
}
populateLeagueOptions();

const I18N = {
en: {
  tagline: 'PATH OF EXILE · FLIP ADVISOR',
  menu_title: 'Menu', menu_home: 'Home', menu_bosses: 'Boss Farm', menu_snipe: 'Trade Sniper', menu_poe2_campaign: 'PoE2 Campaign', menu_flip_advisor: 'Flip Advisor', menu_campaign: 'Campaign Guide',
  chip_league: 'League', chip_price: 'Price', chip_sync: 'Sync', chip_next: 'next',
  btn_refresh: 'Refresh', btn_syncing: 'syncing…',
  footer_disclaimer: 'Unofficial fan tool — not affiliated with or endorsed by Grinding Gear Games.',
  footer_made_by: 'Built by Erick Lúcio',
  footer_dm: 'Send me an email there for comment or feedback',
  flip_disclaimer: 'This uses the official currency-exchange market\'s hourly aggregate data — delayed and historical, not a live orderbook. A high spread% means that pair\'s rate moved a lot within the last completed hour (a volatility signal worth checking), not a guaranteed profit. Always verify the live price on the Bulk Item Exchange before trading.',
  flip_status_loading: 'Loading…',
  flip_status_ready: 'Showing top {n} pairs for {league} — hour data as of {time}',
  flip_status_error: 'Could not load flip data right now.',
  flip_open_exchange: 'Open Bulk Item Exchange →',
  flip_empty: 'No opportunities found for this league right now.',
  flip_spread: 'spread',
  flip_volume: 'liquidity',
  flip_liquidity_note: 'Liquidity is shown in chaos-equivalent value (volume × that currency\'s own chaos price), not raw unit counts — a lower number means fewer results but higher confidence; a higher number shows more results, some from thinner/more volatile markets.',
  flip_liquidity_label: 'Min. liquidity',
  flip_apply: 'Apply',
  snipe_unit_chaos: 'Chaos',
  snipe_unit_divine: 'Divine',
  flip_strategies_label: 'Multi-step flip strategies (start & end with Chaos Orb)',
  flip_strategies_empty: 'No profitable multi-step cycle found at this liquidity level right now — try lowering the minimum liquidity, or check back next hour.',
  flip_pairs_label: 'Single-pair spreads',
  flip_sell: 'Sell',
  flip_buy: 'Buy',
  flip_steps: 'steps',
  flip_min_step_liq: 'min. step liquidity',
  flip_guide_change: 'Change {den} {sell} to {num} {buy}',
},
pt: {
  tagline: 'PATH OF EXILE · CONSELHEIRO DE FLIP',
  menu_title: 'Menu', menu_home: 'Início', menu_bosses: 'Farm de Chefes', menu_snipe: 'Caçador de Ofertas', menu_poe2_campaign: 'Campanha PoE2', menu_flip_advisor: 'Conselheiro de Flip', menu_campaign: 'Guia de Campanha',
  chip_league: 'Liga', chip_price: 'Preço', chip_sync: 'Sincronia', chip_next: 'próximo',
  btn_refresh: 'Atualizar', btn_syncing: 'sincronizando…',
  footer_disclaimer: 'Ferramenta não-oficial feita por fã — sem afiliação com a Grinding Gear Games.',
  footer_made_by: 'Feito por Erick Lúcio',
  footer_dm: 'Manda um e-mail lá para comentário ou feedback',
  flip_disclaimer: 'Isso usa os dados agregados por hora do mercado oficial de troca de moedas — atrasados e históricos, não uma carteira de ordens ao vivo. Um spread% alto significa que a taxa daquele par variou bastante na última hora completa (um sinal de volatilidade que vale a pena checar), não um lucro garantido. Sempre confira o preço ao vivo no Bulk Item Exchange antes de negociar.',
  flip_status_loading: 'Carregando…',
  flip_status_ready: 'Mostrando os {n} melhores pares para {league} — dados da hora {time}',
  flip_status_error: 'Não foi possível carregar os dados de flip agora.',
  flip_open_exchange: 'Abrir Bulk Item Exchange →',
  flip_empty: 'Nenhuma oportunidade encontrada para esta liga agora.',
  flip_spread: 'spread',
  flip_volume: 'liquidez',
  flip_liquidity_note: 'A liquidez é mostrada em valor equivalente em chaos (volume × o preço em chaos daquela moeda), não em contagem bruta de unidades — um número menor significa menos resultados, mas mais confiança; um número maior mostra mais resultados, alguns de mercados mais finos/voláteis.',
  flip_liquidity_label: 'Liquidez mín.',
  flip_apply: 'Aplicar',
  snipe_unit_chaos: 'Caos',
  snipe_unit_divine: 'Divino',
  flip_strategies_label: 'Estratégias de flip em múltiplas etapas (começa e termina com Chaos Orb)',
  flip_strategies_empty: 'Nenhum ciclo de múltiplas etapas lucrativo encontrado com este nível de liquidez agora — tente diminuir a liquidez mínima, ou volte na próxima hora.',
  flip_pairs_label: 'Spreads de par único',
  flip_sell: 'Vender',
  flip_buy: 'Comprar',
  flip_steps: 'etapas',
  flip_min_step_liq: 'liquidez mín. da etapa',
  flip_guide_change: 'Troque {den} {sell} por {num} {buy}',
},
};

function renderFlipList(items){
  const list = document.getElementById('flipList');
  const empty = document.getElementById('flipEmpty');
  if(!items || !items.length){
    empty.hidden = false;
    list.innerHTML = '';
    return;
  }
  empty.hidden = true;
  list.innerHTML = items.map(it => `<div class="flip-card">
    <div class="flip-pair">${escAttr(it.nameA)} &harr; ${escAttr(it.nameB)}</div>
    <div class="flip-rate">${it.rateLow} – ${it.rateHigh}</div>
    <div class="flip-spread">${it.spreadPct}% <span style="font-size:10px;color:var(--ink-dim);font-weight:400">${t('flip_spread')}</span></div>
    <div class="flip-volume">${t('flip_volume')}: ${Math.round(it.liquidityChaos).toLocaleString()}c</div>
  </div>`).join('');
}

// PoE currency isn't divisible — a rate like "1 Chaos -> 0.7 Exalted" isn't
// an executable trade (can't sell 0.7 of an Exalted Orb). This finds a
// single WHOLE-NUMBER starting quantity of the chain's first currency such
// that every step along the whole chain lands on a whole number too — not
// just each step rationalized independently. Rationalizing each step's rate
// on its own (an earlier, real bug caught live) produces internally
// inconsistent amounts: step 1 says "7 Chaos -> 1 Metallic Fossil" while
// step 2 separately says "33 Metallic Fossil -> 2 Divine Orb" — but the
// chain only ever produced 1 Metallic Fossil from step 1, not 33. This
// instead walks the chain ONCE with a shared starting amount, rounding each
// step's output to the nearest whole number and carrying that EXACT number
// into the next step as its input — so step i's "buy" amount and step i+1's
// "sell" amount are always the literal same number, by construction.
//
// The starting amount isn't just "smallest that avoids any step rounding to
// zero" — a second real bug caught live: that alone found a starting amount
// so small that per-step rounding error compounded into a chain whose
// overall ratio (300/60 = 5x) was wildly off from the strategy's actual
// computed profit (~2.6x). Instead this searches increasing starting
// amounts for the smallest one whose overall rounded ratio is within
// CHAIN_ACCURACY_TOLERANCE of the TRUE (unrounded) cumulative rate product
// — accurate AND practically small — falling back to whichever candidate
// found during the search had the lowest error if none met the tolerance
// within the search cap.
const CHAIN_ACCURACY_TOLERANCE = 0.02; // 2% — matches this data's own hourly-aggregate precision
const CHAIN_MAX_START = 200000;

function chainWholeAmounts(rates){
  const trueRatio = rates.reduce((a, r) => a * r, 1);
  let best = null, bestErr = Infinity;
  for(let start = 1; start <= CHAIN_MAX_START; start++){
    const amounts = [start];
    let amt = start, ok = true;
    for(const r of rates){
      amt = Math.round(amt * r);
      if(amt < 1){ ok = false; break; }
      amounts.push(amt);
    }
    if(!ok) continue;
    const err = Math.abs((amt / start) - trueRatio) / trueRatio;
    if(err < bestErr){ best = amounts; bestErr = err; }
    if(err <= CHAIN_ACCURACY_TOLERANCE) return amounts;
  }
  return best || [1, ...rates.map(() => 1)]; // pathological fallback, should never hit in practice
}

// Every step is one atomic exchange: SELL the "from" currency, BUY the "to"
// currency, at `rate` units of "to" per 1 unit of "from" — labeled
// explicitly per step (not just an arrow) since which side is being bought
// vs sold was the whole point of asking for this over the single-pair list.
// The "guide" column is the same trade expressed in whole, actually-
// tradeable, CHAIN-CONSISTENT units (see chainWholeAmounts() above).
function renderStrategies(strategies){
  const list = document.getElementById('flipStrategies');
  const empty = document.getElementById('flipStrategiesEmpty');
  if(!strategies || !strategies.length){
    empty.hidden = false;
    list.innerHTML = '';
    return;
  }
  empty.hidden = true;
  list.innerHTML = strategies.map(s => {
    const chainAmounts = chainWholeAmounts(s.steps.map(st => st.rate));
    const steps = s.steps.map((st, i) => {
      const guide = t('flip_guide_change')
        .replace('{den}', chainAmounts[i]).replace('{sell}', escAttr(st.sell))
        .replace('{num}', chainAmounts[i + 1]).replace('{buy}', escAttr(st.buy));
      return `<div class="flip-step">
      <span class="step-num">${i + 1}.</span>
      <span class="step-op sell">${t('flip_sell')}</span>
      <span class="step-desc">1 ${escAttr(st.sell)}</span>
      <span class="step-op buy">${t('flip_buy')}</span>
      <span class="step-desc">${st.rate} ${escAttr(st.buy)}</span>
      <span class="step-guide">${guide}</span>
      <span class="step-liq">${t('flip_volume')}: ${Math.round(st.liquidityChaos).toLocaleString()}c</span>
    </div>`;
    }).join('');
    return `<div class="flip-strategy-card">
      <div class="flip-strategy-head">
        <span class="flip-strategy-profit">+${s.profitPct}%</span>
        <span class="flip-strategy-meta">${s.steps.length} ${t('flip_steps')} · 1 &rarr; ${s.endAmount} Chaos Orb · ${t('flip_min_step_liq')}: ${Math.round(s.minStepLiquidityChaos).toLocaleString()}c</span>
      </div>
      <div class="flip-steps">${steps}</div>
    </div>`;
  }).join('');
}

let lastFlipData = null;

function currentMinLiquidityChaos(){
  const raw = Number(document.getElementById('flipMinLiquidity').value) || 0;
  const unit = document.getElementById('flipLiquidityUnit').value;
  if(unit === 'divine'){
    const divineRate = (lastFlipData && lastFlipData.divineRateChaos) || 180;
    return raw * divineRate;
  }
  return raw;
}

async function loadFlipAdvisor(){
  const league = document.getElementById('leaguesel').value || LEAGUE;
  document.getElementById('flipTradeLink').href = 'https://www.pathofexile.com/trade/exchange/' + encodeURIComponent(league);
  const minLiquidity = currentMinLiquidityChaos();
  setStatus('flip_status_loading');
  try{
    const url = FLIP_ADVISOR_BASE + '?league=' + encodeURIComponent(league) + '&minLiquidity=' + encodeURIComponent(minLiquidity);
    const r = await fetch(url, {signal: AbortSignal.timeout(30000)});
    const data = await r.json();
    if(!data.ok) throw new Error(data.error || 'failed');
    lastFlipData = data;
    renderFlipList(data.items);
    renderStrategies(data.strategies);
    const time = data.hourTimestamp ? new Date(data.hourTimestamp * 1000).toLocaleString() : '?';
    setStatus('flip_status_ready', {n: data.items.length, league, time});
  }catch(e){
    console.error('[Flip Advisor] /api/flipadvisor request failed.', e);
    lastFlipData = null;
    setStatus('flip_status_error');
    renderFlipList([]);
    renderStrategies([]);
  }
}

function setStatus(key, vars){
  const el = document.getElementById('flipStatus');
  let text = t(key);
  if(vars) for(const k of Object.keys(vars)) text = text.replace('{' + k + '}', vars[k]);
  el.textContent = text;
}

function refreshDynamicI18n(){
  if(lastFlipData){
    renderFlipList(lastFlipData.items);
    renderStrategies(lastFlipData.strategies);
    const time = lastFlipData.hourTimestamp ? new Date(lastFlipData.hourTimestamp * 1000).toLocaleString() : '?';
    setStatus('flip_status_ready', {n: lastFlipData.items.length, league: document.getElementById('leaguesel').value || LEAGUE, time});
  }
}

const langSel = document.getElementById('langsel');
langSel.value = lang;
langSel.addEventListener('change', () => {
  lang = langSel.value;
  try { localStorage.setItem('bossFarmLang', lang); } catch(e) {}
  applyStaticI18n();
  refreshDynamicI18n();
});

document.getElementById('leaguesel').addEventListener('change', e => {
  try { localStorage.setItem('bossFarmLeague', e.target.value); } catch(err) {}
  document.getElementById('league').textContent = e.target.value;
  loadFlipAdvisor();
});

document.getElementById('flipApply').addEventListener('click', loadFlipAdvisor);
document.getElementById('flipMinLiquidity').addEventListener('keydown', e => {
  if(e.key === 'Enter') loadFlipAdvisor();
});
document.getElementById('flipLiquidityPresets').addEventListener('click', e => {
  const btn = e.target.closest('button[data-liq]');
  if(!btn) return;
  document.getElementById('flipMinLiquidity').value = btn.dataset.liq;
  document.getElementById('flipLiquidityUnit').value = 'chaos';
  document.querySelectorAll('#flipLiquidityPresets button').forEach(b => b.classList.toggle('active', b === btn));
  loadFlipAdvisor();
});

applyStaticI18n();
loadFlipAdvisor();
"""


def render_flip_advisor_page():
    head = (SHARED_HEAD_TEMPLATE
            .replace("__PAGE_TITLE__", 'Flip Advisor — Path of Exile Currency Exchange Spread Watcher')
            .replace("__PAGE_DESCRIPTION__", 'Ranks Path of Exile currency-exchange pairs by historical hourly spread — a volatility signal for potential flips, sourced from the official currency-exchange market data.')
            .replace("__PAGE_SOCIAL_TITLE__", 'Flip Advisor — PoE Currency Exchange Spread Watcher')
            .replace("__PAGE_SOCIAL_DESCRIPTION__", 'Ranks Path of Exile currency-exchange pairs by historical hourly spread, sourced from the official currency-exchange market data.')
            .replace("__PAGE_APP_NAME__", 'Flip Advisor')
            .replace("__PAGE_JSONLD_DESCRIPTION__", 'Ranks Path of Exile currency-exchange pairs by historical hourly spread.')
            .replace("__FAVICON_URL__", _favicon_data_uri("\U0001F4B1")))
    header = (SHARED_HEADER_HTML.replace("__EXTRA_CONTROLS__", FLIP_ADVISOR_EXTRA_CONTROLS)
              .replace("__BRAND_ICON__", "&#128177;").replace("__BRAND_TITLE__", "Flip Advisor")
              .replace("__PRICECHIPS_ATTR__", "hidden").replace("__DIVINE_CHIP_ATTR__", "hidden"))
    return (head + "\n" + SHARED_CSS + '\n</head>\n<body>' + "\n"
            + render_sitemenu("flip-advisor") + header + FLIP_ADVISOR_BODY + SHARED_FOOTER_HTML
            + '\n\n<div class="popover" id="popover" role="tooltip"></div>\n\n'
            + "<script>\nconst PAGE_REQUIRES_ADMIN = false;\n" + SHARED_JS_CHROME + FLIP_ADVISOR_JS
            + '\n</script>\n</body>\n</html>')


# --------------------------------------------------------------------------- #
# Minification (optional — see minify_page() docstring for the safety model)
# --------------------------------------------------------------------------- #
def _minify_css(css):
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    css = re.sub(r"\s+", " ", css)
    css = re.sub(r"\s*([{}:;,])\s*", r"\1", css)
    return css.strip()


def _minify_js(js):
    """Best-effort JS minify via `npx esbuild --minify`. Returns js unchanged
    on any failure (esbuild/node not installed, no network, timeout, etc.) —
    minification must never be able to break or block the app. A hand-rolled
    regex JS minifier is NOT used here on purpose: template literals, regex
    literals, and ASI make that genuinely risky to get exactly right, and
    shipping subtly-broken JS to every visitor is worse than shipping
    unminified JS.
    """
    try:
        result = subprocess.run(
            ["npx", "--yes", "esbuild", "--minify", "--loader=js"],
            input=js.encode("utf-8"), capture_output=True, timeout=30,
            shell=(os.name == "nt"),
        )
        if result.returncode != 0 or not result.stdout.strip():
            return js
        return result.stdout.decode("utf-8")
    except Exception:
        return js


def minify_page(html):
    """Shrinks the <style>/<script> blocks of a fully-rendered PAGE (i.e.
    __POLL_MS__/__CANONICAL_URL__ already substituted). Safe to call on any
    HTML string; falls back to returning it unchanged if minification isn't
    possible right now.
    """
    def sub_style(m):
        return "<style>" + _minify_css(m.group(1)) + "</style>"

    def sub_script(m):
        return "<script>" + _minify_js(m.group(1)) + "</script>"

    html = re.sub(r"<style>(.*?)</style>", sub_style, html, count=1, flags=re.DOTALL)
    html = re.sub(r"<script>(.*?)</script>", sub_script, html, count=1, flags=re.DOTALL)
    return html


# --------------------------------------------------------------------------- #
# Server
# --------------------------------------------------------------------------- #
# Static image assets (e.g. imgs/poe1/act1.png, used as Campaign Guide act
# backgrounds — see _render_campaign_acts()'s img_dir param) referenced from
# rendered pages as "/imgs/<...>". The static build (build_static.py) copies
# this whole directory into out_dir/imgs so Cloudflare's static-asset layer
# serves them the same way; this do_GET branch is only for the local dev
# server, which has no such layer of its own.
IMGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "imgs")


def make_handler(league, poll_ms, pages_html):
    pages_bytes = {slug: html.encode("utf-8") for slug, html in pages_html.items()}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _redirect(self, location):
            self.send_response(302)
            self.send_header("Location", location)
            self.end_headers()

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            slug = path[1:] if path.startswith("/") else path
            if slug in pages_bytes:
                self._send(200, pages_bytes[slug], "text/html; charset=utf-8")
            elif path == "/":
                self._redirect("/home")
            elif path == "/api/data":
                qs = urllib.parse.parse_qs(parsed.query)
                req_league = (qs.get("league") or [league])[0].strip()[:64] or league
                try:
                    body = json.dumps(build_payload(req_league)).encode("utf-8")
                    self._send(200, body, "application/json")
                except Exception as e:
                    self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")
            elif path == "/api/patchnotes":
                qs = urllib.parse.parse_qs(parsed.query)
                game = (qs.get("game") or ["poe1"])[0].strip()
                if game not in PATCH_NOTES_URLS:
                    self._send(400, json.dumps({"ok": False, "error": "unknown game"}).encode(), "application/json")
                else:
                    try:
                        items = _get_patch_notes(game)
                        body = json.dumps({"ok": True, "items": items}).encode("utf-8")
                        self._send(200, body, "application/json")
                    except Exception as e:
                        self._send(500, json.dumps({"ok": False, "error": str(e)}).encode(), "application/json")
            elif path.startswith("/imgs/"):
                requested = os.path.normpath(os.path.join(IMGS_DIR, path[len("/imgs/"):]))
                if not requested.startswith(IMGS_DIR + os.sep) or not os.path.isfile(requested):
                    self._send(404, b"not found", "text/plain")
                else:
                    ctype = mimetypes.guess_type(requested)[0] or "application/octet-stream"
                    with open(requested, "rb") as f:
                        self._send(200, f.read(), ctype)
            elif path == "/api/flipadvisor":
                qs = urllib.parse.parse_qs(parsed.query)
                req_league = (qs.get("league") or [league])[0].strip()[:64] or league
                min_liq_raw = (qs.get("minLiquidity") or [None])[0]
                try:
                    min_liq = float(min_liq_raw) if min_liq_raw not in (None, "") else None
                except ValueError:
                    min_liq = None
                try:
                    data = _get_flip_opportunities(req_league, min_liq)
                    body = json.dumps({"ok": True, **data}).encode("utf-8")
                    self._send(200, body, "application/json")
                except Exception as e:
                    self._send(500, json.dumps({"ok": False, "error": str(e)}).encode(), "application/json")
            else:
                self._send(404, b"not found", "text/plain")
    return H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--league", default="Allflame")
    ap.add_argument("--league-poe2", default="Return of the Ancients",
                    help="PoE2 league name, shown only on /poe2-campaign's league dropdown")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--poll", type=int, default=120,
                    help="browser auto-refresh interval, in seconds")
    ap.add_argument("--minify", action="store_true",
                    help="minify the served HTML/CSS/JS via `npx esbuild` (falls back to "
                    "unminified if esbuild/Node isn't available). Off by default so this file "
                    "keeps working with zero dependencies beyond stdlib Python out of the box.")
    args = ap.parse_args()

    url = f"http://localhost:{args.port}/home"
    pages_html = {
        "home": (render_home_page().replace("__POLL_MS__", str(args.poll * 1000))
                 .replace("__LEAGUE_JSON__", json.dumps(args.league))
                 .replace("__CANONICAL_URL__", url)),
        "bosses": (render_bosses_page().replace("__POLL_MS__", str(args.poll * 1000))
                   .replace("__CANONICAL_URL__", f"http://localhost:{args.port}/bosses")),
        "snipe": (render_snipe_page().replace("__POLL_MS__", str(args.poll * 1000))
                  .replace("__LEAGUE_JSON__", json.dumps(args.league))
                  .replace("__CANONICAL_URL__", f"http://localhost:{args.port}/snipe")),
        "poe2-campaign": (render_poe2_campaign_page().replace("__POLL_MS__", str(args.poll * 1000))
                          .replace("__LEAGUE_JSON__", json.dumps(args.league_poe2))
                          .replace("__CANONICAL_URL__", f"http://localhost:{args.port}/poe2-campaign")),
        "flip-advisor": (render_flip_advisor_page().replace("__POLL_MS__", str(args.poll * 1000))
                         .replace("__LEAGUE_JSON__", json.dumps(args.league))
                         .replace("__CANONICAL_URL__", f"http://localhost:{args.port}/flip-advisor")),
        "campaign": (render_campaign_page().replace("__POLL_MS__", str(args.poll * 1000))
                     .replace("__LEAGUE_JSON__", json.dumps(args.league))
                     .replace("__CANONICAL_URL__", f"http://localhost:{args.port}/campaign")),
    }
    if args.minify:
        pages_html = {slug: minify_page(html) for slug, html in pages_html.items()}

    srv = ThreadingHTTPServer(("127.0.0.1", args.port),
                              make_handler(args.league, args.poll * 1000, pages_html))
    print(f"Boss Farm Estimator at {url}  (league: {args.league}, price: exchange->stash)"
          f"  --  Ctrl+C to stop"
          f"\nBoss Farm dashboard at http://localhost:{args.port}/bosses"
          f"\nTrade Sniper (needs the deployed Worker to actually work) at "
          f"http://localhost:{args.port}/snipe"
          f"\nPoE2 Campaign at http://localhost:{args.port}/poe2-campaign"
          f"\nFlip Advisor (admin-only, PoE1) at http://localhost:{args.port}/flip-advisor"
          f"\nCampaign Guide (PoE1) at http://localhost:{args.port}/campaign")
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