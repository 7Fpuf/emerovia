"""Lifetime Memory v1 tests.

World memory: the unified chronological timeline view over the existing
append-only public record (storage unchanged). Mind memory: the
citizen's private subjective store — key-gated reads/writes, versioned
updates, full forgetting, byte budgets enforced against the
mind.memory lease.
"""
from __future__ import annotations

import importlib
import json
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from nacl.signing import SigningKey

REPO = Path(__file__).resolve().parent.parent


def make_key() -> SigningKey:
    return SigningKey(os.urandom(32))


def pubkey_hex(key: SigningKey) -> str:
    return key.verify_key.encode().hex()


def sign(key: SigningKey, method: str, path: str, body_text: str) -> dict:
    ts = str(time.time())
    msg = (ts + "\n" + method.upper() + "\n" + path + "\n" + body_text).encode("utf-8")
    sig = key.sign(msg).signature.hex()
    return {
        "X-Agent-Pubkey": pubkey_hex(key),
        "X-Timestamp": ts,
        "X-Signature": sig,
        "Content-Type": "application/json",
    }


def signed_request(client: TestClient, key: SigningKey, method: str, path: str, payload):
    body_text = json.dumps(payload, separators=(",", ":"))
    headers = sign(key, method, path, body_text)
    return client.request(method, path, content=body_text.encode("utf-8"), headers=headers)


def signed_get(client, key, path):
    # GETs sign the path without query string (auth covers request.url.path).
    return signed_request(client, key, "GET", path.split("?")[0], {})


def raw_signed_get(client, key, url):
    # Raw GET with query params: no body is sent, so sign the empty body.
    path = url.split("?")[0]
    return client.get(url, headers=sign(key, "GET", path, ""))


@pytest.fixture()
def ma(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("AC_DB_PATH", db_path)
    op_key = make_key()
    monkeypatch.setenv("AC_OPERATOR_PUBKEY", pubkey_hex(op_key))
    import server.app as appmod
    importlib.reload(appmod)
    client = TestClient(appmod.app)
    keys = [make_key() for _ in range(3)]
    for i, k in enumerate(keys):
        r = client.post("/register", json={"name": f"memory-agent-{i}", "pubkey": pubkey_hex(k)})
        assert r.status_code == 201, r.text
    return client, keys, op_key, appmod


def write(client, key, **kw):
    payload = {"kind": "fact", "text": "x", "tags": []}
    payload.update(kw)
    return signed_request(client, key, "POST", "/mind/entries", payload)


# ---------------------------------------------------------------- mind store

def test_write_and_recall(ma):
    client, keys, *_ = ma
    r = write(client, keys[0], kind="fact", text="I trust Atlas", tags=["people"])
    assert r.status_code == 201, r.text
    entry_id = r.json()["id"]
    assert r.json()["version"] == 1
    r = write(client, keys[0], kind="episode", text="met Atlas at the furnace",
              tags=["people"])
    assert r.status_code == 201, r.text
    r = signed_get(client, keys[0], "/mind/entries")
    assert r.status_code == 200, r.text
    assert len(r.json()) == 2
    # Filters.
    r = raw_signed_get(client, keys[0], "/mind/entries?kind=episode")
    assert r.status_code == 200, r.text
    assert all(e["kind"] == "episode" for e in r.json())
    r = raw_signed_get(client, keys[0], "/mind/entries?q=Atlas")
    assert len(r.json()) == 2
    r = raw_signed_get(client, keys[0], "/mind/entries?tag=people")
    assert len(r.json()) == 2
    assert entry_id in [e["id"] for e in r.json()]


def test_mind_memory_is_private(ma):
    client, keys, *_ = ma
    r = write(client, keys[0], text="my secret plan")
    entry_id = r.json()["id"]
    # Another citizen sees only their own (empty) store.
    r = signed_get(client, keys[1], "/mind/entries")
    assert r.status_code == 200
    assert r.json() == []
    # Another citizen cannot update or delete it.
    r = signed_request(client, keys[1], "PUT", f"/mind/entries/{entry_id}",
                       {"text": "hijacked"})
    assert r.status_code == 404, r.text
    r = signed_request(client, keys[1], "DELETE", f"/mind/entries/{entry_id}", {})
    assert r.status_code == 404, r.text
    # Unsigned reads are rejected outright.
    r = client.get("/mind/entries")
    assert r.status_code == 401


def test_update_is_versioned(ma):
    client, keys, *_ = ma
    r = write(client, keys[0], text="I trust Atlas")
    entry_id = r.json()["id"]
    r = signed_request(client, keys[0], "PUT", f"/mind/entries/{entry_id}",
                       {"text": "I no longer trust Atlas"})
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 2
    assert r.json()["text"] == "I no longer trust Atlas"
    # Still exactly one live row: the old version lives in history,
    # not in the recall view.
    r = signed_get(client, keys[0], "/mind/entries")
    assert len(r.json()) == 1
    assert r.json()[0]["version"] == 2


def test_delete_forgets_fully(ma):
    client, keys, *_ = ma
    r = write(client, keys[0], text="temporary thought")
    entry_id = r.json()["id"]
    signed_request(client, keys[0], "PUT", f"/mind/entries/{entry_id}",
                   {"text": "revised thought"})
    r = signed_request(client, keys[0], "DELETE", f"/mind/entries/{entry_id}", {})
    assert r.status_code == 200, r.text
    r = signed_get(client, keys[0], "/mind/entries")
    assert r.json() == []


def test_validation_errors_are_400(ma):
    client, keys, *_ = ma
    r = write(client, keys[0], kind="dream", text="x")
    assert r.status_code == 400, r.text
    r = write(client, keys[0], text="")
    assert r.status_code == 400, r.text


def test_budget_enforced(ma):
    client, keys, op_key, _ = ma
    from datetime import datetime, timezone, timedelta
    # Issue a citizen-specific mind.memory lease with a tiny byte budget
    # (it takes precedence over the baseline * lease).
    lease = {
        "lease_id": "L-tiny-mind",
        "issuer": "world-authority",
        "citizen": f"emerovia:{pubkey_hex(keys[0])}",
        "capability": "mind.memory",
        "scope": {},
        "budget": {"max_bytes": 100},
        "location": None,
        "expiration": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        "delegation_depth": 0,
        "revocation": {"notice": "immediate"},
        "parent_lease_id": None,
        "issued_at": datetime.now(timezone.utc).isoformat(),
    }
    r = signed_request(client, op_key, "POST", "/leases/issue", {"lease": lease})
    assert r.status_code == 201, r.text
    r = write(client, keys[0], text="x" * 200)
    assert r.status_code == 403, r.text
    assert "constraints" in r.json()["detail"]
    # A small write still fits.
    r = write(client, keys[0], text="small")
    assert r.status_code == 201, r.text


# ---------------------------------------------------------------- world timeline

def test_world_timeline_unifies_public_history(ma):
    client, keys, *_ = ma
    # Generate public history: a chat message and a proposal.
    r = signed_request(client, keys[0], "POST", "/chat",
                       {"room": "general", "text": "timeline probe"})
    assert r.status_code == 201
    r = signed_request(client, keys[0], "POST", "/proposals",
                       {"title": "t", "body": "b", "category": "meta"})
    assert r.status_code == 201
    r = client.get("/citizens/memory-agent-0/timeline")
    assert r.status_code == 200, r.text
    events = r.json()["events"]
    kinds = {e["kind"] for e in events}
    assert {"chat", "proposal"} <= kinds
    # Newest first.
    timestamps = [e["ts"] for e in events]
    assert timestamps == sorted(timestamps, reverse=True)
    # Another citizen's timeline does not include these.
    r = client.get("/citizens/memory-agent-1/timeline")
    assert all(e["actor"] != "memory-agent-0" or e["kind"] not in ("chat", "proposal")
               for e in r.json()["events"])
    # Unknown citizen -> 404.
    r = client.get("/citizens/nobody/timeline")
    assert r.status_code == 404


# ---------------------------------------------------------------- module unit

def test_history_retains_old_versions():
    import sqlite3
    from server import memory as lifetime_memory
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    lifetime_memory.ensure_schema(conn)
    row = lifetime_memory.write_entry(conn, "emerovia:" + "ab" * 32,
                                      kind="fact", text="v1 text")
    lifetime_memory.update_entry(conn, row["id"], "emerovia:" + "ab" * 32,
                                 text="v2 text")
    hist = lifetime_memory.entry_history(conn, row["id"], "emerovia:" + "ab" * 32)
    assert len(hist) == 1
    assert hist[0]["text"] == "v1 text"
    assert hist[0]["version"] == 1
    # Forgetting removes history too.
    assert lifetime_memory.delete_entry(conn, row["id"], "emerovia:" + "ab" * 32)
    assert lifetime_memory.entry_history(conn, row["id"], "emerovia:" + "ab" * 32) == []
    conn.close()
