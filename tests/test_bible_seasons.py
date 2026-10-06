"""Bible — seasons (Systems Bible §7).

Covers: season_index = (days_since_genesis // 14) % 4
(Spring → Summer → Autumn → Winter), the per-resource multiplier table,
tooled-gather yield adjustment (>=1.25 → +1; <=0.50 → -1, min 1; bare
hands unaffected; farmed grain seasonless), and publication in
/world/info.seasons.
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
def b10(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("AC_DB_PATH", db_path)
    op_key = make_key()
    monkeypatch.setenv("AC_OPERATOR_PUBKEY", pubkey_hex(op_key))
    import server.app as appmod
    importlib.reload(appmod)
    client = TestClient(appmod.app)
    keys = [make_key() for _ in range(2)]
    for i, k in enumerate(keys):
        register(client, f"bible-season-{i}", k)
    return client, keys, db_path, appmod


def db(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def set_genesis_days_ago(db_path, days):
    """Force the season clock: pretend the world was seeded `days` ago."""
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


def plains_tile_with(db_path, resource):
    """A plains tile currently stocking `resource` (overlay is 50/50)."""
    conn = db(db_path)
    try:
        row = conn.execute(
            "SELECT s.x, s.y FROM world_resource_stock s"
            " JOIN world_tiles t ON t.x = s.x AND t.y = s.y"
            " WHERE t.terrain = 'plains' AND s.resource = ? AND s.stock > 0"
            " LIMIT 1",
            (resource,),
        ).fetchone()
        assert row is not None, f"no plains tile stocks {resource}"
        return (row["x"], row["y"])
    finally:
        conn.close()


# ---------------------------------------------------------------- tests

def test_season_index_math(b10):
    _, _, db_path, _ = b10
    import server.world as w
    for days, expected in [(0, 0), (13, 0), (14, 1), (27, 1),
                           (28, 2), (42, 3), (55, 3), (56, 0), (70, 1)]:
        set_genesis_days_ago(db_path, days)
        now = time.time()  # captured after the set: (now - genesis) >= days * 86400
        conn = db(db_path)
        try:
            assert w.season_index_at(conn, now) == expected, days
            assert w.SEASON_ORDER[expected] == ["spring", "summer", "autumn", "winter"][expected]
        finally:
            conn.close()


def test_season_info_published(b10):
    client, keys, db_path, _ = b10
    spawn(client, keys[0])
    set_genesis_days_ago(db_path, 20)  # day 20 → summer (index 1)
    info = client.get("/world/info").json()
    seasons = info["seasons"]
    assert seasons["season"] == "summer"
    assert seasons["index"] == 1
    assert seasons["days_since_genesis"] == 20
    assert seasons["day_boundaries"] == {
        "season_started_day": 14, "season_ends_day": 28}
    assert seasons["multipliers"]["fruit"] == {
        "spring": 0.75, "summer": 1.50, "autumn": 1.25, "winter": 0.25}
    assert seasons["multipliers"]["coal"]["winter"] == 1.25


def test_season_yield_adj_table(b10):
    _, _, _, _ = b10
    import server.world as w
    S = {name: i for i, name in enumerate(w.SEASON_ORDER)}
    # >= 1.25 → +1 tooled
    assert w.season_yield_adj("fruit", S["summer"], True) == 1
    assert w.season_yield_adj("herbs", S["spring"], True) == 1
    assert w.season_yield_adj("timber", S["spring"], True) == 1
    assert w.season_yield_adj("coal", S["winter"], True) == 1
    # <= 0.50 → -1 tooled
    assert w.season_yield_adj("grain", S["winter"], True) == -1
    assert w.season_yield_adj("fruit", S["winter"], True) == -1
    assert w.season_yield_adj("herbs", S["winter"], True) == -1
    # neutral
    assert w.season_yield_adj("timber", S["summer"], True) == 0
    assert w.season_yield_adj("stone", S["winter"], True) == 0
    assert w.season_yield_adj("timber", S["winter"], True) == 0  # 0.75: no shift
    # bare hands unaffected, unknown resource → 0
    assert w.season_yield_adj("fruit", S["summer"], False) == 0
    assert w.season_yield_adj("grain", S["winter"], False) == 0
    assert w.season_yield_adj("unobtainium", S["summer"], True) == 0


def test_tooled_gather_gets_season_bonus(b10, monkeypatch):
    client, keys, db_path, appmod = b10
    monkeypatch.setitem(appmod.RATE_LIMITS, "gather", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "craft", (100, 60))
    spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    # Summer: fruit ×1.50 → tooled gather 2 + 1 = 3.
    set_genesis_days_ago(db_path, 20)
    set_inventory(db_path, pk, {"timber": 2, "grain": 1, "fiber": 1})
    r = signed_request(client, keys[0], "POST", "/world/craft",
                       {"recipe_id": "crude_sickle"})
    assert r.status_code == 200, r.text
    teleport(db_path, pk, *plains_tile_with(db_path, "fruit"))
    r = signed_request(client, keys[0], "POST", "/world/gather",
                       {"resource": "fruit"})
    assert r.status_code == 200, r.text
    assert r.json()["tooled"] is True
    assert r.json()["gained"] == 3


def test_winter_wild_grain_penalty_floors_at_one(b10, monkeypatch):
    client, keys, db_path, appmod = b10
    monkeypatch.setitem(appmod.RATE_LIMITS, "gather", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "craft", (100, 60))
    spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    # Winter: wild grain ×0.50 → tooled gather max(1, 2 - 1) = 1.
    set_genesis_days_ago(db_path, 50)
    set_inventory(db_path, pk, {"timber": 2, "grain": 1, "fiber": 1})
    r = signed_request(client, keys[0], "POST", "/world/craft",
                       {"recipe_id": "crude_sickle"})
    assert r.status_code == 200, r.text
    teleport(db_path, pk, *plains_tile_with(db_path, "fruit"))
    r = signed_request(client, keys[0], "POST", "/world/gather",
                       {"resource": "grain"})
    assert r.status_code == 200, r.text
    assert r.json()["gained"] == 1


def test_bare_hands_unaffected_by_season(b10, monkeypatch):
    client, keys, db_path, appmod = b10
    monkeypatch.setitem(appmod.RATE_LIMITS, "gather", (100, 60))
    spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    set_genesis_days_ago(db_path, 20)  # summer: fruit ×1.50
    teleport(db_path, pk, *plains_tile_with(db_path, "fruit"))
    r = signed_request(client, keys[0], "POST", "/world/gather",
                       {"resource": "fruit"})
    assert r.status_code == 200, r.text
    assert r.json()["tooled"] is False
    assert r.json()["gained"] == 1
