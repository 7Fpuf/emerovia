"""Bible ch.7 — upkeep (Systems Bible §8).

Covers: tithe rates (shelter 1 / mill 3 / furnace 5 grain per 7-day
week), the entry hook auto-paying affordable full weeks on
move/gather/craft/build/refine/farm, dereliction at 4+ weeks behind
(no furnace refining, no mill bonus, no shelter AP cap, inert farm
plots), catch-up via POST /world/tithe restoring derelict structures,
and flavor structures being tithe-free.
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


def grain_of(db_path, pubkey):
    conn = db(db_path)
    try:
        row = conn.execute(
            "SELECT qty FROM inventories WHERE agent_pubkey = ? AND resource = 'grain'",
            (pubkey,),
        ).fetchone()
        return int(row["qty"]) if row else 0
    finally:
        conn.close()


def build_on_own_tile(client, keys, db_path, kind, materials, key_idx=0):
    me = signed_request(client, keys[key_idx], "GET", "/world/me", {}).json()
    pk = pubkey_hex(keys[key_idx])
    assert signed_request(client, keys[key_idx], "POST", "/world/claim",
                          {"x": me["x"], "y": me["y"]}).status_code == 200
    set_inventory(db_path, pk, materials)
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


# ---------------------------------------------------------------- tests

def test_upkeep_constants(b7):
    _, _, _, _ = b7
    import server.world as w
    assert w.TITHE_RATES == {"shelter": 1, "mill": 3, "furnace": 5}
    assert w.DERELICT_WEEKS == 4


def test_entry_hook_auto_pays_arrears(b7, monkeypatch):
    client, keys, db_path, appmod = b7
    monkeypatch.setitem(appmod.RATE_LIMITS, "claim", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "build", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "gather", (100, 60))
    spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    sid, me = build_on_own_tile(client, keys, db_path, "furnace", {"stone": 10})
    age_structure(db_path, sid, 2)  # 2 weeks × 5 grain owed
    set_inventory(db_path, pk, {"grain": 10})
    import server.world as w
    now_week = w._tithe_week(time.time())
    r = signed_request(client, keys[0], "POST", "/world/gather",
                       {"resource": {"plains": "grain", "forest": "timber",
                                     "mountain": "iron_ore",
                                     "desert": "glass"}[me["terrain"]]})
    assert r.status_code == 200, r.text
    assert grain_of(db_path, pk) == 0  # 10 grain tithed on entry
    assert tithe_week_of(db_path, sid) == now_week


def test_hook_pays_only_affordable_full_weeks(b7, monkeypatch):
    client, keys, db_path, appmod = b7
    monkeypatch.setitem(appmod.RATE_LIMITS, "claim", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "build", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "gather", (100, 60))
    spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    sid, me = build_on_own_tile(client, keys, db_path, "furnace", {"stone": 10})
    age_structure(db_path, sid, 2)  # owes 10
    set_inventory(db_path, pk, {"grain": 7})  # affords 1 week (5)
    import server.world as w
    now_week = w._tithe_week(time.time())
    r = signed_request(client, keys[0], "POST", "/world/gather",
                       {"resource": {"plains": "grain", "forest": "timber",
                                     "mountain": "iron_ore",
                                     "desert": "glass"}[me["terrain"]]})
    assert r.status_code == 200, r.text
    assert grain_of(db_path, pk) == 2  # 5 paid, 2 kept — no partial week
    assert tithe_week_of(db_path, sid) == now_week - 1


def test_hook_leaves_unaffordable_arrears(b7, monkeypatch):
    client, keys, db_path, appmod = b7
    monkeypatch.setitem(appmod.RATE_LIMITS, "claim", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "build", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "gather", (100, 60))
    spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    sid, me = build_on_own_tile(client, keys, db_path, "furnace", {"stone": 10})
    age_structure(db_path, sid, 2)
    set_inventory(db_path, pk, {"grain": 4})  # less than one week's 5
    r = signed_request(client, keys[0], "POST", "/world/gather",
                       {"resource": {"plains": "grain", "forest": "timber",
                                     "mountain": "iron_ore",
                                     "desert": "glass"}[me["terrain"]]})
    assert r.status_code == 200, r.text
    assert grain_of(db_path, pk) == 4  # untouched
    import server.world as w
    assert tithe_week_of(db_path, sid) == w._tithe_week(time.time()) - 2


def test_derelict_furnace_blocks_refine(b7, monkeypatch):
    client, keys, db_path, appmod = b7
    monkeypatch.setitem(appmod.RATE_LIMITS, "claim", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "build", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "refine", (100, 60))
    spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    sid, _ = build_on_own_tile(client, keys, db_path, "furnace", {"stone": 10})
    age_structure(db_path, sid, 4)  # derelict threshold
    set_inventory(db_path, pk, {"timber": 2})
    r = signed_request(client, keys[0], "POST", "/world/refine",
                       {"item": "lumber"})
    assert r.status_code == 400, r.text
    assert "kept-up furnace" in r.json()["detail"]


def test_derelict_shelter_plots_inert_and_no_ap_cap(b7, monkeypatch):
    client, keys, db_path, appmod = b7
    monkeypatch.setitem(appmod.RATE_LIMITS, "claim", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "build", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "farm", (100, 60))
    spawn(client, keys[0])
    sid, _ = build_on_own_tile(client, keys, db_path, "shelter", {"timber": 8})
    age_structure(db_path, sid, 5)
    me = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    assert me["ap_cap"] == 100  # shelter bonus gone while derelict
    r = signed_request(client, keys[0], "POST", "/world/farm",
                       {"structure_id": sid, "action": "till", "slot": 0})
    assert r.status_code == 400, r.text
    assert "derelict" in r.json()["detail"]


def test_derelict_mill_loses_timber_bonus(b7, monkeypatch):
    client, keys, db_path, appmod = b7
    monkeypatch.setitem(appmod.RATE_LIMITS, "claim", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "build", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "gather", (100, 60))
    me = spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    res = TERRAIN_RESOURCE[me["terrain"]]
    tool = TERRAIN_TOOL[me["terrain"]]
    sid, _ = build_on_own_tile(client, keys, db_path, "mill", {"timber": 10})
    age_structure(db_path, sid, 4)
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
    # Tooled 2, no mill bonus (derelict) — even on timber.
    assert g.json()["gained"] == 2


def test_catch_up_tithe_restores_derelict(b7, monkeypatch):
    client, keys, db_path, appmod = b7
    monkeypatch.setitem(appmod.RATE_LIMITS, "claim", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "build", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "farm", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "tithe", (100, 60))
    spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    sid, _ = build_on_own_tile(client, keys, db_path, "shelter", {"timber": 8})
    age_structure(db_path, sid, 4)  # 4 weeks × 1 grain owed
    set_inventory(db_path, pk, {"grain": 4})
    r = signed_request(client, keys[0], "POST", "/world/tithe",
                       {"structure_id": sid})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["weeks_paid"] == 4
    assert body["grain_paid"] == 4
    assert body["derelict"] is False
    assert grain_of(db_path, pk) == 0
    # Farm plots live again.
    r = signed_request(client, keys[0], "POST", "/world/farm",
                       {"structure_id": sid, "action": "till", "slot": 0})
    assert r.status_code == 200, r.text
    me = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    assert me["ap_cap"] == 110  # shelter bonus back


def test_tithe_nothing_owed_400(b7, monkeypatch):
    client, keys, db_path, appmod = b7
    monkeypatch.setitem(appmod.RATE_LIMITS, "claim", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "build", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "tithe", (100, 60))
    spawn(client, keys[0])
    sid, _ = build_on_own_tile(client, keys, db_path, "shelter", {"timber": 8})
    r = signed_request(client, keys[0], "POST", "/world/tithe",
                       {"structure_id": sid})
    assert r.status_code == 400, r.text
    assert "no tithe owed" in r.json()["detail"]


def test_tithe_insufficient_grain_400(b7, monkeypatch):
    client, keys, db_path, appmod = b7
    monkeypatch.setitem(appmod.RATE_LIMITS, "claim", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "build", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "tithe", (100, 60))
    spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    sid, _ = build_on_own_tile(client, keys, db_path, "furnace", {"stone": 10})
    age_structure(db_path, sid, 2)  # owes 10
    set_inventory(db_path, pk, {"grain": 4})
    r = signed_request(client, keys[0], "POST", "/world/tithe",
                       {"structure_id": sid})
    assert r.status_code == 400, r.text
    assert "insufficient grain" in r.json()["detail"]


def test_tithe_other_agents_structure_400(b7, monkeypatch):
    client, keys, db_path, appmod = b7
    monkeypatch.setitem(appmod.RATE_LIMITS, "claim", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "build", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "tithe", (100, 60))
    spawn(client, keys[0])
    spawn(client, keys[1])
    sid, _ = build_on_own_tile(client, keys, db_path, "shelter", {"timber": 8},
                               key_idx=0)
    r = signed_request(client, keys[1], "POST", "/world/tithe",
                       {"structure_id": sid})
    assert r.status_code == 400, r.text


def test_flavor_structures_tithe_free(b7, monkeypatch):
    client, keys, db_path, appmod = b7
    monkeypatch.setitem(appmod.RATE_LIMITS, "claim", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "build", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "gather", (100, 60))
    spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    sid, me = build_on_own_tile(client, keys, db_path, "statue", {"timber": 5})
    age_structure(db_path, sid, 10)  # long overdue — still free
    set_inventory(db_path, pk, {"grain": 9})
    import server.world as w
    old_week = w._tithe_week(time.time()) - 10
    r = signed_request(client, keys[0], "POST", "/world/gather",
                       {"resource": TERRAIN_RESOURCE[me["terrain"]]})
    assert r.status_code == 200, r.text
    assert grain_of(db_path, pk) == 9  # nothing taken
    assert tithe_week_of(db_path, sid) == old_week  # untouched
