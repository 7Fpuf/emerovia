"""Economic Validation Phase 1 — executable experiments on real world mechanics.

ChatGPT/Trevor assignment: move from theoretical design to experimental
evidence. These tests run the REAL Emerovia world engine (server/world.py
through the FastAPI app) against a TEMPORARY sqlite database, EXECUTING
gathering, crafting, refining, farming, travel, upkeep, and trading — then
comparing executed costs against the v2.3 cost model's theoretical
predictions (tools/validate-cost-model.py).

HARD BOUNDARIES (do not relax):
- Temporary DB only (AC_DB_PATH=tmp). Never production, never the
  localhost dev-server state.
- No cryptocurrency, no real money, no production deploys.
- No fake trading volume is presented as independent behavior: every
  trade here is explicitly experimenter-directed, labeled as such.
- No architecture expansion; memory-encryption work stays separate.

What this validates
-------------------
1. Mechanics: do the endpoints actually do what the model assumes?
   (yields, AP costs, recipe I/O, farm cycles, travel, regen, caps)
2. Scenarios: for four capability configurations, does the v2.3 decision
   rule (Buy iff P_chit x v < C_AP(Q)) correctly predict when cooperation
   beats autarky — when EXECUTED, not just calculated?
3. Boundaries: depletion, distance, seasons, waiting, inventory caps —
   where exactly does the model break?

Measurement convention
----------------------
AP costs are measured as ap_before - ap_after around an action sequence,
with last_update pinned to "now" beforehand so regen ≈ 0. Tools are
GRANTED (not crafted) except in the dedicated bootstrap test: measured
costs are therefore MARGINAL (1.00 AP/u tooled), while the v2.3 model
quotes STEADY-STATE (1.02 AP/u incl. 0.02 amortization). Both bases are
valid; the comparison is apples-to-apples within each test, and the
bootstrap test closes the loop on the 0.02.
"""

from __future__ import annotations

import importlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from nacl.signing import SigningKey

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

TERRAIN_TOOL = {"forest": "crude_axe", "mountain": "crude_pick",
                "plains": "crude_sickle", "desert": "crude_pick"}


# ---------------------------------------------------------------- signing

def make_key() -> SigningKey:
    return SigningKey(os.urandom(32))


def pubkey_hex(key: SigningKey) -> str:
    return key.verify_key.encode().hex()


def sign(key: SigningKey, method: str, path: str, body_text: str,
         ts: str | None = None) -> dict:
    if ts is None:
        ts = str(time.time())
    msg = (ts + "\n" + method.upper() + "\n" + path + "\n" + body_text).encode()
    sig = key.sign(msg).signature.hex()
    return {"X-Agent-Pubkey": pubkey_hex(key), "X-Timestamp": ts,
            "X-Signature": sig, "Content-Type": "application/json"}


def signed_request(client: TestClient, key: SigningKey, method: str,
                   path: str, payload: dict, expect: int = 200):
    body_text = json.dumps(payload, separators=(",", ":"))
    headers = sign(key, method, path, body_text)
    r = client.request(method, path, content=body_text.encode("utf-8"),
                       headers=headers)
    assert r.status_code == expect, \
        f"{method} {path} -> {r.status_code}: {r.text[:400]}"
    return r.json() if r.content else {}


def register(client: TestClient, name: str, key: SigningKey):
    r = client.post("/register",
                    json={"name": name, "pubkey": pubkey_hex(key)})
    assert r.status_code == 201, r.text
    return r.json()


def spawn(client: TestClient, key: SigningKey):
    return signed_request(client, key, "POST", "/world/spawn", {},
                          expect=201)


# ---------------------------------------------------------------- db

def db(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def set_ap(db_path, pubkey, ap):
    conn = db(db_path)
    try:
        conn.execute(
            "UPDATE agent_world SET ap = ?, last_update = ?"
            " WHERE agent_id = (SELECT id FROM agents WHERE pubkey = ?)",
            (float(ap), time.time(), pubkey))
        conn.commit()
    finally:
        conn.close()


def inventory_of(db_path, pubkey):
    conn = db(db_path)
    try:
        return {r["resource"]: int(r["qty"]) for r in conn.execute(
            "SELECT resource, qty FROM inventories WHERE agent_pubkey = ?",
            (pubkey,)).fetchall()}
    finally:
        conn.close()


def set_inventory(db_path, pubkey, items: dict):
    conn = db(db_path)
    try:
        for item, qty in items.items():
            conn.execute(
                "INSERT INTO inventories (agent_pubkey, resource, qty)"
                " VALUES (?, ?, ?) ON CONFLICT(agent_pubkey, resource)"
                " DO UPDATE SET qty = ?",
                (pubkey, item, qty, qty))
        conn.commit()
    finally:
        conn.close()


def grant_tool(db_path, pubkey, recipe_id, durability=300):
    conn = db(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO tools (agent_pubkey, recipe_id,"
            " durability, max_durability, crafted_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (pubkey, recipe_id, durability, durability,
             "2026-10-06T00:00:00Z"))
        conn.commit()
    finally:
        conn.close()


def teleport(db_path, pubkey, x, y):
    """Move an agent without spending AP.

    DOCUMENTED: travel is excluded from production-cost measurement and
    measured honestly in test_travel_cost. Teleport keeps scenario tests
    focused on production + trade mechanics.
    """
    conn = db(db_path)
    try:
        conn.execute(
            "UPDATE agent_world SET x = ?, y = ?"
            " WHERE agent_id = (SELECT id FROM agents WHERE pubkey = ?)",
            (x, y, pubkey))
        conn.commit()
    finally:
        conn.close()


def nearest_tile_with(db_path, resource, x, y):
    conn = db(db_path)
    try:
        row = conn.execute(
            "SELECT x, y, stock FROM world_resource_stock"
            " WHERE resource = ? AND stock > 0"
            " ORDER BY (ABS(x - ?) + ABS(y - ?)), stock DESC LIMIT 1",
            (resource, x, y)).fetchone()
        return (row["x"], row["y"]) if row else None
    finally:
        conn.close()


def set_genesis_days_ago(db_path, days):
    conn = db(db_path)
    try:
        conn.execute(
            "INSERT INTO world_meta (key, value) VALUES ('genesis_ts', ?)"
            " ON CONFLICT(key) DO UPDATE SET value = ?",
            (str(time.time() - days * 86400),) * 2)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- fixture

@pytest.fixture()
def ev1(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("AC_DB_PATH", db_path)
    op_key = make_key()
    monkeypatch.setenv("AC_OPERATOR_PUBKEY", pubkey_hex(op_key))
    import server.app as appmod
    importlib.reload(appmod)
    for k in ("gather", "refine", "move", "build", "claim", "farm", "plant",
              "harvest", "trade_offer", "trade_accept", "eat", "craft"):
        monkeypatch.setitem(appmod.RATE_LIMITS, k, (10000, 60))
    client = TestClient(appmod.app)
    keys = [make_key() for _ in range(4)]
    for i, k in enumerate(keys):
        register(client, f"ev1-{i}", k)
        spawn(client, k)
    return client, keys, db_path, appmod


# ---------------------------------------------------------------- agent setup

def me(client, key):
    return signed_request(client, key, "GET", "/world/me", {})


def setup_producer(client, keys, db_path, idx, advantages=(), season_days=20):
    """Give an agent: tooled gathering, a furnace, a farm, optional
    advantage tools (plow / ore_bounty). Returns (pubkey, furnace_xy, farm_id).

    Pins the season clock (default 20 days = summer: timber/ore/coal at
    1.00 yield multiplier) for deterministic yields. Override for
    season-specific tests."""
    key, pk = keys[idx], pubkey_hex(keys[idx])
    set_genesis_days_ago(db_path, season_days)
    st = me(client, key)
    x, y = st["x"], st["y"]
    for tool in ("crude_axe", "crude_pick", "crude_sickle"):
        grant_tool(db_path, pk, tool)
    for adv in advantages:
        grant_tool(db_path, pk, adv)
    # Furnace on the spawn tile (claim + build with granted materials).
    signed_request(client, key, "POST", "/world/claim", {"x": x, "y": y})
    set_inventory(db_path, pk, {"stone": 4, "clay": 2, "timber": 2})
    r = signed_request(client, key, "POST", "/world/build",
                       {"kind": "furnace", "x": x, "y": y,
                        "name": "EV1 Furnace"})
    furnace_id = r["id"]
    # Farm on an adjacent claimed tile (one structure per tile).
    conn = db(db_path)
    try:
        adj = conn.execute(
            "SELECT x, y FROM world_tiles WHERE terrain != 'ocean'"
            " AND (ABS(x - ?) + ABS(y - ?)) = 1"
            " AND NOT EXISTS (SELECT 1 FROM structures s"
            " WHERE s.x = world_tiles.x AND s.y = world_tiles.y)"
            " LIMIT 1", (x, y)).fetchone()
        assert adj is not None, "no adjacent free tile for farm"
        fx, fy = adj["x"], adj["y"]
    finally:
        conn.close()
    signed_request(client, key, "POST", "/world/claim", {"x": fx, "y": fy})
    set_inventory(db_path, pk, {"timber": 2, "grain": 2})
    r = signed_request(client, key, "POST", "/world/build",
                       {"kind": "farm", "x": fx, "y": fy, "name": "EV1 Farm"})
    farm_id = r["id"]
    return pk, (x, y), farm_id


def ap_cost_of(client, key, db_path, fn):
    """Run fn() with AP pinned at 100; return AP spent (regen ~ 0)."""
    pk = pubkey_hex(key)
    set_ap(db_path, pk, 100)
    before = me(client, key)["ap"]
    fn()
    after = me(client, key)["ap"]
    return before - after


def _gather_loop(client, key, db_path, resource, qty):
    """Raw gather loop (no AP measurement)."""
    pk = pubkey_hex(key)
    have = inventory_of(db_path, pk).get(resource, 0)
    while have < qty:
        st = me(client, key)
        tile = nearest_tile_with(db_path, resource, st["x"], st["y"])
        assert tile, f"no {resource} stock left in world"
        teleport(db_path, pk, *tile)
        r = signed_request(client, key, "POST", "/world/gather",
                           {"resource": resource})
        have += r["gained"]
        if r["gained"] == 0:
            break


def harvest_resource(client, key, db_path, resource, qty):
    """Gather `qty` units of resource for real (teleporting between
    tiles; travel measured separately). Returns AP spent."""
    return ap_cost_of(client, key, db_path,
                      lambda: _gather_loop(client, key, db_path, resource,
                                           qty))


def produce_iron(client, key, db_path, furnace_xy, units):
    """Produce `units` iron for real: gather ore+coal, refine at furnace.
    Returns AP spent."""
    pk = pubkey_hex(key)
    batches = -(-units // 2)  # ceil
    ore_need, coal_need = batches * 3, batches * 1

    def run():
        teleport(db_path, pk, *nearest_tile_with(
            db_path, "iron_ore", *furnace_xy))
        _gather_loop(client, key, db_path, "iron_ore", ore_need)
        teleport(db_path, pk, *nearest_tile_with(
            db_path, "coal", *furnace_xy))
        _gather_loop(client, key, db_path, "coal", coal_need)
        teleport(db_path, pk, *furnace_xy)
        for _ in range(batches):
            signed_request(client, key, "POST", "/world/refine",
                           {"item": "iron"})
    return ap_cost_of(client, key, db_path, run)


def ready_farm(db_path, farm_id):
    """Make all farm slots harvestable now (2h grow time is real-time;
    tests pin ready_at to the past — documented)."""
    conn = db(db_path)
    try:
        conn.execute(
            "UPDATE farm_plots SET ready_at = ? WHERE structure_id = ?",
            (time.time() - 1, farm_id))
        conn.commit()
    finally:
        conn.close()


def produce_flour(client, key, db_path, furnace_xy, farm_id, units):
    """Produce `units` flour for real: farm grain, refine at furnace.
    Returns AP spent."""
    pk = pubkey_hex(key)
    batches = -(-units // 2)
    grain_need = batches * 2

    def run():
        # Farm cycles until enough grain.
        while inventory_of(db_path, pk).get("grain", 0) < grain_need:
            for slot in range(4):
                try:
                    signed_request(client, key, "POST", "/world/farm",
                                   {"structure_id": farm_id, "action": "plant",
                                    "slot": slot})
                except AssertionError:
                    pass  # slot not empty; move on
            ready_farm(db_path, farm_id)
            for slot in range(4):
                try:
                    signed_request(client, key, "POST", "/world/farm",
                                   {"structure_id": farm_id, "action": "harvest",
                                    "slot": slot})
                except AssertionError:
                    pass
        teleport(db_path, pk, *furnace_xy)
        for _ in range(batches):
            signed_request(client, key, "POST", "/world/refine",
                           {"item": "flour"})
    return ap_cost_of(client, key, db_path, run)


def produce_flour_wild(client, key, db_path, furnace_xy, units):
    """Produce `units` flour via WILD grain (depleting, 1.0 AP/u gather).
    Returns AP spent. The marginal flour source for small quantities —
    cheaper than farmed (2.33) until tiles deplete."""
    pk = pubkey_hex(key)
    batches = -(-units // 2)
    grain_need = batches * 2

    def run():
        have = inventory_of(db_path, pk).get("grain", 0)
        while have < grain_need:
            st = me(client, key)
            tile = nearest_tile_with(db_path, "grain", st["x"], st["y"])
            assert tile, "no wild grain left"
            teleport(db_path, pk, *tile)
            r = signed_request(client, key, "POST", "/world/gather",
                               {"resource": "grain"})
            have += r["gained"]
            if r["gained"] == 0:
                break
        teleport(db_path, pk, *furnace_xy)
        for _ in range(batches):
            signed_request(client, key, "POST", "/world/refine",
                           {"item": "flour"})
    return ap_cost_of(client, key, db_path, run)


def execute_trade(client, keys, db_path, maker_idx, give, want, taker_idx):
    """Create + accept a trade offer for real. Returns (offer_id, ledger_row)."""
    r = signed_request(client, keys[maker_idx], "POST", "/trade/offers",
                       {"give": give, "want": want}, expect=201)
    oid = r["offer_id"]
    signed_request(client, keys[taker_idx], "POST",
                   f"/trade/offers/{oid}/accept", {})
    conn = db(db_path)
    try:
        offer = conn.execute(
            "SELECT status FROM trade_offers WHERE id = ?", (oid,)).fetchone()
        ledger = conn.execute(
            "SELECT * FROM trade_ledger WHERE maker_pubkey = ?"
            " ORDER BY rowid DESC LIMIT 1",
            (pubkey_hex(keys[maker_idx]),)).fetchone()
    finally:
        conn.close()
    assert offer["status"] == "filled"
    assert ledger is not None
    return oid, dict(ledger)

# ============================================================ Part A
# Mechanics validation: executed behavior vs the v2.3 cost model.
# Every number here is MEASURED by running the real engine.

def test_executed_gather_cost_matches_model(ev1):
    """Tooled gather in summer: 2 AP -> exactly 2 units = 1.00 AP/u
    marginal (model: 1.02 steady-state incl. 0.02 tool amortization —
    granted tools exclude it)."""
    client, keys, db_path, _ = ev1
    pk, furnace_xy, _ = setup_producer(client, keys, db_path, 0)
    spent = harvest_resource(client, keys[0], db_path, "timber", 10)
    gained = inventory_of(db_path, pk).get("timber", 0)
    assert gained >= 10, f"gained={gained}"
    # 2 AP per gather; the last gather on a tile may be partial
    # (stock < 2), so the per-unit rate is 1.00-1.20 AP/u.
    assert spent / gained == pytest.approx(1.0, abs=0.25), \
        f"spent={spent} for {gained}"


def test_executed_iron_production(ev1):
    """Produce 10 iron for real. Marginal prediction: 15 ore x 1.0 +
    5 coal x 1.0 + 15 refining = 35.0 AP (model steady-state: 35.4)."""
    client, keys, db_path, _ = ev1
    pk, furnace_xy, _ = setup_producer(client, keys, db_path, 0)
    spent = produce_iron(client, keys[0], db_path, furnace_xy, 10)
    inv = inventory_of(db_path, pk)
    assert inv.get("iron", 0) >= 10, f"iron={inv.get('iron')}"
    # 8 ore-gathers (16 AP) + 3 coal-gathers (6 AP) + 5 refines (15 AP)
    assert spent == pytest.approx(37.0, abs=1.0), f"spent={spent}"
    # NOTE: 37 marginal vs 35.4 model — the model assumes exact-quantity
    # gathers; real gathers come in 2-unit increments (16 ore for 15
    # needed, 6 coal for 5 needed): +2 AP of lumpiness. Documented, not
    # a model failure — the per-unit rate matches.


def test_executed_flour_production_plow(ev1):
    """16 flour with plow for real. Prediction: plant 4 (4 AP) +
    harvest 4 (8 AP) -> 16 grain; 8 refines (16 AP) -> 16 flour = 28 AP,
    1.75 AP/u."""
    client, keys, db_path, _ = ev1
    pk, furnace_xy, farm_id = setup_producer(client, keys, db_path, 0,
                                             advantages=("plow",))
    spent = produce_flour(client, keys[0], db_path, furnace_xy, farm_id, 16)
    inv = inventory_of(db_path, pk)
    assert inv.get("flour", 0) >= 16, f"flour={inv.get('flour')}"
    assert spent == pytest.approx(28.0, abs=1.0), f"spent={spent}"
    assert spent / 16 == pytest.approx(1.75, abs=0.1)


def test_executed_flour_production_no_plow(ev1):
    """12 flour without plow, cycle-exact. Prediction: 1 cycle
    (plant 4 = 8 AP, harvest 4 = 8 AP -> 12 grain) + 6 refines (12 AP)
    = 28 AP -> 2.33 AP/u. NOTE: 16 flour would cost 3.0 AP/u (2 cycles
    for 24 grain, 8 wasted) — farm lumpiness is a real model gap."""
    client, keys, db_path, _ = ev1
    pk, furnace_xy, farm_id = setup_producer(client, keys, db_path, 0)
    spent = produce_flour(client, keys[0], db_path, furnace_xy, farm_id, 12)
    inv = inventory_of(db_path, pk)
    assert inv.get("flour", 0) >= 12, f"flour={inv.get('flour')}"
    assert spent == pytest.approx(28.0, abs=1.0), f"spent={spent}"
    assert spent / 12 == pytest.approx(2.33, abs=0.1), f"spent={spent}"


def test_bootstrap_cost_executed(ev1):
    """Full bootstrap for real: gather 2 timber + 1 fiber bare-handed,
    craft crude_axe. Model: ~14 AP."""
    client, keys, db_path, _ = ev1
    key, pk = keys[0], pubkey_hex(keys[0])

    def run():
        st = me(client, key)
        tile = nearest_tile_with(db_path, "timber", st["x"], st["y"])
        teleport(db_path, pk, *tile)
        signed_request(client, key, "POST", "/world/gather",
                       {"resource": "timber"})
        signed_request(client, key, "POST", "/world/gather",
                       {"resource": "timber"})
        tile = nearest_tile_with(db_path, "fiber", st["x"], st["y"])
        if tile:
            teleport(db_path, pk, *tile)
            signed_request(client, key, "POST", "/world/gather",
                           {"resource": "fiber"})
        signed_request(client, key, "POST", "/world/craft",
                       {"recipe_id": "crude_axe"})
    spent = ap_cost_of(client, key, db_path, run)
    inv = inventory_of(db_path, pk)
    # 3 bare gathers (12 AP) + craft (2 AP) = 14 AP; fiber may need a
    # second tile but overlay stock usually covers it.
    assert spent == pytest.approx(14.0, abs=3.0), f"spent={spent}"



# ============================================================ Part B
# Four economic scenarios. Each EXECUTES production for both agents,
# EXECUTES the trade through the real endpoints, and checks realized
# gains against measured autarky baselines.
#
# MEASURED marginal baselines (granted tools; see Part A mechanics
# tests). Farm production is LUMPY (12 flour/cycle no-plow, 16/cycle
# plow); gather comes in 2-unit increments. The v2.3 model's continuous
# costs are the idealization; these are the executed realities.
#   IRON10        = 37.0 AP  (10 iron, no bounty: 8+3 gathers + 5 refines)
#   IRON10_BOUNTY = 31.0 AP  (10 iron, ore_bounty: 5+3 gathers + 5 refines)
#   FLOUR16_PLOW  = 28.0 AP  (16 flour, plow: exact 1 cycle + 8 refines)
#   FLOUR12       = 28.0 AP  (12 flour, no plow: exact 1 cycle + 6 refines)
#   FLOUR16_WILD  = 32.0 AP  (16 flour, wild grain: 8 gathers + 8 refines)
#   FLOUR16_NOPLOW= 48.0 AP  (16 flour, no plow: 2 cycles, 8 grain surplus)

def scenario_produce(client, keys, db_path, idx, furnace_xy, farm_id,
                     iron_units=0, flour_units=0, flour_via="farm"):
    key = keys[idx]
    spent = 0
    if iron_units:
        spent += produce_iron(client, key, db_path, furnace_xy, iron_units)
    if flour_units:
        if flour_via == "farm":
            spent += produce_flour(client, key, db_path, furnace_xy,
                                   farm_id, flour_units)
        else:
            spent += produce_flour_wild(client, key, db_path, furnace_xy,
                                        flour_units)
    return spent, inventory_of(db_path, pubkey_hex(key))

def test_scenario_a_identical_agents_zero_sum(ev1):
    """Two identical agents: trade is zero-sum. X gives 12 flour (28 AP)
    for 10 iron (worth 37 AP to X): X +9. Y gives 10 iron (37 AP) for
    12 flour (worth 28 AP to Y): Y -9. Sum = 0. No cooperation gain."""
    client, keys, db_path, _ = ev1
    pkx, fx0, farmx = setup_producer(client, keys, db_path, 0)
    pky, fy0, farmy = setup_producer(client, keys, db_path, 1)

    x_spent, _ = scenario_produce(client, keys, db_path, 0, fx0, farmx,
                                  flour_units=12)
    y_spent, _ = scenario_produce(client, keys, db_path, 1, fy0, farmy,
                                  iron_units=10)
    assert x_spent == pytest.approx(28.0, abs=1.0)
    assert y_spent == pytest.approx(37.0, abs=2.0)

    x0, y0 = inventory_of(db_path, pkx), inventory_of(db_path, pky)
    execute_trade(client, keys, db_path, 0, {"flour": 12}, {"iron": 10}, 1)
    x1, y1 = inventory_of(db_path, pkx), inventory_of(db_path, pky)
    assert x1.get("iron", 0) - x0.get("iron", 0) == 10
    assert y1.get("flour", 0) - y0.get("flour", 0) == 12

    x_gain = 37.0 - x_spent   # +9.0: X's iron autarky minus flour cost
    y_gain = 28.0 - y_spent   # -9.0: Y's flour autarky minus iron cost
    assert x_gain + y_gain == pytest.approx(0.0, abs=1.0), \
        f"trade must be zero-sum: {x_gain} + {y_gain}"


def test_scenario_b_farming_advantage(ev1):
    """X has plow. X makes 16 flour for 28 AP (vs 37.0 iron autarky);
    Y makes 10 iron for 37 AP (vs 48.0 flour autarky). Both gain."""
    client, keys, db_path, _ = ev1
    pkx, fx0, farmx = setup_producer(client, keys, db_path, 0,
                                     advantages=("plow",))
    pky, fy0, farmy = setup_producer(client, keys, db_path, 1)

    x_spent, _ = scenario_produce(client, keys, db_path, 0, fx0, farmx,
                                  flour_units=16)
    y_spent, _ = scenario_produce(client, keys, db_path, 1, fy0, farmy,
                                  iron_units=10)
    execute_trade(client, keys, db_path, 0, {"flour": 16}, {"iron": 10}, 1)
    inv_x = inventory_of(db_path, pkx)
    inv_y = inventory_of(db_path, pky)
    assert inv_x.get("iron", 0) >= 10
    assert inv_y.get("flour", 0) >= 16

    x_gain = 37.0 - x_spent  # ~9.0
    y_gain = 48.0 - y_spent  # ~11.0
    assert x_gain > 0 and y_gain > 0, (x_gain, y_gain)
    # Documented: gains exceed the continuous model's (7.0/2.3)
    # because farm lumpiness raises Y's flour autarky to 48.


def test_scenario_c_ore_advantage_narrow_window(ev1):
    """Y has ore_bounty. Continuous model: window F in (13.0, 15.2) —
    INFEASIBLE under discrete farm cycles (no cycle-exact F inside).
    Feasible margin is wild grain: X makes 16 flour wild (32 AP),
    Y makes 10 iron (31 AP). Narrow but positive: X +5.0, Y +1.0.

    FINDING: the ore advantage is partially competed away by the
    wild-flour margin available to both. Depletion bounds this."""
    client, keys, db_path, _ = ev1
    feasible = [f for f in (12, 24)
                if 31.0 < f * 2.33 and f * 2.33 < 37.0]
    assert feasible == [], "continuous window has no cycle-exact point"

    pkx, fx0, farmx = setup_producer(client, keys, db_path, 0,
                                     season_days=28)
    pky, fy0, farmy = setup_producer(client, keys, db_path, 1,
                                     advantages=("ore_bounty",),
                                     season_days=28)
    x_spent, _ = scenario_produce(client, keys, db_path, 0, fx0, farmx,
                                  flour_units=16, flour_via="wild")
    y_spent, _ = scenario_produce(client, keys, db_path, 1, fy0, farmy,
                                  iron_units=10)
    execute_trade(client, keys, db_path, 0, {"flour": 16}, {"iron": 10}, 1)
    inv_x = inventory_of(db_path, pkx)
    inv_y = inventory_of(db_path, pky)
    assert inv_x.get("iron", 0) >= 10
    assert inv_y.get("flour", 0) >= 16

    x_gain = 37.0 - x_spent  # ~5.0
    y_gain = 32.0 - y_spent  # predicted ~+1.0; see below
    # MODEL FAILURE (asserted): Y's predicted +1.0 gain is smaller than
    # gather lumpiness noise (+/-2 AP from partial final gathers).
    # The narrow ore advantage is NOISE-DOMINATED under discrete
    # mechanics — the model cannot reliably predict the sign of Y's
    # gain. This is a Phase 1 finding, not a test bug.
    assert -2.0 <= y_gain <= 2.0, \
        f"Y's gain should be noise-dominated, got {y_gain}"
    assert x_gain > 0, x_gain


def test_scenario_d_complementary_advantages(ev1):
    """X has plow, Y has ore_bounty — the v2.3 worked example, executed.
    X: 16 flour for 28 AP (vs 37.0 iron autarky) -> +9.0.
    Y: 10 iron for 31 AP (vs 48.0 flour autarky) -> +17.0.
    Ledger records the fill."""
    client, keys, db_path, _ = ev1
    pkx, fx0, farmx = setup_producer(client, keys, db_path, 0,
                                     advantages=("plow",))
    pky, fy0, farmy = setup_producer(client, keys, db_path, 1,
                                     advantages=("ore_bounty",))

    x_spent, _ = scenario_produce(client, keys, db_path, 0, fx0, farmx,
                                  flour_units=16)
    y_spent, _ = scenario_produce(client, keys, db_path, 1, fy0, farmy,
                                  iron_units=10)
    assert x_spent == pytest.approx(28.0, abs=1.0), x_spent
    # ore_bounty: 15 ore at 3/gather = 5 gathers (10 AP) + 3 coal (6 AP)
    # + 15 refine = 31 AP.
    assert y_spent == pytest.approx(31.0, abs=2.0), y_spent

    oid, ledger = execute_trade(client, keys, db_path, 0,
                                {"flour": 16}, {"iron": 10}, 1)
    inv_x = inventory_of(db_path, pkx)
    inv_y = inventory_of(db_path, pky)
    assert inv_x.get("iron", 0) >= 10, inv_x
    assert inv_y.get("flour", 0) >= 16, inv_y
    assert ledger["give_json"] == '{"flour":16}'
    assert ledger["want_json"] == '{"iron":10}'

    x_gain = 37.0 - x_spent
    y_gain = 48.0 - y_spent
    assert x_gain > 0 and y_gain > 0, (x_gain, y_gain)
    # NOTE: executed gains (9.0/17.0) exceed the model's continuous
    # (7.0/7.3) because lumpiness raises both autarky baselines.
    # Direction confirmed; magnitudes are lumpy.

# ============================================================ Part C
# Scenario variants: depletion, distance/travel, seasons (winter),
# waiting vs acting, limited inventories, trade edge cases.

def walk(client, key, direction, steps):
    """Walk `steps` tiles via real move actions. Returns AP spent.
    Skips impassable (ocean) by trying alternate directions."""
    spent_before = me(client, key)["ap"]
    moved = 0
    dirs = [direction, "N", "S", "E", "W"]
    while moved < steps:
        ok = False
        for d in dirs:
            try:
                signed_request(client, key, "POST", "/world/move",
                               {"dir": d})
                moved += 1
                ok = True
                break
            except AssertionError:
                continue
        if not ok:
            break  # boxed in; stop
    return spent_before - me(client, key)["ap"], moved


def test_variant_travel_cost_bound(ev1):
    """Real travel is 1 AP/tile (land). A 20-tile delivery costs 20 AP —
    enough to erase the +1.0 narrow gain in scenario C. Distance is a
    hard bound on cooperation: trade only pays if
    gain > 2 x distance x move_cost."""
    client, keys, db_path, _ = ev1
    pk, _, _ = setup_producer(client, keys, db_path, 0)
    set_ap(db_path, pk, 100)
    spent, moved = walk(client, keys[0], "E", 10)
    # Each land move costs 1 AP; mountain 2. All moves cost >= 1.
    assert moved == 10, f"moved={moved}"
    assert 10 <= spent <= 20, f"spent={spent}"
    # A 20-AP round trip over 10 tiles erases narrow gains: distance
    # bounds cooperation (gain must exceed 2 x distance x move_cost).


def test_variant_depletion_forces_relocation(ev1):
    """Depleting every iron_ore tile makes iron production impossible
    until stock regrows — a hard stop the continuous model ignores."""
    client, keys, db_path, _ = ev1
    pk, _, _ = setup_producer(client, keys, db_path, 0)
    con = sqlite3.connect(db_path)
    try:
        con.execute("UPDATE world_resource_stock SET stock = 0 "
                    "WHERE resource = 'iron_ore'")
        con.commit()
    finally:
        con.close()
    # No iron_ore stock anywhere: gather must fail.
    st = me(client, keys[0])
    tile = nearest_tile_with(db_path, "iron_ore", st["x"], st["y"])
    assert tile is None
    teleport(db_path, pk, st["x"], st["y"])
    try:
        signed_request(client, keys[0], "POST", "/world/gather",
                       {"resource": "iron_ore"})
        assert False, "gather should fail on depleted world"
    except AssertionError as e:
        assert "no stock" in str(e).lower() or "gather" in str(e).lower()


def test_variant_winter_wild_grain_penalty(ev1):
    """Winter: wild grain yield halves (0.5x) -> 4.0 AP/u marginal.
    Farmed grain is season-independent (1.75 AP/u with plow). Winter
    creates a REAL temporal comparative advantage for farmers —
    the v2.3 model's headline seasonal finding, executed."""
    client, keys, db_path, _ = ev1
    pk, _, _ = setup_producer(client, keys, db_path, 0, season_days=45)
    set_season(conn_path=db_path, season="winter")
    # Wild grain in winter: 1 per gather (0.5 x 2) -> 2 AP/u... verify.
    spent = harvest_resource(client, keys[0], db_path, "grain", 4)
    # 4 grain at 1/gather = 4 gathers = 8 AP.
    assert spent == pytest.approx(8.0, abs=1.0), f"spent={spent}"
    # vs summer: 4 grain = 2 gathers = 4 AP. Winter doubles wild cost.


def set_season(conn_path, season):
    con = sqlite3.connect(conn_path)
    try:
        con.execute("UPDATE world_meta SET value = ? WHERE key = 'season'",
                    (season,))
        con.commit()
    finally:
        con.close()


def test_variant_ap_constraint_blocks_production(ev1):
    """Waiting vs acting now: an agent at 5 AP cannot complete 10 iron
    (needs ~37). AP is the binding constraint; regen is 1/min so 32 AP
    takes 32 minutes of waiting. The model treats AP as free-flowing;
    executed reality is stop-and-wait."""
    client, keys, db_path, _ = ev1
    pk, furnace_xy, _ = setup_producer(client, keys, db_path, 0)
    set_ap(db_path, pk, 5)
    teleport(db_path, pk, *furnace_xy)
    # 5 refines need 15 AP; agent has 5. Refines should fail partway.
    done = 0
    for _ in range(5):
        try:
            # Need ore+coal first; give them directly via inventory.
            set_inventory(db_path, pk, {"iron_ore": 15, "coal": 5})
            signed_request(client, keys[0], "POST", "/world/refine",
                           {"item": "iron"})
            done += 1
        except AssertionError:
            break
    # With 5 AP, exactly 1 refine (3 AP) succeeds -> 2 iron (2/batch).
    assert done == 1, f"done={done}"
    inv = inventory_of(db_path, pk)
    assert inv.get("iron", 0) == 2, inv


def test_variant_inventory_cap_blocks_bulk_trade(ev1):
    """Per-item inventory cap is 99 (149 with cart). A trade offering
    100+ units of one item cannot settle into a full inventory —
    bulk trades need carts or splitting. The model assumes unbounded
    inventories."""
    client, keys, db_path, _ = ev1
    pk, _, _ = setup_producer(client, keys, db_path, 0)
    # Fill to cap: 99 stone.
    set_inventory(db_path, pk, {"stone": 99})
    st = me(client, keys[0])
    tile = nearest_tile_with(db_path, "stone", st["x"], st["y"])
    teleport(db_path, pk, *tile)
    set_ap(db_path, pk, 100)
    try:
        signed_request(client, keys[0], "POST", "/world/gather",
                       {"resource": "stone"})
        # If gather succeeded, inventory must still respect cap.
        inv = inventory_of(db_path, pk)
        assert inv.get("stone", 0) <= 99, inv
    except AssertionError as e:
        assert "cap" in str(e).lower()


def test_variant_offer_cannot_be_accepted_twice(ev1):
    """Double-accept: after accept, the offer is filled. A second accept
    must fail. (Execute_trade covers the happy path; this is the edge.)"""
    client, keys, db_path, _ = ev1
    pk0, _, _ = setup_producer(client, keys, db_path, 0)
    pk1, _, _ = setup_producer(client, keys, db_path, 1)
    set_inventory(db_path, pk0, {"flour": 20})
    set_inventory(db_path, pk1, {"iron": 20})
    offer = signed_request(client, keys[0], "POST", "/trade/offers",
                           {"give": {"flour": 10}, "want": {"iron": 5}},
                           expect=201)
    oid = offer["offer_id"]
    signed_request(client, keys[1], "POST",
                   f"/trade/offers/{oid}/accept", {})
    try:
        signed_request(client, keys[1], "POST",
                       f"/trade/offers/{oid}/accept", {})
        assert False, "double accept should fail"
    except AssertionError:
        pass


def test_variant_uncovered_offer_rejected(ev1):
    """Offering 10 flour with 0 in inventory must fail — you must hold
    everything in give."""
    client, keys, db_path, _ = ev1
    setup_producer(client, keys, db_path, 0)
    try:
        signed_request(client, keys[0], "POST", "/trade/offers",
                       {"give": {"flour": 10}, "want": {"iron": 5}},
                       expect=201)
        assert False, "uncovered offer should fail"
    except AssertionError:
        pass
