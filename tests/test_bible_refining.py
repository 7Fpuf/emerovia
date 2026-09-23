"""Bible ch.5 — refining + buildings (Systems Bible §5/§6).

Covers: land claims (6/agent, 3-tile radius, 2 AP, land-only, permanent),
structure raising on claimed land (functional furnace/mill/shelter costs,
flavor fallback, one per tile), the refinery (owned furnace required,
fixed inputs + AP, 1 unit out), and the mill's +1 timber gather bonus.
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
    assert body["ap"] == me["ap"] - 2
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

def build_furnace(client, keys, db_path, key_idx=0):
    me = signed_request(client, keys[key_idx], "GET", "/world/me", {}).json()
    pk = pubkey_hex(keys[key_idx])
    assert claim(client, keys[key_idx], me["x"], me["y"]).status_code == 200
    set_inventory(db_path, pk, {"stone": 10})
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
    # 2 AP claim + 10 AP build.
    assert body["ap"] == me_before["ap"] - 12
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
    set_inventory(db_path, pubkey_hex(keys[0]), {"timber": 10})
    r2 = signed_request(client, keys[0], "POST", "/world/build",
                        {"kind": "mill", "x": me["x"], "y": me["y"]})
    assert r2.status_code == 400, r2.text
    assert "already has a structure" in r2.json()["detail"]


def test_build_flavor_structure(b5, monkeypatch):
    client, keys, db_path, appmod = b5
    monkeypatch.setitem(appmod.RATE_LIMITS, "claim", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "build", (100, 60))
    me = spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    assert claim(client, keys[0], me["x"], me["y"]).status_code == 200
    set_inventory(db_path, pk, {"timber": 5})
    me_before = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    r = signed_request(client, keys[0], "POST", "/world/build",
                       {"kind": "statue", "x": me["x"], "y": me["y"],
                        "description": "a monument to hubris"})
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "statue"
    assert r.json()["ap"] == me_before["ap"] - 5  # flavor cost: 5 AP


def test_build_insufficient_materials_400(b5, monkeypatch):
    client, keys, db_path, appmod = b5
    monkeypatch.setitem(appmod.RATE_LIMITS, "claim", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "build", (100, 60))
    me = spawn(client, keys[0])
    assert claim(client, keys[0], me["x"], me["y"]).status_code == 200
    set_inventory(db_path, pubkey_hex(keys[0]), {"stone": 9})  # need 10
    r = signed_request(client, keys[0], "POST", "/world/build",
                       {"kind": "furnace", "x": me["x"], "y": me["y"]})
    assert r.status_code == 400, r.text
    assert "insufficient" in r.json()["detail"]


# ---------------------------------------------------------------- refine tests

def test_refine_lumber(b5, monkeypatch):
    client, keys, db_path, appmod = b5
    monkeypatch.setitem(appmod.RATE_LIMITS, "claim", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "build", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "refine", (100, 60))
    spawn(client, keys[0])
    r, _ = build_furnace(client, keys, db_path)
    assert r.status_code == 200, r.text
    pk = pubkey_hex(keys[0])
    set_inventory(db_path, pk, {"timber": 2})
    me_before = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    r = signed_request(client, keys[0], "POST", "/world/refine",
                       {"item": "lumber"})
    assert r.status_code == 200, r.text
    assert r.json()["item"] == "lumber"
    assert r.json()["gained"] == 1
    assert r.json()["ap"] == me_before["ap"] - 4
    assert inventory_of(db_path, pk) == {"lumber": 1}


def test_refine_requires_furnace_400(b5, monkeypatch):
    client, keys, db_path, appmod = b5
    monkeypatch.setitem(appmod.RATE_LIMITS, "refine", (100, 60))
    spawn(client, keys[0])
    set_inventory(db_path, pubkey_hex(keys[0]), {"timber": 2})
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
    set_inventory(db_path, pubkey_hex(keys[0]), {"timber": 1})  # need 2
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
    for item, (inputs, _) in w.REFINERY_RECIPES.items():
        set_inventory(db_path, pk, dict(inputs))
        r = signed_request(client, keys[0], "POST", "/world/refine",
                           {"item": item})
        assert r.status_code == 200, (item, r.text)
        assert inventory_of(db_path, pk) == {item: 1}
        # reset for the next recipe
        conn = db(db_path)
        try:
            conn.execute("DELETE FROM inventories WHERE agent_pubkey = ?", (pk,))
            conn.commit()
        finally:
            conn.close()


def test_mill_timber_gather_bonus(b5, monkeypatch):
    client, keys, db_path, appmod = b5
    monkeypatch.setitem(appmod.RATE_LIMITS, "claim", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "build", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "gather", (100, 60))
    me = spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    res = TERRAIN_RESOURCE[me["terrain"]]
    tool = TERRAIN_TOOL[me["terrain"]]
    assert claim(client, keys[0], me["x"], me["y"]).status_code == 200
    set_inventory(db_path, pk, {"timber": 10})
    r = signed_request(client, keys[0], "POST", "/world/build",
                       {"kind": "mill", "x": me["x"], "y": me["y"]})
    assert r.status_code == 200, r.text
    conn = db(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO tools (agent_pubkey, recipe_id, durability,"
            " max_durability, crafted_at) VALUES (?, ?, 120, 120, ?)",
            (pk, tool, "2026-09-23T00:00:00Z"),
        )
        conn.commit()
    finally:
        conn.close()
    g = signed_request(client, keys[0], "POST", "/world/gather",
                       {"resource": res})
    assert g.status_code == 200, g.text
    # Tooled 2, +1 only for timber while the owner holds a mill.
    expected = 3 if res == "timber" else 2
    assert g.json()["gained"] == expected
