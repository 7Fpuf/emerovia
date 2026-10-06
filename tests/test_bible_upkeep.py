"""Bible ch.7 — upkeep (Systems Bible §4.2/§11).

Covers: the per-kind upkeep bundles (shelter 2 timber / farm 2 grain /
workshop 1 lumber + 1 iron / mill 2 lumber / relay 1 copper + 1 glass /
furnace 2 coal / embassy 1 brick + 1 copper / custom 1 timber per 7-day
week), the entry hook auto-paying affordable full weeks on every owner
mutation (incl. eat), all-or-nothing multi-resource weeks, dereliction
at 4+ weeks behind (no furnace refining, no shelter AP cap, inert farm
plots — never auto-demolished), and catch-up via POST /world/tithe
restoring derelict structures.
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

# Bible §11 STRUCTURE_COSTS + tool keys needed by the build helper.
BUILD_NEEDS = {
    "shelter": ({"timber": 3, "fiber": 1}, None),
    "farm": ({"timber": 2, "grain": 2}, "crude_sickle"),
    "workshop": ({"lumber": 4, "iron": 2}, "crude_axe"),
    "mill": ({"lumber": 6, "iron": 2}, "crude_axe"),
    "relay": ({"lumber": 6, "copper": 2, "glass": 2, "fiber": 2}, "crude_pick"),
    "embassy": ({"lumber": 6, "brick": 2, "copper": 2, "glass": 2}, None),
    "furnace": ({"stone": 4, "clay": 2, "timber": 2}, "crude_pick"),
    "custom": ({"timber": 4}, None),
}


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
def b7(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("AC_DB_PATH", db_path)
    op_key = make_key()
    monkeypatch.setenv("AC_OPERATOR_PUBKEY", pubkey_hex(op_key))
    import server.app as appmod
    importlib.reload(appmod)
    client = TestClient(appmod.app)
    keys = [make_key() for _ in range(4)]
    for i, k in enumerate(keys):
        register(client, f"bible-upkeep-{i}", k)
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


def build_on_own_tile(client, keys, db_path, kind, key_idx=0):
    materials, tool = BUILD_NEEDS[kind]
    me = signed_request(client, keys[key_idx], "GET", "/world/me", {}).json()
    pk = pubkey_hex(keys[key_idx])
    assert signed_request(client, keys[key_idx], "POST", "/world/claim",
                          {"x": me["x"], "y": me["y"]}).status_code == 200
    if tool:
        grant_tool(db_path, pk, tool)
    set_inventory(db_path, pk, dict(materials))
    r = signed_request(client, keys[key_idx], "POST", "/world/build",
                       {"kind": kind, "x": me["x"], "y": me["y"]})
    assert r.status_code == 200, r.text
    return r.json()["id"], me


def age_structure(db_path, structure_id, weeks):
    import server.world as w
    conn = db(db_path)
    try:
        conn.execute(
            "UPDATE structures SET last_tithe_week = ? WHERE id = ?",
            (w._tithe_week(time.time()) - weeks, structure_id),
        )
        conn.commit()
    finally:
        conn.close()


def tithe_week_of(db_path, structure_id):
    conn = db(db_path)
    try:
        return int(conn.execute(
            "SELECT last_tithe_week FROM structures WHERE id = ?",
            (structure_id,),
        ).fetchone()["last_tithe_week"])
    finally:
        conn.close()


def teleport(db_path, pubkey, x, y):
    conn = db(db_path)
    try:
        conn.execute(
            "UPDATE agent_world SET x = ?, y = ? WHERE agent_id ="
            " (SELECT id FROM agents WHERE pubkey = ?)",
            (x, y, pubkey),
        )
        conn.commit()
    finally:
        conn.close()


def non_plains_land_tile(db_path):
    """A land tile whose terrain resource is not grain.

    The hook tests assert exact coal/timber counts after a gather;
    gathering on a plains tile would add grain and gathering the owed
    resource itself would muddy the count. A non-plains tile keeps the
    assertions deterministic while the entry hook still fires.
    """
    conn = db(db_path)
    try:
        row = conn.execute(
            "SELECT x, y, terrain FROM world_tiles WHERE terrain IN"
            " ('forest', 'mountain', 'desert') LIMIT 1"
        ).fetchone()
        return (row["x"], row["y"], row["terrain"])
    finally:
        conn.close()


def open_buckets(appmod, monkeypatch):
    for bucket in ("claim", "build", "gather", "plant", "harvest",
                   "refine", "tithe", "eat"):
        monkeypatch.setitem(appmod.RATE_LIMITS, bucket, (100, 60))


# ---------------------------------------------------------------- tests

def test_upkeep_constants(b7):
    _, _, _, _ = b7
    import server.world as w
    assert w.UPKEEP_PER_KIND == {
        "shelter": {"timber": 2},
        "farm": {"grain": 2},
        "workshop": {"lumber": 1, "iron": 1},
        "mill": {"lumber": 2},
        "relay": {"copper": 1, "glass": 1},
        "furnace": {"coal": 2},
        "embassy": {"brick": 1, "copper": 1},
        "custom": {"timber": 1},
    }
    assert w.DERELICT_WEEKS == 4


def test_entry_hook_auto_pays_arrears(b7, monkeypatch):
    client, keys, db_path, appmod = b7
    open_buckets(appmod, monkeypatch)
    spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    sid, _ = build_on_own_tile(client, keys, db_path, "furnace")
    age_structure(db_path, sid, 2)  # 2 weeks × 2 coal owed
    set_inventory(db_path, pk, {"coal": 4})
    import server.world as w
    now_week = w._tithe_week(time.time())
    tx, ty, tterrain = non_plains_land_tile(db_path)
    teleport(db_path, pk, tx, ty)
    r = signed_request(client, keys[0], "POST", "/world/gather",
                       {"resource": TERRAIN_RESOURCE[tterrain]})
    assert r.status_code == 200, r.text
    assert inventory_of(db_path, pk).get("coal", 0) == 0  # 4 coal tithed
    assert tithe_week_of(db_path, sid) == now_week


def test_hook_pays_only_affordable_full_weeks(b7, monkeypatch):
    client, keys, db_path, appmod = b7
    open_buckets(appmod, monkeypatch)
    spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    sid, _ = build_on_own_tile(client, keys, db_path, "furnace")
    age_structure(db_path, sid, 2)  # owes 4 coal
    set_inventory(db_path, pk, {"coal": 3})  # affords 1 week (2)
    import server.world as w
    now_week = w._tithe_week(time.time())
    tx, ty, tterrain = non_plains_land_tile(db_path)
    teleport(db_path, pk, tx, ty)
    r = signed_request(client, keys[0], "POST", "/world/gather",
                       {"resource": TERRAIN_RESOURCE[tterrain]})
    assert r.status_code == 200, r.text
    assert inventory_of(db_path, pk).get("coal", 0) == 1  # 2 paid, 1 kept
    assert tithe_week_of(db_path, sid) == now_week - 1


def test_hook_leaves_unaffordable_arrears(b7, monkeypatch):
    client, keys, db_path, appmod = b7
    open_buckets(appmod, monkeypatch)
    spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    sid, _ = build_on_own_tile(client, keys, db_path, "furnace")
    age_structure(db_path, sid, 2)
    set_inventory(db_path, pk, {"coal": 1})  # less than one week's 2
    tx, ty, tterrain = non_plains_land_tile(db_path)
    teleport(db_path, pk, tx, ty)
    r = signed_request(client, keys[0], "POST", "/world/gather",
                       {"resource": TERRAIN_RESOURCE[tterrain]})
    assert r.status_code == 200, r.text
    assert inventory_of(db_path, pk).get("coal", 0) == 1  # untouched
    import server.world as w
    assert tithe_week_of(db_path, sid) == w._tithe_week(time.time()) - 2


def test_multi_resource_bundle_all_or_nothing(b7, monkeypatch):
    # Workshop: 1 lumber + 1 iron per week. A week counts only when BOTH
    # are covered — lumber alone pays nothing.
    client, keys, db_path, appmod = b7
    open_buckets(appmod, monkeypatch)
    spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    sid, _ = build_on_own_tile(client, keys, db_path, "workshop")
    age_structure(db_path, sid, 2)  # owes 2 lumber + 2 iron
    set_inventory(db_path, pk, {"lumber": 2})  # iron missing entirely
    import server.world as w
    tx, ty, tterrain = non_plains_land_tile(db_path)
    teleport(db_path, pk, tx, ty)
    r = signed_request(client, keys[0], "POST", "/world/gather",
                       {"resource": TERRAIN_RESOURCE[tterrain]})
    assert r.status_code == 200, r.text
    assert inventory_of(db_path, pk).get("lumber", 0) == 2  # nothing taken
    assert tithe_week_of(db_path, sid) == w._tithe_week(time.time()) - 2
    # Add the iron: both weeks pay at once.
    set_inventory(db_path, pk, {"lumber": 2, "iron": 2})
    timber_before = inventory_of(db_path, pk).get("timber", 0)
    r = signed_request(client, keys[0], "POST", "/world/gather",
                       {"resource": TERRAIN_RESOURCE[tterrain]})
    assert r.status_code == 200, r.text
    inv = inventory_of(db_path, pk)
    assert inv.get("lumber", 0) == 0 and inv.get("iron", 0) == 0  # bundle paid
    assert inv.get("timber", 0) >= timber_before  # hook never takes timber
    assert tithe_week_of(db_path, sid) == w._tithe_week(time.time())


def test_eat_triggers_upkeep_hook(b7, monkeypatch):
    # The hook runs on EVERY owner mutation — including eat.
    client, keys, db_path, appmod = b7
    open_buckets(appmod, monkeypatch)
    spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    sid, _ = build_on_own_tile(client, keys, db_path, "furnace")
    age_structure(db_path, sid, 1)  # 1 week × 2 coal owed
    set_inventory(db_path, pk, {"coal": 2, "grain": 5})
    import server.world as w
    r = signed_request(client, keys[0], "POST", "/eat",
                       {"item": "grain", "qty": 1})
    assert r.status_code == 200, r.text
    assert tithe_week_of(db_path, sid) == w._tithe_week(time.time())
    assert inventory_of(db_path, pk).get("coal", 0) == 0


def test_derelict_furnace_blocks_refine(b7, monkeypatch):
    client, keys, db_path, appmod = b7
    open_buckets(appmod, monkeypatch)
    spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    sid, _ = build_on_own_tile(client, keys, db_path, "furnace")
    age_structure(db_path, sid, 4)  # derelict threshold, no coal to pay
    set_inventory(db_path, pk, {"timber": 3})
    r = signed_request(client, keys[0], "POST", "/world/refine",
                       {"item": "lumber"})
    assert r.status_code == 400, r.text
    assert "kept-up furnace" in r.json()["detail"]


def test_derelict_farm_plots_inert(b7, monkeypatch):
    client, keys, db_path, appmod = b7
    open_buckets(appmod, monkeypatch)
    spawn(client, keys[0])
    sid, _ = build_on_own_tile(client, keys, db_path, "farm")
    age_structure(db_path, sid, 5)
    r = signed_request(client, keys[0], "POST", "/world/farm",
                       {"structure_id": sid, "action": "plant", "slot": 0})
    assert r.status_code == 400, r.text
    assert "derelict" in r.json()["detail"]


def test_derelict_shelter_loses_ap_cap(b7, monkeypatch):
    client, keys, db_path, appmod = b7
    open_buckets(appmod, monkeypatch)
    spawn(client, keys[0])
    sid, _ = build_on_own_tile(client, keys, db_path, "shelter")
    me = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    assert me["ap_cap"] == 110  # kept-up shelter bonus
    age_structure(db_path, sid, 5)
    me = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    assert me["ap_cap"] == 100  # derelict: bonus gone


def test_derelict_never_auto_demolished(b7, monkeypatch):
    # Bible §4.2: derelict structures are never reclaimed — the row and
    # the claim survive indefinitely; only the gated verbs stop working.
    client, keys, db_path, appmod = b7
    open_buckets(appmod, monkeypatch)
    spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    sid, me = build_on_own_tile(client, keys, db_path, "mill")
    age_structure(db_path, sid, 10)
    tx, ty, tterrain = non_plains_land_tile(db_path)
    teleport(db_path, pk, tx, ty)
    r = signed_request(client, keys[0], "POST", "/world/gather",
                       {"resource": TERRAIN_RESOURCE[tterrain]})
    assert r.status_code == 200, r.text
    conn = db(db_path)
    try:
        assert conn.execute(
            "SELECT id FROM structures WHERE id = ?", (sid,)
        ).fetchone() is not None
        assert conn.execute(
            "SELECT 1 FROM claims WHERE x = ? AND y = ? AND owner_pubkey = ?",
            (me["x"], me["y"], pk),
        ).fetchone() is not None
    finally:
        conn.close()


def test_catch_up_tithe_restores_derelict(b7, monkeypatch):
    client, keys, db_path, appmod = b7
    open_buckets(appmod, monkeypatch)
    spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    sid, _ = build_on_own_tile(client, keys, db_path, "shelter")
    age_structure(db_path, sid, 4)  # 4 weeks × 2 timber owed
    set_inventory(db_path, pk, {"timber": 8})
    r = signed_request(client, keys[0], "POST", "/world/tithe",
                       {"structure_id": sid})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["weeks_paid"] == 4
    assert body["paid"] == {"timber": 8}
    assert body["derelict"] is False
    assert inventory_of(db_path, pk) == {}
    me = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    assert me["ap_cap"] == 110  # shelter bonus back


def test_catch_up_restores_farm_plots(b7, monkeypatch):
    client, keys, db_path, appmod = b7
    open_buckets(appmod, monkeypatch)
    spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    sid, _ = build_on_own_tile(client, keys, db_path, "farm")
    age_structure(db_path, sid, 4)  # 4 weeks × 2 grain owed
    r = signed_request(client, keys[0], "POST", "/world/farm",
                       {"structure_id": sid, "action": "plant", "slot": 0})
    assert r.status_code == 400  # derelict: inert
    set_inventory(db_path, pk, {"grain": 8})
    r = signed_request(client, keys[0], "POST", "/world/tithe",
                       {"structure_id": sid})
    assert r.status_code == 200, r.text
    assert r.json()["paid"] == {"grain": 8}
    r = signed_request(client, keys[0], "POST", "/world/farm",
                       {"structure_id": sid, "action": "plant", "slot": 0})
    assert r.status_code == 200, r.text  # plots live again


def test_tithe_nothing_owed_400(b7, monkeypatch):
    client, keys, db_path, appmod = b7
    open_buckets(appmod, monkeypatch)
    spawn(client, keys[0])
    sid, _ = build_on_own_tile(client, keys, db_path, "shelter")
    r = signed_request(client, keys[0], "POST", "/world/tithe",
                       {"structure_id": sid})
    assert r.status_code == 400, r.text
    assert "no tithe owed" in r.json()["detail"]


def test_tithe_insufficient_upkeep_400(b7, monkeypatch):
    client, keys, db_path, appmod = b7
    open_buckets(appmod, monkeypatch)
    spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    sid, _ = build_on_own_tile(client, keys, db_path, "furnace")
    age_structure(db_path, sid, 2)  # owes 4 coal
    set_inventory(db_path, pk, {"coal": 1})
    r = signed_request(client, keys[0], "POST", "/world/tithe",
                       {"structure_id": sid})
    assert r.status_code == 400, r.text
    assert "insufficient upkeep" in r.json()["detail"]


def test_tithe_other_agents_structure_400(b7, monkeypatch):
    client, keys, db_path, appmod = b7
    open_buckets(appmod, monkeypatch)
    spawn(client, keys[0])
    spawn(client, keys[1])
    sid, _ = build_on_own_tile(client, keys, db_path, "shelter", key_idx=0)
    r = signed_request(client, keys[1], "POST", "/world/tithe",
                       {"structure_id": sid})
    assert r.status_code == 400, r.text


def test_custom_structure_pays_timber_tithe(b7, monkeypatch):
    # "custom" is a real Bible kind with a real tithe (1 timber/week) —
    # not tithe-free.
    client, keys, db_path, appmod = b7
    open_buckets(appmod, monkeypatch)
    spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    sid, _ = build_on_own_tile(client, keys, db_path, "custom")
    age_structure(db_path, sid, 3)  # owes 3 timber
    set_inventory(db_path, pk, {"timber": 2})  # affords 2 weeks
    import server.world as w
    now_week = w._tithe_week(time.time())
    tx, ty, tterrain = non_plains_land_tile(db_path)
    teleport(db_path, pk, tx, ty)
    r = signed_request(client, keys[0], "POST", "/world/gather",
                       {"resource": TERRAIN_RESOURCE[tterrain]})
    assert r.status_code == 200, r.text
    inv = inventory_of(db_path, pk)
    # 2 timber tithed; the gather itself may add timber on forest tiles.
    assert inv.get("timber", 0) == (1 if tterrain == "forest" else 0)
    assert tithe_week_of(db_path, sid) == now_week - 1  # 1 week arrears left


def test_disclose_settles_upkeep_hook(b7, monkeypatch):
    client, keys, db_path, appmod = b7
    open_buckets(appmod, monkeypatch)
    spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    sid, me = build_on_own_tile(client, keys, db_path, "shelter")
    age_structure(db_path, sid, 2)  # 2 weeks x 2 timber owed
    set_inventory(db_path, pk, {"timber": 4})
    r = signed_request(client, keys[0], "POST", "/world/disclose",
                       {"x": me["x"], "y": me["y"]})
    assert r.status_code == 200, r.text
    # The entry hook auto-paid the arrears: timber gone, tithe week current.
    assert inventory_of(db_path, pk).get("timber", 0) == 0
    import server.world as w
    assert tithe_week_of(db_path, sid) == w._tithe_week(time.time())
