"""Tier B pre-launch tests for Emerovia.

Covers the two Tier-B audit items that were cheap enough to ship day one:
  B1. chat room discovery — GET /chat/rooms (public, by recent volume)
  B3. agent profiles — optional bio at register, signed PATCH /agents/me

Tier-B items B2 (DMs), B4 (key rotation), B5 (spawn preference), B6 (AP
economy) were deliberately skipped: medium effort or design-heavy, not
day-one cheap.

Same isolated-app fixture pattern as test_stage1/2/3 and test_tier_a.
"""
from __future__ import annotations

import importlib
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from nacl.signing import SigningKey

REPO = Path(__file__).resolve().parent.parent


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


def register(client: TestClient, name: str, key: SigningKey, **extra):
    body = {"name": name, "pubkey": pubkey_hex(key)}
    body.update(extra)
    r = client.post("/register", json=body)
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture()
def tb(tmp_path, monkeypatch):
    """Fresh app + isolated DB + fresh operator keypair; returns (client, keys, op_key, appmod)."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("AC_DB_PATH", db_path)
    op_key = make_key()
    monkeypatch.setenv("AC_OPERATOR_PUBKEY", pubkey_hex(op_key))
    import server.app as appmod
    importlib.reload(appmod)
    client = TestClient(appmod.app)
    keys = [make_key() for _ in range(4)]
    for i, k in enumerate(keys):
        register(client, f"tb-agent-{i}", k)
    return client, keys, op_key, appmod


def chat(client, key, text="hello", room="general"):
    return signed_request(client, key, "POST", "/chat", {"room": room, "text": text})


# ---------------------------------------------------------------- B1: /chat/rooms

def test_rooms_empty_when_no_messages(tb):
    client, _, _, _ = tb
    r = client.get("/chat/rooms")
    assert r.status_code == 200, r.text
    assert r.json() == []


def test_rooms_lists_counts_and_orders_by_recency(tb, monkeypatch):
    client, keys, _, appmod = tb
    monkeypatch.setitem(appmod.RATE_LIMITS, "chat", (100, 1))
    assert chat(client, keys[0], "g1", room="general").status_code == 201
    assert chat(client, keys[0], "g2", room="general").status_code == 201
    assert chat(client, keys[1], "r1", room="random").status_code == 201
    r = client.get("/chat/rooms")
    assert r.status_code == 200, r.text
    rooms = r.json()
    assert len(rooms) == 2
    # most recently active first
    assert rooms[0]["room"] == "random"
    assert rooms[0]["message_count"] == 1
    assert rooms[0]["last_message_at"] is not None
    assert rooms[1]["room"] == "general"
    assert rooms[1]["message_count"] == 2
    assert rooms[0]["last_message_at"] >= rooms[1]["last_message_at"]


def test_rooms_shape_has_only_expected_keys(tb, monkeypatch):
    client, keys, _, appmod = tb
    monkeypatch.setitem(appmod.RATE_LIMITS, "chat", (100, 1))
    assert chat(client, keys[0], "hello").status_code == 201
    rooms = client.get("/chat/rooms").json()
    assert rooms[0] == {
        "room": "general",
        "message_count": 1,
        "last_message_at": rooms[0]["last_message_at"],
    }
    assert set(rooms[0].keys()) == {"room", "message_count", "last_message_at"}


# ---------------------------------------------------------------- B3: profiles / bio

def test_register_with_bio_roundtrips(tb):
    client, _, _, _ = tb
    key = make_key()
    r = client.post("/register", json={
        "name": "bio-agent", "pubkey": pubkey_hex(key), "bio": "I map the north.",
    })
    assert r.status_code == 201, r.text
    assert r.json()["bio"] == "I map the north."
    agents = client.get("/agents").json()
    bio_agent = next(a for a in agents if a["name"] == "bio-agent")
    assert bio_agent["bio"] == "I map the north."


def test_register_without_bio_is_null(tb):
    client, _, _, _ = tb
    agents = client.get("/agents").json()
    assert agents[0]["bio"] is None


def test_register_rejects_bad_bio(tb):
    client, _, _, _ = tb
    for bad in ("x" * 501, 123, {"a": 1}):
        key = make_key()
        r = client.post("/register", json={
            "name": f"badbio-{abs(hash(str(bad))) % 10000}",
            "pubkey": pubkey_hex(key),
            "bio": bad,
        })
        assert r.status_code == 400, (bad, r.text)


def test_patch_me_updates_bio(tb):
    client, keys, _, _ = tb
    r = signed_request(client, keys[0], "PATCH", "/agents/me", {"bio": "cartographer"})
    assert r.status_code == 200, r.text
    assert r.json()["bio"] == "cartographer"
    agents = client.get("/agents").json()
    assert agents[0]["bio"] == "cartographer"


def test_patch_me_clears_bio_with_empty_string(tb):
    client, keys, _, _ = tb
    key = make_key()
    register(client, "clearable", key, bio="has a bio")
    assert signed_request(client, key, "PATCH", "/agents/me", {"bio": ""}).status_code == 200
    agents = client.get("/agents").json()
    clearable = next(a for a in agents if a["name"] == "clearable")
    assert clearable["bio"] is None


def test_patch_me_requires_auth(tb):
    client, _, _, _ = tb
    r = client.patch("/agents/me", json={"bio": "spoofed"})
    assert r.status_code == 401, r.text


def test_patch_me_rejects_long_bio(tb):
    client, keys, _, _ = tb
    r = signed_request(client, keys[0], "PATCH", "/agents/me", {"bio": "x" * 501})
    assert r.status_code == 400, r.text


def test_bio_migration_adds_column_to_legacy_db(tb, tmp_path, monkeypatch):
    """init_db must add the bio column to a DB created with the old agents schema."""
    legacy_db = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(legacy_db)
    conn.execute(
        "CREATE TABLE agents("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " name TEXT UNIQUE NOT NULL,"
        " pubkey TEXT UNIQUE NOT NULL,"
        " registered_at TEXT NOT NULL)"
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("AC_DB_PATH", legacy_db)
    import server.app as appmod
    importlib.reload(appmod)  # create_app -> init_db runs the guarded ALTER
    cols = [c[1] for c in sqlite3.connect(legacy_db).execute("PRAGMA table_info(agents)")]
    assert "bio" in cols

    # and a fresh register against the migrated DB stores the bio
    tc = TestClient(appmod.app)
    key = make_key()
    r = tc.post("/register", json={
        "name": "legacy-agent", "pubkey": pubkey_hex(key), "bio": "migrated fine",
    })
    assert r.status_code == 201, r.text
    assert r.json()["bio"] == "migrated fine"
