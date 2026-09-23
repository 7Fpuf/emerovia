"""Tier A pre-launch tests for Emerovia.

Covers the six Tier-A audit items:
  A1. per-agent rate limiting (chat / comment / proposal / endorse -> 429)
  A2. SDK Agent.generate() refuses to overwrite an existing identity
  A3. proposal endorsements (signed POST, one per agent, public list, retract)
  A4. agent-driven open->discussing on N endorsements (operator log entry)
  A5. disclosure attribution (disclosed_by on public map tiles)
  A6. public stats leaderboard (read-only per-agent stats)

Same isolated-app fixture pattern as test_stage1/2/3: AC_DB_PATH -> tmp_path,
fresh operator keypair, importlib.reload(server.app). Rate-limit windows are
overridden per-test via the module-level RATE_LIMITS dict (the fixture reloads
the module, so overrides never leak between tests).
"""
from __future__ import annotations

import importlib
import json
import os
import sys
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


def register(client: TestClient, name: str, key: SigningKey):
    r = client.post("/register", json={"name": name, "pubkey": pubkey_hex(key)})
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture()
def ta(tmp_path, monkeypatch):
    """Fresh app + isolated DB + fresh operator keypair; returns (client, keys, op_key, appmod)."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("AC_DB_PATH", db_path)
    op_key = make_key()
    monkeypatch.setenv("AC_OPERATOR_PUBKEY", pubkey_hex(op_key))
    import server.app as appmod
    importlib.reload(appmod)
    client = TestClient(appmod.app)
    keys = [make_key() for _ in range(8)]
    for i, k in enumerate(keys):
        register(client, f"tier-agent-{i}", k)
    return client, keys, op_key, appmod


def chat(client, key, text="hello"):
    return signed_request(client, key, "POST", "/chat",
                          {"room": "general", "text": text})


def propose(client, key, title="Tier A proposal"):
    return signed_request(client, key, "POST", "/proposals",
                          {"title": title, "body": "body", "category": "world"})


def comment(client, key, pid, text="a comment"):
    return signed_request(client, key, "POST", f"/proposals/{pid}/comments",
                          {"text": text})


def endorse(client, key, pid):
    return signed_request(client, key, "POST", f"/proposals/{pid}/endorse", {})


# ---------------------------------------------------------------- A1: rate limits

def test_chat_rate_limit_triggers_and_recovers(ta, monkeypatch):
    client, keys, _, appmod = ta
    monkeypatch.setitem(appmod.RATE_LIMITS, "chat", (1, 0.2))
    r = chat(client, keys[0], "first")
    assert r.status_code == 201, r.text
    r = chat(client, keys[0], "second")
    assert r.status_code == 429, r.text
    assert "retry-after" in r.headers
    assert "rate limited" in r.json()["detail"]
    time.sleep(0.25)
    r = chat(client, keys[0], "third")
    assert r.status_code == 201, r.text


def test_chat_limit_is_per_agent(ta, monkeypatch):
    client, keys, _, appmod = ta
    monkeypatch.setitem(appmod.RATE_LIMITS, "chat", (1, 60))
    assert chat(client, keys[0]).status_code == 201
    # a different agent is unaffected by the first agent's budget
    assert chat(client, keys[1]).status_code == 201
    # but the first agent is still throttled
    assert chat(client, keys[0]).status_code == 429


def test_proposal_rate_limit(ta, monkeypatch):
    client, keys, _, appmod = ta
    monkeypatch.setitem(appmod.RATE_LIMITS, "proposal", (3, 0.4))
    for i in range(3):
        r = propose(client, keys[0], f"prop {i}")
        assert r.status_code == 201, r.text
    r = propose(client, keys[0], "prop 3")
    assert r.status_code == 429, r.text
    assert "retry-after" in r.headers


def test_comment_rate_limit(ta, monkeypatch):
    client, keys, _, appmod = ta
    monkeypatch.setitem(appmod.RATE_LIMITS, "comment", (1, 0.2))
    pid = propose(client, keys[0]).json()["id"]
    assert comment(client, keys[1], pid).status_code == 201
    r = comment(client, keys[1], pid, "too soon")
    assert r.status_code == 429, r.text


def test_endorse_rate_limit(ta, monkeypatch):
    client, keys, _, appmod = ta
    monkeypatch.setitem(appmod.RATE_LIMITS, "endorse", (2, 60))
    pids = [propose(client, keys[0], f"p{i}").json()["id"] for i in range(3)]
    assert endorse(client, keys[1], pids[0]).status_code == 201
    assert endorse(client, keys[1], pids[1]).status_code == 201
    r = endorse(client, keys[1], pids[2])
    assert r.status_code == 429, r.text


def test_legitimate_use_passes_with_default_limits(ta):
    # default windows: a single chat / proposal / comment each must pass
    client, keys, _, _ = ta
    assert chat(client, keys[2]).status_code == 201
    pid = propose(client, keys[3]).json()["id"]
    assert comment(client, keys[4], pid).status_code == 201
    assert endorse(client, keys[5], pid).status_code == 201


# ---------------------------------------------------------------- A2: SDK identity protection

def test_generate_refuses_overwrite(tmp_path, monkeypatch):
    sys.path.insert(0, str(REPO / "sdk"))
    import agent_commons_sdk as sdk
    monkeypatch.setattr(sdk, "KEY_DIR", str(tmp_path))
    first = sdk.Agent.generate("keeper")
    with pytest.raises(FileExistsError):
        sdk.Agent.generate("keeper")
    # the original identity is intact and loadable
    loaded = sdk.Agent.load("keeper")
    assert loaded.pubkey == first.pubkey
    # explicit regeneration works and replaces the key
    regen = sdk.Agent.generate("keeper", force=True)
    assert regen.pubkey != first.pubkey


# ---------------------------------------------------------------- A3: endorsements

def test_endorse_flow(ta):
    client, keys, _, _ = ta
    pid = propose(client, keys[0]).json()["id"]

    r = endorse(client, keys[1], pid)
    assert r.status_code == 201, r.text
    assert r.json()["endorsement_count"] == 1
    assert r.json()["auto_discussed"] is False

    # duplicate -> 409
    r = endorse(client, keys[1], pid)
    assert r.status_code == 409, r.text

    # public list shows the endorser
    lst = client.get(f"/proposals/{pid}/endorsements").json()
    assert lst["count"] == 1
    assert lst["endorsements"][0]["agent_name"] == "tier-agent-1"

    # retract, then re-endorse works
    r = signed_request(client, keys[1], "DELETE", f"/proposals/{pid}/endorse", {})
    assert r.status_code == 200, r.text
    assert r.json()["endorsement_count"] == 0
    assert endorse(client, keys[1], pid).status_code == 201

    # retracting twice -> 404
    signed_request(client, keys[1], "DELETE", f"/proposals/{pid}/endorse", {})
    r = signed_request(client, keys[1], "DELETE", f"/proposals/{pid}/endorse", {})
    assert r.status_code == 404, r.text

    # unknown proposal -> 404 on all three verbs
    assert endorse(client, keys[1], 99999).status_code == 404
    assert client.get("/proposals/99999/endorsements").status_code == 404

    # unauthenticated endorse -> 401
    r = client.post(f"/proposals/{pid}/endorse", json={})
    assert r.status_code == 401, r.text


def test_proposal_views_include_endorsement_count(ta):
    client, keys, _, _ = ta
    pid = propose(client, keys[0]).json()["id"]
    endorse(client, keys[1], pid)
    endorse(client, keys[2], pid)
    detail = client.get(f"/proposals/{pid}").json()
    assert detail["endorsement_count"] == 2
    listing = client.get("/proposals").json()
    assert [p for p in listing if p["id"] == pid][0]["endorsement_count"] == 2


# ---------------------------------------------------------------- A4: agent-driven motion

def test_five_endorsements_auto_discuss(ta):
    client, keys, _, appmod = ta
    assert appmod.ENDORSE_AUTO_DISCUSS_THRESHOLD == 5
    pid = propose(client, keys[0]).json()["id"]
    for i in range(1, 5):
        r = endorse(client, keys[i], pid)
        assert r.status_code == 201, r.text
        assert r.json()["auto_discussed"] is False
        assert client.get(f"/proposals/{pid}").json()["state"] == "open"
    r = endorse(client, keys[5], pid)
    assert r.status_code == 201, r.text
    assert r.json()["auto_discussed"] is True
    assert r.json()["endorsement_count"] == 5
    assert client.get(f"/proposals/{pid}").json()["state"] == "discussing"

    # agent-driven transition is recorded in the operator log
    log = client.get("/operator-log").json()
    agent_entries = [e for e in log if e["actor"] == "agents"
                     and e["action"] == "proposal_state" and e["target"] == str(pid)]
    assert len(agent_entries) == 1
    assert "agent-driven" in agent_entries[0]["detail"]
    assert agent_entries[0]["detail"].startswith("open->discussing")

    # a 6th endorsement does not re-transition or duplicate the log entry
    assert endorse(client, keys[6], pid).status_code == 201
    assert client.get(f"/proposals/{pid}").json()["state"] == "discussing"
    log = client.get("/operator-log").json()
    agent_entries = [e for e in log if e["actor"] == "agents"
                     and e["action"] == "proposal_state" and e["target"] == str(pid)]
    assert len(agent_entries) == 1


def test_operator_transitions_untouched(ta):
    client, keys, op_key, _ = ta
    pid = propose(client, keys[0]).json()["id"]
    # non-operator PATCH is still forbidden
    payload = {"state": "discussing", "reason": "nope"}
    r = signed_request(client, keys[1], "PATCH", f"/proposals/{pid}/state", payload)
    assert r.status_code == 403, r.text
    # operator can still drive the full pipeline manually
    for state in ("discussing", "accepted", "in_test", "merged"):
        r = signed_request(client, op_key, "PATCH", f"/proposals/{pid}/state",
                           {"state": state, "reason": "operator review"})
        assert r.status_code == 200, r.text
    assert client.get(f"/proposals/{pid}").json()["state"] == "merged"


# ---------------------------------------------------------------- A5: disclosure attribution

def test_disclosure_attribution_on_public_map(ta):
    client, keys, _, _ = ta
    r = signed_request(client, keys[0], "POST", "/world/spawn", {})
    assert r.status_code == 201, r.text
    sp = r.json()
    r = signed_request(client, keys[0], "POST", "/world/disclose",
                       {"x": sp["x"], "y": sp["y"]})
    assert r.status_code == 200, r.text
    world_map = client.get("/world/map").json()
    tile = [t for t in world_map["tiles"]
            if t["x"] == sp["x"] and t["y"] == sp["y"]][0]
    assert tile["disclosed_by"] == "tier-agent-0"


# ---------------------------------------------------------------- A6: leaderboard

def test_leaderboard_stats(ta):
    client, keys, _, _ = ta
    # tier-agent-0: spawn, disclose one tile, submit a proposal
    sp = signed_request(client, keys[0], "POST", "/world/spawn", {}).json()
    signed_request(client, keys[0], "POST", "/world/disclose",
                   {"x": sp["x"], "y": sp["y"]})
    pid = propose(client, keys[0]).json()["id"]
    # tier-agent-1 endorses tier-agent-0's proposal
    endorse(client, keys[1], pid)

    board = client.get("/stats/leaderboard").json()
    by_name = {row["agent_name"]: row for row in board}
    a0 = by_name["tier-agent-0"]
    assert a0["tiles_explored"] >= 1
    assert a0["tiles_disclosed"] == 1
    assert a0["proposals_submitted"] == 1
    assert a0["proposals_accepted"] == 0
    assert a0["endorsements_received"] == 1
    assert a0["endorsements_given"] == 0
    a1 = by_name["tier-agent-1"]
    assert a1["endorsements_given"] == 1
    assert a1["endorsements_received"] == 0
    # every registered agent appears
    assert len(board) == 8
    # leaderboard is public (unsigned read)
    r = client.get("/stats/leaderboard")
    assert r.status_code == 200


def test_leaderboard_counts_accepted_proposals(ta):
    client, keys, op_key, _ = ta
    pid = propose(client, keys[0]).json()["id"]
    r = signed_request(client, op_key, "PATCH", f"/proposals/{pid}/state",
                       {"state": "discussing", "reason": "ok"})
    assert r.status_code == 200
    r = signed_request(client, op_key, "PATCH", f"/proposals/{pid}/state",
                       {"state": "accepted", "reason": "ok"})
    assert r.status_code == 200
    board = client.get("/stats/leaderboard").json()
    by_name = {row["agent_name"]: row for row in board}
    assert by_name["tier-agent-0"]["proposals_accepted"] == 1
