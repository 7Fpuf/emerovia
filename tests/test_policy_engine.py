"""Policy Engine v1 tests.

Covers the single enforcement point: the five-check pipeline
(identity -> world law -> mandate -> lease -> constraints), deny by
default, closed namespaces, statutes, denial ledger-logging, and
decision determinism.

Same isolated-app fixture pattern as the other test files: a fresh tmp
DB per test, AC_OPERATOR_PUBKEY set to a kept operator key.
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


def op_signed_request(client, op_key, method, path, payload):
    return signed_request(client, op_key, method, path, payload)


@pytest.fixture()
def pa(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("AC_DB_PATH", db_path)
    op_key = make_key()
    monkeypatch.setenv("AC_OPERATOR_PUBKEY", pubkey_hex(op_key))
    import server.app as appmod
    importlib.reload(appmod)
    client = TestClient(appmod.app)
    keys = [make_key() for _ in range(3)]
    for i, k in enumerate(keys):
        r = client.post("/register", json={"name": f"policy-agent-{i}", "pubkey": pubkey_hex(k)})
        assert r.status_code == 201, r.text
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    yield client, keys, op_key, appmod, conn, db_path
    conn.close()


def chat(client, key, text="hello"):
    return signed_request(client, key, "POST", "/chat", {"room": "general", "text": text})


# ---------------------------------------------------------------- baseline

def test_registered_citizen_passes_guard(pa):
    client, keys, *_ = pa
    r = chat(client, keys[0])
    assert r.status_code == 201, r.text


def test_default_mandate_issued_at_registration(pa):
    client, keys, *_ = pa
    r = client.get("/citizens/policy-agent-0/mandate")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["issuer"] == "world-authority"
    assert body["citizen"] == f"emerovia:{pubkey_hex(keys[0])}"


def test_baseline_leases_cover_all_mapped_verbs(pa):
    client, keys, *_ = pa
    # Every capability in the route map must resolve to an allowed
    # decision for a fresh citizen (v1 preserves existing behavior).
    from server import policy as policy_engine
    seen = set()
    for _, _, capability in policy_engine.ROUTE_CAPABILITIES:
        seen.add(capability)
    for capability in sorted(seen):
        r = signed_request(client, keys[1], "POST", "/policy/check", {"capability": capability})
        assert r.status_code == 200, r.text
        assert r.json()["allowed"], f"{capability} should be allowed by baseline"


# ---------------------------------------------------------------- world law

def test_closed_tool_namespace_denied(pa):
    client, keys, *_ = pa
    r = signed_request(client, keys[0], "POST", "/policy/check", {"capability": "tool.compute"})
    assert r.status_code == 200
    body = r.json()
    assert body["allowed"] is False
    assert body["failed_check"] == "world_law"
    assert "closed" in body["reason"]


def test_statute_can_forbid_a_capability(pa):
    client, keys, op_key, *_ = pa
    # Enact world law forbidding trade.
    r = op_signed_request(client, op_key, "POST", "/policy/statutes", {
        "name": "no-trade-tuesday",
        "description": "test statute",
        "rule": {"capability": "econ.trade"},
    })
    assert r.status_code == 201, r.text
    # The verb is now denied at check 2, before leases are consulted.
    r = signed_request(client, keys[0], "POST", "/policy/check", {"capability": "econ.trade"})
    assert r.json()["allowed"] is False
    assert r.json()["failed_check"] == "world_law"
    # And a real mutation is denied + logged.
    r = signed_request(client, keys[0], "POST", "/trade/offers", {"give": {}, "want": {}})
    assert r.status_code == 403, r.text
    denials = client.get("/policy/denials").json()
    assert any(d["failed_check"] == "world_law" and d["capability"] == "econ.trade"
               for d in denials)


def test_statute_requires_operator(pa):
    client, keys, *_ = pa
    r = signed_request(client, keys[0], "POST", "/policy/statutes", {
        "name": "x", "description": "y", "rule": {"capability": "world.chat"},
    })
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------- lease layer

def test_revoking_baseline_lease_denies_verb(pa):
    client, keys, op_key, *_ = pa
    r = op_signed_request(client, op_key, "POST", "/leases/baseline:world.chat/revoke", {})
    assert r.status_code == 200, r.text
    assert r.json()["revoked"] == ["baseline:world.chat"]
    r = chat(client, keys[0])
    assert r.status_code == 403, r.text
    assert "policy denied (lease)" in r.json()["detail"]
    denials = client.get("/policy/denials").json()
    match = [d for d in denials if d["capability"] == "world.chat"]
    assert match and match[0]["failed_check"] == "lease"
    assert match[0]["citizen"] == f"emerovia:{pubkey_hex(keys[0])}"


def test_deny_by_default_unmapped_mutation(pa):
    client, keys, *_ = pa
    # /policy/check with a capability no lease covers -> denied (lease).
    r = signed_request(client, keys[0], "POST", "/policy/check",
                       {"capability": "gov.vote"})
    assert r.status_code == 200
    body = r.json()
    assert body["allowed"] is False
    assert body["failed_check"] == "lease"


# ---------------------------------------------------------------- mandate layer

def test_no_mandate_denies(pa):
    client, keys, op_key, appmod, conn, _ = pa
    from server import policy as policy_engine
    citizen_id = f"emerovia:{pubkey_hex(keys[2])}"
    # Revoke the citizen's mandate directly (data-model level).
    row = policy_engine.get_active_mandate(conn, citizen_id)
    assert row is not None
    policy_engine.revoke_mandate(conn, row["id"], "world-authority")
    conn.commit()
    r = chat(client, keys[2])
    assert r.status_code == 403, r.text
    assert "policy denied (mandate)" in r.json()["detail"]


# ---------------------------------------------------------------- determinism

def test_decision_is_deterministic(pa):
    client, keys, *_ = pa
    first = signed_request(client, keys[0], "POST", "/policy/check",
                           {"capability": "world.move"}).json()
    second = signed_request(client, keys[0], "POST", "/policy/check",
                            {"capability": "world.move"}).json()
    assert first == second
    # Dry-run does not log denials.
    assert client.get("/policy/denials").json() == []


def test_check_order_identity_first(pa):
    client, keys, *_ = pa
    from server import policy as policy_engine
    import sqlite3 as _sqlite3
    # Engine-level: unknown citizen + closed namespace -> identity fails first.
    conn = _sqlite3.connect(":memory:")
    conn.row_factory = _sqlite3.Row
    conn.execute("CREATE TABLE agents(id INTEGER PRIMARY KEY, pubkey TEXT)")
    decision = policy_engine.evaluate(
        conn, "ab" * 32, policy_engine.Action(capability="tool.compute"),
        log_denials=False,
    )
    assert decision.allowed is False
    assert decision.failed_check == "identity"


# ---------------------------------------------------------------- introspection

def test_policy_authority_published(pa):
    client, *_ = pa
    r = client.get("/policy/authority")
    assert r.status_code == 200
    body = r.json()
    assert body["identity"] == "world-authority"
    assert len(body["pubkey"]) == 64
