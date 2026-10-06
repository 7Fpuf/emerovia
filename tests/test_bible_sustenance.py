"""Bible — sustenance (Systems Bible §2.6).

Covers: POST /eat {item, qty} converts food to AP server-side
(grain +2, fruit +3, flour +5, herbs +8), daily caps per food (UTC),
eating never pushes above the effective AP cap, food is destroyed,
24h idempotency, and rejection of non-food / over-cap / insufficient.
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
                   payload: dict, ts: str | None = None, idem: str | None = None):
    body_text = json.dumps(payload, separators=(",", ":"))
    headers = sign(key, method, path, body_text, ts=ts)
    if idem:
        headers["Idempotency-Key"] = idem
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
def b9(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("AC_DB_PATH", db_path)
    op_key = make_key()
    monkeypatch.setenv("AC_OPERATOR_PUBKEY", pubkey_hex(op_key))
    import server.app as appmod
    importlib.reload(appmod)
    client = TestClient(appmod.app)
    keys = [make_key() for _ in range(2)]
    for i, k in enumerate(keys):
        register(client, f"bible-eat-{i}", k)
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


def qty_of(db_path, pubkey, item):
    conn = db(db_path)
    try:
        row = conn.execute(
            "SELECT qty FROM inventories WHERE agent_pubkey = ? AND resource = ?",
            (pubkey, item),
        ).fetchone()
        return int(row["qty"]) if row else 0
    finally:
        conn.close()


def eat(client, key, item, qty, **kw):
    return signed_request(client, key, "POST", "/eat",
                          {"item": item, "qty": qty}, **kw)


# ---------------------------------------------------------------- tests

def test_eat_grain_converts_to_ap(b9):
    client, keys, db_path, _ = b9
    me = spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    set_inventory(db_path, pk, {"grain": 10})
    set_ap(db_path, pk, 10)
    r = eat(client, keys[0], "grain", 3)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ap_gained"] == 6
    assert body["ap"] == 16
    assert body["eaten_today"] == 3
    assert qty_of(db_path, pk, "grain") == 7  # destroyed


def test_eat_food_table(b9):
    client, keys, db_path, _ = b9
    spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    set_inventory(db_path, pk, {"fruit": 4, "flour": 3, "herbs": 1})
    set_ap(db_path, pk, 0)
    assert eat(client, keys[0], "fruit", 2).json()["ap_gained"] == 6
    assert eat(client, keys[0], "flour", 1).json()["ap_gained"] == 5
    assert eat(client, keys[0], "herbs", 1).json()["ap_gained"] == 8
    me = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    assert me["ap"] == 6 + 5 + 8


def test_eat_daily_caps(b9):
    client, keys, db_path, _ = b9
    spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    set_inventory(db_path, pk, {"grain": 20, "herbs": 5})
    set_ap(db_path, pk, 0)
    # Grain cap is 5/day: 3 + 2 ok, a 6th refused.
    assert eat(client, keys[0], "grain", 3).status_code == 200
    assert eat(client, keys[0], "grain", 2).status_code == 200
    r = eat(client, keys[0], "grain", 1)
    assert r.status_code == 400, r.text
    assert "daily cap" in r.json()["detail"]
    # Herbs cap is 1/day.
    assert eat(client, keys[0], "herbs", 1).status_code == 200
    assert eat(client, keys[0], "herbs", 1).status_code == 400


def test_eat_never_above_ap_cap(b9):
    client, keys, db_path, _ = b9
    spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    set_inventory(db_path, pk, {"flour": 3})
    set_ap(db_path, pk, 98)  # cap is 100
    r = eat(client, keys[0], "flour", 2)  # +10 would overshoot
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ap"] == 100
    assert body["ap_gained"] == 2  # clamped, excess lost
    assert body["ap_cap"] == 100


def test_eat_rejects(b9):
    client, keys, db_path, _ = b9
    spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    set_inventory(db_path, pk, {"grain": 2})
    # Not food.
    r = eat(client, keys[0], "timber", 1)
    assert r.status_code == 400, r.text
    # Insufficient inventory.
    r = eat(client, keys[0], "grain", 5)
    assert r.status_code == 400, r.text
    assert "insufficient" in r.json()["detail"]
    # Bad qty.
    r = eat(client, keys[0], "grain", 0)
    assert r.status_code == 400, r.text


def test_eat_idempotent(b9):
    client, keys, db_path, _ = b9
    spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    set_inventory(db_path, pk, {"grain": 10})
    set_ap(db_path, pk, 10)
    r1 = eat(client, keys[0], "grain", 2, idem="eat-once-1")
    assert r1.status_code == 200, r1.text
    r2 = eat(client, keys[0], "grain", 2, idem="eat-once-1")
    assert r2.status_code == 200, r2.text
    assert r2.json() == r1.json()  # replayed, not re-executed
    assert qty_of(db_path, pk, "grain") == 8  # consumed once
    me = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    assert me["ap"] == 14  # +4 once
