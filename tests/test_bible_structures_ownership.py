"""Bible §8 — structure transfer/demolish; Bible §2.5 — refine location.

Covers: owner-only demolish (1 AP, no refunds, claim retained,
derelict OK, farm plots die with the farm), owner-authorized transfer
(claim moves with the building when held by the transferor, recipient
claim cap checked, derelict OK), and refining requires standing on the
actor's own non-derelict furnace.
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


def make_key() -> SigningKey:
    return SigningKey(os.urandom(32))


def pubkey_hex(key: SigningKey) -> str:
    return key.verify_key.encode().hex()


def sign(key: SigningKey, method: str, path: str, body_text: str, ts: str | None = None) -> dict:
    if ts is None:
        ts = str(time.time())
    msg = (ts + "\n" + method.upper() + "\n" + path + "\n" + body_text).encode("utf-8")
    return {
        "X-Agent-Pubkey": pubkey_hex(key),
        "X-Timestamp": ts,
        "X-Signature": key.sign(msg).signature.hex(),
        "Content-Type": "application/json",
    }


def signed_request(client: TestClient, key: SigningKey, method: str, path: str,
                   payload: dict, ts: str | None = None):
    body_text = json.dumps(payload, separators=(",", ":"))
    return client.request(method, path, content=body_text.encode("utf-8"),
                          headers=sign(key, method, path, body_text, ts=ts))


def register(client: TestClient, name: str, key: SigningKey):
    r = client.post("/register", json={"name": name, "pubkey": pubkey_hex(key)})
    assert r.status_code == 201, r.text


def spawn(client: TestClient, key: SigningKey):
    r = signed_request(client, key, "POST", "/world/spawn", {})
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture()
def bx(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("AC_DB_PATH", db_path)
    monkeypatch.setenv("AC_OPERATOR_PUBKEY", pubkey_hex(make_key()))
    import server.app as appmod
    importlib.reload(appmod)
    client = TestClient(appmod.app)
    keys = [make_key() for _ in range(3)]
    for i, k in enumerate(keys):
        register(client, f"bible-own-{i}", k)
    return client, keys, db_path, appmod


def db(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def week_now():
    return int(time.time() // 604800)


def plant_structure(db_path, pubkey, x, y, kind="shelter", weeks_ago=0):
    conn = db(db_path)
    try:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        conn.execute(
            "INSERT OR IGNORE INTO claims (x, y, owner_pubkey, claimed_at)"
            " VALUES (?, ?, ?, ?)",
            (x, y, pubkey, now),
        )
        cur = conn.execute(
            "INSERT INTO structures (owner_pubkey, kind, x, y, raised_at,"
            " last_tithe_week, settlement_asset) VALUES (?, ?, ?, ?, ?, ?, 0)",
            (pubkey, kind, x, y, now, week_now() - weeks_ago),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def free_tile(db_path):
    conn = db(db_path)
    try:
        row = conn.execute(
            "SELECT x, y FROM world_tiles WHERE terrain != 'ocean'"
            " AND NOT EXISTS (SELECT 1 FROM claims WHERE claims.x = world_tiles.x"
            " AND claims.y = world_tiles.y)"
            " AND NOT EXISTS (SELECT 1 FROM structures WHERE structures.x = world_tiles.x"
            " AND structures.y = world_tiles.y) LIMIT 1",
        ).fetchone()
        assert row is not None
        return (row["x"], row["y"])
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


def claim_count(db_path, pubkey):
    conn = db(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM claims WHERE owner_pubkey = ?",
                            (pubkey,)).fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------- demolish

def test_demolish(bx):
    client, keys, db_path, appmod = bx
    spawn(client, keys[0])
    a = pubkey_hex(keys[0])
    x, y = free_tile(db_path)
    sid = plant_structure(db_path, a, x, y, "shelter")
    set_ap(db_path, a, 10)
    r = signed_request(client, keys[0], "POST", "/world/demolish",
                       {"structure_id": sid})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "demolished"
    assert body["ap"] == 9  # 1 AP
    conn = db(db_path)
    try:
        assert conn.execute("SELECT 1 FROM structures WHERE id = ?",
                            (sid,)).fetchone() is None
        # Claim retained.
        assert conn.execute("SELECT owner_pubkey FROM claims WHERE x = ? AND y = ?",
                            (x, y)).fetchone()["owner_pubkey"] == a
    finally:
        conn.close()


def test_demolish_non_owner_rejected(bx):
    client, keys, db_path, appmod = bx
    spawn(client, keys[0])
    spawn(client, keys[1])
    a = pubkey_hex(keys[0])
    x, y = free_tile(db_path)
    sid = plant_structure(db_path, a, x, y)
    r = signed_request(client, keys[1], "POST", "/world/demolish",
                       {"structure_id": sid})
    assert r.status_code == 400, r.text


def test_demolish_kills_farm_plots_no_refund(bx):
    client, keys, db_path, appmod = bx
    spawn(client, keys[0])
    a = pubkey_hex(keys[0])
    x, y = free_tile(db_path)
    sid = plant_structure(db_path, a, x, y, "farm")
    conn = db(db_path)
    try:
        for slot in range(4):
            conn.execute(
                "INSERT INTO farm_plots (structure_id, slot, state) VALUES (?, ?, 'empty')",
                (sid, slot),
            )
        conn.commit()
    finally:
        conn.close()
    set_inventory(db_path, a, {"timber": 50})
    set_ap(db_path, a, 10)
    r = signed_request(client, keys[0], "POST", "/world/demolish",
                       {"structure_id": sid})
    assert r.status_code == 200, r.text
    conn = db(db_path)
    try:
        assert conn.execute("SELECT 1 FROM farm_plots WHERE structure_id = ?",
                            (sid,)).fetchone() is None
        # No refunds: timber untouched.
        assert conn.execute("SELECT qty FROM inventories WHERE agent_pubkey = ?"
                            " AND resource = 'timber'", (a,)).fetchone()["qty"] == 50
    finally:
        conn.close()


def test_demolish_derelict_ok(bx):
    client, keys, db_path, appmod = bx
    spawn(client, keys[0])
    a = pubkey_hex(keys[0])
    x, y = free_tile(db_path)
    sid = plant_structure(db_path, a, x, y, "shelter", weeks_ago=9)
    set_ap(db_path, a, 10)
    r = signed_request(client, keys[0], "POST", "/world/demolish",
                       {"structure_id": sid})
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------- transfer

def test_transfer_moves_structure_and_claim(bx):
    client, keys, db_path, appmod = bx
    spawn(client, keys[0])
    spawn(client, keys[1])
    a, b = pubkey_hex(keys[0]), pubkey_hex(keys[1])
    x, y = free_tile(db_path)
    sid = plant_structure(db_path, a, x, y, "workshop")
    r = signed_request(client, keys[0], "POST", "/world/transfer",
                       {"structure_id": sid, "to_pubkey": b})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "transferred"
    assert body["claim_moved"] is True
    conn = db(db_path)
    try:
        assert conn.execute("SELECT owner_pubkey FROM structures WHERE id = ?",
                            (sid,)).fetchone()["owner_pubkey"] == b
        assert conn.execute("SELECT owner_pubkey FROM claims WHERE x = ? AND y = ?",
                            (x, y)).fetchone()["owner_pubkey"] == b
    finally:
        conn.close()


def test_transfer_non_owner_rejected(bx):
    client, keys, db_path, appmod = bx
    spawn(client, keys[0])
    spawn(client, keys[1])
    spawn(client, keys[2])
    a, b, c = (pubkey_hex(k) for k in keys)
    x, y = free_tile(db_path)
    sid = plant_structure(db_path, a, x, y)
    r = signed_request(client, keys[2], "POST", "/world/transfer",
                       {"structure_id": sid, "to_pubkey": b})
    assert r.status_code == 400, r.text


def test_transfer_recipient_cap(bx):
    """Bible §8 'transfers check recipient cap': the claim moves with the
    building, so the recipient must have claim capacity (6)."""
    client, keys, db_path, appmod = bx
    spawn(client, keys[0])
    spawn(client, keys[1])
    a, b = pubkey_hex(keys[0]), pubkey_hex(keys[1])
    x, y = free_tile(db_path)
    sid = plant_structure(db_path, a, x, y)
    # Fill the recipient's claim book to 6.
    for _ in range(6):
        tx, ty = free_tile(db_path)
        conn = db(db_path)
        try:
            conn.execute(
                "INSERT OR IGNORE INTO claims (x, y, owner_pubkey, claimed_at)"
                " VALUES (?, ?, ?, ?)",
                (tx, ty, b, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
            )
            conn.commit()
        finally:
            conn.close()
    assert claim_count(db_path, b) == 6
    r = signed_request(client, keys[0], "POST", "/world/transfer",
                       {"structure_id": sid, "to_pubkey": b})
    assert r.status_code == 400, r.text
    assert "claim limit" in r.json()["detail"]


def test_transfer_derelict_ok(bx):
    client, keys, db_path, appmod = bx
    spawn(client, keys[0])
    spawn(client, keys[1])
    a, b = pubkey_hex(keys[0]), pubkey_hex(keys[1])
    x, y = free_tile(db_path)
    sid = plant_structure(db_path, a, x, y, "mill", weeks_ago=9)
    r = signed_request(client, keys[0], "POST", "/world/transfer",
                       {"structure_id": sid, "to_pubkey": b})
    assert r.status_code == 200, r.text


def test_transfer_self_and_unknown_rejected(bx):
    client, keys, db_path, appmod = bx
    spawn(client, keys[0])
    a = pubkey_hex(keys[0])
    x, y = free_tile(db_path)
    sid = plant_structure(db_path, a, x, y)
    r = signed_request(client, keys[0], "POST", "/world/transfer",
                       {"structure_id": sid, "to_pubkey": a})
    assert r.status_code == 400, r.text
    r = signed_request(client, keys[0], "POST", "/world/transfer",
                       {"structure_id": sid, "to_pubkey": "ab" * 32})
    assert r.status_code == 400, r.text


# ---------------------------------------------------------------- refine

def test_refine_requires_standing_on_own_furnace(bx, monkeypatch):
    client, keys, db_path, appmod = bx
    monkeypatch.setitem(appmod.RATE_LIMITS, "refine", (100, 5))
    spawn(client, keys[0])
    a = pubkey_hex(keys[0])
    x, y = free_tile(db_path)
    plant_structure(db_path, a, x, y, "furnace")
    set_inventory(db_path, a, {"timber": 10})
    set_ap(db_path, a, 20)
    # Standing elsewhere (spawn point) → 400.
    r = signed_request(client, keys[0], "POST", "/world/refine",
                       {"item": "lumber"})
    assert r.status_code == 400, r.text
    # Standing on the furnace → 200.
    teleport(db_path, a, x, y)
    r = signed_request(client, keys[0], "POST", "/world/refine",
                       {"item": "lumber"})
    assert r.status_code == 200, r.text
    assert r.json()["gained"] == 2


def test_refine_other_owners_furnace_rejected(bx):
    client, keys, db_path, appmod = bx
    spawn(client, keys[0])
    spawn(client, keys[1])
    a, b = pubkey_hex(keys[0]), pubkey_hex(keys[1])
    x, y = free_tile(db_path)
    plant_structure(db_path, b, x, y, "furnace")
    teleport(db_path, a, x, y)
    set_inventory(db_path, a, {"timber": 10})
    set_ap(db_path, a, 20)
    r = signed_request(client, keys[0], "POST", "/world/refine",
                       {"item": "lumber"})
    assert r.status_code == 400, r.text


def test_refine_derelict_furnace_rejected(bx):
    client, keys, db_path, appmod = bx
    spawn(client, keys[0])
    a = pubkey_hex(keys[0])
    x, y = free_tile(db_path)
    plant_structure(db_path, a, x, y, "furnace", weeks_ago=9)
    teleport(db_path, a, x, y)
    set_inventory(db_path, a, {"timber": 10})
    set_ap(db_path, a, 20)
    r = signed_request(client, keys[0], "POST", "/world/refine",
                       {"item": "lumber"})
    assert r.status_code == 400, r.text
