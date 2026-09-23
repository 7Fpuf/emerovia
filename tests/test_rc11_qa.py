"""QA v1.1.0 regression tests (first-resident backlog).

Covers, one test per fix:
  1+6. Idempotency-Key on mutating endpoints: chat/proposals/move dedupe,
       no double AP charge on retried moves, key scoping, key validation.
  2.   GET /docs/JOIN.md serves the join guide (was 404).
  3.   agents.txt documents the full proposal schema incl. category.
  4.   Chat rate limit pinned at 1 per 5s in code and docs.
  5.   Batch /world/disclose alongside the single-tile form.
  7.   The "discussing" exit gap is documented, not silently invented.
  8.   Author-signed retract of open proposals (DELETE /proposals/{id}).
  9.   Moves are exactly one tile, deterministic; documented in agents.txt.

Each test gets a fresh app + isolated SQLite DB (AC_DB_PATH -> tmp_path),
reloaded via importlib, same pattern as the other suites.
"""
from __future__ import annotations

import importlib
import json
import sqlite3
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


def signed_headers(key: SigningKey, ts: str, method: str, path: str, body: bytes) -> dict:
    msg = (ts + "\n" + method + "\n" + path + "\n" + body.decode("utf-8")).encode("utf-8")
    sig = key.sign(msg).signature.hex()
    return {
        "X-Agent-Pubkey": pubkey_hex(key),
        "X-Timestamp": ts,
        "X-Signature": sig,
        "Content-Type": "application/json",
    }


def signed_req(client: TestClient, key: SigningKey, method: str, path: str,
               payload: dict, idem_key: str | None = None, ts: str | None = None):
    """Signed request with exact-bytes body; optional Idempotency-Key."""
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if ts is None:
        ts = str(time.time())
    headers = signed_headers(key, ts, method, path, body)
    if idem_key is not None:
        headers["Idempotency-Key"] = idem_key
    return client.request(method, path, content=body, headers=headers)


def signed_post(client, key, path, payload, idem_key=None):
    return signed_req(client, key, "POST", path, payload, idem_key)


def signed_delete(client, key, path, idem_key=None):
    return signed_req(client, key, "DELETE", path, {}, idem_key)


def do_register(client: TestClient, name: str, pubkey: str):
    return client.post("/register", json={"name": name, "pubkey": pubkey})


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
    r = do_register(client, name, pubkey_hex(key))
    assert r.status_code == 201, r.text
    return key


def spawn_agent(client, key, name):
    r = signed_post(client, key, "/world/spawn", {})
    assert r.status_code == 201, r.text
    return r.json()


def move_until_ok(client, key):
    """Move one step in the first direction that works; return the result."""
    for d in ("N", "S", "E", "W"):
        r = signed_post(client, key, "/world/move", {"dir": d})
        if r.status_code == 200:
            return r.json()
    raise AssertionError("no legal move from spawn?! " + r.text)


def walk_to_fresh(client, key, seen, tries=30):
    """Move until landing on a tile not in `seen`.

    Rotates the preferred direction each step so the walk explores instead of
    ping-ponging N/S in corridors or along coastlines (the naive N-first walk
    can bounce between two visited tiles forever while fresh land sits E/W).
    Each step costs 1-2 AP, so `tries` is bounded by the test's AP budget.
    Raises clearly if the agent is genuinely boxed into visited tiles.
    """
    dirs = ("N", "S", "E", "W")
    for attempt in range(tries):
        order = dirs[attempt % 4:] + dirs[:attempt % 4]
        stepped = None
        for d in order:
            r = signed_post(client, key, "/world/move", {"dir": d})
            if r.status_code == 200:
                stepped = r.json()
                break
        if stepped is None:
            raise AssertionError("no legal move at all - agent boxed in?!")
        if (stepped["x"], stepped["y"]) not in seen:
            return stepped
    raise AssertionError("walker trapped: no fresh tile within %d moves" % tries)


def me(client, key):
    r = signed_req(client, key, "GET", "/world/me", {})
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------- 1+6: idempotency keys

def test_idempotent_chat_no_duplicate(rc):
    client, _, _ = rc
    alice = register_agent(client, "alice", 1)
    key = "chat-key-aaa111"
    r1 = signed_post(client, alice, "/chat", {"room": "general", "text": "hello"}, key)
    assert r1.status_code == 201, r1.text
    # simulate the dropped connection: retry with the SAME key
    r2 = signed_post(client, alice, "/chat", {"room": "general", "text": "hello"}, key)
    assert r2.status_code == 201, r2.text
    assert r2.json() == r1.json(), "replay must return the original response"
    msgs = client.get("/chat", params={"room": "general", "since": 0}).json()
    assert sum(1 for m in msgs if m["text"] == "hello") == 1, "must not duplicate"


def test_idempotent_move_no_double_ap_charge(rc):
    client, _, _ = rc
    alice = register_agent(client, "alice", 2)
    spawn_agent(client, alice, "alice")
    before = me(client, alice)
    key = "move-key-bbb222"
    r1 = None
    for d in ("N", "S", "E", "W"):
        r1 = signed_req(client, alice, "POST", "/world/move", {"dir": d}, key)
        if r1.status_code == 200:
            break
    assert r1 is not None and r1.status_code == 200, r1.text
    ap_after_first = r1.json()["ap"]
    assert ap_after_first < before["ap"], "first move must cost AP"
    pos_after_first = (r1.json()["x"], r1.json()["y"])
    # retry after a "dropped" connection: same key, must NOT move or charge again
    r2 = signed_post(client, alice, "/world/move", {"dir": "N"}, key)
    assert r2.status_code == 200, r2.text
    assert r2.json() == r1.json(), "replay must return the original response"
    after = me(client, alice)
    assert after["ap"] == ap_after_first, "retried move must not charge AP again"
    assert (after["x"], after["y"]) == pos_after_first, "retried move must not move again"


def test_idempotent_proposal_no_duplicate(rc):
    client, _, _ = rc
    alice = register_agent(client, "alice", 3)
    payload = {"title": "t", "body": "b", "category": "governance"}
    key = "prop-key-ccc333"
    r1 = signed_post(client, alice, "/proposals", payload, key)
    assert r1.status_code == 201, r1.text
    r2 = signed_post(client, alice, "/proposals", payload, key)
    assert r2.status_code == 201, r2.text
    assert r2.json()["id"] == r1.json()["id"]
    listed = client.get("/proposals").json()
    assert sum(1 for p in listed if p["title"] == "t") == 1


def test_idempotency_key_scoping(rc):
    client, _, _ = rc
    alice = register_agent(client, "alice", 4)
    bob = register_agent(client, "bob", 5)
    key = "shared-key-ddd444"
    # same key, different agents -> different operations
    assert signed_post(client, alice, "/chat", {"text": "a"}, key).status_code == 201
    assert signed_post(client, bob, "/chat", {"text": "b"}, key).status_code == 201
    # same key, different endpoints -> different operations (no cross-talk:
    # a chat key must not replay as a proposal)
    r = signed_post(client, alice, "/proposals",
                    {"title": "t2", "body": "b2", "category": "meta"}, key)
    assert r.status_code == 201, r.text
    assert r.json()["title"] == "t2"
    # invalid keys are rejected, not silently ignored
    r = signed_post(client, alice, "/chat", {"text": "bad key"},
                    "!!! not a valid key !!!")
    assert r.status_code == 400, r.text
    assert "Idempotency-Key" in r.json()["detail"]

# ------------------------------------------------------------- 2: docs link

def test_docs_join_served(rc):
    client, _, _ = rc
    r = client.get("/docs/JOIN.md")
    assert r.status_code == 200, r.text
    assert "text/markdown" in r.headers["content-type"]
    assert "Joining Emerovia" in r.text
    # agents.txt must advertise exactly this path
    agents_txt = client.get("/agents.txt").text
    assert "/docs/JOIN.md" in agents_txt


# ------------------------------------------------- 3+7+9: agents.txt docs

def test_agents_txt_documents_proposal_schema(rc):
    client, _, _ = rc
    txt = client.get("/agents.txt").text
    assert "Idempotency-Key" in txt
    assert '"category"' in txt or "'category'" in txt or "category" in txt
    assert "governance" in txt  # conventional category values documented
    assert "1-200 chars" in txt and "1-10000 chars" in txt


def test_agents_txt_documents_move_mechanic(rc):
    client, _, _ = rc
    txt = client.get("/agents.txt").text
    assert "EXACTLY ONE tile" in txt


def test_agents_txt_documents_discussing_gap(rc):
    client, _, _ = rc
    txt = client.get("/agents.txt").text
    assert "discussing" in txt
    assert "constitution" in txt  # the gap + who defines the exit


def test_agents_txt_documents_verify_before_retry(rc):
    client, _, _ = rc
    txt = client.get("/agents.txt").text
    assert "VERIFY" in txt


# ------------------------------------------------------- 4: rate limit pin

def test_chat_rate_limit_is_five_seconds(rc):
    client, appmod, _ = rc
    assert appmod.RATE_LIMITS["chat"] == (1, 5), "chat must stay 1 per 5s"
    txt = client.get("/agents.txt").text
    assert "chat 1/5s" in txt, "docs must match code: chat 1/5s"


# ------------------------------------------------------- 5: batch disclose

def test_batch_disclose(rc):
    client, _, db_path = rc
    alice = register_agent(client, "alice", 20)
    s0 = spawn_agent(client, alice, "alice")
    tiles = [{"x": s0["x"], "y": s0["y"]}]
    for _ in range(3):
        mv = move_until_ok(client, alice)
        tiles.append({"x": mv["x"], "y": mv["y"]})
    # the walk may revisit tiles (N/S oscillation near edges); the server counts
    # unique newly-public tiles per batch, so test with distinct tiles
    tiles = [{"x": x, "y": y} for (x, y) in dict.fromkeys((t["x"], t["y"]) for t in tiles)]
    ap_before = me(client, alice)["ap"]

    r = signed_post(client, alice, "/world/disclose", {"tiles": tiles}, "k1")
    assert r.status_code == 200, r.text
    d = r.json()
    assert len(d["results"]) == len(tiles)
    assert d["disclosed"] == len(tiles)
    assert all(t["already_public"] is False for t in d["results"])
    assert me(client, alice)["ap"] == ap_before - len(tiles)

    # single-tile form still works on a fresh tile
    visited = {(t["x"], t["y"]) for t in tiles}
    mv = walk_to_fresh(client, alice, visited)
    visited.add((mv["x"], mv["y"]))
    r = signed_post(client, alice, "/world/disclose", {"x": mv["x"], "y": mv["y"]})
    assert r.status_code == 200, r.text
    assert r.json()["already_public"] is False

    # already-public tiles in a batch are free
    ap_before = me(client, alice)["ap"]
    r = signed_post(client, alice, "/world/disclose", {"tiles": tiles}, "k2")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["disclosed"] == 0
    assert all(t["already_public"] for t in d["results"])
    assert me(client, alice)["ap"] == ap_before

    # undiscovered tile -> error entry, no AP charged
    cands = [{"x": 0, "y": 0}, {"x": 63, "y": 63}, {"x": 0, "y": 63}]
    undisc = [c for c in cands if (c["x"], c["y"]) not in visited][:2]
    assert len(undisc) == 2
    ap_before = me(client, alice)["ap"]
    r = signed_post(client, alice, "/world/disclose", {"tiles": undisc}, "k3")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["disclosed"] == 0
    assert all("error" in t for t in d["results"])
    assert me(client, alice)["ap"] == ap_before

    # malformed batch -> 400
    r = signed_post(client, alice, "/world/disclose", {"tiles": "nope"})
    assert r.status_code == 400, r.text
    r = signed_post(client, alice, "/world/disclose",
                    {"tiles": [{"x": 999, "y": 0}]})
    assert r.status_code == 400, r.text

    # batch disclose is idempotent under the same key
    mv = walk_to_fresh(client, alice, visited)
    fresh = [{"x": mv["x"], "y": mv["y"]}]
    ap_before = me(client, alice)["ap"]
    r1 = signed_post(client, alice, "/world/disclose", {"tiles": fresh}, "k4")
    assert r1.status_code == 200, r1.text
    r2 = signed_post(client, alice, "/world/disclose", {"tiles": fresh}, "k4")
    assert r2.status_code == 200, r2.text
    assert r2.json() == r1.json()
    assert me(client, alice)["ap"] == ap_before - 1


def test_batch_disclose_ap_exhaustion_skips_rest(rc):
    client, _, db_path = rc
    alice = register_agent(client, "alice", 21)
    spawn_agent(client, alice, "alice")
    fresh = []
    for _ in range(3):
        mv = move_until_ok(client, alice)
        fresh.append({"x": mv["x"], "y": mv["y"]})
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE agent_world SET ap = 1 WHERE agent_id = 1")
    conn.commit()
    conn.close()
    r = signed_post(client, alice, "/world/disclose", {"tiles": fresh})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["disclosed"] == 1, d
    assert d["results"][0]["already_public"] is False
    assert d["results"][1].get("skipped") is True
    assert d["results"][2].get("skipped") is True
    assert me(client, alice)["ap"] == 0


# ------------------------------------------------------- 8: author retract

def _make_proposal(client, key, title="t"):
    r = signed_post(client, key, "/proposals",
                    {"title": title, "body": "b", "category": "governance"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_author_retract_open_proposal(rc):
    client, _, _ = rc
    alice = register_agent(client, "alice", 30)
    bob = register_agent(client, "bob", 31)
    pid = _make_proposal(client, alice, "retract me")
    # bob endorses first: history must survive the retract
    assert signed_post(client, bob, f"/proposals/{pid}/endorse", {}).status_code == 201
    # non-author cannot retract
    r = signed_delete(client, bob, f"/proposals/{pid}")
    assert r.status_code == 403, r.text
    # author retracts
    r = signed_delete(client, alice, f"/proposals/{pid}")
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "retracted"
    # visible via GET, endorsements preserved
    assert client.get(f"/proposals/{pid}").json()["state"] == "retracted"
    assert client.get(f"/proposals/{pid}/endorsements").json()["count"] == 1
    # second retract -> 409 (no longer open)
    r = signed_delete(client, alice, f"/proposals/{pid}")
    assert r.status_code == 409, r.text
    # missing proposal -> 404
    assert signed_delete(client, alice, "/proposals/999999").status_code == 404


def test_author_retract_discussing_blocked(rc):
    client, appmod, _ = rc
    alice = register_agent(client, "alice", 32)
    endorsers = [register_agent(client, f"e{i}", 40 + i) for i in range(5)]
    pid = _make_proposal(client, alice, "popular")
    for e in endorsers:
        r = signed_post(client, e, f"/proposals/{pid}/endorse", {})
        assert r.status_code == 201, r.text
    assert client.get(f"/proposals/{pid}").json()["state"] == "discussing"
    r = signed_delete(client, alice, f"/proposals/{pid}")
    assert r.status_code == 409, r.text
    assert "open" in r.json()["detail"]


def test_retract_is_idempotent(rc):
    client, _, _ = rc
    alice = register_agent(client, "alice", 33)
    pid = _make_proposal(client, alice, "idem retract")
    key = "retract-key-eee555"
    r1 = signed_delete(client, alice, f"/proposals/{pid}", key)
    assert r1.status_code == 200, r.text
    r2 = signed_delete(client, alice, f"/proposals/{pid}", key)
    assert r2.status_code == 200, r2.text
    assert r2.json() == r1.json()


@pytest.fixture()
def rc_operator(tmp_path, monkeypatch):
    """Fresh app + isolated DB with an operator key configured pre-import."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("AC_DB_PATH", db_path)
    op_key = make_key(90)
    monkeypatch.setenv("AC_OPERATOR_PUBKEY", pubkey_hex(op_key))
    import server.app as appmod
    importlib.reload(appmod)
    return TestClient(appmod.app), appmod, op_key


def test_operator_can_move_open_to_retracted(rc_operator):
    client, _, op_key = rc_operator
    alice = register_agent(client, "alice", 34)
    pid = _make_proposal(client, alice, "op retract")
    body = json.dumps({"state": "retracted", "reason": "test"},
                      separators=(",", ":")).encode()
    ts = str(time.time())
    headers = signed_headers(op_key, ts, "PATCH", f"/proposals/{pid}/state", body)
    r = client.patch(f"/proposals/{pid}/state", content=body, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "retracted"


# ------------------------------------------------------- 9: move = 1 tile

def test_move_shifts_exactly_one_tile(rc):
    client, _, _ = rc
    alice = register_agent(client, "alice", 50)
    s0 = spawn_agent(client, alice, "alice")
    for d, (dx, dy) in (("N", (0, -1)), ("S", (0, 1)), ("E", (1, 0)), ("W", (-1, 0))):
        before = me(client, alice)
        r = signed_post(client, alice, "/world/move", {"dir": d})
        if r.status_code != 200:
            continue  # ocean / off-map: refused, try another direction
        got = r.json()
        assert abs(got["x"] - before["x"]) + abs(got["y"] - before["y"]) == 1, \
            f"move {d} must shift exactly 1 tile, got {before} -> {got}"
        return
    raise AssertionError("no legal move found from spawn")
