"""Bible-completeness audit fixes (pod/bible-completeness).

Regression tests for the gap closed in this branch:

PATCH /proposals/{id}/state (operator-only) was the one mutating route
without idempotency support. It now honors the Idempotency-Key header
like every other mutating route, scoped to the operator (not to any
individual agent).
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
                   payload: dict, extra_headers: dict | None = None):
    body_text = json.dumps(payload, separators=(",", ":"))
    headers = sign(key, method, path, body_text)
    if extra_headers:
        headers.update(extra_headers)
    return client.request(method, path, content=body_text.encode("utf-8"), headers=headers)


def register(client: TestClient, name: str, key: SigningKey):
    r = client.post("/register", json={"name": name, "pubkey": pubkey_hex(key)})
    assert r.status_code == 201, r.text
    return r.json()


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


def _db(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------- tests

def test_patch_proposal_state_idempotent(op):
    """Same Idempotency-Key replayed: identical 200 body, one operator_log row."""
    client, op_key, agent_key, db_path = op
    pid = _submit_proposal(client, agent_key)
    r1 = _patch_state(client, op_key, pid, "discussing", key="audit-key-1")
    assert r1.status_code == 200, r1.text
    r2 = _patch_state(client, op_key, pid, "discussing", key="audit-key-1")
    assert r2.status_code == 200, r2.text
    assert r2.json() == r1.json()
    conn = _db(db_path)
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
