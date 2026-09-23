"""Agent Commons Stage 2 — shared world engine.

Pure, stdlib-only terrain generation (seeded value noise) plus the
database-backed world logic: spawning, movement with action points,
private discoveries, and public disclosure. All AP math is done
server-side; the client never supplies AP values.

Endpoint wiring lives in server/app.py; this module exposes constants,
the two module-level pure helpers required by the contract, LEGEND /
AP_RULES payloads, and DB functions that take a ``connect`` callable
(the same one create_app() builds, so they hit AC_DB_PATH).
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import random
import sqlite3
import time
from datetime import datetime, timezone

# ---- pinned constants -------------------------------------------------

WORLD_SIZE = 64
SEED_ID = "agent-commons-genesis-v1"
AP_START = 50
AP_CAP = 100
AP_REGEN_SECONDS = 60
MOVE_COST_LAND = 1
MOVE_COST_MOUNTAIN = 2
DISCLOSE_COST = 1

# ---- Stage 4 — economy experiment (scarce resources + valueless chits) --

# Each land terrain yields one resource; ocean yields nothing.
TERRAIN_RESOURCE = {
    "plains": "grain",
    "forest": "timber",
    "mountain": "iron_ore",
    "desert": "glass",
}
# Bible §2.1: 11 raw resources in two passes. The legacy pass (Stage 4) seeds
# one resource per land tile under the genesis seed; the overlay pass (§2.4
# ruling 3) adds exactly one more per land tile under NATURAL_SEED. A tile
# can therefore bear two resources (legacy + overlay).
# The Stage 4 legacy set (what the old TERRAIN_RESOURCE seeded). Note the
# legacy desert "glass" rows: they stay gatherable as the last wild glass
# veins (Bible §2.4 ruling 1) even though glass is not in the raw tier.
LEGACY_RESOURCES = ("grain", "timber", "iron_ore", "glass")
OVERLAY_RESOURCES = ("fruit", "herbs", "fiber", "stone", "copper_ore", "coal",
                     "sand", "clay")
# Bible §2.1: exactly 11 raw resources in the tier table (canonical order).
RAW_RESOURCES = ("timber", "stone", "clay", "sand", "fiber", "grain", "fruit",
                 "herbs", "iron_ore", "copper_ore", "coal")
# Gatherable = raw tier + legacy glass veins (depleting, never re-seeded).
GATHERABLE_RESOURCES = RAW_RESOURCES + ("glass",)
# Bible §2.1 refined: made at the furnace, never gathered.
REFINED_RESOURCES = ("lumber", "iron", "copper", "glass", "flour", "brick")
ALL_RESOURCES = RAW_RESOURCES + REFINED_RESOURCES

# ---- Bible §4.1 — crude tool crafting --------------------------------------
# Known on day one. Crafted once per agent (recraft after break); dedicated
# tool rows (never inventory, never traded).
CRUDE_RECIPES = {
    # recipe_id: (inputs, ap_cost)
    "crude_axe":    ({"timber": 2, "fiber": 1}, 2),
    "crude_pick":   ({"timber": 2, "iron_ore": 2}, 3),
    "crude_sickle": ({"timber": 2, "grain": 1, "fiber": 1}, 2),
    "crude_sieve":  ({"timber": 3, "fiber": 1}, 3),
}
# Which wild resources each tool works on. The crude pick also works the
# legacy desert glass veins (§2.4 ruling 1); refined glass is never gathered.
TOOL_COVERAGE = {
    "crude_axe":    ("timber",),
    "crude_pick":   ("stone", "iron_ore", "copper_ore", "coal", "clay", "glass"),
    "crude_sickle": ("grain", "fruit", "fiber", "herbs"),
    "crude_sieve":  ("sand",),
}
TOOL_DURABILITY_CRUDE = 120
TOOL_DURABILITY_DISCOVERED = 300

# Gather modes (§3.1 / §4.1): bare hands cost 4 AP for 1; a matching tool
# costs 2 AP for 2. GATHER_COST is the legacy alias kept for compatibility.
GATHER_BARE_AP = 4
GATHER_BARE_YIELD = 1
GATHER_TOOLED_AP = 2
GATHER_TOOLED_YIELD = 2
# Season adjustment applies to the tooled yield only (§11). The season clock
# itself lands in ch.11; until then this returns 0.
SEASON_GATHER_ADJ = {"spring": 0, "summer": 1, "autumn": 0, "winter": -1}

# ---- Bible §4.2 — hidden recipe discovery -----------------------------------
# Experiments may only use these canonical items (never refined goods).
DISCOVERY_CANONICAL_ITEMS = ["glass", "grain", "iron_ore", "timber"]
# The 12 discoverable effects, in the fixed order the genesis draw assigns
# them to combinations. Bounty passives add +1 gather yield on their
# resource while owned; cart/ap_boon are already wired into the caps;
# plow/far_speaker/climbing_gear/deft_hands/iron_lungs/signal_doctrine are
# durable passive tools whose systems land in later chapters.
DISCOVERED_EFFECTS = [
    {"recipe_id": "plow",          "description": "+1 farm yield while owned"},
    {"recipe_id": "far_speaker",   "description": "long-range voice while owned"},
    {"recipe_id": "cart",          "description": "inventory cap 149 while owned"},
    {"recipe_id": "climbing_gear", "description": "ignore mountain move penalty while owned"},
    {"recipe_id": "ap_boon",       "description": "+10 AP cap while owned"},
    {"recipe_id": "timber_bounty", "description": "+1 timber gather yield while owned"},
    {"recipe_id": "ore_bounty",    "description": "+1 iron_ore gather yield while owned"},
    {"recipe_id": "grain_bounty",  "description": "+1 grain gather yield while owned"},
    {"recipe_id": "glass_bounty",  "description": "+1 glass gather yield while owned"},
    {"recipe_id": "deft_hands",    "description": "crafting never fails while owned"},
    {"recipe_id": "iron_lungs",    "description": "ignore ocean-adjacent AP tax while owned"},
    {"recipe_id": "signal_doctrine","description": "herald relay access while owned"},
]
# resource -> bounty tool that adds +1 gather yield while owned
BOUNTY_TOOL_FOR_RESOURCE = {
    "timber": "timber_bounty",
    "iron_ore": "ore_bounty",
    "grain": "grain_bounty",
    "glass": "glass_bounty",
}
# AP costs. Crude costs come from CRUDE_RECIPES; a hidden-recipe craft costs
# 5 AP (Bible §4.1 leaves discovered craft AP unspecified — judgment call,
# documented); an experiment costs 2 AP plus its materials.
DISCOVERED_CRAFT_AP = 5
EXPERIMENT_AP = 2
RECIPE_DISCOVERY_SEED = "emerovia-discovery-v1"

# ---- Bible §5 — refining + §6 buildings -------------------------------------
# Refinery: 1 AP-batch... each recipe turns raw inputs into exactly 1
# refined unit. A furnace (owned structure) is required.
REFINERY_RECIPES = {
    # item: (inputs, ap_cost)
    "lumber": ({"timber": 2}, 4),
    "iron":   ({"iron_ore": 2}, 6),
    "copper": ({"copper_ore": 2}, 6),
    "glass":  ({"sand": 4}, 8),
    "flour":  ({"grain": 3}, 2),
    "brick":  ({"clay": 4}, 4),
}
# Functional structures: fixed material + AP costs (§6). Flavor structures
# (any other kind) cost 5 timber + 5 AP — Bible §6 names no flavor cost,
# so this is a documented judgment call against free-structure spam.
STRUCTURE_DEFS = {
    "furnace": ({"stone": 10}, 10),
    "mill":    ({"timber": 10}, 10),
    "shelter": ({"timber": 8}, 8),
}
FLAVOR_STRUCTURE_INPUTS = {"timber": 5}
FLAVOR_STRUCTURE_AP = 5
BUILDABLE_FUNCTIONAL = ("furnace", "mill", "shelter")
# Land claims: 6 per agent, claimed within a 3-tile (Chebyshev) radius of
# the agent, 2 AP per claim action. Claims are permanent (no release).
CLAIM_MAX = 6
CLAIM_RADIUS = 3
CLAIM_AP = 2


def _tithe_week(now_ts: float) -> int:
    """7-day tithe weeks since the unix epoch — the upkeep clock (§8)."""
    return int(now_ts // 604800)
# Chits are simulation credits (see server/app.py): valueless, transferable
# only inside trade offers, never redeemable.
CHITS_ITEM = "chits"
# Bible §2.2: everything is tradable (trade is not counted toward the ≥2
# mechanical-connections bar, but nothing is excluded from trade).
TRADE_ITEMS = ALL_RESOURCES + (CHITS_ITEM,)
# Foods for POST /eat and settlement feasts (Bible §2.6).
FOODS = ("grain", "fruit", "flour", "herbs")

GATHER_COST = 2
INVENTORY_CAP = 99
# Stock seed: 5 + (sha256("x,y,resource,agent-commons-genesis-v1") mod 6).
STOCK_SEED_MIN = 5
STOCK_SEED_MOD = 6

# ---- Bible §3 — depletion + regrow -----------------------------------------
# Wild tiles regrow 1 unit per (tile, resource) per 7 days, capped at the
# seeded max. Lazy: tracked in tile_regrow, applied on gather — never a sweep.
REGROW_SECONDS = 604800
REGROW_UNITS = 1


# ---- Bible §2.4 ruling 3 — overlay re-seed --------------------------------
# Deterministic under a seed DISTINCT from the genesis stock seed, so the two
# passes are uncorrelated. Exactly one overlay resource per land tile,
# terrain-typed. Overlay pick draw: u = sha256("x,y,pick,<seed>") selects by
# cumulative weight; stock: banded sha256("x,y,resource,<seed>").
NATURAL_SEED = "emerovia-natural-v1"
OVERLAY_RULES = {
    "plains": (("fruit", 50), ("herbs", 50)),
    "forest": (("fiber", 100),),
    "mountain": (("stone", 40), ("coal", 30), ("copper_ore", 30)),
    "desert": (("sand", 70), ("clay", 30)),
}
# Bible §3.1 stock bands: (min, inclusive_mod) per resource.
OVERLAY_STOCK_BANDS = {
    "fruit": (5, 6), "herbs": (5, 6), "fiber": (5, 6),          # food & fiber 5-10
    "stone": (6, 7), "sand": (6, 7), "clay": (6, 7),            # bulk 6-12
    "copper_ore": (6, 5),                                       # scarce mineral 6-10
    "coal": (8, 5),                                             # fuel mineral 8-12
}

TERRAINS = ("ocean", "plains", "forest", "desert", "mountain")
LAND_TERRAINS = ("plains", "forest", "desert", "mountain")

DIRS = {"N": (0, -1), "S": (0, 1), "E": (1, 0), "W": (-1, 0)}

LEGEND = {
    "ocean": {"color": "#1e40af", "label": "Ocean — impassable"},
    "plains": {"color": "#86c06c", "label": "Plains"},
    "forest": {"color": "#2d6a4f", "label": "Forest"},
    "desert": {"color": "#e9c46a", "label": "Desert"},
    "mountain": {"color": "#6b7280", "label": "Mountain — 2 AP"},
}

AP_RULES = {
    "start": AP_START,
    "cap": AP_CAP,
    "regen_per_minute": 1,
    "move_cost_land": MOVE_COST_LAND,
    "move_cost_mountain": MOVE_COST_MOUNTAIN,
    "disclose_cost": DISCLOSE_COST,
    "spawn_cost": 0,
    "gather_cost": GATHER_COST,
    "inventory_cap": INVENTORY_CAP,
    "terrain_resource": TERRAIN_RESOURCE,
}

# ---- pure helpers -----------------------------------------------------


def _lattice(seed: str, octave: int, ix: int, iy: int) -> float:
    """Deterministic pseudo-random value in [0, 1) for a lattice point.

    SHA-256 keyed on (seed, octave, lattice coords) so the sequence is
    stable across Python versions — unlike random.Random, whose seeding
    is not guaranteed stable.
    """
    digest = hashlib.sha256(f"{seed}|{octave}|{ix}|{iy}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / 18446744073709551616.0


def _smootherstep(t: float) -> float:
    return t * t * (3.0 - 2.0 * t)


def _value_noise(seed: str, octave: int, x: float, y: float) -> float:
    """Bilinearly interpolated value noise in [0, 1]."""
    ix, iy = math.floor(x), math.floor(y)
    fx, fy = x - ix, y - iy
    a = _lattice(seed, octave, ix, iy)
    b = _lattice(seed, octave, ix + 1, iy)
    c = _lattice(seed, octave, ix, iy + 1)
    d = _lattice(seed, octave, ix + 1, iy + 1)
    sx, sy = _smootherstep(fx), _smootherstep(fy)
    top = a + (b - a) * sx
    bot = c + (d - c) * sx
    return top + (bot - top) * sy


def generate_world_terrain(seed: str) -> dict[tuple[int, int], str]:
    """Generate the full WORLD_SIZE x WORLD_SIZE terrain map for a seed.

    Pure function: same seed ALWAYS yields the identical map. Two-octave
    seeded value noise produces a height field and a moisture field;
    low height is ocean (impassable), high height is mountain, and the
    rest of land is split into desert/forest/plains by moisture.
    """
    terrain: dict[tuple[int, int], str] = {}
    for y in range(WORLD_SIZE):
        for x in range(WORLD_SIZE):
            height = 0.65 * _value_noise(seed, 0, x / 14.0, y / 14.0) + 0.35 * _value_noise(
                seed, 1, x / 6.0, y / 6.0
            )
            moisture = _value_noise(seed, 2, x / 10.0 + 100.0, y / 10.0 + 100.0)
            if height < 0.44:
                tile = "ocean"
            elif height >= 0.70:
                tile = "mountain"
            elif moisture < 0.42:
                tile = "desert"
            elif moisture > 0.60:
                tile = "forest"
            else:
                tile = "plains"
            terrain[(x, y)] = tile
    return terrain


def ap_after_regen(ap: int, cap: int, last_update_ts: float, now_ts: float) -> int:
    """Pure AP regeneration: floor(elapsed_seconds / 60), capped at cap.

    Never drops AP (negative elapsed from clock skew is clamped to zero
    gain). Never exceeds cap. The caller persists last_update = now_ts.
    """
    elapsed = now_ts - last_update_ts
    if elapsed < 0:
        elapsed = 0
    gained = int(elapsed // AP_REGEN_SECONDS)
    return min(cap, ap + gained)


# ---- errors -----------------------------------------------------------


class WorldError(Exception):
    """Carries an HTTP status + JSON body for world endpoint failures."""

    status_code = 400

    def __init__(self, detail: str, extra: dict | None = None):
        super().__init__(detail)
        self.detail = detail
        self.extra = extra or {}


class AlreadySpawned(WorldError):
    status_code = 409

    def __init__(self):
        super().__init__("already spawned")


class NotSpawned(WorldError):
    status_code = 400

    def __init__(self):
        super().__init__("not spawned")


class BadMove(WorldError):
    status_code = 400


class InsufficientAP(WorldError):
    status_code = 402

    def __init__(self, ap: int, cost: int):
        super().__init__(
            "insufficient AP",
            {"deficit": cost - ap, "ap": ap},
        )


class UnknownDiscovery(WorldError):
    status_code = 404

    def __init__(self):
        super().__init__("tile not discovered")


# ---- Stage 4 economy errors --------------------------------------------


class NothingToGather(WorldError):
    """Ocean tiles (and other resourceless terrain) yield nothing."""

    status_code = 400

    def __init__(self, detail: str = "nothing to gather here"):
        super().__init__(detail)


class UnknownTool(WorldError):
    status_code = 400

    def __init__(self, detail: str = "unknown tool"):
        super().__init__(detail)


class UnknownRecipe(WorldError):
    status_code = 404

    def __init__(self, detail: str = "unknown recipe"):
        super().__init__(detail)


class RecipeNotDiscovered(WorldError):
    status_code = 404

    def __init__(self, detail: str = "recipe not yet discovered"):
        super().__init__(detail)


class AlreadyCrafted(WorldError):
    status_code = 400

    def __init__(self, detail: str = "already crafted"):
        super().__init__(detail)


class InsufficientMaterials(WorldError):
    status_code = 400

    def __init__(self, detail: str = "insufficient materials"):
        super().__init__(detail)


class ToolNotOwned(WorldError):
    status_code = 400

    def __init__(self, detail: str = "tool not owned"):
        super().__init__(detail)


class TileDepleted(WorldError):
    status_code = 400

    def __init__(self, detail: str = "tile depleted"):
        super().__init__(detail)


class SpecifyResource(WorldError):
    """Tile bears two resources (legacy + overlay): the gather must name one."""

    status_code = 400

    def __init__(self, resources: list[str]):
        super().__init__(
            "specify resource",
            {"resources": sorted(resources)},
        )


class InventoryFull(WorldError):
    status_code = 400

    def __init__(self, resource: str):
        super().__init__(
            f"inventory cap reached for {resource}",
            {"resource": resource, "cap": INVENTORY_CAP},
        )


# ---- DB helpers (take the create_app() connect factory) ---------------


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def seed_world_if_empty(conn: sqlite3.Connection) -> int:
    """Generate and store the world from SEED_ID if world_tiles is empty.

    Deterministic across reboots: re-running against an already-seeded
    DB is a no-op. Returns the number of tiles inserted.
    """
    existing = conn.execute("SELECT COUNT(*) FROM world_tiles").fetchone()[0]
    if existing:
        return 0
    terrain = generate_world_terrain(SEED_ID)
    conn.executemany(
        "INSERT INTO world_tiles (x, y, terrain) VALUES (?, ?, ?)",
        [(x, y, t) for (x, y), t in sorted(terrain.items())],
    )
    return len(terrain)


def _get_state(conn: sqlite3.Connection, agent_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM agent_world WHERE agent_id = ?", (agent_id,)
    ).fetchone()


def _regen(conn: sqlite3.Connection, agent_id: int, now_ts: float) -> dict:
    """Regen AP and persist last_update=now. Returns the fresh state dict.

    Raises NotSpawned when the agent has no world presence.
    """
    row = _get_state(conn, agent_id)
    if row is None:
        raise NotSpawned()
    ap = int(row["ap"])
    last = float(row["last_update"])
    pubkey = conn.execute(
        "SELECT pubkey FROM agents WHERE id = ?", (agent_id,)
    ).fetchone()["pubkey"]
    cap = effective_ap_cap(conn, agent_id, pubkey, now_ts)
    new_ap = ap_after_regen(ap, cap, last, now_ts)
    elapsed = max(0.0, now_ts - last)
    if new_ap >= cap:
        seconds_until_next_ap = 0
    else:
        rem = elapsed % AP_REGEN_SECONDS
        seconds_until_next_ap = int(AP_REGEN_SECONDS - rem) if rem > 0 else AP_REGEN_SECONDS
    conn.execute(
        "UPDATE agent_world SET ap = ?, last_update = ? WHERE agent_id = ?",
        (float(new_ap), now_ts, agent_id),
    )
    return {
        "x": row["x"],
        "y": row["y"],
        "ap": new_ap,
        "seconds_until_next_ap": seconds_until_next_ap,
    }


def _tile_terrain(conn: sqlite3.Connection, x: int, y: int) -> str | None:
    row = conn.execute(
        "SELECT terrain FROM world_tiles WHERE x = ? AND y = ?", (x, y)
    ).fetchone()
    return row["terrain"] if row else None


# ---- Stage 4 — resource stock + gathering -------------------------------


def _legacy_seed_name(resource: str) -> str:
    """The resource name the ORIGINAL Stage 4 seed hash was computed with.

    Bible §2.4 ruling 2 renames ore→iron_ore 1:1. Rows seeded before the
    rename hashed "ore"; fresh-DB rows are seeded with the same hash so the
    regrow cap (also computed from the original name) always matches the
    seeded stock exactly, on old and new DBs alike.
    """
    return "ore" if resource == "iron_ore" else resource


def stock_for_tile(x: int, y: int, resource: str) -> int:
    """Deterministic seed stock 5..10 for a legacy tile/resource pair.

    Computed from the ORIGINAL Stage 4 resource name (ore, not iron_ore) so
    the value matches rows seeded before the Bible rename — and fresh rows
    seeded after it. The regrow cap reuses this exact function.
    """
    digest = hashlib.sha256(
        f"{x},{y},{_legacy_seed_name(resource)},{SEED_ID}".encode("utf-8")
    ).hexdigest()
    return STOCK_SEED_MIN + (int(digest, 16) % STOCK_SEED_MOD)


def overlay_resource_for_tile(x: int, y: int, terrain: str) -> str | None:
    """Deterministic overlay resource for a land tile (Bible §2.4 ruling 3).

    Pure function of (x, y) under NATURAL_SEED — a seed distinct from the
    genesis stock seed, so the overlay pass is uncorrelated with the legacy
    pass. Returns None for ocean / unknown terrain (no overlay row).
    """
    rules = OVERLAY_RULES.get(terrain)
    if not rules:
        return None
    u = (
        int.from_bytes(
            hashlib.sha256(f"{x},{y},pick,{NATURAL_SEED}".encode("utf-8")).digest()[:8],
            "big",
        )
        / 18446744073709551616.0
    )
    total = sum(w for _, w in rules)
    cutoff = u * total
    acc = 0
    for resource, weight in rules:
        acc += weight
        if cutoff < acc:
            return resource
    return rules[-1][0]


def overlay_stock_for_tile(x: int, y: int, resource: str) -> int:
    """Deterministic overlay seed stock under NATURAL_SEED, banded per §3.1."""
    band_min, band_mod = OVERLAY_STOCK_BANDS[resource]
    digest = hashlib.sha256(
        f"{x},{y},{resource},{NATURAL_SEED}".encode("utf-8")
    ).hexdigest()
    return band_min + (int(digest, 16) % band_mod)


def _all_discovery_combinations() -> list[tuple[str, dict[str, int]]]:
    """All 352 legal experiment combinations: 2-3 distinct canonical items,
    each quantity 1-4. Returns (canonical_key, inputs) in canonical order."""
    items = DISCOVERY_CANONICAL_ITEMS
    combos: list[tuple[str, dict[str, int]]] = []
    for r in (2, 3):
        for chosen in itertools.combinations(items, r):
            for qtys in itertools.product((1, 2, 3, 4), repeat=r):
                inputs = dict(zip(chosen, qtys))
                key = "+".join(f"{item}:{inputs[item]}" for item in sorted(inputs))
                combos.append((key, inputs))
    combos.sort(key=lambda c: c[0])
    return combos


def generate_hidden_recipes() -> list[dict]:
    """Genesis draw: 12 of the 352 combinations, deterministic under
    RECIPE_DISCOVERY_SEED, assigned the 12 effects in fixed order.

    Pure function — the same 12 rows every run, so genesis is reproducible
    and tests can predict them.
    """
    combos = _all_discovery_combinations()
    assert len(combos) == 352, f"expected 352 discovery combos, got {len(combos)}"
    ranked = sorted(
        combos,
        key=lambda c: (
            hashlib.sha256(f"{c[0]},{RECIPE_DISCOVERY_SEED}".encode()).hexdigest(),
            c[0],
        ),
    )
    recipes = []
    for (key, inputs), effect in zip(ranked[:12], DISCOVERED_EFFECTS):
        recipes.append(
            {
                "recipe_id": effect["recipe_id"],
                "inputs_json": key,
                "effect_json": json.dumps(effect),
            }
        )
    return recipes


def seed_hidden_recipes(conn: sqlite3.Connection) -> int:
    """Insert the 12 genesis hidden recipes (inventor NULL = undiscovered).

    Idempotent: INSERT OR IGNORE, so re-runs insert nothing.
    """
    recipes = generate_hidden_recipes()
    cur = conn.executemany(
        "INSERT OR IGNORE INTO recipes_hidden"
        " (recipe_id, inputs_json, effect_json, inventor_pubkey, inventor_name,"
        "  discovered_at) VALUES (?, ?, ?, NULL, NULL, NULL)",
        [(r["recipe_id"], r["inputs_json"], r["effect_json"]) for r in recipes],
    )
    return cur.rowcount


def seed_resource_overlay(conn: sqlite3.Connection) -> int:
    """Additive overlay re-seed (Bible §2.4 ruling 3): new rows only.

    Exactly one overlay resource per land tile, terrain-typed, deterministic
    under NATURAL_SEED. INSERT OR IGNORE semantics: existing (x,y,resource)
    rows are no-ops, no existing assignment moves, no current yield changes.
    Idempotent: a second run inserts nothing. Returns rows inserted.
    """
    conn.row_factory = sqlite3.Row
    tiles = conn.execute(
        "SELECT x, y, terrain FROM world_tiles WHERE terrain != 'ocean'"
    ).fetchall()
    rows = []
    for t in tiles:
        resource = overlay_resource_for_tile(t["x"], t["y"], t["terrain"])
        if resource is None:
            continue
        rows.append(
            (t["x"], t["y"], resource, overlay_stock_for_tile(t["x"], t["y"], resource))
        )
    if not rows:
        return 0
    before = conn.execute("SELECT COUNT(*) FROM world_resource_stock").fetchone()[0]
    conn.executemany(
        "INSERT OR IGNORE INTO world_resource_stock (x, y, resource, stock)"
        " VALUES (?, ?, ?, ?)",
        rows,
    )
    after = conn.execute("SELECT COUNT(*) FROM world_resource_stock").fetchone()[0]
    return after - before


def migrate_resources_to_bible(conn: sqlite3.Connection) -> dict:
    """Bible §2.4 rulings 1-2 as data migration. Idempotent.

    - Ruling 2: legacy "ore" rows become "iron_ore" 1:1 (a rename, not a
      revaluation) in world_resource_stock, inventories, and open
      trade_offers JSON. The trade LEDGER is append-only history: old rows
      keep saying "ore" verbatim (never touched here).
    - Ruling 1: legacy desert "glass" rows stay as-is — they are the last
      wild glass veins, gatherable with the pick until depleted. No new
      glass stock is ever seeded (glass now comes from the furnace).
    Returns counts of renamed rows for observability.
    """
    conn.row_factory = sqlite3.Row
    counts = {"stock": 0, "inventories": 0, "offers": 0}
    cur = conn.execute(
        "UPDATE world_resource_stock SET resource = 'iron_ore' WHERE resource = 'ore'"
    )
    counts["stock"] = cur.rowcount
    # Inventories: "iron_ore" cannot pre-exist (new name), but merge
    # defensively in case a row does.
    for row in conn.execute(
        "SELECT agent_pubkey, qty FROM inventories WHERE resource = 'ore'"
    ).fetchall():
        pubkey, qty = row["agent_pubkey"], int(row["qty"])
        try:
            conn.execute(
                "UPDATE inventories SET resource = 'iron_ore'"
                " WHERE agent_pubkey = ? AND resource = 'ore'",
                (pubkey,),
            )
            counts["inventories"] += 1
        except sqlite3.IntegrityError:
            conn.execute(
                "UPDATE inventories SET qty = qty + ?"
                " WHERE agent_pubkey = ? AND resource = 'iron_ore'",
                (qty, pubkey),
            )
            conn.execute(
                "DELETE FROM inventories WHERE agent_pubkey = ? AND resource = 'ore'",
                (pubkey,),
            )
            counts["inventories"] += 1
    # Open trade offers are live state, not history: rename in their JSON.
    import json as _json

    for row in conn.execute(
        "SELECT id, give_json, want_json FROM trade_offers WHERE status = 'open'"
    ).fetchall():
        give = _json.loads(row["give_json"])
        want = _json.loads(row["want_json"])
        changed = False
        for side in (give, want):
            if "ore" in side:
                side["iron_ore"] = side.pop("ore")
                changed = True
        if changed:
            conn.execute(
                "UPDATE trade_offers SET give_json = ?, want_json = ? WHERE id = ?",
                (
                    _json.dumps(dict(sorted(give.items())), separators=(",", ":")),
                    _json.dumps(dict(sorted(want.items())), separators=(",", ":")),
                    row["id"],
                ),
            )
            counts["offers"] += 1
    return counts


def seed_resource_stock(conn: sqlite3.Connection) -> int:
    """Seed world_resource_stock from existing terrain, once.

    Every non-ocean tile gets a row per its terrain's resource with
    deterministic stock 5..10. Ocean tiles get no rows. Idempotent:
    a no-op when the table already has rows. Returns rows inserted.
    """
    existing = conn.execute("SELECT COUNT(*) FROM world_resource_stock").fetchone()[0]
    if existing:
        return 0
    tiles = conn.execute(
        "SELECT x, y, terrain FROM world_tiles WHERE terrain != 'ocean'"
    ).fetchall()
    rows = [
        (x, y, TERRAIN_RESOURCE[terrain], stock_for_tile(x, y, TERRAIN_RESOURCE[terrain]))
        for (x, y, terrain) in tiles
        if terrain in TERRAIN_RESOURCE
    ]
    conn.executemany(
        "INSERT OR IGNORE INTO world_resource_stock (x, y, resource, stock)"
        " VALUES (?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def _present_resources(conn: sqlite3.Connection, x: int, y: int) -> dict:
    """Live stock rows on a tile: {resource: stock} for stock > 0."""
    return {
        r["resource"]: int(r["stock"])
        for r in conn.execute(
            "SELECT resource, stock FROM world_resource_stock"
            " WHERE x = ? AND y = ? AND stock > 0",
            (x, y),
        ).fetchall()
    }


def _resolve_gather_resource(
    conn: sqlite3.Connection, x: int, y: int, resource: str | None
) -> str:
    """Bible §2.4 ruling 3: optional {resource} disambiguation.

    Omitted + one resource present → that one (backward compatible);
    omitted + two present → 400 naming both; named but absent/depleted → 400.
    """
    present = _present_resources(conn, x, y)
    if resource is None:
        if not present:
            any_rows = conn.execute(
                "SELECT 1 FROM world_resource_stock WHERE x = ? AND y = ?",
                (x, y),
            ).fetchone()
            raise TileDepleted() if any_rows else NothingToGather()
        if len(present) > 1:
            raise SpecifyResource(list(present))
        return next(iter(present))
    if resource not in GATHERABLE_RESOURCES:
        raise NothingToGather(f"unknown gatherable resource {resource!r}")
    if resource not in present:
        raise TileDepleted(f"tile depleted: no {resource} to gather here")
    return resource


def _owns_tool(conn: sqlite3.Connection, pubkey: str, recipe_id: str) -> bool:
    """Dedicated tool rows: presence in tools = owned (passive effects)."""
    conn.row_factory = sqlite3.Row
    return (
        conn.execute(
            "SELECT 1 FROM tools WHERE agent_pubkey = ? AND recipe_id = ?",
            (pubkey, recipe_id),
        ).fetchone()
        is not None
    )


def inventory_cap(conn: sqlite3.Connection, pubkey: str) -> int:
    """Effective per-item inventory cap: 149 with a cart tool, else 99 (§4.2)."""
    return 149 if _owns_tool(conn, pubkey, "cart") else 99


def effective_ap_cap(conn: sqlite3.Connection, agent_id: int, pubkey: str,
                     now_ts: float) -> int:
    """AP cap: 100 base +10 active shelter +10 ap_boon +10 active feast (§8/§9).

    Shelters exist from ch.5; derelict filtering lands with upkeep in ch.8.
    ap_boon (ch.4) and feast buffs (ch.9) have no rows until those chapters.
    """
    conn.row_factory = sqlite3.Row
    cap = AP_CAP
    if (
        conn.execute(
            "SELECT 1 FROM structures WHERE owner_pubkey = ? AND kind = 'shelter'"
            " LIMIT 1",
            (pubkey,),
        ).fetchone()
        is not None
    ):
        cap += 10
    if _owns_tool(conn, pubkey, "ap_boon"):
        cap += 10
    if (
        conn.execute(
            "SELECT 1 FROM feast_buffs WHERE agent_pubkey = ? AND expires_at > ?",
            (pubkey, now_ts),
        ).fetchone()
        is not None
    ):
        cap += 10
    return cap


def _resolve_gather_tool(conn: sqlite3.Connection, pubkey: str,
                         tool: str | None, resource: str) -> str | None:
    """Pick the tool recipe_id for a gather, or None for bare hands (§4.1).

    Explicit unknown recipe_id → 400. Owned-but-not-covering → bare hands
    (the tool is NOT worn). A passive discovered tool named explicitly has
    no coverage, so it also falls back to bare hands. Omitted → the covering
    owned tool with the highest durability (ties: lowest recipe_id).
    """
    conn.row_factory = sqlite3.Row
    if tool is not None:
        if tool not in TOOL_COVERAGE:
            if tool not in CRUDE_RECIPES and not _owns_tool(conn, pubkey, tool):
                raise UnknownTool(f"unknown tool {tool!r}")
            return None
        row = conn.execute(
            "SELECT durability FROM tools WHERE agent_pubkey = ? AND recipe_id = ?",
            (pubkey, tool),
        ).fetchone()
        if row is None:
            raise ToolNotOwned(f"tool not owned: {tool!r}")
        if resource not in TOOL_COVERAGE[tool]:
            return None  # mismatched tool: bare hands, no wear
        return tool
    best: tuple[int, str] | None = None
    for tool_id, covers in TOOL_COVERAGE.items():
        if resource not in covers:
            continue
        row = conn.execute(
            "SELECT durability FROM tools WHERE agent_pubkey = ? AND recipe_id = ?",
            (pubkey, tool_id),
        ).fetchone()
        if row is None:
            continue
        cand = (int(row["durability"]), tool_id)
        if best is None or cand[0] > best[0] or (cand[0] == best[0] and cand[1] < best[1]):
            best = cand
    return best[1] if best else None


def _wear_tool(conn: sqlite3.Connection, pubkey: str, tool_id: str) -> bool:
    """Wear one durability off a tool; delete the row at 0 (§4.1).

    Returns True when the tool broke on this gather (the gather itself is
    still valid). Row-count guard keeps it atomic under concurrency.
    """
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "UPDATE tools SET durability = durability - 1"
        " WHERE agent_pubkey = ? AND recipe_id = ? AND durability > 0"
        " RETURNING durability",
        (pubkey, tool_id),
    ).fetchone()
    if row is None:
        return True  # raced to zero — treat as broken; shouldn't happen
    if int(row["durability"]) <= 0:
        conn.execute(
            "DELETE FROM tools WHERE agent_pubkey = ? AND recipe_id = ?",
            (pubkey, tool_id),
        )
        return True
    return False


def _season_gather_adj(now_ts: float) -> int:
    """Tooled-yield season adjustment (§11). Stub until the ch.11 season clock."""
    return 0


def _gather_yield(conn: sqlite3.Connection, pubkey: str, resource: str,
                  tooled: bool, now_ts: float) -> int:
    """Units per gather before the stock clamp.

    Bare hands: flat 1. Tooled: 2 + season adj (tooled only). Mill timber
    +1 (ch.5) and discovered bounty passives (ch.4) extend this function
    when those chapters land.
    """
    if not tooled:
        return GATHER_BARE_YIELD
    y = max(1, GATHER_TOOLED_YIELD + _season_gather_adj(now_ts))
    # Mill +1 timber while the owner holds a mill (ch.5). Derelict
    # filtering lands with upkeep in ch.8.
    if resource == "timber":
        mill = conn.execute(
            "SELECT 1 FROM structures WHERE owner_pubkey = ? AND kind = 'mill'"
            " LIMIT 1",
            (pubkey,),
        ).fetchone()
        if mill is not None:
            y += 1
    # Discovered bounty passives (ch.4): +1 on their resource while owned.
    bounty = BOUNTY_TOOL_FOR_RESOURCE.get(resource)
    if bounty is not None and _owns_tool(conn, pubkey, bounty):
        y += 1
    return y


def _seeded_max(x: int, y: int, resource: str) -> int:
    """Regrow cap: the seeded max for a (tile, resource) stock row.

    Legacy rows use the genesis-seed function (with the original ore name);
    overlay rows use the NATURAL_SEED banded function. The two resource sets
    are disjoint, so membership alone disambiguates.
    """
    if resource in OVERLAY_RESOURCES:
        return overlay_stock_for_tile(x, y, resource)
    return stock_for_tile(x, y, resource)


def _apply_regrow_tile(conn: sqlite3.Connection, x: int, y: int, now_ts: float) -> None:
    """Apply the regrow trickle to every stock row on one tile.

    Runs before resource resolution so a fully-stripped row that has earned
    regrow is visible again. Bounded: at most two rows per tile (legacy +
    overlay). Still lazy — only the tile being gathered is touched.
    """
    conn.row_factory = sqlite3.Row
    resources = [
        r["resource"]
        for r in conn.execute(
            "SELECT resource FROM world_resource_stock WHERE x = ? AND y = ?", (x, y)
        ).fetchall()
    ]
    for resource in resources:
        _apply_regrow_row(conn, x, y, resource, now_ts)


def _apply_regrow_row(
    conn: sqlite3.Connection, x: int, y: int, resource: str, now_ts: float
) -> None:
    """Bible §3.2: lazy regrow, 1 unit per (tile, resource) per 7 days.

    Applied on gather, inside the caller's transaction: floor((now -
    last_touch) / 604800) units are added, capped at the seeded max, and
    last_touch advances to now. The first touch after v1.2.0 only starts the
    clock (no retroactive refill): a tile stripped long ago does not refill
    on deploy — the trickle starts being tracked from first contact.
    """
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT last_touch FROM tile_regrow WHERE x = ? AND y = ? AND resource = ?",
        (x, y, resource),
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT OR IGNORE INTO tile_regrow (x, y, resource, last_touch)"
            " VALUES (?, ?, ?, ?)",
            (x, y, resource, now_ts),
        )
        return
    last_touch = float(row["last_touch"])
    units = int((now_ts - last_touch) // REGROW_SECONDS) * REGROW_UNITS
    if units > 0:
        cap = _seeded_max(x, y, resource)
        conn.execute(
            "UPDATE world_resource_stock SET stock = MIN(?, stock + ?)"
            " WHERE x = ? AND y = ? AND resource = ?",
            (cap, units, x, y, resource),
        )
    conn.execute(
        "UPDATE tile_regrow SET last_touch = ? WHERE x = ? AND y = ? AND resource = ?",
        (now_ts, x, y, resource),
    )


def gather(
    connect,
    agent_id: int,
    now_ts: float,
    resource: str | None = None,
    tool: str | None = None,
) -> dict:
    """Gather wild resources. Bible §3.1 + §4.1.

    Bare hands: 4 AP → 1 unit. A matching tool: 2 AP → 2 units. The optional
    ``tool`` is a recipe_id — unknown → 400; owned but mismatched → bare
    hands (the tool is NOT worn); omitted → the covering tool with the
    highest durability (ties: lowest recipe_id). Each gather wears the used
    tool 1 durability; at 0 the tool breaks (row deleted) and the gather
    stays valid. Regrow (§3.2) applies first, in-transaction.

    Atomic: regrow, AP, stock, inventory, and wear commit together.
    Per-item inventory cap is 99 (149 with a cart, §4.2). Insufficient AP
    is 402; depleted tiles refuse (400).
    """
    conn = connect()
    try:
        st = _regen(conn, agent_id, now_ts)
        _apply_regrow_tile(conn, st["x"], st["y"], now_ts)
        resource = _resolve_gather_resource(conn, st["x"], st["y"], resource)
        pubkey = conn.execute(
            "SELECT pubkey FROM agents WHERE id = ?", (agent_id,)
        ).fetchone()["pubkey"]
        tool_id = _resolve_gather_tool(conn, pubkey, tool, resource)
        tooled = tool_id is not None
        cost = GATHER_TOOLED_AP if tooled else GATHER_BARE_AP
        if st["ap"] < cost:
            raise InsufficientAP(st["ap"], cost)
        take = _gather_yield(conn, pubkey, resource, tooled, now_ts)
        inv = conn.execute(
            "SELECT qty FROM inventories WHERE agent_pubkey = ? AND resource = ?",
            (pubkey, resource),
        ).fetchone()
        have = int(inv["qty"]) if inv is not None else 0
        if have + take > inventory_cap(conn, pubkey):
            raise InventoryFull(resource)
        # Clamp the take to what's actually on the tile; the rowcount guard
        # keeps the decrement atomic under concurrency.
        before = conn.execute(
            "SELECT stock FROM world_resource_stock"
            " WHERE x = ? AND y = ? AND resource = ?",
            (st["x"], st["y"], resource),
        ).fetchone()["stock"]
        gained = min(take, int(before))
        cur = conn.execute(
            "UPDATE world_resource_stock SET stock = stock - ?"
            " WHERE x = ? AND y = ? AND resource = ? AND stock >= ?",
            (gained, st["x"], st["y"], resource, gained),
        )
        if cur.rowcount == 0:
            raise TileDepleted()
        tool_broke = _wear_tool(conn, pubkey, tool_id) if tooled else False
        new_ap = st["ap"] - cost
        conn.execute(
            "UPDATE agent_world SET ap = ?, last_update = ? WHERE agent_id = ?",
            (float(new_ap), now_ts, agent_id),
        )
        conn.execute(
            "INSERT INTO inventories (agent_pubkey, resource, qty)"
            " VALUES (?, ?, ?)"
            " ON CONFLICT(agent_pubkey, resource) DO UPDATE SET qty = qty + ?",
            (pubkey, resource, gained, gained),
        )
        conn.commit()
        return {
            "resource": resource,
            "gained": gained,
            "stock_remaining": int(before) - gained,
            "ap": new_ap,
            "x": st["x"],
            "y": st["y"],
            "tool": tool_id,
            "tooled": tooled,
            "tool_broke": tool_broke,
        }
    finally:
        conn.close()


def spawn(connect, agent_id: int, agent_name: str, now_ts: float) -> dict:
    """Spawn the agent on a random unoccupied LAND tile. Free. One per agent."""
    conn = connect()
    try:
        if _get_state(conn, agent_id) is not None:
            raise AlreadySpawned()
        land = conn.execute(
            "SELECT x, y, terrain FROM world_tiles WHERE terrain != 'ocean'"
        ).fetchall()
        occupied = {
            (r["x"], r["y"])
            for r in conn.execute("SELECT x, y FROM agent_world").fetchall()
        }
        free = [r for r in land if (r["x"], r["y"]) not in occupied]
        if not free:
            raise WorldError(409, "no spawn tiles available")
        pick = random.SystemRandom().choice(free)
        x, y, terrain = pick["x"], pick["y"], pick["terrain"]
        conn.execute(
            "INSERT INTO agent_world (agent_id, x, y, ap, last_update, spawned_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (agent_id, x, y, float(AP_START), now_ts, utcnow_iso()),
        )
        conn.execute(
            "INSERT INTO discoveries (agent_id, x, y, terrain, discovered_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (agent_id, x, y, terrain, utcnow_iso()),
        )
        conn.commit()
        return {
            "agent_name": agent_name,
            "x": x,
            "y": y,
            "terrain": terrain,
            "ap": AP_START,
            "ap_cap": AP_CAP,
        }
    finally:
        conn.close()


def move(connect, agent_id: int, direction: str, now_ts: float) -> dict:
    """Move one tile cardinally. Costs 1 AP (2 for mountain). Ocean and
    off-map moves are refused with no AP spent."""
    if direction not in DIRS:
        raise BadMove("invalid direction")
    dx, dy = DIRS[direction]
    conn = connect()
    try:
        st = _regen(conn, agent_id, now_ts)
        nx, ny = st["x"] + dx, st["y"] + dy
        if not (0 <= nx < WORLD_SIZE and 0 <= ny < WORLD_SIZE):
            raise BadMove("off map")
        terrain = _tile_terrain(conn, nx, ny)
        if terrain == "ocean":
            raise BadMove("ocean is impassable")
        cost = MOVE_COST_MOUNTAIN if terrain == "mountain" else MOVE_COST_LAND
        if st["ap"] < cost:
            raise InsufficientAP(st["ap"], cost)
        new_ap = st["ap"] - cost
        conn.execute(
            "UPDATE agent_world SET x = ?, y = ?, ap = ?, last_update = ?"
            " WHERE agent_id = ?",
            (nx, ny, float(new_ap), now_ts, agent_id),
        )
        conn.execute(
            "INSERT OR IGNORE INTO discoveries (agent_id, x, y, terrain, discovered_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (agent_id, nx, ny, terrain, utcnow_iso()),
        )
        conn.commit()
        return {"x": nx, "y": ny, "terrain": terrain, "ap": new_ap, "cost": cost}
    finally:
        conn.close()


def _consume_materials(conn: sqlite3.Connection, pubkey: str,
                        inputs: dict[str, int]) -> None:
    """Consume craft/experiment materials atomically.

    Rowcount-guarded: each UPDATE only fires when the agent holds enough,
    so concurrent consumption can't drive a balance negative. Zero-qty
    rows are cleaned up afterward.
    """
    conn.row_factory = sqlite3.Row
    for item, qty in inputs.items():
        cur = conn.execute(
            "UPDATE inventories SET qty = qty - ?"
            " WHERE agent_pubkey = ? AND resource = ? AND qty >= ?",
            (qty, pubkey, item, qty),
        )
        if cur.rowcount == 0:
            raise InsufficientMaterials(f"insufficient {item}: need {qty}")
    conn.execute(
        "DELETE FROM inventories WHERE agent_pubkey = ? AND qty <= 0", (pubkey,)
    )


def _craft_tool_row(conn: sqlite3.Connection, pubkey: str, agent_name: str,
                    recipe_id: str, durability: int, now_ts: float) -> None:
    """Insert the durable tool row. Caller has consumed inputs and AP."""
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT INTO tools (agent_pubkey, recipe_id, durability,"
        " max_durability, crafted_at) VALUES (?, ?, ?, ?, ?)",
        (
            pubkey,
            recipe_id,
            durability,
            durability,
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_ts)),
        ),
    )


def craft(connect, agent_id: int, agent_name: str, now_ts: float,
          recipe_id: str) -> dict:
    """Craft a tool. Bible §4.1.

    Crude recipes are day-one known (inputs + 2-3 AP from CRUDE_RECIPES).
    Hidden recipes are craftable only after discovery (404 before — the
    response never distinguishes hidden from nonexistent). One per agent:
    recraft only after the tool breaks. Atomic: AP + materials + tool row
    commit together; insufficient materials/AP is 400/402.
    """
    conn = connect()
    try:
        st = _regen(conn, agent_id, now_ts)
        pubkey = conn.execute(
            "SELECT pubkey FROM agents WHERE id = ?", (agent_id,)
        ).fetchone()["pubkey"]
        if recipe_id in CRUDE_RECIPES:
            inputs, ap_cost = CRUDE_RECIPES[recipe_id]
            durability = TOOL_DURABILITY_CRUDE
        else:
            row = conn.execute(
                "SELECT inputs_json, inventor_pubkey FROM recipes_hidden"
                " WHERE recipe_id = ?",
                (recipe_id,),
            ).fetchone()
            if row is None:
                raise UnknownRecipe(f"unknown recipe {recipe_id!r}")
            if row["inventor_pubkey"] is None:
                raise RecipeNotDiscovered(
                    f"recipe {recipe_id!r} not yet discovered"
                )
            inputs = {
                item: int(qty)
                for item, qty in (
                    part.split(":") for part in row["inputs_json"].split("+")
                )
            }
            ap_cost = DISCOVERED_CRAFT_AP
            durability = TOOL_DURABILITY_DISCOVERED
        if _owns_tool(conn, pubkey, recipe_id):
            raise AlreadyCrafted(
                f"{recipe_id!r} already crafted — recraft after it breaks"
            )
        if st["ap"] < ap_cost:
            raise InsufficientAP(st["ap"], ap_cost)
        _consume_materials(conn, pubkey, inputs)
        new_ap = st["ap"] - ap_cost
        conn.execute(
            "UPDATE agent_world SET ap = ?, last_update = ? WHERE agent_id = ?",
            (float(new_ap), now_ts, agent_id),
        )
        _craft_tool_row(conn, pubkey, agent_name, recipe_id, durability, now_ts)
        conn.commit()
        return {
            "recipe_id": recipe_id,
            "durability": durability,
            "ap": new_ap,
        }
    finally:
        conn.close()


def experiment(connect, agent_id: int, agent_name: str, now_ts: float,
               items: dict[str, int]) -> dict:
    """Probe a material combination for a hidden recipe. Bible §4.2.

    The combination must use 2-3 distinct canonical items, 1-4 of each —
    anything else is 400. Every experiment costs 2 AP and consumes the
    submitted materials, match or not. A first-ever match carves the
    inventor publicly (name + pubkey + timestamp, forever) and creates the
    durable tool row; a later match just creates the tool row; a non-match
    reports cleanly with no discovery.
    """
    conn = connect()
    try:
        st = _regen(conn, agent_id, now_ts)
        pubkey = conn.execute(
            "SELECT pubkey FROM agents WHERE id = ?", (agent_id,)
        ).fetchone()["pubkey"]
        if not isinstance(items, dict) or not (2 <= len(items) <= 3):
            raise InvalidExperiment(
                "experiment needs 2-3 distinct canonical items"
            )
        for item, qty in items.items():
            if item not in DISCOVERY_CANONICAL_ITEMS:
                raise InvalidExperiment(
                    f"not a canonical experiment item: {item!r}"
                )
            if not isinstance(qty, int) or isinstance(qty, bool) or not (1 <= qty <= 4):
                raise InvalidExperiment(
                    f"quantity for {item!r} must be an integer 1-4"
                )
        key = "+".join(f"{item}:{items[item]}" for item in sorted(items))
        if st["ap"] < EXPERIMENT_AP:
            raise InsufficientAP(st["ap"], EXPERIMENT_AP)
        match = conn.execute(
            "SELECT recipe_id, inventor_pubkey FROM recipes_hidden"
            " WHERE inputs_json = ?",
            (key,),
        ).fetchone()
        recipe_id = match["recipe_id"] if match else None
        if recipe_id is not None and _owns_tool(conn, pubkey, recipe_id):
            raise AlreadyCrafted(
                f"{recipe_id!r} already crafted — recraft after it breaks"
            )
        _consume_materials(conn, pubkey, dict(items))
        new_ap = st["ap"] - EXPERIMENT_AP
        conn.execute(
            "UPDATE agent_world SET ap = ?, last_update = ? WHERE agent_id = ?",
            (float(new_ap), now_ts, agent_id),
        )
        carved = False
        if match is not None:
            _craft_tool_row(
                conn, pubkey, agent_name, recipe_id,
                TOOL_DURABILITY_DISCOVERED, now_ts,
            )
            if match["inventor_pubkey"] is None:
                conn.execute(
                    "UPDATE recipes_hidden SET inventor_pubkey = ?,"
                    " inventor_name = ?, discovered_at = ? WHERE recipe_id = ?",
                    (
                        pubkey,
                        agent_name,
                        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_ts)),
                        recipe_id,
                    ),
                )
                carved = True
        conn.commit()
        return {
            "discovered": carved,
            "recipe_id": recipe_id,
            "carved_inventor": agent_name if carved else None,
            "ap": new_ap,
        }
    finally:
        conn.close()


def claim(connect, agent_id: int, now_ts: float, x: int, y: int) -> dict:
    """Claim a land tile. Bible §6.

    6 claims per agent, land only, unclaimed only, within a 3-tile
    (Chebyshev) radius of the agent, 2 AP per claim. Claims are permanent.
    Atomic: AP + claim row commit together.
    """
    conn = connect()
    try:
        st = _regen(conn, agent_id, now_ts)
        pubkey = conn.execute(
            "SELECT pubkey FROM agents WHERE id = ?", (agent_id,)
        ).fetchone()["pubkey"]
        terrain = _tile_terrain(conn, x, y)
        if terrain is None:
            raise ClaimError("no such tile")
        if terrain == "ocean":
            raise ClaimError("cannot claim ocean")
        if max(abs(x - st["x"]), abs(y - st["y"])) > CLAIM_RADIUS:
            raise ClaimError(f"tile beyond {CLAIM_RADIUS}-tile claim radius")
        n = conn.execute(
            "SELECT COUNT(*) FROM claims WHERE owner_pubkey = ?", (pubkey,)
        ).fetchone()[0]
        if n >= CLAIM_MAX:
            raise ClaimError(f"claim limit reached ({CLAIM_MAX})")
        if st["ap"] < CLAIM_AP:
            raise InsufficientAP(st["ap"], CLAIM_AP)
        try:
            conn.execute(
                "INSERT INTO claims (x, y, owner_pubkey, claimed_at)"
                " VALUES (?, ?, ?, ?)",
                (x, y, pubkey,
                 time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_ts))),
            )
        except sqlite3.IntegrityError:
            raise ClaimError("tile already claimed")
        new_ap = st["ap"] - CLAIM_AP
        conn.execute(
            "UPDATE agent_world SET ap = ?, last_update = ? WHERE agent_id = ?",
            (float(new_ap), now_ts, agent_id),
        )
        conn.commit()
        return {"x": x, "y": y, "ap": new_ap, "claims": n + 1}
    finally:
        conn.close()


def build(connect, agent_id: int, agent_name: str, now_ts: float, kind: str,
          x: int, y: int, name: str | None = None,
          description: str | None = None, purpose: str | None = None) -> dict:
    """Raise a structure on the agent's claimed land. Bible §6.

    Functional kinds (furnace/mill/shelter) have fixed material + AP costs;
    any other kind is flavor (5 timber + 5 AP — documented judgment call).
    One structure per tile; land only; the builder must hold the claim.
    Atomic: AP + materials + structure row commit together.
    """
    conn = connect()
    try:
        conn.row_factory = sqlite3.Row
        st = _regen(conn, agent_id, now_ts)
        pubkey = conn.execute(
            "SELECT pubkey FROM agents WHERE id = ?", (agent_id,)
        ).fetchone()["pubkey"]
        if not isinstance(kind, str) or not kind.strip():
            raise BuildError("kind must be a non-empty string")
        kind = kind.strip().lower()
        if kind in STRUCTURE_DEFS:
            inputs, ap_cost = STRUCTURE_DEFS[kind]
        else:
            inputs, ap_cost = FLAVOR_STRUCTURE_INPUTS, FLAVOR_STRUCTURE_AP
        terrain = _tile_terrain(conn, x, y)
        if terrain is None:
            raise BuildError("no such tile")
        if terrain == "ocean":
            raise BuildError("cannot build on ocean")
        if (
            conn.execute(
                "SELECT 1 FROM structures WHERE x = ? AND y = ?", (x, y)
            ).fetchone()
            is not None
        ):
            raise BuildError("tile already has a structure")
        if (
            conn.execute(
                "SELECT 1 FROM claims WHERE x = ? AND y = ? AND owner_pubkey = ?",
                (x, y, pubkey),
            ).fetchone()
            is None
        ):
            raise BuildError("not your claimed land")
        if st["ap"] < ap_cost:
            raise InsufficientAP(st["ap"], ap_cost)
        _consume_materials(conn, pubkey, dict(inputs))
        new_ap = st["ap"] - ap_cost
        conn.execute(
            "UPDATE agent_world SET ap = ?, last_update = ? WHERE agent_id = ?",
            (float(new_ap), now_ts, agent_id),
        )
        cur = conn.execute(
            "INSERT INTO structures (owner_pubkey, kind, x, y, name,"
            " description, purpose, raised_at, last_tithe_week,"
            " settlement_asset) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (
                pubkey, kind, x, y, name, description, purpose,
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_ts)),
                _tithe_week(now_ts),
            ),
        )
        conn.commit()
        return {
            "id": cur.lastrowid,
            "kind": kind,
            "x": x,
            "y": y,
            "ap": new_ap,
        }
    finally:
        conn.close()


def refine(connect, agent_id: int, now_ts: float, item: str) -> dict:
    """Refine one unit of a refined good. Bible §5.

    Requires an owned furnace; consumes the recipe's raw inputs + AP;
    produces exactly 1 unit into inventory (per-item cap applies).
    """
    conn = connect()
    try:
        conn.row_factory = sqlite3.Row
        st = _regen(conn, agent_id, now_ts)
        pubkey = conn.execute(
            "SELECT pubkey FROM agents WHERE id = ?", (agent_id,)
        ).fetchone()["pubkey"]
        recipe = REFINERY_RECIPES.get(item)
        if recipe is None:
            raise RefineError(f"unknown refinery recipe {item!r}")
        inputs, ap_cost = recipe
        if (
            conn.execute(
                "SELECT 1 FROM structures WHERE owner_pubkey = ?"
                " AND kind = 'furnace' LIMIT 1",
                (pubkey,),
            ).fetchone()
            is None
        ):
            raise RefineError("refining requires an owned furnace")
        if st["ap"] < ap_cost:
            raise InsufficientAP(st["ap"], ap_cost)
        inv = conn.execute(
            "SELECT qty FROM inventories WHERE agent_pubkey = ? AND resource = ?",
            (pubkey, item),
        ).fetchone()
        have = int(inv["qty"]) if inv is not None else 0
        if have + 1 > inventory_cap(conn, pubkey):
            raise InventoryFull(item)
        _consume_materials(conn, pubkey, dict(inputs))
        new_ap = st["ap"] - ap_cost
        conn.execute(
            "UPDATE agent_world SET ap = ?, last_update = ? WHERE agent_id = ?",
            (float(new_ap), now_ts, agent_id),
        )
        conn.execute(
            "INSERT INTO inventories (agent_pubkey, resource, qty) VALUES (?, ?, 1)"
            " ON CONFLICT(agent_pubkey, resource) DO UPDATE SET qty = qty + 1",
            (pubkey, item),
        )
        conn.commit()
        return {"item": item, "gained": 1, "ap": new_ap}
    finally:
        conn.close()


def list_recipes(connect) -> dict:
    """Public recipe book: discovered recipes with inventor credit, plus the
    count of still-hidden ones. Bible §4.2 — the carving is public."""
    conn = connect()
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT recipe_id, effect_json, inventor_name, discovered_at"
            " FROM recipes_hidden WHERE inventor_pubkey IS NOT NULL"
            " ORDER BY discovered_at, recipe_id"
        ).fetchall()
        hidden = conn.execute(
            "SELECT COUNT(*) FROM recipes_hidden WHERE inventor_pubkey IS NULL"
        ).fetchone()[0]
        return {
            "discovered": [
                {
                    "recipe_id": r["recipe_id"],
                    "effect": json.loads(r["effect_json"])["description"],
                    "inventor": r["inventor_name"],
                    "discovered_at": r["discovered_at"],
                }
                for r in rows
            ],
            "still_hidden": int(hidden),
        }
    finally:
        conn.close()


class ClaimError(WorldError):
    status_code = 400

    def __init__(self, detail: str = "claim failed"):
        super().__init__(detail)


class BuildError(WorldError):
    status_code = 400

    def __init__(self, detail: str = "build failed"):
        super().__init__(detail)


class RefineError(WorldError):
    status_code = 400

    def __init__(self, detail: str = "refine failed"):
        super().__init__(detail)


class InvalidExperiment(WorldError):
    status_code = 400

    def __init__(self, detail: str = "invalid experiment"):
        super().__init__(detail)


def me_view(connect, agent_id: int, agent_name: str, now_ts: float) -> dict:
    conn = connect()
    try:
        st = _regen(conn, agent_id, now_ts)
        conn.commit()
        terrain = _tile_terrain(conn, st["x"], st["y"])
        pubkey = conn.execute(
            "SELECT pubkey FROM agents WHERE id = ?", (agent_id,)
        ).fetchone()["pubkey"]
        tools = [
            r["recipe_id"]
            for r in conn.execute(
                "SELECT recipe_id, durability, max_durability FROM tools"
                " WHERE agent_pubkey = ? ORDER BY recipe_id",
                (pubkey,),
            ).fetchall()
        ]
        count = conn.execute(
            "SELECT COUNT(*) FROM discoveries WHERE agent_id = ?", (agent_id,)
        ).fetchone()[0]
        undisclosed = conn.execute(
            "SELECT COUNT(*) FROM discoveries d WHERE d.agent_id = ?"
            " AND NOT EXISTS (SELECT 1 FROM public_map p"
            " WHERE p.x = d.x AND p.y = d.y)",
            (agent_id,),
        ).fetchone()[0]
        return {
            "agent_name": agent_name,
            "x": st["x"],
            "y": st["y"],
            "terrain": terrain,
            "ap": st["ap"],
            # Bible ch.3: effective AP cap (100 +10 shelter +10 ap_boon
            # +10 feast). Fresh agents have no bonuses, so this stays 100.
            "ap_cap": effective_ap_cap(conn, agent_id, pubkey, now_ts),
            "ap_per_minute": 1,
            # Bible ch.3: durable tool rows (recipe_ids), worn on gather.
            "tools": tools,
            "seconds_until_next_ap": st["seconds_until_next_ap"],
            "private_discoveries": count,
            # QA v1.1.0 round 2: additive field (item 11). private_discoveries
            # is a LIFETIME counter of all discoveries ever; undisclosed_tiles
            # is the count of discovered-but-not-yet-public tiles for this
            # agent (i.e. tiles still eligible for POST /world/disclose).
            "undisclosed_tiles": undisclosed,
        }
    finally:
        conn.close()


def disclose(connect, agent_id: int, x: int, y: int, now_ts: float) -> dict:
    """Publish a privately-discovered tile to the public map for 1 AP."""
    conn = connect()
    try:
        st = _regen(conn, agent_id, now_ts)
        disc = conn.execute(
            "SELECT terrain FROM discoveries WHERE agent_id = ? AND x = ? AND y = ?",
            (agent_id, x, y),
        ).fetchone()
        if disc is None:
            raise UnknownDiscovery()
        terrain = disc["terrain"]
        already = conn.execute(
            "SELECT 1 FROM public_map WHERE x = ? AND y = ?", (x, y)
        ).fetchone()
        if already:
            conn.commit()  # persist the regen's last_update; no AP charged
            return {"x": x, "y": y, "terrain": terrain, "already_public": True}
        if st["ap"] < DISCLOSE_COST:
            raise InsufficientAP(st["ap"], DISCLOSE_COST)
        new_ap = st["ap"] - DISCLOSE_COST
        conn.execute(
            "INSERT INTO public_map (x, y, terrain, disclosed_by, disclosed_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (x, y, terrain, agent_id, utcnow_iso()),
        )
        conn.execute(
            "UPDATE agent_world SET ap = ?, last_update = ? WHERE agent_id = ?",
            (float(new_ap), now_ts, agent_id),
        )
        conn.commit()
        return {"x": x, "y": y, "terrain": terrain, "already_public": False}
    finally:
        conn.close()


def disclose_batch(
    connect, agent_id: int, tiles: list[tuple[int, int]], now_ts: float
) -> dict:
    """Disclose many privately-discovered tiles in one call (QA v1.1.0).

    The single-tile form POST /world/disclose {"x","y"} keeps working;
    this powers the batch form {"tiles": [{"x","y"}, ...]}.

    Costs 1 AP per NEWLY disclosed tile; already-public tiles are free,
    undiscovered tiles are skipped with an error entry. Processing stops
    at the first tile the agent cannot afford — remaining tiles are
    returned as skipped. One transaction: all-or-nothing per tile, with
    AP persisted once at the end.
    """
    conn = connect()
    try:
        st = _regen(conn, agent_id, now_ts)
        ap = st["ap"]
        results: list[dict] = []
        disclosed = 0
        out_of_ap = False
        for x, y in tiles:
            if out_of_ap:
                results.append({"x": x, "y": y, "error": "insufficient AP", "skipped": True})
                continue
            disc = conn.execute(
                "SELECT terrain FROM discoveries WHERE agent_id = ? AND x = ? AND y = ?",
                (agent_id, x, y),
            ).fetchone()
            if disc is None:
                results.append({"x": x, "y": y, "error": "tile not discovered"})
                continue
            already = conn.execute(
                "SELECT 1 FROM public_map WHERE x = ? AND y = ?", (x, y)
            ).fetchone()
            if already:
                results.append(
                    {"x": x, "y": y, "terrain": disc["terrain"], "already_public": True}
                )
                continue
            if ap < DISCLOSE_COST:
                out_of_ap = True
                results.append({"x": x, "y": y, "error": "insufficient AP", "skipped": True})
                continue
            ap -= DISCLOSE_COST
            conn.execute(
                "INSERT INTO public_map (x, y, terrain, disclosed_by, disclosed_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (x, y, disc["terrain"], agent_id, utcnow_iso()),
            )
            disclosed += 1
            results.append(
                {"x": x, "y": y, "terrain": disc["terrain"], "already_public": False}
            )
        conn.execute(
            "UPDATE agent_world SET ap = ?, last_update = ? WHERE agent_id = ?",
            (float(ap), now_ts, agent_id),
        )
        conn.commit()
        return {
            "results": results,
            "disclosed": disclosed,
            "ap": ap,
        }
    finally:
        conn.close()


def public_map_view(connect) -> dict:
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT p.x, p.y, p.terrain, a.name AS disclosed_by
            FROM public_map p JOIN agents a ON a.id = p.disclosed_by
            ORDER BY p.y ASC, p.x ASC
            """
        ).fetchall()
        tiles = [
            {
                "x": r["x"],
                "y": r["y"],
                "terrain": r["terrain"],
                "disclosed_by": r["disclosed_by"],
            }
            for r in rows
        ]
        return {
            "width": WORLD_SIZE,
            "height": WORLD_SIZE,
            "seed_id": SEED_ID,
            "public_count": len(tiles),
            "tiles": tiles,
        }
    finally:
        conn.close()


def info_view(connect) -> dict:
    conn = connect()
    try:
        count = conn.execute("SELECT COUNT(*) FROM agent_world").fetchone()[0]
        return {
            "width": WORLD_SIZE,
            "height": WORLD_SIZE,
            "seed_id": SEED_ID,
            "legend": LEGEND,
            "ap_rules": AP_RULES,
            "agents_in_world": count,
        }
    finally:
        conn.close()


def agents_view(connect) -> list:
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT a.name AS agent_name, w.x, w.y, t.terrain
            FROM agent_world w
            JOIN agents a ON a.id = w.agent_id
            JOIN world_tiles t ON t.x = w.x AND t.y = w.y
            ORDER BY a.id ASC
            """
        ).fetchall()
        return [
            {
                "agent_name": r["agent_name"],
                "x": r["x"],
                "y": r["y"],
                "terrain": r["terrain"],
            }
            for r in rows
        ]
    finally:
        conn.close()


def now() -> float:
    return time.time()
