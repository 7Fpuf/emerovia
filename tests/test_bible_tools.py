"""Bible ch.3 — tools + durability (Systems Bible §2.3/§4.1).

Covers: bare hands (4 AP → 1), matching tool (2 AP → 2), tool auto-pick
(highest durability, lowest recipe_id tiebreak), mismatched tool →
bare hands without wear, unknown/unowned tool 400s, 1 durability wear per
gather with break at 0 (row deleted, gather stays valid), partial take on
low stock, per-item inventory cap (99; 149 with cart), and the me_view
tools/ap_cap fields.

Tools are inserted directly here — the crafting path is ch.4's job.
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
def b3(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("AC_DB_PATH", db_path)
    op_key = make_key()
    monkeypatch.setenv("AC_OPERATOR_PUBKEY", pubkey_hex(op_key))
    import server.app as appmod
    importlib.reload(appmod)
    client = TestClient(appmod.app)
    keys = [make_key() for _ in range(4)]
    for i, k in enumerate(keys):
        register(client, f"bible-tools-{i}", k)
    return client, keys, db_path, appmod


def db(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def gather(client, key, resource=None, tool=None):
    payload = {}
    if resource is not None:
        payload["resource"] = resource
    if tool is not None:
        payload["tool"] = tool
    return signed_request(client, key, "POST", "/world/gather", payload)


def give_tool(db_path, pubkey, recipe_id, durability=120):
    conn = db(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO tools"
            " (agent_pubkey, recipe_id, durability, max_durability, crafted_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (pubkey, recipe_id, durability, durability, "2026-09-23T00:00:00Z"),
        )
        conn.commit()
    finally:
        conn.close()


def tool_durability(db_path, pubkey, recipe_id):
    conn = db(db_path)
    try:
        row = conn.execute(
            "SELECT durability FROM tools WHERE agent_pubkey = ? AND recipe_id = ?",
            (pubkey, recipe_id),
        ).fetchone()
        return int(row["durability"]) if row else None
    finally:
        conn.close()


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
    deterministic under the §7 season table (plains→autumn grain 1.00,
    forest→summer timber 1.00; iron_ore/glass are 1.00 in spring)."""
    days = {"plains": 28, "forest": 20, "mountain": 0, "desert": 0}[terrain]
    set_genesis_days_ago(db_path, days)


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


def set_inventory(db_path, pubkey, resource, qty):
    conn = db(db_path)
    try:
        conn.execute(
            "INSERT INTO inventories (agent_pubkey, resource, qty) VALUES (?, ?, ?)"
            " ON CONFLICT(agent_pubkey, resource) DO UPDATE SET qty = ?",
            (pubkey, resource, qty, qty),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- tests

def test_tool_constants(b3):
    _, _, _, _ = b3
    import server.world as w
    assert set(w.CRUDE_RECIPES) == {"crude_axe", "crude_pick", "crude_sickle", "crude_sieve"}
    assert w.CRUDE_RECIPES["crude_axe"] == ({"timber": 2, "fiber": 1}, 2)
    assert w.CRUDE_RECIPES["crude_pick"] == ({"timber": 2, "iron_ore": 2}, 3)
    assert w.CRUDE_RECIPES["crude_sickle"] == ({"timber": 2, "grain": 1, "fiber": 1}, 2)
    assert w.CRUDE_RECIPES["crude_sieve"] == ({"timber": 3, "fiber": 1}, 3)
    # Every raw resource is covered by exactly one crude tool; the pick
    # additionally covers the legacy desert glass veins.
    covered = [r for cov in w.TOOL_COVERAGE.values() for r in cov]
    assert set(covered) == set(w.RAW_RESOURCES) | {"glass"}
    assert w.TOOL_DURABILITY_CRUDE == 120
    assert w.TOOL_DURABILITY_DISCOVERED == 300
    assert w.GATHER_BARE_AP == 4 and w.GATHER_BARE_YIELD == 1
    assert w.GATHER_TOOLED_AP == 2 and w.GATHER_TOOLED_YIELD == 2


def test_bare_hands_gather_costs_4_yields_1(b3, monkeypatch):
    client, keys, db_path, appmod = b3
    monkeypatch.setitem(appmod.RATE_LIMITS, "gather", (100, 60))
    spawn(client, keys[0])
    me = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    res = TERRAIN_RESOURCE[me["terrain"]]
    r = gather(client, keys[0], res)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["gained"] == 1
    assert body["tooled"] is False
    assert body["tool"] is None
    assert body["tool_broke"] is False
    assert body["ap"] == me["ap"] - 4


def test_matching_tool_gather_costs_2_yields_2(b3, monkeypatch):
    client, keys, db_path, appmod = b3
    monkeypatch.setitem(appmod.RATE_LIMITS, "gather", (100, 60))
    spawn(client, keys[0])
    me = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    pin_neutral_season(db_path, me["terrain"])
    res = TERRAIN_RESOURCE[me["terrain"]]
    tool = TERRAIN_TOOL[me["terrain"]]
    give_tool(db_path, pubkey_hex(keys[0]), tool)
    r = gather(client, keys[0], res)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["gained"] == 2
    assert body["tooled"] is True
    assert body["tool"] == tool
    assert body["ap"] == me["ap"] - 2
    assert tool_durability(db_path, pubkey_hex(keys[0]), tool) == 119


def test_auto_pick_uses_covering_tool(b3, monkeypatch):
    client, keys, db_path, appmod = b3
    monkeypatch.setitem(appmod.RATE_LIMITS, "gather", (100, 60))
    spawn(client, keys[0])
    me = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    res = TERRAIN_RESOURCE[me["terrain"]]
    right = TERRAIN_TOOL[me["terrain"]]
    wrong = "crude_axe" if right != "crude_axe" else "crude_sieve"
    pk = pubkey_hex(keys[0])
    give_tool(db_path, pk, wrong)
    give_tool(db_path, pk, right)
    r = gather(client, keys[0], res)  # no tool named: auto-pick
    assert r.status_code == 200, r.text
    assert r.json()["tool"] == right
    assert r.json()["tooled"] is True
    # The non-covering tool was not worn.
    assert tool_durability(db_path, pk, wrong) == 120


def test_mismatched_tool_falls_back_to_bare_hands(b3, monkeypatch):
    client, keys, db_path, appmod = b3
    monkeypatch.setitem(appmod.RATE_LIMITS, "gather", (100, 60))
    spawn(client, keys[0])
    me = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    res = TERRAIN_RESOURCE[me["terrain"]]
    wrong = "crude_axe" if TERRAIN_TOOL[me["terrain"]] != "crude_axe" else "crude_sieve"
    pk = pubkey_hex(keys[0])
    give_tool(db_path, pk, wrong)
    r = gather(client, keys[0], res, tool=wrong)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tooled"] is False
    assert body["gained"] == 1
    assert body["ap"] == me["ap"] - 4
    assert tool_durability(db_path, pk, wrong) == 120  # not worn


def test_unknown_tool_400(b3, monkeypatch):
    client, keys, db_path, appmod = b3
    monkeypatch.setitem(appmod.RATE_LIMITS, "gather", (100, 60))
    spawn(client, keys[0])
    me = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    res = TERRAIN_RESOURCE[me["terrain"]]
    r = gather(client, keys[0], res, tool="crude_hammer")
    assert r.status_code == 400, r.text
    assert "unknown tool" in r.json()["detail"]


def test_unowned_tool_400(b3, monkeypatch):
    client, keys, db_path, appmod = b3
    monkeypatch.setitem(appmod.RATE_LIMITS, "gather", (100, 60))
    spawn(client, keys[0])
    me = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    res = TERRAIN_RESOURCE[me["terrain"]]
    r = gather(client, keys[0], res, tool=TERRAIN_TOOL[me["terrain"]])
    assert r.status_code == 400, r.text
    assert "not owned" in r.json()["detail"]


def test_tool_breaks_at_zero_durability(b3, monkeypatch):
    client, keys, db_path, appmod = b3
    monkeypatch.setitem(appmod.RATE_LIMITS, "gather", (100, 60))
    spawn(client, keys[0])
    me = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    res = TERRAIN_RESOURCE[me["terrain"]]
    tool = TERRAIN_TOOL[me["terrain"]]
    pk = pubkey_hex(keys[0])
    pin_neutral_season(db_path, me["terrain"])
    give_tool(db_path, pk, tool, durability=2)
    r1 = gather(client, keys[0], res)
    assert r1.status_code == 200, r1.text
    assert r1.json()["tool_broke"] is False
    assert tool_durability(db_path, pk, tool) == 1
    r2 = gather(client, keys[0], res)
    assert r2.status_code == 200, r2.text
    assert r2.json()["tool_broke"] is True
    assert r2.json()["gained"] == 2  # the breaking gather still yields
    assert tool_durability(db_path, pk, tool) is None  # row deleted
    # Now bare hands again.
    r3 = gather(client, keys[0], res)
    assert r3.status_code == 200, r3.text
    assert r3.json()["tooled"] is False
    assert r3.json()["gained"] == 1


def test_partial_take_on_low_stock(b3, monkeypatch):
    client, keys, db_path, appmod = b3
    monkeypatch.setitem(appmod.RATE_LIMITS, "gather", (100, 60))
    spawn(client, keys[0])
    me = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    res = TERRAIN_RESOURCE[me["terrain"]]
    tool = TERRAIN_TOOL[me["terrain"]]
    give_tool(db_path, pubkey_hex(keys[0]), tool)
    conn = db(db_path)
    try:
        conn.execute(
            "UPDATE world_resource_stock SET stock = 1 WHERE x = ? AND y = ? AND resource = ?",
            (me["x"], me["y"], res),
        )
        conn.commit()
    finally:
        conn.close()
    r = gather(client, keys[0], res)
    assert r.status_code == 200, r.text
    assert r.json()["gained"] == 1  # clamped to stock
    assert r.json()["stock_remaining"] == 0


def test_inventory_cap_blocks_overfill(b3, monkeypatch):
    client, keys, db_path, appmod = b3
    monkeypatch.setitem(appmod.RATE_LIMITS, "gather", (100, 60))
    spawn(client, keys[0])
    me = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    res = TERRAIN_RESOURCE[me["terrain"]]
    tool = TERRAIN_TOOL[me["terrain"]]
    pk = pubkey_hex(keys[0])
    pin_neutral_season(db_path, me["terrain"])
    give_tool(db_path, pk, tool)
    set_inventory(db_path, pk, res, 98)  # 98 + 2 > 99
    r = gather(client, keys[0], res)
    assert r.status_code == 400, r.text
    assert "inventory cap reached" in r.json()["detail"]
    # With a cart the cap is 149: same fill level now fits.
    give_tool(db_path, pk, "cart")
    r = gather(client, keys[0], res)
    assert r.status_code == 200, r.text
    assert r.json()["gained"] == 2


def test_insufficient_ap_scales_with_mode(b3, monkeypatch):
    client, keys, db_path, appmod = b3
    monkeypatch.setitem(appmod.RATE_LIMITS, "gather", (100, 60))
    spawn(client, keys[0])
    me = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    res = TERRAIN_RESOURCE[me["terrain"]]
    tool = TERRAIN_TOOL[me["terrain"]]
    pk = pubkey_hex(keys[0])
    give_tool(db_path, pk, tool)
    set_ap(db_path, pk, 3)
    assert gather(client, keys[0], res, tool=tool).status_code == 200  # 2 AP ok
    set_ap(db_path, pk, 1)
    assert gather(client, keys[0], res, tool=tool).status_code == 402
    r = gather(client, keys[0], res)  # bare hands needs 4
    assert r.status_code == 402, r.text
    assert "insufficient AP" in r.json()["detail"]


def test_me_view_lists_tools_and_effective_cap(b3, monkeypatch):
    client, keys, db_path, appmod = b3
    spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    give_tool(db_path, pk, "crude_axe")
    me = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    assert me["tools"] == ["crude_axe"]
    assert me["ap_cap"] == 100  # no bonuses yet
