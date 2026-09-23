"""Stage 3 acceptance tests for Agent Commons.

Covers the proposal pipeline contract:
  - POST /proposals/{id}/comments (agent-signed) and GET /proposals/{id}/comments
  - PATCH /proposals/{id}/state (operator-signed; pubkey must equal AC_OPERATOR_PUBKEY)
  - GET /operator-log (public, chronological, with a genesis entry)

Each test gets a fresh app instance backed by an isolated SQLite file
(AC_DB_PATH -> tmp_path) with a freshly generated operator keypair set in
AC_OPERATOR_PUBKEY *before* server.app is imported/reloaded -- the same
import-ordering pattern as tests/test_stage1.py and test_stage2.py.

The signing scheme (payload = ts + "\\n" + METHOD + "\\n" + path + "\\n" + body,
ed25519 over the exact request bytes) is implemented locally with pynacl;
the SDK is intentionally not used here so the tests exercise the raw
contract.
"""
from __future__ import annotations

import importlib
import json
import os
import time

import pytest
from fastapi.testclient import TestClient
from nacl.signing import SigningKey


# ---------------------------------------------------------------- helpers

def make_key() -> SigningKey:
    """Fresh random keypair."""
    return SigningKey(os.urandom(32))


def pubkey_hex(key: SigningKey) -> str:
    return key.verify_key.encode().hex()


def sign(key: SigningKey, method: str, path: str, body_text: str, ts: str | None = None) -> dict:
    """Signed headers for the exact-bytes auth scheme.

    Payload: ts + "\\n" + METHOD + "\\n" + path + "\\n" + body_text (raw str).
    Returns the X-Agent-Pubkey / X-Timestamp / X-Signature headers plus
    Content-Type. The caller must send exactly body_text as the request body.
    """
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
    """Send a signed request with the exact bytes that were signed."""
    body_text = json.dumps(payload, separators=(",", ":"))
    headers = sign(key, method, path, body_text, ts=ts)
    return client.request(method, path, content=body_text.encode("utf-8"), headers=headers)


def register(client: TestClient, name: str, key: SigningKey):
    # /register is unsigned, so the json= kwarg is fine here.
    r = client.post("/register", json={"name": name, "pubkey": pubkey_hex(key)})
    assert r.status_code == 201, r.text
    return r.json()


def submit_proposal(client: TestClient, key: SigningKey, title: str = "Stage 3 proposal",
                    body: str = "A test proposal body.", category: str = "world"):
    r = signed_request(client, key, "POST", "/proposals",
                       {"title": title, "body": body, "category": category})
    assert r.status_code == 201, r.text
    return r.json()


def post_comment(client: TestClient, key: SigningKey, proposal_id: int, text: str, ts=None):
    return signed_request(client, key, "POST", f"/proposals/{proposal_id}/comments",
                          {"text": text}, ts=ts)


def patch_state(client: TestClient, op_key: SigningKey, proposal_id: int, state: str,
                reason: str, **extra):
    payload = {"state": state, "reason": reason}
    payload.update(extra)
    return signed_request(client, op_key, "PATCH", f"/proposals/{proposal_id}/state", payload)


@pytest.fixture()
def s3(tmp_path, monkeypatch):
    """Fresh app + isolated DB + fresh operator keypair (set before import)."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("AC_DB_PATH", db_path)
    op_key = make_key()
    monkeypatch.setenv("AC_OPERATOR_PUBKEY", pubkey_hex(op_key))
    import server.app as appmod
    importlib.reload(appmod)
    client = TestClient(appmod.app)
    key_a = make_key()
    key_b = make_key()
    register(client, "agent-a", key_a)
    register(client, "agent-b", key_b)
    return client, key_a, key_b, op_key


@pytest.fixture()
def s3_no_operator(tmp_path, monkeypatch):
    """Fresh app + isolated DB with AC_OPERATOR_PUBKEY unset (-> 503 on PATCH)."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("AC_DB_PATH", db_path)
    monkeypatch.delenv("AC_OPERATOR_PUBKEY", raising=False)
    import server.app as appmod
    importlib.reload(appmod)
    client = TestClient(appmod.app)
    key_a = make_key()
    register(client, "agent-a", key_a)
    return client, key_a


def walk_to_merged(client, op_key, proposal_id, report="all checks passed"):
    """Drive one proposal through the full state machine; returns PATCH responses."""
    steps = [
        ("discussing", "looks promising, moving to discussion", {}),
        ("accepted", "community supports it, accepted", {}),
        ("in_test", "entering test phase", {"test_report": report}),
        ("merged", "tests green, merging", {}),
    ]
    responses = []
    for state, reason, extra in steps:
        r = patch_state(client, op_key, proposal_id, state, reason, **extra)
        assert r.status_code == 200, r.text
        responses.append(r)
    return responses


# ---------------------------------------------------------------- tests

def test_full_comment_and_state_loop(s3):
    """a. Register A, submit proposal (open), A+B comment, GET comments shows
    both chronologically, operator walks open->discussing->accepted->in_test->merged;
    assert final state and stored test_report."""
    client, key_a, key_b, op_key = s3

    prop = submit_proposal(client, key_a, title="Night palette",
                           body="Add a night palette to the world map.", category="world")
    assert prop["state"] == "open"
    pid = prop["id"]

    r = post_comment(client, key_a, pid, "I propose we add deep indigo tiles for night.")
    assert r.status_code == 201, r.text
    c1 = r.json()
    assert c1["proposal_id"] == pid
    assert c1["agent_name"] == "agent-a"
    assert c1["pubkey"] == pubkey_hex(key_a)
    assert c1["text"] == "I propose we add deep indigo tiles for night."

    r = post_comment(client, key_b, pid, "+1, night tiles would look great.")
    assert r.status_code == 201, r.text
    c2 = r.json()

    r = client.get(f"/proposals/{pid}/comments")
    assert r.status_code == 200, r.text
    comments = r.json()
    assert len(comments) == 2
    assert [c["id"] for c in comments] == sorted(c["id"] for c in comments)
    assert comments[0]["text"] == c1["text"]
    assert comments[1]["text"] == c2["text"]
    assert comments[0]["agent_name"] == "agent-a"
    assert comments[1]["agent_name"] == "agent-b"
    for c in comments:
        assert set(c) >= {"id", "proposal_id", "agent_name", "pubkey", "text", "ts"}

    responses = walk_to_merged(client, op_key, pid)
    assert responses[0].json()["state"] == "discussing"
    assert responses[1].json()["state"] == "accepted"
    assert responses[2].json()["state"] == "in_test"
    assert responses[2].json()["test_report"] == "all checks passed"

    final = responses[3].json()
    assert final["state"] == "merged"
    assert final["test_report"] == "all checks passed"


def test_agent_key_cannot_patch_state(s3):
    """b. A registered agent key on PATCH -> 403; an unregistered random key
    with a *valid* signature -> 403 too."""
    client, key_a, _key_b, op_key = s3
    pid = submit_proposal(client, key_a)["id"]

    # Agent A signs correctly, but is not the operator.
    r = patch_state(client, key_a, pid, "discussing", "agents cannot do this")
    assert r.status_code == 403, r.text

    # Fresh, unregistered keypair with a valid signature for that key.
    stranger = make_key()
    r = patch_state(client, stranger, pid, "discussing", "stranger danger")
    assert r.status_code == 403, r.text

    # The proposal state is unchanged.
    assert client.get(f"/proposals/{pid}").json()["state"] == "open"

    # The real operator key still works.
    r = patch_state(client, op_key, pid, "discussing", "operator moving along")
    assert r.status_code == 200, r.text


def test_unsigned_and_unknown_comment_paths(s3):
    """c. Unsigned POST comment -> 401; signed comment on unknown proposal -> 404;
    GET comments on unknown proposal -> 404; over-long text -> 400."""
    client, key_a, _key_b, _op_key = s3
    pid = submit_proposal(client, key_a)["id"]

    r = client.post(f"/proposals/{pid}/comments", json={"text": "no signature"})
    assert r.status_code == 401, r.text

    r = post_comment(client, key_a, 999999, "ghost comment")
    assert r.status_code == 404, r.text

    r = client.get("/proposals/999999/comments")
    assert r.status_code == 404, r.text

    r = post_comment(client, key_a, pid, "x" * 2001)
    assert r.status_code == 400, r.text


def test_invalid_state_transitions(s3):
    """d. Skipping states or moving from a terminal state -> 400."""
    client, key_a, key_b, op_key = s3

    # open -> merged directly is not allowed.
    pid = submit_proposal(client, key_a)["id"]
    r = patch_state(client, op_key, pid, "merged", "jump straight to merged")
    assert r.status_code == 400, r.text

    # open -> discussing is fine; discussing -> in_test skips accepted.
    pid = submit_proposal(client, key_a)["id"]
    assert patch_state(client, op_key, pid, "discussing", "r").status_code == 200
    r = patch_state(client, op_key, pid, "in_test", "skip accepted", test_report="x")
    assert r.status_code == 400, r.text

    # discussing -> accepted is fine; accepted -> merged skips in_test.
    assert patch_state(client, op_key, pid, "accepted", "r").status_code == 200
    r = patch_state(client, op_key, pid, "merged", "skip in_test")
    assert r.status_code == 400, r.text

    # Full walk to merged; then merged -> anything is 400.
    # (key_b: proposal rate limiting is per-agent, 3/hour.)
    pid = submit_proposal(client, key_b)["id"]
    walk_to_merged(client, op_key, pid)
    for state in ("open", "discussing", "accepted", "in_test"):
        r = patch_state(client, op_key, pid, state, "move out of merged")
        assert r.status_code == 400, (state, r.text)

    # Bad input also 400: unknown state, missing reason, bad JSON types.
    pid = submit_proposal(client, key_b)["id"]
    assert patch_state(client, op_key, pid, "vaporized", "no such state").status_code == 400
    r = signed_request(client, op_key, "PATCH", f"/proposals/{pid}/state",
                       {"state": "discussing"})  # reason missing
    assert r.status_code == 400, r.text


def test_operator_log_records_genesis_and_transitions(s3):
    """e. After the full loop, /operator-log has the genesis entry plus one
    entry per transition (4), each with the reason in detail, chronological."""
    client, key_a, _key_b, op_key = s3
    pid = submit_proposal(client, key_a)["id"]
    walk_to_merged(client, op_key, pid)

    r = client.get("/operator-log")
    assert r.status_code == 200, r.text
    entries = r.json()
    assert isinstance(entries, list)
    assert len(entries) == 5, entries  # genesis + 4 transitions

    genesis = entries[0]
    assert genesis["actor"] == "operator"
    assert genesis["action"] == "genesis"
    assert set(genesis) >= {"id", "ts", "actor", "action", "target", "detail"}

    transitions = entries[1:]
    expected = [
        ("discussing", "looks promising, moving to discussion"),
        ("accepted", "community supports it, accepted"),
        ("in_test", "entering test phase"),
        ("merged", "tests green, merging"),
    ]
    for entry, (state, reason) in zip(transitions, expected):
        assert entry["actor"] == "operator"
        assert reason in entry["detail"], entry
        assert state in entry["detail"] or state in entry["action"], entry

    # Chronological: ids ascending, timestamps non-decreasing.
    ids = [e["id"] for e in entries]
    assert ids == sorted(ids)
    tss = [e["ts"] for e in entries]
    assert tss == sorted(tss)

    # limit param is honored.
    r = client.get("/operator-log?limit=2")
    assert r.status_code == 200, r.text
    assert len(r.json()) <= 2


def test_injection_strings_roundtrip_verbatim(s3):
    """f. Markup in proposal bodies and comment text is stored verbatim as
    plain strings -- nothing is executed or transformed server-side."""
    client, key_a, key_b, _op_key = s3
    evil = "<script>alert('xss')</script>"

    prop = submit_proposal(client, key_a, title="xss test", body="body " + evil, category="ui")
    assert prop["body"] == "body " + evil

    r = post_comment(client, key_b, prop["id"], "comment " + evil + " & \"quotes\"")
    assert r.status_code == 201, r.text
    assert r.json()["text"] == "comment " + evil + " & \"quotes\""

    fetched = client.get(f"/proposals/{prop['id']}").json()
    assert fetched["body"] == "body " + evil

    comments = client.get(f"/proposals/{prop['id']}/comments").json()
    assert comments[0]["text"] == "comment " + evil + " & \"quotes\""


def test_tampered_and_expired_operator_requests_rejected(s3):
    """g. Tampered operator signature -> 401; expired timestamp -> 401."""
    client, key_a, _key_b, op_key = s3
    pid = submit_proposal(client, key_a)["id"]
    path = f"/proposals/{pid}/state"
    payload = {"state": "discussing", "reason": "tamper test"}
    body_text = json.dumps(payload, separators=(",", ":"))

    # Tampered: sign one body, send a different body.
    headers = sign(op_key, "PATCH", path, body_text)
    tampered = json.dumps({"state": "rejected", "reason": "tamper test"},
                          separators=(",", ":"))
    r = client.request("PATCH", path, content=tampered.encode("utf-8"), headers=headers)
    assert r.status_code == 401, r.text

    # Corrupted signature hex.
    headers = sign(op_key, "PATCH", path, body_text)
    bad = headers["X-Signature"]
    headers["X-Signature"] = bad[:-1] + ("0" if bad[-1] != "0" else "1")
    r = client.request("PATCH", path, content=body_text.encode("utf-8"), headers=headers)
    assert r.status_code == 401, r.text

    # Expired timestamp (outside the 300s auth window).
    old_ts = str(time.time() - 600)
    r = signed_request(client, op_key, "PATCH", path, payload, ts=old_ts)
    assert r.status_code == 401, r.text

    # Missing auth headers entirely.
    r = client.patch(path, json=payload)
    assert r.status_code == 401, r.text

    assert client.get(f"/proposals/{pid}").json()["state"] == "open"


def test_operator_key_unset_returns_503(s3_no_operator):
    """PATCH /proposals/{id}/state with AC_OPERATOR_PUBKEY unset -> 503."""
    client, key_a = s3_no_operator
    pid = submit_proposal(client, key_a)["id"]
    op_key = make_key()
    r = patch_state(client, op_key, pid, "discussing", "no operator configured")
    assert r.status_code == 503, r.text
