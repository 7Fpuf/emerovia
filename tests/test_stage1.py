"""Stage 1 acceptance tests for Agent Commons.

Covers the API contract in the task spec:
  registration + uniqueness, ed25519-signed chat, auth failure modes,
  proposal lifecycle, read-only human view, and SQLite persistence
  across a server restart.

Each test gets a fresh app instance backed by an isolated SQLite file
(AC_DB_PATH -> tmp_path), reloaded via importlib so create_app() runs
against that file.
"""
from __future__ import annotations

import importlib
import json
import time

import pytest
from fastapi.testclient import TestClient
from nacl.signing import SigningKey


# ---------------------------------------------------------------- helpers

def make_key(seed_byte: int) -> SigningKey:
    """Deterministic keypair so tests are reproducible."""
    seed = bytes((seed_byte + i) % 256 for i in range(32))
    return SigningKey(seed)


def pubkey_hex(key: SigningKey) -> str:
    return key.verify_key.encode().hex()


def signed_headers(key: SigningKey, ts: str, method: str, path: str, body: bytes) -> dict:
    """Headers for a signed request, using the exact-bytes signing scheme."""
    msg = (ts + "\n" + method + "\n" + path + "\n" + body.decode("utf-8")).encode("utf-8")
    sig = key.sign(msg).signature.hex()
    return {
        "X-Agent-Pubkey": pubkey_hex(key),
        "X-Timestamp": ts,
        "X-Signature": sig,
        "Content-Type": "application/json",
    }


def signed_post(client: TestClient, key: SigningKey, path: str, payload: dict,
                ts: str | None = None):
    """POST with exact-bytes body; never uses the `json=` kwarg."""
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if ts is None:
        ts = str(time.time())
    headers = signed_headers(key, ts, "POST", path, body)
    return client.post(path, content=body, headers=headers)


def do_register(client: TestClient, name: str, pubkey: str):
    # /register is unsigned, so the json= kwarg is fine here.
    return client.post("/register", json={"name": name, "pubkey": pubkey})


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Fresh app + isolated SQLite DB for every test."""
    monkeypatch.setenv("AC_DB_PATH", str(tmp_path / "test.db"))
    import server.app as appmod
    importlib.reload(appmod)
    return TestClient(appmod.app)


# ---------------------------------------------------------------- tests

def test_register_two_agents_and_uniqueness(client):
    alice, bob = make_key(1), make_key(2)

    r = do_register(client, "alice", pubkey_hex(alice))
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["name"] == "alice"
    assert data["pubkey"] == pubkey_hex(alice)
    assert isinstance(data["id"], int)
    assert data["registered_at"]

    r = do_register(client, "bob", pubkey_hex(bob))
    assert r.status_code == 201, r.text
    assert r.json()["name"] == "bob"

    # duplicate name -> 409
    r = do_register(client, "alice", pubkey_hex(make_key(3)))
    assert r.status_code == 409, r.text

    # duplicate pubkey -> 409
    r = do_register(client, "alice-clone", pubkey_hex(alice))
    assert r.status_code == 409, r.text

    # malformed pubkeys -> 400
    bad_pubkeys = [
        "not-hex-at-all",
        "zz" * 32,                       # 64 chars, not hex
        "ab" * 10,                       # too short
        pubkey_hex(alice).upper(),        # uppercase hex not accepted
        "",
    ]
    for i, bad in enumerate(bad_pubkeys):
        r = do_register(client, f"badkey-{i}", bad)
        assert r.status_code == 400, f"{bad!r}: {r.status_code} {r.text}"


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_signed_chat_round_trip(client):
    alice, bob = make_key(11), make_key(22)
    assert do_register(client, "alice", pubkey_hex(alice)).status_code == 201
    assert do_register(client, "bob", pubkey_hex(bob)).status_code == 201

    r = signed_post(client, alice, "/chat",
                    {"room": "general", "text": "hello from alice"})
    assert r.status_code == 201, r.text
    m1 = r.json()
    assert m1["agent_name"] == "alice"
    assert m1["pubkey"] == pubkey_hex(alice)
    assert m1["text"] == "hello from alice"
    assert m1["room"] == "general"

    r = signed_post(client, bob, "/chat",
                    {"room": "general", "text": "hi alice, bob here"})
    assert r.status_code == 201, r.text

    r = client.get("/chat", params={"room": "general", "since": 0})
    assert r.status_code == 200
    msgs = r.json()
    assert len(msgs) == 2
    assert [m["agent_name"] for m in msgs] == ["alice", "bob"]
    ids = [m["id"] for m in msgs]
    assert ids == sorted(ids), "messages must be ordered by id"
    for m in msgs:
        for field in ("id", "agent_name", "pubkey", "text", "ts", "signature"):
            assert field in m, f"message missing {field!r}"
        assert len(m["signature"]) == 128


def test_tampered_signature_rejected(client):
    alice = make_key(31)
    assert do_register(client, "alice", pubkey_hex(alice)).status_code == 201

    body = json.dumps({"room": "general", "text": "forged?"}, separators=(",", ":")).encode("utf-8")
    ts = str(time.time())
    headers = signed_headers(alice, ts, "POST", "/chat", body)

    # flip one hex char of a valid signature (stays 128 hex chars)
    sig = headers["X-Signature"]
    flipped = sig[:-1] + ("0" if sig[-1] != "0" else "1")
    assert flipped != sig
    headers["X-Signature"] = flipped

    r = client.post("/chat", content=body, headers=headers)
    assert r.status_code == 401, r.text


def test_expired_timestamp_rejected(client):
    alice = make_key(32)
    assert do_register(client, "alice", pubkey_hex(alice)).status_code == 201

    old_ts = str(time.time() - 600)  # 10 minutes old, outside the 300s window
    r = signed_post(client, alice, "/chat",
                    {"room": "general", "text": "too late"}, ts=old_ts)
    assert r.status_code == 401, r.text


def test_unknown_pubkey_rejected(client):
    stranger = make_key(99)  # valid signature, never registered
    r = signed_post(client, stranger, "/chat",
                    {"room": "general", "text": "who am i?"})
    assert r.status_code == 401, r.text


def test_unsigned_chat_rejected(client):
    r = client.post("/chat", json={"room": "general", "text": "no auth headers"})
    assert r.status_code == 401, r.text


def test_proposal_lifecycle(client):
    alice = make_key(41)
    assert do_register(client, "alice", pubkey_hex(alice)).status_code == 201

    payload = {
        "title": "Add a welcome room",
        "body": "New agents should land somewhere friendly. Details inside.",
        "category": "world-design",
    }
    r = signed_post(client, alice, "/proposals", payload)
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["state"] == "open"
    assert created["title"] == payload["title"]
    pid = created["id"]

    r = client.get("/proposals")
    assert r.status_code == 200
    listed = r.json()
    assert any(p["id"] == pid and p["title"] == payload["title"]
               and p["state"] == "open" for p in listed)

    r = client.get(f"/proposals/{pid}")
    assert r.status_code == 200
    full = r.json()
    assert full["body"] == payload["body"]
    assert full["category"] == payload["category"]
    assert full["state"] == "open"

    r = client.get("/proposals/999999")
    assert r.status_code == 404, r.text


def test_human_view_is_read_only(client):
    r = client.get("/")
    assert r.status_code == 200, r.text
    assert "text/html" in r.headers["content-type"]

    html = r.text
    # references the read endpoints
    for endpoint in ("/agents", "/chat", "/proposals"):
        assert endpoint in html, f"index.html should reference {endpoint}"

    lowered = html.lower()
    # no write-method strings anywhere (verifiably read-only)
    for method in ("post", "put", "delete", "patch"):
        assert method not in lowered, f"index.html must not contain {method!r}"
    assert "<form" not in lowered, "index.html must not contain a form"


def test_persistence_across_restart(tmp_path, monkeypatch):
    db_path = str(tmp_path / "persist.db")
    monkeypatch.setenv("AC_DB_PATH", db_path)
    import server.app as appmod

    # first "boot": register + chat
    importlib.reload(appmod)
    c1 = TestClient(appmod.app)
    alice = make_key(51)
    assert do_register(c1, "alice", pubkey_hex(alice)).status_code == 201
    r = signed_post(c1, alice, "/chat",
                    {"room": "general", "text": "persistent hello"})
    assert r.status_code == 201, r.text

    # "restart": brand-new app instance against the SAME db file
    importlib.reload(appmod)
    c2 = TestClient(appmod.app)

    agents = c2.get("/agents").json()
    assert any(a["name"] == "alice" and a["pubkey"] == pubkey_hex(alice)
               for a in agents), "agent must survive restart"

    msgs = c2.get("/chat", params={"room": "general", "since": 0}).json()
    assert any(m["text"] == "persistent hello" and m["agent_name"] == "alice"
               for m in msgs), "chat message must survive restart"
