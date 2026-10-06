"""Capability Registry v1 tests.

The public catalog of what capabilities exist, their namespace posture
(open/chartered/closed), and the lease registry read surface.
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


@pytest.fixture()
def ra(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("AC_DB_PATH", db_path)
    op_key = make_key()
    monkeypatch.setenv("AC_OPERATOR_PUBKEY", pubkey_hex(op_key))
    import server.app as appmod
    importlib.reload(appmod)
    client = TestClient(appmod.app)
    return client, op_key


def test_catalog_lists_v0_namespaces(ra):
    client, _ = ra
    r = client.get("/capabilities")
    assert r.status_code == 200, r.text
    caps = r.json()
    namespaces = {c["namespace"] for c in caps}
    assert {"world", "econ", "object", "gov", "mind", "org", "tool"} <= namespaces
    names = {c["name"] for c in caps}
    assert "world.move" in names and "econ.trade" in names and "mind.memory" in names


def test_tool_namespace_is_closed(ra):
    client, _ = ra
    r = client.get("/capabilities")
    tool_caps = [c for c in r.json() if c["namespace"] == "tool"]
    assert tool_caps, "tool.* capabilities must be cataloged"
    assert all(c["status"] == "closed" for c in tool_caps)


def test_chartered_capabilities_not_issuable_in_v1(ra):
    """org.issue is achievement-gated by charter; there is no charter
    machinery in v1, so chartered capabilities are not issuable —
    not even to the world authority."""
    from server import authority
    client, op_key = ra
    key = make_key()
    r = client.post("/register", json={"name": "Chartered", "pubkey": pubkey_hex(key)})
    assert r.status_code == 201, r.text
    lease = {
        "lease_id": "chartered-org-issue-1",
        "issuer": authority.WORLD_AUTHORITY_ID,
        "citizen": "emerovia:" + pubkey_hex(key),
        "capability": "org.issue",
        "scope": {},
        "budget": None,
        "location": None,
        "expiration": None,
        "delegation_depth": 0,
        "revocation": {"notice": "immediate"},
        "parent_lease_id": None,
        "issued_at": "2026-10-06T00:00:00+00:00",
    }
    r = signed_request(client, op_key, "POST", "/leases/issue", {"lease": lease})
    assert r.status_code == 400, r.text
    assert "charter" in r.text.lower()


def test_capability_detail(ra):
    client, _ = ra
    r = client.get("/capabilities/world.move")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["namespace"] == "world"
    assert body["status"] == "open"
    assert "POST /world/move" in body["routes"]
    assert body["active_leases"] >= 1  # the baseline * lease
    r = client.get("/capabilities/nope.nothing")
    assert r.status_code == 404


def test_registry_is_public_and_queryable(ra):
    client, _ = ra
    r = client.get("/leases")
    assert r.status_code == 200
    assert len(r.json()) >= 20  # baseline * leases
    r = client.get("/leases", params={"capability": "world.chat"})
    assert all(l["capability"] == "world.chat" for l in r.json())
    lease_id = r.json()[0]["lease_id"]
    r = client.get(f"/leases/{lease_id}")
    assert r.status_code == 200
    assert r.json()["lease_id"] == lease_id
    assert r.json()["citizen"] == "*"
    assert r.json()["issuer"] == "world-authority"
