"""Capability Leases v1 tests.

The lease primitive: issuance (signature verification, issuer
authorization), delegation with narrowing, revocation with chain
propagation, lazy expiry, the constitutional token-gating guard, and
the closed tool.* namespace. In-world capabilities only.
"""
from __future__ import annotations

import importlib
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from nacl.signing import SigningKey

REPO = Path(__file__).resolve().parent.parent


def make_key() -> SigningKey:
    return SigningKey(os.urandom(32))


def pubkey_hex(key: SigningKey) -> str:
    return key.verify_key.encode().hex()


def cid(key: SigningKey) -> str:
    return f"emerovia:{pubkey_hex(key)}"


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


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture()
def la(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("AC_DB_PATH", db_path)
    op_key = make_key()
    monkeypatch.setenv("AC_OPERATOR_PUBKEY", pubkey_hex(op_key))
    import server.app as appmod
    importlib.reload(appmod)
    client = TestClient(appmod.app)
    keys = [make_key() for _ in range(3)]
    for i, k in enumerate(keys):
        r = client.post("/register", json={"name": f"lease-agent-{i}", "pubkey": pubkey_hex(k)})
        assert r.status_code == 201, r.text
    return client, keys, op_key, appmod


def sign_envelope(issuer_key: SigningKey, lease: dict) -> str:
    from server import authority, leases as lease_registry
    env = lease_registry.envelope_for(lease)
    return issuer_key.sign(authority.canonical_bytes(env)).signature.hex()


def base_lease(issuer: str, citizen: str, capability: str, **over) -> dict:
    lease = {
        "lease_id": f"L-{os.urandom(4).hex()}",
        "issuer": issuer,
        "citizen": citizen,
        "capability": capability,
        "scope": {},
        "budget": None,
        "location": None,
        "expiration": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
        "delegation_depth": 0,
        "revocation": {"notice": "immediate"},
        "parent_lease_id": None,
        "issued_at": now_iso(),
    }
    lease.update(over)
    return lease


def issue(client, signer_key, lease: dict):
    sig = sign_envelope(signer_key, lease)
    return signed_request(client, signer_key, "POST", "/leases/issue",
                          {"lease": lease, "signature": sig})


# ---------------------------------------------------------------- issuance

def test_operator_issues_world_authority_lease(la):
    client, keys, op_key, _ = la
    lease = base_lease("world-authority", cid(keys[0]), "object.furnace.use",
                       expiration=None, delegation_depth=1)
    r = signed_request(client, op_key, "POST", "/leases/issue", {"lease": lease})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["issuer"] == "world-authority"
    assert body["delegation_depth"] == 1
    # The citizen can now exercise the capability.
    r = signed_request(client, keys[0], "POST", "/policy/check",
                       {"capability": "object.furnace.use"})
    assert r.json()["allowed"] is True


def test_issue_requires_issuer_signature(la):
    client, keys, *_ = la
    lease = base_lease(cid(keys[0]), cid(keys[1]), "object.furnace.use")
    # Signed by the wrong key -> 400.
    r = signed_request(client, keys[0], "POST", "/leases/issue",
                       {"lease": lease, "signature": sign_envelope(keys[2], lease)})
    assert r.status_code == 400, r.text


def test_only_issuer_may_issue(la):
    client, keys, *_ = la
    lease = base_lease(cid(keys[0]), cid(keys[1]), "object.furnace.use")
    # keys[1] tries to issue a lease naming keys[0] as issuer -> 403.
    r = signed_request(client, keys[1], "POST", "/leases/issue",
                       {"lease": lease, "signature": sign_envelope(keys[0], lease)})
    assert r.status_code == 403, r.text


def test_closed_namespace_not_issuable_even_by_world_authority(la):
    client, keys, op_key, _ = la
    lease = base_lease("world-authority", cid(keys[0]), "tool.compute")
    r = signed_request(client, op_key, "POST", "/leases/issue", {"lease": lease})
    assert r.status_code == 400, r.text
    assert "closed" in r.json()["detail"]


def test_token_gating_is_unconstitutional(la):
    client, keys, op_key, _ = la
    lease = base_lease("world-authority", cid(keys[0]), "object.furnace.use",
                       scope={"requires_token": "some-token"})
    r = signed_request(client, op_key, "POST", "/leases/issue", {"lease": lease})
    assert r.status_code == 400, r.text
    assert "unconstitutional" in r.json()["detail"]


def test_indefinite_lease_requires_world_authority(la):
    client, keys, *_ = la
    lease = base_lease(cid(keys[0]), cid(keys[1]), "object.furnace.use",
                       expiration=None)
    r = issue(client, keys[0], lease)
    assert r.status_code == 400, r.text


def test_statutory_star_lease_requires_world_authority(la):
    client, keys, *_ = la
    lease = base_lease(cid(keys[0]), "*", "object.furnace.use")
    r = issue(client, keys[0], lease)
    assert r.status_code == 400, r.text


# ---------------------------------------------------------------- delegation

def test_delegation_with_narrowing(la):
    client, keys, op_key, _ = la
    exp = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    # World authority grants keys[0] a delegable furnace lease with a budget.
    parent = base_lease("world-authority", cid(keys[0]), "object.furnace.use",
                        budget={"max_uses_per_day": 10}, delegation_depth=1,
                        expiration=exp)
    r = signed_request(client, op_key, "POST", "/leases/issue", {"lease": parent})
    assert r.status_code == 201, r.text
    parent_id = r.json()["lease_id"]
    # keys[0] sub-leases to keys[1], narrower budget, depth 0.
    child = base_lease(cid(keys[0]), cid(keys[1]), "object.furnace.use",
                       budget={"max_uses_per_day": 3}, delegation_depth=0,
                       parent_lease_id=parent_id, expiration=exp)
    r = issue(client, keys[0], child)
    assert r.status_code == 201, r.text
    r = signed_request(client, keys[1], "POST", "/policy/check",
                       {"capability": "object.furnace.use"})
    assert r.json()["allowed"] is True
    assert r.json()["lease_id"] == child["lease_id"]


def test_sublease_wider_than_parent_rejected(la):
    client, keys, op_key, _ = la
    parent = base_lease("world-authority", cid(keys[0]), "object.furnace.use",
                        budget={"max_uses_per_day": 10}, delegation_depth=1)
    r = signed_request(client, op_key, "POST", "/leases/issue", {"lease": parent})
    parent_id = r.json()["lease_id"]
    # Wider budget than the parent -> 400.
    child = base_lease(cid(keys[0]), cid(keys[1]), "object.furnace.use",
                       budget={"max_uses_per_day": 50}, delegation_depth=0,
                       parent_lease_id=parent_id)
    r = issue(client, keys[0], child)
    assert r.status_code == 400, r.text
    # Wrong depth (must be exactly parent - 1) -> 400.
    child2 = base_lease(cid(keys[0]), cid(keys[1]), "object.furnace.use",
                        budget={"max_uses_per_day": 3}, delegation_depth=1,
                        parent_lease_id=parent_id)
    r = issue(client, keys[0], child2)
    assert r.status_code == 400, r.text


def test_only_holder_may_delegate(la):
    client, keys, op_key, _ = la
    parent = base_lease("world-authority", cid(keys[0]), "object.furnace.use",
                        delegation_depth=1)
    r = signed_request(client, op_key, "POST", "/leases/issue", {"lease": parent})
    parent_id = r.json()["lease_id"]
    # keys[2] (not the holder) tries to delegate keys[0]'s lease -> 403
    # at the request-signer check.
    child = base_lease(cid(keys[2]), cid(keys[1]), "object.furnace.use",
                        delegation_depth=0, parent_lease_id=parent_id)
    r = issue(client, keys[2], child)
    assert r.status_code in (400, 403), r.text


def test_no_delegation_without_depth(la):
    client, keys, op_key, _ = la
    parent = base_lease("world-authority", cid(keys[0]), "object.furnace.use",
                        delegation_depth=0)
    r = signed_request(client, op_key, "POST", "/leases/issue", {"lease": parent})
    parent_id = r.json()["lease_id"]
    child = base_lease(cid(keys[0]), cid(keys[1]), "object.furnace.use",
                       delegation_depth=0, parent_lease_id=parent_id)
    r = issue(client, keys[0], child)
    assert r.status_code == 400, r.text


# ---------------------------------------------------------------- revocation

def test_revocation_propagates_down_chain(la):
    client, keys, op_key, _ = la
    exp = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    parent = base_lease("world-authority", cid(keys[0]), "object.furnace.use",
                        delegation_depth=1, expiration=exp)
    r = signed_request(client, op_key, "POST", "/leases/issue", {"lease": parent})
    parent_id = r.json()["lease_id"]
    child = base_lease(cid(keys[0]), cid(keys[1]), "object.furnace.use",
                       delegation_depth=0, parent_lease_id=parent_id,
                       expiration=exp)
    r = issue(client, keys[0], child)
    assert r.status_code == 201, r.text
    child_id = r.json()["lease_id"]
    # The issuer revokes the parent: the child dies with it.
    r = signed_request(client, op_key, "POST", f"/leases/{parent_id}/revoke", {})
    assert r.status_code == 200, r.text
    assert set(r.json()["revoked"]) == {parent_id, child_id}
    r = signed_request(client, keys[1], "POST", "/policy/check",
                       {"capability": "object.furnace.use"})
    assert r.json()["allowed"] is False
    # And the registry shows both revoked.
    r = client.get(f"/leases/{child_id}")
    assert r.json()["status"] == "revoked"


def test_revocation_requires_issuer_or_world_authority(la):
    client, keys, op_key, _ = la
    lease = base_lease("world-authority", cid(keys[0]), "object.furnace.use")
    r = signed_request(client, op_key, "POST", "/leases/issue", {"lease": lease})
    lease_id = r.json()["lease_id"]
    # keys[1] is neither issuer nor world authority -> 403.
    r = signed_request(client, keys[1], "POST", f"/leases/{lease_id}/revoke", {})
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------- expiry

def test_expired_lease_covers_nothing(la):
    client, keys, op_key, _ = la
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    lease = base_lease("world-authority", cid(keys[0]), "object.furnace.use",
                       expiration=past)
    r = signed_request(client, op_key, "POST", "/leases/issue", {"lease": lease})
    assert r.status_code == 201, r.text
    r = signed_request(client, keys[0], "POST", "/policy/check",
                       {"capability": "object.furnace.use"})
    assert r.json()["allowed"] is False


# ---------------------------------------------------------------- registry

def test_lease_registry_reads(la):
    client, keys, op_key, _ = la
    lease = base_lease("world-authority", cid(keys[0]), "object.furnace.use")
    r = signed_request(client, op_key, "POST", "/leases/issue", {"lease": lease})
    lease_id = r.json()["lease_id"]
    r = client.get(f"/leases/{lease_id}")
    assert r.status_code == 200
    assert r.json()["capability"] == "object.furnace.use"
    r = client.get("/leases", params={"citizen": cid(keys[0])})
    assert any(l["lease_id"] == lease_id for l in r.json())
    r = client.get("/leases/does-not-exist")
    assert r.status_code == 404
