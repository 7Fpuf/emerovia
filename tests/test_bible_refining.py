"""Bible ch.5 — refining + buildings (Systems Bible §2.5/§6/§11).

Covers: land claims (6/agent, 3-tile radius, 5 AP, land-only, permanent),
structure raising on claimed land (the 8 Bible kinds with §11 costs;
unknown kinds refused; tool keys owned-not-consumed; one per tile),
the refinery (owned kept-up furnace required; Bible recipes with coal
fuel; 2 units out; at-cap 400; 1/5s), and the absence of any mill
gather bonus (not in the Bible).
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

TERRAIN_RESOURCE = {"plains": "grain", "forest": "timber",
                    "mountain": "iron_ore", "desert": "glass"}
TERRAIN_TOOL = {"plains": "crude_sickle", "forest": "crude_axe",
                "mountain": "crude_pick", "desert": "crude_pick"}


# ---------------------------------------------------------------- helpers

def make_key() -> SigningKey:
    return SigningKey(os.urandom(32))


def pubkey_hex(key: SigningKey) -> str:
    return key.verify_key.encode().hex()


def sign(key: SigningKey, method: str, path: str, body_text: str, ts: str | None = None) -> dict:
    if ts is None:
        ts = str(time.time())
    msg = (ts + "\n" + method.upper() + "\n" + path + "\n" + body_text).encode("utf-8")
    sig = key.sign(msg).signature.hex()
    return {
        "X-Agent-Pubkey": pubkey_hex(key),
        "X-Timestamp": ts,
        "X-Signature": sig,
        "Content-Type": "application/json",
    }


def signed_request(client: TestClient, key: SigningKey, method: str, path: str,
                   payload: dict, ts: str | None = None):
    body_text = json.dumps(payload, separators=(",", ":"))
    headers = sign(key, method, path, body_text, ts=ts)
    return client.request(method, path, content=body_text.encode("utf-8"), headers=headers)


def register(client: TestClient, name: str, key: SigningKey):
    r = client.post("/register", json={"name": name, "pubkey": pubkey_hex(key)})
    assert r.status_code == 201, r.text
    return r.json()


def spawn(client: TestClient, key: SigningKey):
    r = signed_request(client, key, "POST", "/world/spawn", {})
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture()
def b5(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("AC_DB_PATH", db_path)
    op_key = make_key()
    monkeypatch.setenv("AC_OPERATOR_PUBKEY", pubkey_hex(op_key))
    import server.app as appmod
    importlib.reload(appmod)
    client = TestClient(appmod.app)
    keys = [make_key() for _ in range(4)]
    for i, k in enumerate(keys):
        register(client, f"bible-refine-{i}", k)
    return client, keys, db_path, appmod


def set_genesis_days_ago(db_path, days):
    conn = db(db_path)
    try:
        conn.execute(
            "INSERT INTO world_meta (key, value) VALUES ('genesis_ts', ?)"
            " ON CONFLICT(key) DO UPDATE SET value = ?",
            (str(time.time() - days * 86400),) * 2,
        )
        conn.commit()
    finally:
        conn.close()


def pin_neutral_season(db_path, terrain):
    """Pin the season clock to a season where the terrain's resource has a
    1.00 abundance multiplier, so tooled-gather yield assertions stay
    deterministic under the §7 season table."""
    days = {"plains": 28, "forest": 20, "mountain": 0, "desert": 0}[terrain]
    set_genesis_days_ago(db_path, days)


def db(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def set_inventory(db_path, pubkey, items: dict):
    conn = db(db_path)
    try:
        for item, qty in items.items():
            conn.execute(
                "INSERT INTO inventories (agent_pubkey, resource, qty) VALUES (?, ?, ?)"
                " ON CONFLICT(agent_pubkey, resource) DO UPDATE SET qty = ?",
                (pubkey, item, qty, qty),
            )
        conn.commit()
    finally:
        conn.close()


def inventory_of(db_path, pubkey):
    conn = db(db_path)
    try:
        return {
            r["resource"]: int(r["qty"])
            for r in conn.execute(
                "SELECT resource, qty FROM inventories WHERE agent_pubkey = ?",
                (pubkey,),
            ).fetchall()
        }
    finally:
        conn.close()


def set_ap(db_path, pubkey, ap):
    conn = db(db_path)
    try:
        conn.execute(
            "UPDATE agent_world SET ap = ? WHERE agent_id ="
            " (SELECT id FROM agents WHERE pubkey = ?)",
            (float(ap), pubkey),
        )
        conn.commit()
    finally:
        conn.close()


def land_tiles_near(db_path, x, y, radius, n):
    conn = db(db_path)
    try:
        return [
            (r["x"], r["y"])
            for r in conn.execute(
                "SELECT x, y FROM world_tiles WHERE terrain != 'ocean'"
                " AND ABS(x - ?) <= ? AND ABS(y - ?) <= ?"
                " ORDER BY ABS(x - ?) + ABS(y - ?), x, y LIMIT ?",
                (x, radius, y, radius, x, y, n),
            ).fetchall()
        ]
    finally:
        conn.close()


def any_ocean_tile(db_path, x, y):
    # The ocean check runs before the claim-radius check, so any ocean
    # tile on the map exercises it deterministically (no skips).
    conn = db(db_path)
    try:
        row = conn.execute(
            "SELECT x, y FROM world_tiles WHERE terrain = 'ocean'"
            " ORDER BY ABS(x - ?) + ABS(y - ?) LIMIT 1",
            (x, y),
        ).fetchone()
        assert row is not None, "map has no ocean?!"
        return (row["x"], row["y"])
    finally:
        conn.close()


def claim(client, key, x, y):
    return signed_request(client, key, "POST", "/world/claim", {"x": x, "y": y})


# ---------------------------------------------------------------- claim tests

def test_claim_land_tile(b5, monkeypatch):
    client, keys, db_path, appmod = b5
    monkeypatch.setitem(appmod.RATE_LIMITS, "claim", (100, 60))
    me = spawn(client, keys[0])
    r = claim(client, keys[0], me["x"], me["y"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["x"] == me["x"] and body["y"] == me["y"]
    assert body["ap"] == me["ap"] - 5  # Bible §11 CLAIM_COST_AP
    assert body["claims"] == 1


def test_claim_ocean_400(b5, monkeypatch):
    client, keys, db_path, appmod = b5
    monkeypatch.setitem(appmod.RATE_LIMITS, "claim", (100, 60))
    me = spawn(client, keys[0])
    tile = any_ocean_tile(db_path, me["x"], me["y"])
    r = claim(client, keys[0], *tile)
    assert r.status_code == 400, r.text
    assert "ocean" in r.json()["detail"]


def test_claim_twice_400(b5, monkeypatch):
    client, keys, db_path, appmod = b5
    monkeypatch.setitem(appmod.RATE_LIMITS, "claim", (100, 60))
    me = spawn(client, keys[0])
    assert claim(client, keys[0], me["x"], me["y"]).status_code == 200
    r = claim(client, keys[0], me["x"], me["y"])
    assert r.status_code == 400, r.text
    assert "already claimed" in r.json()["detail"]


def test_claim_limit_six(b5, monkeypatch):
    client, keys, db_path, appmod = b5
    monkeypatch.setitem(appmod.RATE_LIMITS, "claim", (100, 60))
    me = spawn(client, keys[0])
    tiles = land_tiles_near(db_path, me["x"], me["y"], 3, 7)
    assert len(tiles) == 7
    for i, (x, y) in enumerate(tiles[:6]):
        r = claim(client, keys[0], x, y)
        assert r.status_code == 200, (i, r.text)
        assert r.json()["claims"] == i + 1
    r = claim(client, keys[0], *tiles[6])
    assert r.status_code == 400, r.text
    assert "claim limit" in r.json()["detail"]


def test_claim_radius_400(b5, monkeypatch):
    client, keys, db_path, appmod = b5
    monkeypatch.setitem(appmod.RATE_LIMITS, "claim", (100, 60))
    me = spawn(client, keys[0])
    # A LAND tile beyond the 3-tile radius (ocean/off-map tiles fail
    # earlier checks, so pick land explicitly).
    conn = db(db_path)
    try:
        row = conn.execute(
            "SELECT x, y FROM world_tiles WHERE terrain != 'ocean'"
            " AND MAX(ABS(x - ?), ABS(y - ?)) > 3 LIMIT 1",
            (me["x"], me["y"]),
        ).fetchone()
        assert row is not None
        tx, ty = row["x"], row["y"]
    finally:
        conn.close()
    r = claim(client, keys[0], tx, ty)
    assert r.status_code == 400, r.text
    assert "radius" in r.json()["detail"]


def test_claim_insufficient_ap_402(b5, monkeypatch):
    client, keys, db_path, appmod = b5
    monkeypatch.setitem(appmod.RATE_LIMITS, "claim", (100, 60))
    me = spawn(client, keys[0])
    set_ap(db_path, pubkey_hex(keys[0]), 1)
    r = claim(client, keys[0], me["x"], me["y"])
    assert r.status_code == 402, r.text


# ---------------------------------------------------------------- build tests

def grant_tool(db_path, pubkey, recipe_id, durability=120):
    conn = db(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO tools (agent_pubkey, recipe_id, durability,"
            " max_durability, crafted_at) VALUES (?, ?, ?, ?, ?)",
            (pubkey, recipe_id, durability, durability,
             "2026-09-23T00:00:00Z"),
        )
        conn.commit()
    finally:
        conn.close()


def build_furnace(client, keys, db_path, key_idx=0):
    # Bible §11: furnace = 6 AP + 4 stone + 2 clay + 2 timber, owned
    # crude_pick as key (never consumed).
    me = signed_request(client, keys[key_idx], "GET", "/world/me", {}).json()
    pk = pubkey_hex(keys[key_idx])
    assert claim(client, keys[key_idx], me["x"], me["y"]).status_code == 200
    grant_tool(db_path, pk, "crude_pick")
    set_inventory(db_path, pk, {"stone": 4, "clay": 2, "timber": 2})
    return signed_request(client, keys[key_idx], "POST", "/world/build",
                          {"kind": "furnace", "x": me["x"], "y": me["y"],
                           "name": "Test Furnace"}), me


def test_build_furnace(b5, monkeypatch):
    client, keys, db_path, appmod = b5
    monkeypatch.setitem(appmod.RATE_LIMITS, "claim", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "build", (100, 60))
    spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    me_before = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    r, me = build_furnace(client, keys, db_path)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "furnace"
    assert body["id"] > 0
    # 5 AP claim + 6 AP build (Bible §11).
    assert body["ap"] == me_before["ap"] - 11
    assert inventory_of(db_path, pk) == {}
    conn = db(db_path)
    try:
        row = conn.execute("SELECT * FROM structures WHERE id = ?",
                           (body["id"],)).fetchone()
        assert row["owner_pubkey"] == pk
        assert row["name"] == "Test Furnace"
        assert row["settlement_asset"] == 0
    finally:
        conn.close()


def test_build_unclaimed_land_400(b5, monkeypatch):
    client, keys, db_path, appmod = b5
    monkeypatch.setitem(appmod.RATE_LIMITS, "build", (100, 60))
    me = spawn(client, keys[0])
    grant_tool(db_path, pubkey_hex(keys[0]), "crude_pick")
    set_inventory(db_path, pubkey_hex(keys[0]), {"stone": 10})
    r = signed_request(client, keys[0], "POST", "/world/build",
                       {"kind": "furnace", "x": me["x"], "y": me["y"]})
    assert r.status_code == 400, r.text
    assert "not your claimed land" in r.json()["detail"]


def test_build_ocean_400(b5, monkeypatch):
    client, keys, db_path, appmod = b5
    monkeypatch.setitem(appmod.RATE_LIMITS, "build", (100, 60))
    me = spawn(client, keys[0])
    tile = any_ocean_tile(db_path, me["x"], me["y"])
    grant_tool(db_path, pubkey_hex(keys[0]), "crude_pick")
    set_inventory(db_path, pubkey_hex(keys[0]), {"stone": 10})
    r = signed_request(client, keys[0], "POST", "/world/build",
                       {"kind": "furnace", "x": tile[0], "y": tile[1]})
    assert r.status_code == 400, r.text
    assert "ocean" in r.json()["detail"]


def test_build_one_structure_per_tile(b5, monkeypatch):
    client, keys, db_path, appmod = b5
    monkeypatch.setitem(appmod.RATE_LIMITS, "claim", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "build", (100, 60))
    spawn(client, keys[0])
    r, me = build_furnace(client, keys, db_path)
    assert r.status_code == 200, r.text
    grant_tool(db_path, pubkey_hex(keys[0]), "crude_axe")
    set_inventory(db_path, pubkey_hex(keys[0]), {"lumber": 6, "iron": 2})
    r2 = signed_request(client, keys[0], "POST", "/world/build",
                        {"kind": "mill", "x": me["x"], "y": me["y"]})
    assert r2.status_code == 400, r2.text
    assert "already has a structure" in r2.json()["detail"]


def test_build_custom_structure(b5, monkeypatch):
    # Bible §11: "custom" is the free-form kind — 4 AP + 4 timber.
    client, keys, db_path, appmod = b5
    monkeypatch.setitem(appmod.RATE_LIMITS, "claim", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "build", (100, 60))
    me = spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    assert claim(client, keys[0], me["x"], me["y"]).status_code == 200
    set_inventory(db_path, pk, {"timber": 4})
    me_before = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    r = signed_request(client, keys[0], "POST", "/world/build",
                       {"kind": "custom", "x": me["x"], "y": me["y"],
                        "description": "a monument to hubris"})
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "custom"
    assert r.json()["ap"] == me_before["ap"] - 4
    assert inventory_of(db_path, pk) == {}


def test_build_unknown_kind_400(b5, monkeypatch):
    # Kinds the Bible does not name do not ship — no flavor fallback.
    client, keys, db_path, appmod = b5
    monkeypatch.setitem(appmod.RATE_LIMITS, "claim", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "build", (100, 60))
    me = spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    assert claim(client, keys[0], me["x"], me["y"]).status_code == 200
    set_inventory(db_path, pk, {"timber": 5})
    r = signed_request(client, keys[0], "POST", "/world/build",
                       {"kind": "statue", "x": me["x"], "y": me["y"]})
    assert r.status_code == 400, r.text
    assert "unknown structure kind" in r.json()["detail"]


def test_build_requires_owned_tool_key(b5, monkeypatch):
    # Bible §11 BUILDING_TOOL_REQUIREMENTS: furnace needs an owned
    # crude_pick (key, never consumed) — the tool row must survive.
    client, keys, db_path, appmod = b5
    monkeypatch.setitem(appmod.RATE_LIMITS, "claim", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "build", (100, 60))
    me = spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    assert claim(client, keys[0], me["x"], me["y"]).status_code == 200
    set_inventory(db_path, pk, {"stone": 4, "clay": 2, "timber": 2})
    r = signed_request(client, keys[0], "POST", "/world/build",
                       {"kind": "furnace", "x": me["x"], "y": me["y"]})
    assert r.status_code == 400, r.text
    assert "crude_pick" in r.json()["detail"]
    grant_tool(db_path, pk, "crude_pick")
    r = signed_request(client, keys[0], "POST", "/world/build",
                       {"kind": "furnace", "x": me["x"], "y": me["y"]})
    assert r.status_code == 200, r.text
    conn = db(db_path)
    try:
        row = conn.execute(
            "SELECT durability FROM tools WHERE agent_pubkey = ?"
            " AND recipe_id = 'crude_pick'",
            (pk,),
        ).fetchone()
        assert row is not None and int(row["durability"]) == 120
    finally:
        conn.close()


def test_build_insufficient_materials_400(b5, monkeypatch):
    client, keys, db_path, appmod = b5
    monkeypatch.setitem(appmod.RATE_LIMITS, "claim", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "build", (100, 60))
    me = spawn(client, keys[0])
    assert claim(client, keys[0], me["x"], me["y"]).status_code == 200
    grant_tool(db_path, pubkey_hex(keys[0]), "crude_pick")
    set_inventory(db_path, pubkey_hex(keys[0]), {"stone": 3})  # need 4+2clay+2timber
    r = signed_request(client, keys[0], "POST", "/world/build",
                       {"kind": "furnace", "x": me["x"], "y": me["y"]})
    assert r.status_code == 400, r.text
    assert "insufficient" in r.json()["detail"]


# ---------------------------------------------------------------- refine tests

def test_refine_lumber(b5, monkeypatch):
    # Bible §11: 3 timber → 2 lumber, 3 AP.
    client, keys, db_path, appmod = b5
    monkeypatch.setitem(appmod.RATE_LIMITS, "claim", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "build", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "refine", (100, 60))
    spawn(client, keys[0])
    r, _ = build_furnace(client, keys, db_path)
    assert r.status_code == 200, r.text
    pk = pubkey_hex(keys[0])
    set_inventory(db_path, pk, {"timber": 3})
    me_before = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    r = signed_request(client, keys[0], "POST", "/world/refine",
                       {"item": "lumber"})
    assert r.status_code == 200, r.text
    assert r.json()["item"] == "lumber"
    assert r.json()["gained"] == 2
    assert r.json()["ap"] == me_before["ap"] - 3
    assert inventory_of(db_path, pk) == {"lumber": 2}


def test_refine_requires_furnace_400(b5, monkeypatch):
    client, keys, db_path, appmod = b5
    monkeypatch.setitem(appmod.RATE_LIMITS, "refine", (100, 60))
    spawn(client, keys[0])
    set_inventory(db_path, pubkey_hex(keys[0]), {"timber": 3})
    r = signed_request(client, keys[0], "POST", "/world/refine",
                       {"item": "lumber"})
    assert r.status_code == 400, r.text
    assert "furnace" in r.json()["detail"]


def test_refine_unknown_recipe_400(b5, monkeypatch):
    client, keys, db_path, appmod = b5
    monkeypatch.setitem(appmod.RATE_LIMITS, "claim", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "build", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "refine", (100, 60))
    spawn(client, keys[0])
    r, _ = build_furnace(client, keys, db_path)
    assert r.status_code == 200, r.text
    r = signed_request(client, keys[0], "POST", "/world/refine",
                       {"item": "steel"})
    assert r.status_code == 400, r.text
    assert "unknown refinery recipe" in r.json()["detail"]


def test_refine_insufficient_materials_400(b5, monkeypatch):
    client, keys, db_path, appmod = b5
    monkeypatch.setitem(appmod.RATE_LIMITS, "claim", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "build", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "refine", (100, 60))
    spawn(client, keys[0])
    r, _ = build_furnace(client, keys, db_path)
    assert r.status_code == 200, r.text
    set_inventory(db_path, pubkey_hex(keys[0]), {"timber": 2})  # need 3
    r = signed_request(client, keys[0], "POST", "/world/refine",
                       {"item": "lumber"})
    assert r.status_code == 400, r.text


def test_refine_all_recipes_once(b5, monkeypatch):
    client, keys, db_path, appmod = b5
    monkeypatch.setitem(appmod.RATE_LIMITS, "claim", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "build", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "refine", (100, 60))
    spawn(client, keys[0])
    r, _ = build_furnace(client, keys, db_path)
    assert r.status_code == 200, r.text
    pk = pubkey_hex(keys[0])
    import server.world as w
    for item, (inputs, _ap, _out) in w.REFINERY_RECIPES.items():
        set_inventory(db_path, pk, dict(inputs))
        r = signed_request(client, keys[0], "POST", "/world/refine",
                           {"item": item})
        assert r.status_code == 200, (item, r.text)
        assert r.json()["gained"] == 2
        assert inventory_of(db_path, pk) == {item: 2}
        # reset for the next recipe
        conn = db(db_path)
        try:
            conn.execute("DELETE FROM inventories WHERE agent_pubkey = ?", (pk,))
            conn.commit()
        finally:
            conn.close()


def test_refine_smelt_needs_coal_fuel(b5, monkeypatch):
    # Bible §2.5 fuel coherence: every smelt burns 1 coal.
    client, keys, db_path, appmod = b5
    monkeypatch.setitem(appmod.RATE_LIMITS, "claim", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "build", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "refine", (100, 60))
    spawn(client, keys[0])
    r, _ = build_furnace(client, keys, db_path)
    assert r.status_code == 200, r.text
    pk = pubkey_hex(keys[0])
    set_inventory(db_path, pk, {"iron_ore": 3})  # no coal
    r = signed_request(client, keys[0], "POST", "/world/refine",
                       {"item": "iron"})
    assert r.status_code == 400, r.text
    set_inventory(db_path, pk, {"iron_ore": 3, "coal": 1})
    r = signed_request(client, keys[0], "POST", "/world/refine",
                       {"item": "iron"})
    assert r.status_code == 200, r.text
    assert r.json()["gained"] == 2
    assert inventory_of(db_path, pk) == {"iron": 2}


def test_refine_at_cap_400_costs_nothing(b5, monkeypatch):
    # Bible §2.5/§8: at-cap → 400, never voids outputs — the cap is
    # checked before anything is consumed, so a refused refine is free.
    client, keys, db_path, appmod = b5
    monkeypatch.setitem(appmod.RATE_LIMITS, "claim", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "build", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "refine", (100, 60))
    spawn(client, keys[0])
    r, _ = build_furnace(client, keys, db_path)
    assert r.status_code == 200, r.text
    pk = pubkey_hex(keys[0])
    set_inventory(db_path, pk, {"timber": 3, "lumber": 98})  # 98 + 2 > 99
    me_before = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    r = signed_request(client, keys[0], "POST", "/world/refine",
                       {"item": "lumber"})
    assert r.status_code == 400, r.text
    inv = inventory_of(db_path, pk)
    assert inv == {"timber": 3, "lumber": 98}  # nothing consumed
    me_after = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    assert me_after["ap"] == me_before["ap"]  # no AP charged


def test_mill_gives_no_gather_bonus(b5, monkeypatch):
    # The Bible has no mill gather bonus — owning a mill must not change
    # tooled yield (pins the reconciliation cut).
    client, keys, db_path, appmod = b5
    monkeypatch.setitem(appmod.RATE_LIMITS, "claim", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "build", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "gather", (100, 60))
    me = spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    res = TERRAIN_RESOURCE[me["terrain"]]
    pin_neutral_season(db_path, me["terrain"])
    tool = TERRAIN_TOOL[me["terrain"]]
    assert claim(client, keys[0], me["x"], me["y"]).status_code == 200
    grant_tool(db_path, pk, "crude_axe")
    set_inventory(db_path, pk, {"lumber": 6, "iron": 2})
    r = signed_request(client, keys[0], "POST", "/world/build",
                       {"kind": "mill", "x": me["x"], "y": me["y"]})
    assert r.status_code == 200, r.text
    grant_tool(db_path, pk, tool)
    g = signed_request(client, keys[0], "POST", "/world/gather",
                       {"resource": res})
    assert g.status_code == 200, g.text
    assert g.json()["gained"] == 2  # tooled 2, no mill bonus
