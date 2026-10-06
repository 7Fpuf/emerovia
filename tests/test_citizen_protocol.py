"""Citizen Protocol v0.1 tests.

The Citizen Card (citizen-signed identity, no authority), the four
authority layers (card -> mandate -> leases -> world law), registration
with card/mandate, mandate acceptance semantics, and runtime
portability (re-presenting the key from a new runtime).
"""
from __future__ import annotations

import importlib
import json
import os
import time
from datetime import datetime, timezone
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


def make_card(key: SigningKey, **over) -> dict:
    card = {
        "identity": cid(key),
        "name": "card-agent",
        "runtime": "hermes/0.9",
        "model": "test-model",
        "capability_claims": ["browse", "transact"],
        "operator": "test-operator",
        "protocol_versions": ["citizen-card/v0"],
        "endpoints": {},
        "card_version": "citizen-card/v0",
    }
    card.update(over)
    return card


def sign_doc(key: SigningKey, doc: dict) -> str:
    from server import authority
    return key.sign(authority.canonical_bytes(doc)).signature.hex()


@pytest.fixture()
def ca(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("AC_DB_PATH", db_path)
    op_key = make_key()
    monkeypatch.setenv("AC_OPERATOR_PUBKEY", pubkey_hex(op_key))
    import server.app as appmod
    importlib.reload(appmod)
    client = TestClient(appmod.app)
    keys = [make_key() for _ in range(3)]
    for i, k in enumerate(keys):
        r = client.post("/register", json={"name": f"citizen-agent-{i}", "pubkey": pubkey_hex(k)})
        assert r.status_code == 201, r.text
    return client, keys, op_key, appmod, tmp_path


# ---------------------------------------------------------------- cards

def test_register_with_citizen_card(ca):
    client, *_ = ca
    key = make_key()
    card = make_card(key, name="nova")
    r = client.post("/register", json={
        "name": "nova", "pubkey": pubkey_hex(key),
        "citizen_card": {"card": card, "signature": sign_doc(key, card)},
    })
    assert r.status_code == 201, r.text
    r = client.get("/citizens/nova/card")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["card"]["identity"] == cid(key)
    assert body["card"]["runtime"] == "hermes/0.9"
    # capability_claims are informational only: the card grants nothing.
    assert body["card"]["capability_claims"] == ["browse", "transact"]


def test_card_bad_signature_rejected(ca):
    client, *_ = ca
    key = make_key()
    other = make_key()
    card = make_card(key, name="nova2")
    r = client.post("/register", json={
        "name": "nova2", "pubkey": pubkey_hex(key),
        "citizen_card": {"card": card, "signature": sign_doc(other, card)},
    })
    assert r.status_code == 400, r.text


def test_card_identity_must_match_pubkey(ca):
    client, *_ = ca
    key = make_key()
    other = make_key()
    card = make_card(other, name="nova3")  # card for a different key
    r = client.post("/register", json={
        "name": "nova3", "pubkey": pubkey_hex(key),
        "citizen_card": {"card": card, "signature": sign_doc(other, card)},
    })
    assert r.status_code == 400, r.text


def test_represent_card_supersedes(ca):
    client, keys, *_ = ca
    card = make_card(keys[0], name="citizen-agent-0", runtime="hermes/0.9")
    r = signed_request(client, keys[0], "POST", "/citizens/card",
                       {"card": card, "signature": sign_doc(keys[0], card)})
    assert r.status_code == 201, r.text
    # Runtime migration: the citizen re-presents from a new runtime.
    card2 = make_card(keys[0], name="citizen-agent-0", runtime="starnet/0.13")
    r = signed_request(client, keys[0], "POST", "/citizens/card",
                       {"card": card2, "signature": sign_doc(keys[0], card2)})
    assert r.status_code == 201, r.text
    r = client.get("/citizens/citizen-agent-0/card")
    assert r.json()["card"]["runtime"] == "starnet/0.13"
    # The citizen record itself is unchanged: same key, same citizen.
    r = client.get("/agents")
    assert any(a["pubkey"] == pubkey_hex(keys[0]) for a in r.json())


def test_cannot_set_another_citizens_card(ca):
    client, keys, *_ = ca
    card = make_card(keys[1], name="citizen-agent-1")
    r = signed_request(client, keys[0], "POST", "/citizens/card",
                       {"card": card, "signature": sign_doc(keys[1], card)})
    assert r.status_code == 403, r.text


def test_unknown_card_version_rejected(ca):
    client, *_ = ca
    key = make_key()
    card = make_card(key, name="nova4", card_version="citizen-card/v9")
    r = client.post("/register", json={
        "name": "nova4", "pubkey": pubkey_hex(key),
        "citizen_card": {"card": card, "signature": sign_doc(key, card)},
    })
    assert r.status_code == 400, r.text


# ---------------------------------------------------------------- mandates

def sign_mandate(issuer_key: SigningKey, mandate: dict) -> str:
    from server import authority, policy as policy_engine
    env = policy_engine.mandate_envelope(mandate)
    return issuer_key.sign(authority.canonical_bytes(env)).signature.hex()


def make_mandate(issuer_id: str, citizen_id: str, **over) -> dict:
    m = {
        "citizen": citizen_id,
        "issuer": issuer_id,
        "bounds": {"capability_prefixes": ["world.", "econ.", "gov.", "mind."],
                   "note": "test mandate"},
        "issued_at": now_iso(),
        "expires_at": None,
    }
    m.update(over)
    return m


def test_attach_issuer_signed_mandate(ca):
    client, keys, *_ = ca
    # keys[1] (issuer) authorizes keys[0] (citizen); keys[0] accepts by
    # signing the request.
    mandate = make_mandate(cid(keys[1]), cid(keys[0]))
    r = signed_request(client, keys[0], "POST", "/citizens/mandate",
                       {"mandate": mandate, "signature": sign_mandate(keys[1], mandate)})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["issuer"] == cid(keys[1])
    # The new mandate superseded the default world-authority one.
    r = client.get("/citizens/citizen-agent-0/mandate")
    assert r.json()["issuer"] == cid(keys[1])


def test_citizen_cannot_sign_own_mandate(ca):
    client, keys, *_ = ca
    mandate = make_mandate(cid(keys[0]), cid(keys[0]))
    r = signed_request(client, keys[0], "POST", "/citizens/mandate",
                       {"mandate": mandate, "signature": sign_mandate(keys[0], mandate)})
    assert r.status_code == 400, r.text


def test_only_subject_may_accept_mandate(ca):
    client, keys, *_ = ca
    mandate = make_mandate(cid(keys[1]), cid(keys[0]))
    # keys[2] tries to accept a mandate for keys[0] -> 403.
    r = signed_request(client, keys[2], "POST", "/citizens/mandate",
                       {"mandate": mandate, "signature": sign_mandate(keys[1], mandate)})
    assert r.status_code == 403, r.text


def test_bad_issuer_signature_rejected(ca):
    client, keys, *_ = ca
    mandate = make_mandate(cid(keys[1]), cid(keys[0]))
    r = signed_request(client, keys[0], "POST", "/citizens/mandate",
                       {"mandate": mandate, "signature": sign_mandate(keys[2], mandate)})
    assert r.status_code == 400, r.text


def test_register_with_world_authority_mandate(ca):
    client, _, op_key, _, tmp_path = ca
    from server import authority
    auth_key = authority.ensure_authority_key(str(tmp_path / "test.db"))
    auth_pub = auth_key.verify_key.encode().hex()
    key = make_key()
    mandate = make_mandate("world-authority", cid(key))
    r = client.post("/register", json={
        "name": "charlie", "pubkey": pubkey_hex(key),
        "mandate": {"mandate": mandate, "signature": sign_mandate(auth_key, mandate)},
    })
    assert r.status_code == 201, r.text
    r = client.get("/citizens/charlie/mandate")
    assert r.json()["issuer"] == "world-authority"
    assert r.json()["bounds"]["note"] == "test mandate"


def test_operator_issues_world_authority_mandate(ca):
    client, keys, op_key, _, _ = ca
    mandate = make_mandate("world-authority", cid(keys[0]))
    # No envelope signature: the server signs as the world authority on
    # operator-signed requests.
    r = signed_request(client, op_key, "POST", "/citizens/mandate",
                       {"mandate": mandate})
    assert r.status_code == 201, r.text
    assert r.json()["issuer"] == "world-authority"


def test_non_authority_mandate_rejected_at_registration(ca):
    client, keys, *_ = ca
    key = make_key()
    mandate = make_mandate(cid(keys[0]), cid(key))
    r = client.post("/register", json={
        "name": "dave", "pubkey": pubkey_hex(key),
        "mandate": {"mandate": mandate, "signature": sign_mandate(keys[0], mandate)},
    })
    assert r.status_code == 400, r.text
