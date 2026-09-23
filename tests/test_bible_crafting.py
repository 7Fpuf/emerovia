"""Bible ch.4 — crafting + discovery (Systems Bible §2.3/§4).

Covers: the 12-combination genesis draw (deterministic, 352-candidate
space), crude crafting (inputs + AP, one per agent), hidden recipes
uncraftable before discovery (404), experiments (2-3 distinct canonical
items, 1-4 each; 2 AP + materials consumed match or not), first-match
inventor carving (public, forever), later matches granting the tool
without re-carving, already-owned 400s, and the bounty passive (+1
gather yield while owned).
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
TERRAIN_BOUNTY = {"plains": "grain_bounty", "forest": "timber_bounty",
                  "mountain": "ore_bounty", "desert": "glass_bounty"}


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
def b4(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("AC_DB_PATH", db_path)
    op_key = make_key()
    monkeypatch.setenv("AC_OPERATOR_PUBKEY", pubkey_hex(op_key))
    import server.app as appmod
    importlib.reload(appmod)
    client = TestClient(appmod.app)
    keys = [make_key() for _ in range(4)]
    for i, k in enumerate(keys):
        register(client, f"bible-craft-{i}", k)
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


def recipe_inputs(recipe_id):
    import server.world as w
    for r in w.generate_hidden_recipes():
        if r["recipe_id"] == recipe_id:
            return {item: int(q) for item, q in
                    (p.split(":") for p in r["inputs_json"].split("+"))}
    raise AssertionError(f"no genesis recipe {recipe_id}")


def non_matching_combo():
    import server.world as w
    known = {r["inputs_json"] for r in w.generate_hidden_recipes()}
    for key, inputs in w._all_discovery_combinations():
        if key not in known:
            return dict(inputs)
    raise AssertionError("every combo matched?!")  # 352 >> 12


# ---------------------------------------------------------------- tests

def test_genesis_draw_deterministic(b4):
    _, _, db_path, _ = b4
    import server.world as w
    assert len(w._all_discovery_combinations()) == 352
    first = w.generate_hidden_recipes()
    assert w.generate_hidden_recipes() == first  # pure + deterministic
    assert len(first) == 12
    assert {r["recipe_id"] for r in first} == {e["recipe_id"] for e in w.DISCOVERED_EFFECTS}
    conn = db(db_path)
    try:
        n = conn.execute("SELECT COUNT(*) FROM recipes_hidden").fetchone()[0]
        assert n == 12
        hidden = conn.execute(
            "SELECT COUNT(*) FROM recipes_hidden WHERE inventor_pubkey IS NULL"
        ).fetchone()[0]
        assert hidden == 12
    finally:
        conn.close()


def test_recipe_book_starts_empty(b4):
    client, keys, _, _ = b4
    spawn(client, keys[0])
    r = signed_request(client, keys[0], "GET", "/world/recipes", {})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["discovered"] == []
    assert body["still_hidden"] == 12


def test_craft_crude_tool(b4, monkeypatch):
    client, keys, db_path, appmod = b4
    monkeypatch.setitem(appmod.RATE_LIMITS, "craft", (100, 60))
    spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    set_inventory(db_path, pk, {"timber": 2, "fiber": 1})
    me_before = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    r = signed_request(client, keys[0], "POST", "/world/craft",
                       {"recipe_id": "crude_axe"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["recipe_id"] == "crude_axe"
    assert body["durability"] == 120
    assert body["ap"] == me_before["ap"] - 2
    assert inventory_of(db_path, pk) == {}  # materials consumed
    me = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    assert me["tools"] == ["crude_axe"]


def test_craft_crude_insufficient_materials_400(b4, monkeypatch):
    client, keys, db_path, appmod = b4
    monkeypatch.setitem(appmod.RATE_LIMITS, "craft", (100, 60))
    spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    set_inventory(db_path, pk, {"timber": 1})  # need 2 timber + 1 fiber
    r = signed_request(client, keys[0], "POST", "/world/craft",
                       {"recipe_id": "crude_axe"})
    assert r.status_code == 400, r.text
    assert "insufficient" in r.json()["detail"]


def test_craft_unknown_recipe_404(b4, monkeypatch):
    client, keys, _, appmod = b4
    monkeypatch.setitem(appmod.RATE_LIMITS, "craft", (100, 60))
    spawn(client, keys[0])
    r = signed_request(client, keys[0], "POST", "/world/craft",
                       {"recipe_id": "nonsense_tool"})
    assert r.status_code == 404, r.text


def test_craft_undiscovered_hidden_recipe_404(b4, monkeypatch):
    client, keys, _, appmod = b4
    monkeypatch.setitem(appmod.RATE_LIMITS, "craft", (100, 60))
    spawn(client, keys[0])
    # A real hidden recipe id, still undiscovered: indistinguishable from
    # unknown — 404, no hint that it exists.
    r = signed_request(client, keys[0], "POST", "/world/craft",
                       {"recipe_id": "plow"})
    assert r.status_code == 404, r.text
    assert "not yet discovered" in r.json()["detail"]


def test_craft_twice_400(b4, monkeypatch):
    client, keys, db_path, appmod = b4
    monkeypatch.setitem(appmod.RATE_LIMITS, "craft", (100, 60))
    spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    set_inventory(db_path, pk, {"timber": 4, "fiber": 2})
    r1 = signed_request(client, keys[0], "POST", "/world/craft",
                        {"recipe_id": "crude_axe"})
    assert r1.status_code == 200, r1.text
    r2 = signed_request(client, keys[0], "POST", "/world/craft",
                        {"recipe_id": "crude_axe"})
    assert r2.status_code == 400, r2.text
    assert "already crafted" in r2.json()["detail"]


def test_experiment_validation_400s(b4, monkeypatch):
    client, keys, db_path, appmod = b4
    monkeypatch.setitem(appmod.RATE_LIMITS, "experiment", (100, 60))
    spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    set_inventory(db_path, pk, {"glass": 4, "grain": 4, "iron_ore": 4,
                                "timber": 4, "lumber": 4})
    bad = [
        {"glass": 2},                                            # 1 item
        {"glass": 2, "grain": 1, "iron_ore": 1, "timber": 1},     # 4 items
        {"glass": 2, "lumber": 1},                                # non-canonical
        {"glass": 5, "grain": 1},                                 # qty 5
        {"glass": 0, "grain": 1},                                 # qty 0
    ]
    for items in bad:
        r = signed_request(client, keys[0], "POST", "/world/experiment",
                           {"items": items})
        assert r.status_code == 400, (items, r.text)


def test_experiment_no_match_consumes_and_reports_cleanly(b4, monkeypatch):
    client, keys, db_path, appmod = b4
    monkeypatch.setitem(appmod.RATE_LIMITS, "experiment", (100, 60))
    spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    items = non_matching_combo()
    set_inventory(db_path, pk, items)
    me_before = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    r = signed_request(client, keys[0], "POST", "/world/experiment",
                       {"items": items})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["discovered"] is False
    assert body["recipe_id"] is None
    assert body["ap"] == me_before["ap"] - 2
    assert inventory_of(db_path, pk) == {}  # materials consumed on a miss


def test_experiment_first_match_carves_inventor(b4, monkeypatch):
    client, keys, db_path, appmod = b4
    monkeypatch.setitem(appmod.RATE_LIMITS, "experiment", (100, 60))
    spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    items = recipe_inputs("cart")
    set_inventory(db_path, pk, items)
    r = signed_request(client, keys[0], "POST", "/world/experiment",
                       {"items": items})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["discovered"] is True
    assert body["recipe_id"] == "cart"
    assert body["carved_inventor"] == "bible-craft-0"
    conn = db(db_path)
    try:
        row = conn.execute(
            "SELECT inventor_pubkey, inventor_name, discovered_at FROM recipes_hidden"
            " WHERE recipe_id = 'cart'"
        ).fetchone()
        assert row["inventor_pubkey"] == pk
        assert row["inventor_name"] == "bible-craft-0"
        assert row["discovered_at"]
        tool = conn.execute(
            "SELECT durability, max_durability FROM tools"
            " WHERE agent_pubkey = ? AND recipe_id = 'cart'",
            (pk,),
        ).fetchone()
        assert tool is not None
        assert int(tool["durability"]) == 300  # discovered durability
    finally:
        conn.close()
    book = signed_request(client, keys[0], "GET", "/world/recipes", {}).json()
    assert book["still_hidden"] == 11
    carved = [d for d in book["discovered"] if d["recipe_id"] == "cart"][0]
    assert carved["inventor"] == "bible-craft-0"


def test_experiment_later_match_grants_tool_without_recarve(b4, monkeypatch):
    client, keys, db_path, appmod = b4
    monkeypatch.setitem(appmod.RATE_LIMITS, "experiment", (100, 60))
    spawn(client, keys[0])
    spawn(client, keys[1])
    items = recipe_inputs("cart")
    set_inventory(db_path, pubkey_hex(keys[0]), items)
    r0 = signed_request(client, keys[0], "POST", "/world/experiment",
                        {"items": items})
    assert r0.json()["discovered"] is True
    set_inventory(db_path, pubkey_hex(keys[1]), items)
    r1 = signed_request(client, keys[1], "POST", "/world/experiment",
                        {"items": items})
    assert r1.status_code == 200, r1.text
    assert r1.json()["discovered"] is False  # already carved
    assert r1.json()["recipe_id"] == "cart"  # ...but the tool is granted
    conn = db(db_path)
    try:
        row = conn.execute(
            "SELECT inventor_name FROM recipes_hidden WHERE recipe_id = 'cart'"
        ).fetchone()
        assert row["inventor_name"] == "bible-craft-0"  # first carve stands
        tool = conn.execute(
            "SELECT 1 FROM tools WHERE agent_pubkey = ? AND recipe_id = 'cart'",
            (pubkey_hex(keys[1]),),
        ).fetchone()
        assert tool is not None
    finally:
        conn.close()


def test_experiment_already_owned_400(b4, monkeypatch):
    client, keys, db_path, appmod = b4
    monkeypatch.setitem(appmod.RATE_LIMITS, "experiment", (100, 60))
    spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    items = recipe_inputs("cart")
    set_inventory(db_path, pk, items)
    assert signed_request(client, keys[0], "POST", "/world/experiment",
                          {"items": items}).status_code == 200
    set_inventory(db_path, pk, items)
    r = signed_request(client, keys[0], "POST", "/world/experiment",
                       {"items": items})
    assert r.status_code == 400, r.text
    assert "already crafted" in r.json()["detail"]


def test_craft_discovered_recipe(b4, monkeypatch):
    client, keys, db_path, appmod = b4
    monkeypatch.setitem(appmod.RATE_LIMITS, "experiment", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "craft", (100, 60))
    spawn(client, keys[0])
    spawn(client, keys[2])
    items = recipe_inputs("cart")
    set_inventory(db_path, pubkey_hex(keys[0]), items)
    assert signed_request(client, keys[0], "POST", "/world/experiment",
                          {"items": items}).json()["discovered"] is True
    # A third agent crafts the now-public recipe directly (5 AP + inputs).
    pk2 = pubkey_hex(keys[2])
    set_inventory(db_path, pk2, items)
    me_before = signed_request(client, keys[2], "GET", "/world/me", {}).json()
    r = signed_request(client, keys[2], "POST", "/world/craft",
                       {"recipe_id": "cart"})
    assert r.status_code == 200, r.text
    assert r.json()["durability"] == 300
    assert r.json()["ap"] == me_before["ap"] - 5


def test_bounty_tool_adds_gather_yield(b4, monkeypatch):
    client, keys, db_path, appmod = b4
    monkeypatch.setitem(appmod.RATE_LIMITS, "experiment", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "gather", (100, 60))
    spawn(client, keys[0])
    me = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    res = TERRAIN_RESOURCE[me["terrain"]]
    tool = TERRAIN_TOOL[me["terrain"]]
    bounty = TERRAIN_BOUNTY[me["terrain"]]
    pk = pubkey_hex(keys[0])
    set_inventory(db_path, pk, recipe_inputs(bounty))
    r = signed_request(client, keys[0], "POST", "/world/experiment",
                       {"items": recipe_inputs(bounty)})
    assert r.json()["discovered"] is True
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
    # Tooled 2 + bounty 1 = 3.
    assert g.json()["gained"] == 3
    assert g.json()["tooled"] is True
