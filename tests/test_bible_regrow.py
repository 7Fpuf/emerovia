"""Bible ch.2 — depletion + regrow (Systems Bible §3).

Covers: wild stock bands per class, the lazy regrow trickle (1 unit per
tile-resource per 7 days via tile_regrow, applied on gather, capped at the
seeded max), first-touch starts the clock (no retroactive refill), and no
global sweep ever (rows exist only for touched tiles).

Same isolated-app fixture pattern as the other suites.
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
DAY = 86400


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
def b2(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("AC_DB_PATH", db_path)
    op_key = make_key()
    monkeypatch.setenv("AC_OPERATOR_PUBKEY", pubkey_hex(op_key))
    import server.app as appmod
    importlib.reload(appmod)
    client = TestClient(appmod.app)
    keys = [make_key() for _ in range(4)]
    for i, k in enumerate(keys):
        register(client, f"bible-regrow-{i}", k)
    return client, keys, db_path, appmod


def db(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def gather(client, key, resource, now=None):
    if now is not None:
        ts = str(now)
        body_text = json.dumps({"resource": resource}, separators=(",", ":"))
        headers = sign(key, "POST", "/world/gather", body_text, ts=ts)
        return client.request("POST", "/world/gather",
                              content=body_text.encode(), headers=headers)
    return signed_request(client, key, "POST", "/world/gather", {"resource": resource})


def stock_of(db_path, x, y, resource):
    conn = db(db_path)
    try:
        row = conn.execute(
            "SELECT stock FROM world_resource_stock WHERE x = ? AND y = ? AND resource = ?",
            (x, y, resource),
        ).fetchone()
        return int(row["stock"]) if row else None
    finally:
        conn.close()


def set_stock_and_touch(db_path, x, y, resource, stock, last_touch):
    conn = db(db_path)
    try:
        conn.execute(
            "UPDATE world_resource_stock SET stock = ? WHERE x = ? AND y = ? AND resource = ?",
            (stock, x, y, resource),
        )
        conn.execute(
            "INSERT OR REPLACE INTO tile_regrow (x, y, resource, last_touch)"
            " VALUES (?, ?, ?, ?)",
            (x, y, resource, last_touch),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- tests

def test_regrow_one_unit_per_week(b2, monkeypatch):
    client, keys, db_path, appmod = b2
    import server.world as w
    monkeypatch.setitem(appmod.RATE_LIMITS, "gather", (100, 60))
    spawn(client, keys[0])
    me = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    res = TERRAIN_RESOURCE[me["terrain"]]
    now = time.time()
    set_stock_and_touch(db_path, me["x"], me["y"], res, 0, now - 8 * DAY)
    monkeypatch.setattr(w, "now", lambda: now)
    r = gather(client, keys[0], res)
    assert r.status_code == 200, r.text
    body = r.json()
    # 8 days -> 1 unit regrown, then gathered: net stock 0, gained 1.
    assert body["gained"] == 1
    assert body["stock_remaining"] == 0
    assert stock_of(db_path, me["x"], me["y"], res) == 0


def test_regrow_multiple_weeks(b2, monkeypatch):
    client, keys, db_path, appmod = b2
    import server.world as w
    monkeypatch.setitem(appmod.RATE_LIMITS, "gather", (100, 60))
    spawn(client, keys[0])
    me = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    res = TERRAIN_RESOURCE[me["terrain"]]
    now = time.time()
    set_stock_and_touch(db_path, me["x"], me["y"], res, 0, now - 15 * DAY)
    monkeypatch.setattr(w, "now", lambda: now)
    r = gather(client, keys[0], res)
    assert r.status_code == 200, r.text
    # 15 days -> 2 units regrown, 1 gathered -> 1 left.
    assert r.json()["stock_remaining"] == 1


def test_regrow_capped_at_seeded_max(b2, monkeypatch):
    client, keys, db_path, appmod = b2
    import server.world as w
    monkeypatch.setitem(appmod.RATE_LIMITS, "gather", (100, 60))
    spawn(client, keys[0])
    me = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    res = TERRAIN_RESOURCE[me["terrain"]]
    cap = w._seeded_max(me["x"], me["y"], res)
    now = time.time()
    # Stock already at cap, last touch 30 days ago: no overflow past cap.
    set_stock_and_touch(db_path, me["x"], me["y"], res, cap, now - 30 * DAY)
    monkeypatch.setattr(w, "now", lambda: now)
    r = gather(client, keys[0], res)
    assert r.status_code == 200, r.text
    assert r.json()["stock_remaining"] == cap - 1  # capped, then 1 gathered
    assert stock_of(db_path, me["x"], me["y"], res) == cap - 1


def test_regrow_less_than_a_week_nothing(b2, monkeypatch):
    client, keys, db_path, appmod = b2
    import server.world as w
    monkeypatch.setitem(appmod.RATE_LIMITS, "gather", (100, 60))
    spawn(client, keys[0])
    me = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    res = TERRAIN_RESOURCE[me["terrain"]]
    now = time.time()
    set_stock_and_touch(db_path, me["x"], me["y"], res, 0, now - 6 * DAY)
    monkeypatch.setattr(w, "now", lambda: now)
    r = gather(client, keys[0], res)
    assert r.status_code == 400, r.text  # still depleted: 0 regrown
    assert "depleted" in r.json()["detail"]


def test_first_touch_starts_clock_no_retroactive_refill(b2, monkeypatch):
    client, keys, db_path, appmod = b2
    import server.world as w
    monkeypatch.setitem(appmod.RATE_LIMITS, "gather", (100, 60))
    spawn(client, keys[0])
    me = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    res = TERRAIN_RESOURCE[me["terrain"]]
    conn = db(db_path)
    try:
        conn.execute(
            "UPDATE world_resource_stock SET stock = 3 WHERE x = ? AND y = ? AND resource = ?",
            (me["x"], me["y"], res),
        )
        conn.execute(
            "DELETE FROM tile_regrow WHERE x = ? AND y = ? AND resource = ?",
            (me["x"], me["y"], res),
        )
        conn.commit()
    finally:
        conn.close()
    now = time.time()
    monkeypatch.setattr(w, "now", lambda: now)
    r = gather(client, keys[0], res)
    assert r.status_code == 200, r.text
    # No refill on first touch: 3 -> 2.
    assert r.json()["stock_remaining"] == 2
    conn = db(db_path)
    try:
        row = conn.execute(
            "SELECT last_touch FROM tile_regrow WHERE x = ? AND y = ? AND resource = ?",
            (me["x"], me["y"], res),
        ).fetchone()
        assert row is not None
        assert abs(float(row["last_touch"]) - now) < 5
    finally:
        conn.close()


def test_no_sweep_rows_only_for_touched_tiles(b2, monkeypatch):
    client, keys, db_path, appmod = b2
    monkeypatch.setitem(appmod.RATE_LIMITS, "gather", (100, 60))
    spawn(client, keys[0])
    me = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    res = TERRAIN_RESOURCE[me["terrain"]]
    gather(client, keys[0], res)
    conn = db(db_path)
    try:
        n = conn.execute("SELECT COUNT(*) FROM tile_regrow").fetchone()[0]
        # One row per stock row on the touched tile (legacy + overlay);
        # no other tile was touched.
        assert n == 2
        tiles = conn.execute("SELECT DISTINCT x, y FROM tile_regrow").fetchall()
        assert [(t["x"], t["y"]) for t in tiles] == [(me["x"], me["y"])]
    finally:
        conn.close()


def test_regrow_applies_per_resource_row(b2, monkeypatch):
    client, keys, db_path, appmod = b2
    import server.world as w
    monkeypatch.setitem(appmod.RATE_LIMITS, "gather", (100, 60))
    spawn(client, keys[0])
    me = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    legacy = TERRAIN_RESOURCE[me["terrain"]]
    conn = db(db_path)
    try:
        overlay = conn.execute(
            "SELECT resource FROM world_resource_stock WHERE x = ? AND y = ?"
            " AND resource != ?",
            (me["x"], me["y"], legacy),
        ).fetchone()["resource"]
    finally:
        conn.close()
    now = time.time()
    # Strip the overlay row 8 days "ago"; legacy row touched just now.
    set_stock_and_touch(db_path, me["x"], me["y"], overlay, 0, now - 8 * DAY)
    set_stock_and_touch(db_path, me["x"], me["y"], legacy, 2, now)
    monkeypatch.setattr(w, "now", lambda: now)
    r = gather(client, keys[0], overlay)
    assert r.status_code == 200, r.text
    assert r.json()["gained"] == 1  # overlay regrew 1, gathered it
    # Gathering the overlay row did not refill the legacy row.
    assert stock_of(db_path, me["x"], me["y"], legacy) == 2
