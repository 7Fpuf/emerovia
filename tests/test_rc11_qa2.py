"""QA v1.1.0 regression tests, round 2 (Tern's report).

Item 10: agents.txt must document BOTH /world/disclose param forms
  (single-tile {"x","y"} AND batch {"tiles":[...]}, up to 64) — round 1
  documented the batch form; this round fills the remaining gaps.
Item 11: GET /world/me returns a LIFETIME private_discoveries counter.
  A new additive field `undisclosed_tiles` counts discovered-but-not-yet-
  public tiles; the old field is kept untouched (no breaking change).

Fresh app + isolated SQLite DB per test (AC_DB_PATH -> tmp_path),
reloaded via importlib — same pattern as tests/test_rc11_qa.py.
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
    seed = bytes((seed_byte + i) % 256 for i in range(32))
    return SigningKey(seed)


def pubkey_hex(key: SigningKey) -> str:
    return key.verify_key.encode().hex()


def signed_req(client: TestClient, key: SigningKey, method: str, path: str,
               payload: dict, idem_key: str | None = None, ts: str | None = None):
    """Signed request with exact-bytes body; optional Idempotency-Key."""
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if ts is None:
        ts = str(time.time())
    msg = (ts + "\n" + method + "\n" + path + "\n" + body.decode("utf-8")).encode("utf-8")
    sig = key.sign(msg).signature.hex()
    headers = {
        "X-Agent-Pubkey": pubkey_hex(key),
        "X-Timestamp": ts,
        "X-Signature": sig,
        "Content-Type": "application/json",
    }
    if idem_key is not None:
        headers["Idempotency-Key"] = idem_key
    return client.request(method, path, content=body, headers=headers)


def signed_post(client, key, path, payload, idem_key=None):
    return signed_req(client, key, "POST", path, payload, idem_key)


@pytest.fixture()
def rc(tmp_path, monkeypatch):
    """Fresh app + isolated DB. Returns (client, appmod, db_path)."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("AC_DB_PATH", db_path)
    import server.app as appmod
    importlib.reload(appmod)
    return TestClient(appmod.app), appmod, db_path


def register_agent(client, name, seed_byte):
    key = make_key(seed_byte)
    r = client.post("/register", json={"name": name, "pubkey": pubkey_hex(key)})
    assert r.status_code == 201, r.text
    return key


def me(client, key):
    r = signed_req(client, key, "GET", "/world/me", {})
    assert r.status_code == 200, r.text
    return r.json()


def move_until_ok(client, key):
    """Move one step in the first direction that works; return the result."""
    for d in ("N", "S", "E", "W"):
        r = signed_post(client, key, "/world/move", {"dir": d})
        if r.status_code == 200:
            return r.json()
    raise AssertionError("no legal move from spawn?! " + r.text)


def collect_tiles(client, key, n):
    """Return n distinct discovered tile dicts via spawn + moves.

    Straight-line walker: keeps the last successful direction (preference
    persists across steps), turns when blocked. The old naive N-first
    walker could oscillate N/S forever on a coastline and never find a
    third distinct tile.
    """
    r = signed_post(client, key, "/world/spawn", {})
    assert r.status_code == 201, r.text
    seen = {(r.json()["x"], r.json()["y"])}
    order = ["N", "S", "E", "W"]
    pref = 0
    for _ in range(60):
        if len(seen) >= n:
            break
        moved = False
        for k in range(4):
            d = order[(pref + k) % 4]
            rr = signed_post(client, key, "/world/move", {"dir": d})
            if rr.status_code == 200:
                pref = (pref + k) % 4
                seen.add((rr.json()["x"], rr.json()["y"]))
                moved = True
                break
        if not moved:
            raise AssertionError("agent trapped, no legal move")
    assert len(seen) >= n, f"only discovered {len(seen)} tiles in 60 moves"
    return [{"x": x, "y": y} for x, y in sorted(seen)][:n]


def agents_txt(rc):
    client, appmod, _ = rc
    r = client.get("/agents.txt")
    assert r.status_code == 200, r.text
    return r.text


# ------------------------------------------------- item 10: disclose docs

def test_agents_txt_documents_disclose_params(rc):
    txt = agents_txt(rc)
    # single-tile form: {"x","y"}
    assert '{"x":N,"y":N}' in txt
    assert '{"x","y"}' in txt  # response / element shape
    # batch form: {"tiles": [...]} with the 1-64 cap
    assert '{"tiles":[{"x":N,"y":N}, ...]}' in txt
    assert "64" in txt


# ----------------------------------------- item 11: undisclosed_tiles field

def test_undisclosed_tiles_counter(rc):
    client, _, _ = rc
    alice = register_agent(client, "alice", 40)
    tiles = collect_tiles(client, alice, 3)

    m = me(client, alice)
    assert m["private_discoveries"] == 3
    assert m["undisclosed_tiles"] == 3  # new additive field; none disclosed yet

    # disclose exactly 1 of the 3
    r = signed_post(client, alice, "/world/disclose",
                    {"x": tiles[0]["x"], "y": tiles[0]["y"]})
    assert r.status_code == 200, r.text

    m = me(client, alice)
    assert m["private_discoveries"] == 3  # lifetime total: UNCHANGED
    assert m["undisclosed_tiles"] == 2    # discovered-but-not-public: -1


def test_undisclosed_tiles_batch_disclose(rc):
    client, _, _ = rc
    alice = register_agent(client, "alice", 41)
    tiles = collect_tiles(client, alice, 3)

    # batch disclose 2 of the 3
    r = signed_post(client, alice, "/world/disclose",
                    {"tiles": tiles[:2]}, "bkey")
    assert r.status_code == 200, r.text
    assert r.json()["disclosed"] == 2

    m = me(client, alice)
    assert m["private_discoveries"] == 3
    assert m["undisclosed_tiles"] == 1


def test_agents_txt_clarifies_private_discoveries(rc):
    txt = agents_txt(rc)
    assert "private_discoveries" in txt
    assert "LIFETIME" in txt  # documented as a lifetime total, not a remainder
    assert "undisclosed_tiles" in txt
