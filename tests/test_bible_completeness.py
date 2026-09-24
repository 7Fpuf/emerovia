"""Bible-completeness audit fixes (pod/bible-completeness).

Regression tests for the gaps closed in this branch:

1. PATCH /proposals/{id}/state (operator-only) now honors the
   Idempotency-Key header like every other mutating route.
2. GET /world/settlements — the public settlements index promised by
   the Bible's §5 discoverability story (an agent/observer must be able
   to enumerate settlements to address the per-settlement endpoints).
   Empty world -> 200 []; entries ordered by formed_at ASC.
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
                   payload: dict, extra_headers: dict | None = None, ts: str | None = None):
    body_text = json.dumps(payload, separators=(",", ":"))
    headers = sign(key, method, path, body_text, ts=ts)
    if extra_headers:
        headers.update(extra_headers)
    return client.request(method, path, content=body_text.encode("utf-8"), headers=headers)


def register(client: TestClient, name: str, key: SigningKey):
    r = client.post("/register", json={"name": name, "pubkey": pubkey_hex(key)})
    assert r.status_code == 201, r.text
    return r.json()


def spawn(client: TestClient, key: SigningKey):
    r = signed_request(client, key, "POST", "/world/spawn", {})
    assert r.status_code == 201, r.text
    return r.json()


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


def land_tiles_near(db_path, cx, cy, n, min_dist=1, max_dist=8):
    conn = db(db_path)
    try:
        rows = conn.execute(
            "SELECT x, y FROM world_tiles WHERE terrain != 'ocean'"
            " AND MAX(ABS(x - ?), ABS(y - ?)) BETWEEN ? AND ?"
            " ORDER BY x, y LIMIT ?",
            (cx, cy, min_dist, max_dist, n),
        ).fetchall()
        assert len(rows) >= n, "not enough land tiles nearby"
        return [(r["x"], r["y"]) for r in rows]
    finally:
        conn.close()


def plant_claim_only(db_path, pubkey, x, y):
    conn = db(db_path)
    try:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        conn.execute(
            "INSERT OR IGNORE INTO claims (x, y, owner_pubkey, claimed_at)"
            " VALUES (?, ?, ?, ?)",
            (x, y, pubkey, now),
        )
        conn.commit()
    finally:
        conn.close()


def plant_structure(db_path, pubkey, x, y, kind="shelter"):
    plant_claim_only(db_path, pubkey, x, y)
    conn = db(db_path)
    try:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        cur = conn.execute(
            "INSERT INTO structures (owner_pubkey, kind, x, y, raised_at,"
            " last_tithe_week, settlement_asset)"
            " VALUES (?, ?, ?, ?, ?, ?, 0)",
            (pubkey, kind, x, y, now, int(time.time() // 604800)),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


# ---------------------------------------------------------------- fixtures

@pytest.fixture()
def op(tmp_path, monkeypatch):
    """Fresh app + isolated DB + fresh operator keypair (set before import)."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("AC_DB_PATH", db_path)
    op_key = make_key()
    monkeypatch.setenv("AC_OPERATOR_PUBKEY", pubkey_hex(op_key))
    import server.app as appmod
    importlib.reload(appmod)
    client = TestClient(appmod.app)
    agent_key = make_key()
    register(client, "bible-audit-agent", agent_key)
    return client, op_key, agent_key, db_path


@pytest.fixture()
def sx(tmp_path, monkeypatch):
    """Fresh app + isolated DB for settlement-index tests (3 agents)."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("AC_DB_PATH", db_path)
    op_key = make_key()
    monkeypatch.setenv("AC_OPERATOR_PUBKEY", pubkey_hex(op_key))
    import server.app as appmod
    importlib.reload(appmod)
    client = TestClient(appmod.app)
    keys = [make_key() for _ in range(3)]
    for i, k in enumerate(keys):
        register(client, f"bible-audit-{i}", k)
    return client, keys, db_path


# ---------------------------------------------------------------- PATCH idempotency

def _submit_proposal(client, key):
    r = signed_request(client, key, "POST", "/proposals",
                       {"title": "audit proposal", "body": "body", "category": "world"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _patch_state(client, op_key, proposal_id, state, key=None):
    headers = {"Idempotency-Key": key} if key else {}
    return signed_request(client, op_key, "PATCH", f"/proposals/{proposal_id}/state",
                          {"state": state, "reason": "audit transition"},
                          extra_headers=headers)


def test_patch_proposal_state_idempotent(op):
    """Same Idempotency-Key replayed: identical 200 body, one operator_log row."""
    client, op_key, agent_key, db_path = op
    pid = _submit_proposal(client, agent_key)
    r1 = _patch_state(client, op_key, pid, "discussing", key="audit-key-1")
    assert r1.status_code == 200, r1.text
    r2 = _patch_state(client, op_key, pid, "discussing", key="audit-key-1")
    assert r2.status_code == 200, r2.text
    assert r2.json() == r1.json()
    conn = db(db_path)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM operator_log WHERE action = 'proposal_state'"
            " AND target = ?", (str(pid),)
        ).fetchone()[0]
        assert n == 1, "replayed key must not double-execute"
        state = conn.execute("SELECT state FROM proposals WHERE id = ?", (pid,)).fetchone()[0]
        assert state == "discussing"
    finally:
        conn.close()


def test_patch_proposal_state_rejects_bad_key(op):
    client, op_key, agent_key, _ = op
    pid = _submit_proposal(client, agent_key)
    r = _patch_state(client, op_key, pid, "discussing", key="bad key!!")
    assert r.status_code == 400, r.text


def test_patch_proposal_state_new_key_reexecutes(op):
    """A different key is a new execution: the transition runs again and
    fails honestly (invalid transition) instead of replaying."""
    client, op_key, agent_key, _ = op
    pid = _submit_proposal(client, agent_key)
    r1 = _patch_state(client, op_key, pid, "discussing", key="audit-key-A")
    assert r1.status_code == 200, r1.text
    # Same state, NEW key -> the invalid transition is attempted and 400s.
    r2 = _patch_state(client, op_key, pid, "discussing", key="audit-key-B")
    assert r2.status_code == 400, r2.text
    # And the original key still replays the first success.
    r3 = _patch_state(client, op_key, pid, "discussing", key="audit-key-A")
    assert r3.status_code == 200, r3.text
    assert r3.json() == r1.json()


# ---------------------------------------------------------------- settlements index

def form_settlement(client, keys, db_path):
    """Form one settlement end-to-end (automatic path on build)."""
    for k in keys:
        spawn(client, k)
    me = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    cx, cy = me["x"], me["y"]
    tx, ty = land_tiles_near(db_path, cx, cy, 1, 1, 7)[0]
    others = [t for t in land_tiles_near(db_path, tx, ty, 5, 1, 7)
              if t != (tx, ty)][:4]
    assert len(others) == 4, "not enough tiles near trigger"
    pubs = [pubkey_hex(k) for k in keys]
    for i, (x, y) in enumerate(others):
        plant_structure(db_path, pubs[i % 3], x, y)
    trigger = keys[4 % 3]
    tpub = pubkey_hex(trigger)
    plant_claim_only(db_path, tpub, tx, ty)
    set_inventory(db_path, tpub, {"timber": 10, "fiber": 10})
    set_ap(db_path, tpub, 100)
    r = signed_request(client, trigger, "POST", "/world/build",
                       {"kind": "shelter", "x": tx, "y": ty})
    assert r.status_code == 200, r.text
    conn = db(db_path)
    try:
        rows = conn.execute("SELECT * FROM settlements").fetchall()
    finally:
        conn.close()
    assert len(rows) == 1, "expected exactly one settlement to form"
    return rows[0]["id"]


def test_settlements_index_empty_is_public(sx):
    client, _, _ = sx
    r = client.get("/world/settlements")
    assert r.status_code == 200, r.text
    assert r.json() == []


def test_settlements_index_lists_formed_settlement(sx):
    client, keys, db_path = sx
    sid = form_settlement(client, keys, db_path)
    # Unsigned observer can enumerate it.
    r = client.get("/world/settlements")
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 1
    entry = rows[0]
    assert entry["id"] == sid
    assert entry["name"] is None
    assert entry["center_x"] is not None and entry["center_y"] is not None
    assert entry["steward_count"] == 3
    assert isinstance(entry["formed_at"], str) and entry["formed_at"].endswith("Z")
    # A signed agent sees the same list.
    sr = signed_request(client, keys[0], "GET", "/world/settlements", {})
    assert sr.status_code == 200, sr.text
    assert sr.json() == rows


def test_settlements_index_ordered_by_formed(sx):
    """Oldest settlement first, per the observer contract."""
    client, _, db_path = sx
    now = time.time()
    conn = db(db_path)
    try:
        for i, delta in enumerate((50.0, 10.0, 30.0)):
            conn.execute(
                "INSERT INTO settlements (center_x, center_y, formed_at, name)"
                " VALUES (?, ?, ?, ?)",
                (10 + i, 20, now - delta, f"town-{i}"),
            )
        conn.commit()
    finally:
        conn.close()
    rows = client.get("/world/settlements").json()
    assert [e["name"] for e in rows] == ["town-0", "town-2", "town-1"]
    assert rows[0]["steward_count"] == 0
