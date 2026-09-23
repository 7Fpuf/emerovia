"""v1.1.0 RC round 3 tests (QA backlog items 12 & 13).

12. Chat pagination: document room/since/limit in agents.txt (docs-only)
    and add order=desc for newest-first reads. Tests below pin:
    desc ordering, desc + since cursor semantics, limit cap in desc mode.
13. Trade settlement: pin that creating an offer moves nothing (no escrow
    at creation) — inventory unchanged until accept.

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


def chat(client: TestClient, key: SigningKey, text: str = "hello"):
    return signed_request(client, key, "POST", "/chat",
                          {"room": "general", "text": text})


@pytest.fixture()
def rc3(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("AC_DB_PATH", db_path)
    monkeypatch.setenv("AC_OPERATOR_PUBKEY", pubkey_hex(make_key()))
    import server.app as appmod
    importlib.reload(appmod)
    client = TestClient(appmod.app)
    keys = [make_key() for _ in range(2)]
    for i, k in enumerate(keys):
        register(client, f"rc3-agent-{i}", k)
    return client, keys, db_path, appmod


def _post_n_chats(client, key, appmod, monkeypatch, n):
    monkeypatch.setitem(appmod.RATE_LIMITS, "chat", (n + 10, 60))
    ids = []
    for i in range(n):
        r = chat(client, key, f"rc3 msg {i}")
        assert r.status_code == 201, r.text
    msgs = client.get("/chat?room=general&since=0").json()
    return [m["id"] for m in msgs]


# ------------------------------------------------- 12: order=desc basics

def test_chat_order_desc_newest_first(rc3, monkeypatch):
    client, keys, _, appmod = rc3
    posted = _post_n_chats(client, keys[0], appmod, monkeypatch, 5)
    r = client.get("/chat?room=general&since=0&order=desc")
    assert r.status_code == 200
    got = [m["id"] for m in r.json()]
    assert got == sorted(posted, reverse=True)[:100]


def test_chat_order_desc_is_case_insensitive(rc3, monkeypatch):
    client, keys, _, appmod = rc3
    posted = _post_n_chats(client, keys[0], appmod, monkeypatch, 3)
    r = client.get("/chat?room=general&since=0&order=DESC")
    assert r.status_code == 200
    assert [m["id"] for m in r.json()] == sorted(posted, reverse=True)


def test_chat_order_invalid_rejected(rc3, monkeypatch):
    client, keys, _, appmod = rc3
    _post_n_chats(client, keys[0], appmod, monkeypatch, 2)
    r = client.get("/chat?room=general&since=0&order=newest")
    assert r.status_code == 400


# ------------------------------------------------- 12: desc + since cursor

def test_chat_order_desc_since_cursor_pages_backwards(rc3, monkeypatch):
    """since=0 starts at the newest; subsequent pages pass since=<last id
    of previous page> to walk backwards without gaps or overlap."""
    client, keys, _, appmod = rc3
    posted = _post_n_chats(client, keys[0], appmod, monkeypatch, 9)
    newest_first = sorted(posted, reverse=True)
    # page 1: newest 4
    p1 = client.get("/chat?room=general&since=0&order=desc&limit=4").json()
    assert [m["id"] for m in p1] == newest_first[:4]
    # page 2: older than page-1's last id
    cursor = p1[-1]["id"]
    p2 = client.get(f"/chat?room=general&since={cursor}&order=desc&limit=4").json()
    assert [m["id"] for m in p2] == newest_first[4:8]
    # page 3: remainder, no overlap with earlier pages
    cursor2 = p2[-1]["id"]
    p3 = client.get(f"/chat?room=general&since={cursor2}&order=desc&limit=4").json()
    assert [m["id"] for m in p3] == newest_first[8:]
    seen = [m["id"] for m in p1 + p2 + p3]
    assert seen == newest_first and len(set(seen)) == 9


def test_chat_asc_behavior_unchanged(rc3, monkeypatch):
    """Round 3 is additive: the asc path returns exactly what it did before."""
    client, keys, _, appmod = rc3
    posted = _post_n_chats(client, keys[0], appmod, monkeypatch, 5)
    r = client.get("/chat?room=general&since=0&limit=100")
    assert r.status_code == 200
    assert [m["id"] for m in r.json()] == sorted(posted)
    r2 = client.get("/chat?room=general&since=0&limit=100&order=asc")
    assert [m["id"] for m in r2.json()] == sorted(posted)


# ------------------------------------------------- 12: limit cap in desc mode

def test_chat_order_desc_limit_cap_honored(rc3, monkeypatch):
    client, keys, _, appmod = rc3
    _post_n_chats(client, keys[0], appmod, monkeypatch, 105)
    r = client.get("/chat?room=general&since=0&order=desc&limit=99999")
    assert r.status_code == 200
    got = r.json()
    assert len(got) == 100  # server cap, same as asc
    ids = [m["id"] for m in got]
    assert ids == sorted(ids, reverse=True)  # newest 100, newest-first


# ------------------------------------------------- 13: no escrow at creation

def test_offer_creation_moves_nothing(rc3):
    """Pinning item 13's verified semantics: POST /trade/offers creates an
    open row but moves NO inventory — goods stay spendable until accept."""
    client, keys, db_path, _ = rc3
    maker, taker = keys
    # Seed the maker with 5 glass directly (bypasses gather randomness).
    import sqlite3
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO inventories (agent_pubkey, resource, qty) VALUES (?, 'glass', 5)",
            (pubkey_hex(maker),),
        )
        conn.commit()
    finally:
        conn.close()
    before = signed_request(client, maker, "GET", "/world/inventory", {}).json()
    assert before["inventory"] == {"glass": 5}

    r = signed_request(client, maker, "POST", "/trade/offers",
                       {"give": {"glass": 5}, "want": {"chits": 10}})
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "open"

    after = signed_request(client, maker, "GET", "/world/inventory", {}).json()
    assert after["inventory"] == {"glass": 5}, "offer creation moved inventory!"
    assert after["chits"] == before["chits"]


def test_offered_goods_still_spendable_until_accept(rc3):
    """Consequence of no-escrow: after creating an offer, the same goods can
    back further open offers (double-commit characteristic, item 13)."""
    client, keys, db_path, _ = rc3
    maker = keys[0]
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO inventories (agent_pubkey, resource, qty) VALUES (?, 'glass', 5)",
            (pubkey_hex(maker),),
        )
        conn.commit()
    finally:
        conn.close()
    # Trade-offer rate limit is 3/hr; two offers are fine.
    r1 = signed_request(client, maker, "POST", "/trade/offers",
                        {"give": {"glass": 5}, "want": {"chits": 10}})
    assert r1.status_code == 201, r1.text
    r2 = signed_request(client, maker, "POST", "/trade/offers",
                        {"give": {"glass": 5}, "want": {"chits": 20}})
    assert r2.status_code == 201, r2.text  # same goods back a second offer
    opens = client.get("/trade/offers").json()
    maker_offers = [o for o in opens if o["id"] in (r1.json()["offer_id"], r2.json()["offer_id"])]
    assert len(maker_offers) == 2
