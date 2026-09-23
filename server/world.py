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
    "mountain": "ore",
    "desert": "glass",
}
RESOURCES = ("grain", "timber", "ore", "glass")
# Chits are simulation credits (see server/app.py): valueless, transferable
# only inside trade offers, never redeemable.
CHITS_ITEM = "chits"
TRADE_ITEMS = RESOURCES + (CHITS_ITEM,)

GATHER_COST = 2
INVENTORY_CAP = 99
# Stock seed: 5 + (sha256("x,y,resource,agent-commons-genesis-v1") mod 6).
STOCK_SEED_MIN = 5
STOCK_SEED_MOD = 6

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


class TileDepleted(WorldError):
    status_code = 400

    def __init__(self):
        super().__init__("tile depleted")


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
    new_ap = ap_after_regen(ap, AP_CAP, last, now_ts)
    elapsed = max(0.0, now_ts - last)
    if new_ap >= AP_CAP:
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


def stock_for_tile(x: int, y: int, resource: str) -> int:
    """Deterministic seed stock 5..10 for a tile/resource pair."""
    digest = hashlib.sha256(
        f"{x},{y},{resource},{SEED_ID}".encode("utf-8")
    ).hexdigest()
    return STOCK_SEED_MIN + (int(digest, 16) % STOCK_SEED_MOD)


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


def gather(connect, agent_id: int, now_ts: float) -> dict:
    """Gather 1 unit of the resource on the agent's current tile.

    Costs 2 AP. Atomic: deducts AP, decrements tile stock, increments
    inventory in one transaction. Ocean tiles yield nothing (400),
    depleted tiles refuse (400), per-resource inventory cap is 99 (400),
    and insufficient AP is 402 — same convention as move/disclose.
    """
    conn = connect()
    try:
        st = _regen(conn, agent_id, now_ts)
        terrain = _tile_terrain(conn, st["x"], st["y"])
        resource = TERRAIN_RESOURCE.get(terrain or "")
        if resource is None:
            raise NothingToGather()
        if st["ap"] < GATHER_COST:
            raise InsufficientAP(st["ap"], GATHER_COST)
        inv = conn.execute(
            "SELECT qty FROM inventories WHERE agent_pubkey ="
            " (SELECT pubkey FROM agents WHERE id = ?) AND resource = ?",
            (agent_id, resource),
        ).fetchone()
        if inv is not None and int(inv["qty"]) >= INVENTORY_CAP:
            raise InventoryFull(resource)
        pubkey = conn.execute(
            "SELECT pubkey FROM agents WHERE id = ?", (agent_id,)
        ).fetchone()["pubkey"]
        # Decrement stock only if still positive — atomic scarcity.
        cur = conn.execute(
            "UPDATE world_resource_stock SET stock = stock - 1"
            " WHERE x = ? AND y = ? AND resource = ? AND stock > 0",
            (st["x"], st["y"], resource),
        )
        if cur.rowcount == 0:
            raise TileDepleted()
        new_ap = st["ap"] - GATHER_COST
        conn.execute(
            "UPDATE agent_world SET ap = ?, last_update = ? WHERE agent_id = ?",
            (float(new_ap), now_ts, agent_id),
        )
        conn.execute(
            "INSERT INTO inventories (agent_pubkey, resource, qty)"
            " VALUES (?, ?, 1)"
            " ON CONFLICT(agent_pubkey, resource) DO UPDATE SET qty = qty + 1",
            (pubkey, resource),
        )
        stock = conn.execute(
            "SELECT stock FROM world_resource_stock WHERE x = ? AND y = ? AND resource = ?",
            (st["x"], st["y"], resource),
        ).fetchone()["stock"]
        conn.commit()
        return {
            "resource": resource,
            "gained": 1,
            "stock_remaining": int(stock),
            "ap": new_ap,
            "x": st["x"],
            "y": st["y"],
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


def me_view(connect, agent_id: int, agent_name: str, now_ts: float) -> dict:
    conn = connect()
    try:
        st = _regen(conn, agent_id, now_ts)
        conn.commit()
        terrain = _tile_terrain(conn, st["x"], st["y"])
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
            "ap_cap": AP_CAP,
            "ap_per_minute": 1,
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
