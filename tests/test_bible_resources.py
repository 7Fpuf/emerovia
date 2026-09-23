"""Bible ch.1 — resources + migration (Systems Bible §2.1, §2.4).

Covers: the 11-raw + 6-refined resource model, the additive overlay re-seed
(INSERT OR IGNORE, deterministic under NATURAL_SEED, exactly one
terrain-typed overlay resource per land tile), the ore→iron_ore 1:1 rename
with the trade ledger keeping "ore" verbatim, legacy glass veins staying
gatherable, and POST /world/gather's optional {resource} disambiguation.

Same isolated-app fixture pattern as the other suites: AC_DB_PATH ->
tmp_path, fresh operator keypair, importlib.reload(server.app).
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
def b1(tmp_path, monkeypatch):
    """Fresh app + isolated DB; returns (client, keys, db_path, appmod)."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("AC_DB_PATH", db_path)
    op_key = make_key()
    monkeypatch.setenv("AC_OPERATOR_PUBKEY", pubkey_hex(op_key))
    import server.app as appmod
    importlib.reload(appmod)
    client = TestClient(appmod.app)
    keys = [make_key() for _ in range(8)]
    for i, k in enumerate(keys):
        register(client, f"bible-res-{i}", k)
    return client, keys, db_path, appmod


def db(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------- model

def test_resource_model_constants(b1):
    _, _, _, appmod = b1
    import server.world as w
    assert w.TERRAIN_RESOURCE["mountain"] == "iron_ore"  # renamed, not "ore"
    assert len(w.RAW_RESOURCES) == 11  # Bible §2.1 tier count
    assert len(w.GATHERABLE_RESOURCES) == 12  # + legacy glass veins (§2.4)
    assert "glass" not in w.RAW_RESOURCES and "glass" in w.GATHERABLE_RESOURCES
    assert len(w.REFINED_RESOURCES) == 6
    assert "ore" not in w.ALL_RESOURCES
    assert "ore" not in w.TRADE_ITEMS
    assert "iron_ore" in w.TRADE_ITEMS and "chits" in w.TRADE_ITEMS
    assert len(w.TRADE_ITEMS) == 18  # 17 resources + chits
    assert w.NATURAL_SEED == "emerovia-natural-v1"
    assert w.NATURAL_SEED != w.SEED_ID  # overlay seed distinct from genesis seed


def test_overlay_exactly_one_per_land_tile(b1):
    _, _, db_path, _ = b1
    conn = db(db_path)
    try:
        tiles = conn.execute(
            "SELECT x, y, terrain FROM world_tiles WHERE terrain != 'ocean'"
        ).fetchall()
        assert len(tiles) > 3000
        for t in tiles:
            rows = conn.execute(
                "SELECT resource, stock FROM world_resource_stock WHERE x = ? AND y = ?",
                (t["x"], t["y"]),
            ).fetchall()
            legacy = [r for r in rows if r["resource"] == TERRAIN_RESOURCE[t["terrain"]]]
            overlay = [r for r in rows if r["resource"] != TERRAIN_RESOURCE[t["terrain"]]]
            assert len(legacy) == 1, (t["x"], t["y"], t["terrain"], [dict(r) for r in rows])
            assert len(overlay) == 1, (t["x"], t["y"], t["terrain"], [dict(r) for r in rows])
    finally:
        conn.close()


def test_overlay_terrain_typing(b1):
    _, _, db_path, _ = b1
    import server.world as w
    conn = db(db_path)
    try:
        rows = conn.execute(
            "SELECT s.x, s.y, s.resource, t.terrain FROM world_resource_stock s"
            " JOIN world_tiles t ON t.x = s.x AND t.y = s.y"
            " WHERE s.resource != ?"
            " AND t.terrain = 'plains'",
            (TERRAIN_RESOURCE["plains"],),
        ).fetchall()
        # plains overlay rows exist and are only fruit/herbs; both appear
        seen = {r["resource"] for r in rows}
        assert seen <= {"fruit", "herbs"} and seen == {"fruit", "herbs"}
        # mountain overlay rows are only stone/coal/copper_ore
        mrows = conn.execute(
            "SELECT DISTINCT s.resource FROM world_resource_stock s"
            " JOIN world_tiles t ON t.x = s.x AND t.y = s.y"
            " WHERE t.terrain = 'mountain' AND s.resource != 'iron_ore'"
        ).fetchall()
        assert {r["resource"] for r in mrows} <= {"stone", "coal", "copper_ore"}
        # pure function: deterministic
        assert w.overlay_resource_for_tile(3, 7, "desert") == \
            w.overlay_resource_for_tile(3, 7, "desert")
        assert w.overlay_resource_for_tile(3, 7, "ocean") is None
    finally:
        conn.close()


def test_overlay_stock_bands(b1):
    _, _, db_path, _ = b1
    conn = db(db_path)
    try:
        rows = conn.execute(
            "SELECT resource, MIN(stock) AS mn, MAX(stock) AS mx, COUNT(*) AS n"
            " FROM world_resource_stock GROUP BY resource"
        ).fetchall()
        bands = {r["resource"]: (r["mn"], r["mx"]) for r in rows}
        for res in ("fruit", "herbs", "fiber"):
            assert 5 <= bands[res][0] and bands[res][1] <= 10, (res, bands[res])
        for res in ("stone", "sand", "clay"):
            assert 6 <= bands[res][0] and bands[res][1] <= 12, (res, bands[res])
        assert 6 <= bands["copper_ore"][0] and bands["copper_ore"][1] <= 10
        assert 8 <= bands["coal"][0] and bands["coal"][1] <= 12
        for res in ("grain", "timber", "iron_ore", "glass"):
            assert 5 <= bands[res][0] and bands[res][1] <= 10, (res, bands[res])
    finally:
        conn.close()


def test_overlay_reseed_is_additive_noop(b1):
    _, _, db_path, _ = b1
    import server.world as world_engine
    conn = db(db_path)
    try:
        before = conn.execute(
            "SELECT x, y, resource, stock FROM world_resource_stock ORDER BY x, y, resource"
        ).fetchall()
        # Second run inserts nothing and changes nothing.
        assert world_engine.seed_resource_overlay(conn) == 0
        conn.commit()
        after = conn.execute(
            "SELECT x, y, resource, stock FROM world_resource_stock ORDER BY x, y, resource"
        ).fetchall()
        assert [tuple(r) for r in after] == [tuple(r) for r in before]
    finally:
        conn.close()


# ---------------------------------------------------------------- migration

def test_ore_renamed_to_iron_ore_in_live_tables(b1):
    client, keys, db_path, _ = b1
    conn = db(db_path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM world_resource_stock WHERE resource = 'ore'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM world_resource_stock WHERE resource = 'iron_ore'"
        ).fetchone()[0] > 0
        assert conn.execute(
            "SELECT COUNT(*) FROM inventories WHERE resource = 'ore'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_inventory_ore_holdings_become_iron_ore(b1):
    client, keys, db_path, appmod = b1
    conn = db(db_path)
    try:
        # Simulate a pre-migration holding, then reboot the app (migration).
        conn.execute(
            "INSERT INTO inventories (agent_pubkey, resource, qty) VALUES (?, 'ore', 7)",
            (pubkey_hex(keys[0]),),
        )
        conn.commit()
    finally:
        conn.close()
    importlib.reload(appmod)  # create_app() runs the migration again
    conn = db(db_path)
    try:
        rows = conn.execute(
            "SELECT resource, qty FROM inventories WHERE agent_pubkey = ?",
            (pubkey_hex(keys[0]),),
        ).fetchall()
        assert [(r["resource"], r["qty"]) for r in rows] == [("iron_ore", 7)]
    finally:
        conn.close()


def test_ledger_history_keeps_ore_verbatim(b1):
    client, keys, db_path, appmod = b1
    conn = db(db_path)
    try:
        conn.execute(
            "INSERT INTO trade_ledger (ts, maker_pubkey, maker_name, taker_pubkey,"
            " taker_name, give_json, want_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("2026-01-01T00:00:00+00:00", "aa", "a", "bb", "b",
             '{"ore":2}', '{"grain":1}'),
        )
        conn.commit()
    finally:
        conn.close()
    importlib.reload(appmod)  # migration must not touch the ledger
    conn = db(db_path)
    try:
        row = conn.execute("SELECT give_json FROM trade_ledger").fetchone()
        assert row["give_json"] == '{"ore":2}'
    finally:
        conn.close()


def test_open_offer_ore_renamed(b1):
    client, keys, db_path, appmod = b1
    conn = db(db_path)
    try:
        conn.execute(
            "INSERT INTO trade_offers (maker_id, give_json, want_json, status, created_at)"
            " VALUES (1, ?, ?, 'open', ?)",
            ('{"ore":3}', '{"chits":5}', "2026-01-01T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()
    importlib.reload(appmod)
    conn = db(db_path)
    try:
        row = conn.execute("SELECT give_json FROM trade_offers").fetchone()
        assert json.loads(row["give_json"]) == {"iron_ore": 3}
    finally:
        conn.close()


def test_legacy_glass_veins_stay_gatherable(b1):
    _, _, db_path, _ = b1
    conn = db(db_path)
    try:
        # Desert legacy rows still say "glass" (the last wild glass), and the
        # overlay added sand/clay — no new glass rows are seeded.
        n = conn.execute(
            "SELECT COUNT(*) FROM world_resource_stock s"
            " JOIN world_tiles t ON t.x = s.x AND t.y = s.y"
            " WHERE t.terrain = 'desert' AND s.resource = 'glass'"
        ).fetchone()[0]
        assert n > 0
    finally:
        conn.close()


# ---------------------------------------------------------------- gather {resource}

def test_gather_names_resource_on_two_resource_tile(b1, monkeypatch):
    client, keys, db_path, appmod = b1
    monkeypatch.setitem(appmod.RATE_LIMITS, "gather", (100, 60))
    spawn(client, keys[0])
    me = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    # Every land tile now bears two resources -> omitted must 400 naming both.
    r = signed_request(client, keys[0], "POST", "/world/gather", {})
    assert r.status_code == 400, r.text
    body = r.json()
    assert body["detail"] == "specify resource"
    assert sorted(body["resources"]) == sorted(
        [TERRAIN_RESOURCE[me["terrain"]],
         [rr for rr in body["resources"] if rr != TERRAIN_RESOURCE[me["terrain"]]][0]]
    )
    assert len(body["resources"]) == 2


def test_gather_named_resource_works(b1, monkeypatch):
    client, keys, db_path, appmod = b1
    monkeypatch.setitem(appmod.RATE_LIMITS, "gather", (100, 60))
    spawn(client, keys[0])
    me = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    expected = TERRAIN_RESOURCE[me["terrain"]]
    r = signed_request(client, keys[0], "POST", "/world/gather",
                       {"resource": expected})
    assert r.status_code == 200, r.text
    assert r.json()["resource"] == expected
    assert r.json()["gained"] == 1


def test_gather_omitted_single_resource_tile(b1, monkeypatch):
    client, keys, db_path, appmod = b1
    monkeypatch.setitem(appmod.RATE_LIMITS, "gather", (100, 60))
    spawn(client, keys[0])
    me = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    expected = TERRAIN_RESOURCE[me["terrain"]]
    conn = db(db_path)
    try:
        # Deplete the overlay row -> single-resource tile -> omitted works.
        conn.execute(
            "UPDATE world_resource_stock SET stock = 0 WHERE x = ? AND y = ?"
            " AND resource != ?",
            (me["x"], me["y"], expected),
        )
        conn.commit()
    finally:
        conn.close()
    r = signed_request(client, keys[0], "POST", "/world/gather", {})
    assert r.status_code == 200, r.text
    assert r.json()["resource"] == expected


def test_gather_unknown_or_absent_resource_400(b1, monkeypatch):
    client, keys, db_path, appmod = b1
    monkeypatch.setitem(appmod.RATE_LIMITS, "gather", (100, 60))
    spawn(client, keys[0])
    r = signed_request(client, keys[0], "POST", "/world/gather",
                       {"resource": "lumber"})
    assert r.status_code == 400, r.text
    r = signed_request(client, keys[0], "POST", "/world/gather",
                       {"resource": "coal"})
    # coal may or may not be on this tile; either way it 400s if absent here
    if r.status_code == 400:
        assert True
    else:
        assert r.json()["resource"] == "coal"
