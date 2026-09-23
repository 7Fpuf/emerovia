"""Bible comms pod — S9 proximity voice, S10 relay network, S11 herald
recruitment, spawn anti-isolation, and the aether (legacy global /chat).

Covers, against Systems Bible §11 constants:
- whisper: same tile only (radius 0), free
- talk: Chebyshev radius 3, free
- shout: radius 9 (18 with a far_speaker discovery tool), 4 AP
- rate limits: voice 1/2s, shout 1/30s, relay 1/300s (exact §11)
- Idempotency-Key on every mutating voice endpoint, replay-before-rate-limit
- voice retention VOICE_RETENTION_SECONDS=604800 (lazy prune on send)
- delivery scoping from the sender's tile AT SEND TIME; readers query with
  their own (server-side) position
- relay: catch 3, hop 15, max 10 towers/send, 3+1/tower AP, tower_path
  recorded and observer-visible; derelict towers don't carry signal
- herald recruitment: optional referred_by (name or pubkey) on register;
  inviter credit recorded; credit vests only on 25 disclosed tiles +
  10 messages + 2 active days of genuine activity (feast buffs never count);
  lazy vesting evaluation on leaderboard read (cached flag, no sweep);
  HERALD_VESTED_REQUIRED=3
- spawn anti-isolation: SPAWN_NEAR_RADIUS=20
- the aether: legacy global /chat stays functional alongside voice; the
  Bible names no quiet-trigger, so none is built (flagged, not invented)
"""
from __future__ import annotations

import importlib
import json
import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from nacl.signing import SigningKey

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


# ---------------------------------------------------------------- helpers

def make_key() -> SigningKey:
    return SigningKey(os.urandom(32))


def pubkey_hex(key: SigningKey) -> str:
    return key.verify_key.encode().hex()


def sign(key: SigningKey, method: str, path: str, body_text: str,
         ts: str | None = None) -> dict:
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
                   payload: dict, idem_key: str | None = None, ts: str | None = None):
    body_text = json.dumps(payload, separators=(",", ":"))
    headers = sign(key, method, path, body_text, ts=ts)
    if idem_key:
        headers["Idempotency-Key"] = idem_key
    return client.request(method, path, content=body_text.encode("utf-8"),
                          headers=headers)


def register(client: TestClient, name: str, key: SigningKey, **extra):
    payload = {"name": name, "pubkey": pubkey_hex(key)}
    payload.update(extra)
    r = client.post("/register", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def spawn(client: TestClient, key: SigningKey):
    r = signed_request(client, key, "POST", "/world/spawn", {})
    assert r.status_code == 201, r.text
    return r.json()


def fresh_key() -> str:
    return uuid.uuid4().hex


@pytest.fixture()
def comms(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("AC_DB_PATH", db_path)
    monkeypatch.setenv("AC_OPERATOR_PUBKEY", pubkey_hex(make_key()))
    import server.app as appmod
    importlib.reload(appmod)
    client = TestClient(appmod.app)
    return client, db_path, appmod


def db(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def agent_row(db_path, key):
    conn = db(db_path)
    try:
        return conn.execute(
            "SELECT * FROM agents WHERE pubkey = ?", (pubkey_hex(key),)
        ).fetchone()
    finally:
        conn.close()


def set_pos(db_path, agent_db_id, x, y):
    conn = db(db_path)
    try:
        conn.execute("UPDATE agent_world SET x = ?, y = ? WHERE agent_id = ?",
                     (x, y, agent_db_id))
        conn.commit()
    finally:
        conn.close()


def get_pos(db_path, agent_db_id):
    conn = db(db_path)
    try:
        r = conn.execute("SELECT x, y FROM agent_world WHERE agent_id = ?",
                         (agent_db_id,)).fetchone()
        return int(r["x"]), int(r["y"])
    finally:
        conn.close()


def grant_tool(db_path, pubkey, recipe_id, durability=300):
    conn = db(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO tools (agent_pubkey, recipe_id, durability,"
            " max_durability, crafted_at) VALUES (?, ?, ?, ?, ?)",
            (pubkey, recipe_id, durability, durability, "2026-09-23T00:00:00Z"),
        )
        conn.commit()
    finally:
        conn.close()


def place_relay(db_path, owner_pubkey, x, y, derelict=False):
    week = int(time.time() // 604800)
    conn = db(db_path)
    try:
        conn.execute(
            "INSERT INTO structures (owner_pubkey, kind, x, y, raised_at,"
            " last_tithe_week, settlement_asset)"
            " VALUES (?, 'relay', ?, ?, ?, ?, 0)",
            (owner_pubkey, x, y, "2026-09-23T00:00:00Z",
             week - (5 if derelict else 0)),
        )
        conn.commit()
    finally:
        conn.close()


def voice(client, key, kind, text, idem_key=None):
    return signed_request(client, key, "POST", f"/voice/{kind}",
                          {"text": text}, idem_key=idem_key)


def feed(client, key, **params):
    body_text = json.dumps({}, separators=(",", ":"))
    headers = sign(key, "GET", "/voice/feed", body_text)
    return client.request("GET", "/voice/feed", params=params,
                          content=body_text.encode("utf-8"), headers=headers)


# ------------------------------------------------------------- S9: whisper

def test_whisper_same_tile_only(comms):
    client, db_path, _ = comms
    ka, kb, kc = make_key(), make_key(), make_key()
    ra = register(client, "comms-whisper-a", ka)
    rb = register(client, "comms-whisper-b", kb)
    rc = register(client, "comms-whisper-c", kc)
    spawn(client, ka)
    spawn(client, kb)
    spawn(client, kc)
    set_pos(db_path, ra["id"], 10, 10)
    set_pos(db_path, rb["id"], 10, 10)   # same tile as A
    set_pos(db_path, rc["id"], 11, 10)   # adjacent tile — out of whisper range

    r = voice(client, ka, "whisper", "psst", idem_key=fresh_key())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["kind"] == "whisper"
    assert body["radius"] == 0
    assert body["send_x"] == 10 and body["send_y"] == 10
    # free: AP unchanged at spawn budget
    assert body["ap"] == 50

    heard_b = feed(client, kb).json()
    assert [m["text"] for m in heard_b] == ["psst"]
    heard_c = feed(client, kc).json()
    assert heard_c == []


def test_talk_radius_3(comms):
    client, db_path, _ = comms
    ka, kb, kc = make_key(), make_key(), make_key()
    ra = register(client, "comms-talk-a", ka)
    rb = register(client, "comms-talk-b", kb)
    rc = register(client, "comms-talk-c", kc)
    spawn(client, ka)
    spawn(client, kb)
    spawn(client, kc)
    set_pos(db_path, ra["id"], 20, 20)
    set_pos(db_path, rb["id"], 23, 20)   # Chebyshev 3 — in range
    set_pos(db_path, rc["id"], 24, 20)   # Chebyshev 4 — out of range

    r = voice(client, ka, "talk", "hello nearby", idem_key=fresh_key())
    assert r.status_code == 201, r.text
    assert r.json()["radius"] == 3
    assert r.json()["ap"] == 50  # free

    assert [m["text"] for m in feed(client, kb).json()] == ["hello nearby"]
    assert feed(client, kc).json() == []


def test_shout_radius_9_costs_4ap(comms):
    client, db_path, _ = comms
    ka, kb, kc = make_key(), make_key(), make_key()
    ra = register(client, "comms-shout-a", ka)
    rb = register(client, "comms-shout-b", kb)
    rc = register(client, "comms-shout-c", kc)
    spawn(client, ka)
    spawn(client, kb)
    spawn(client, kc)
    set_pos(db_path, ra["id"], 30, 30)
    set_pos(db_path, rb["id"], 39, 30)   # Chebyshev 9 — in range
    set_pos(db_path, rc["id"], 40, 30)   # Chebyshev 10 — out of range

    r = voice(client, ka, "shout", "can you hear me", idem_key=fresh_key())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["radius"] == 9
    assert body["ap"] == 46  # 50 - 4

    assert [m["text"] for m in feed(client, kb).json()] == ["can you hear me"]
    assert feed(client, kc).json() == []


def test_shout_far_speaker_doubles_radius(comms):
    # far_speaker IS one of the 12 hidden discovery recipes (Bible §2.1/§11):
    # "long-range voice while owned". Wired: shout radius 9 -> 18.
    client, db_path, _ = comms
    ka, kb, kc = make_key(), make_key(), make_key()
    ra = register(client, "comms-far-a", ka)
    rb = register(client, "comms-far-b", kb)
    rc = register(client, "comms-far-c", kc)
    spawn(client, ka)
    spawn(client, kb)
    spawn(client, kc)
    grant_tool(db_path, pubkey_hex(ka), "far_speaker")
    set_pos(db_path, ra["id"], 10, 40)
    set_pos(db_path, rb["id"], 28, 40)   # Chebyshev 18 — in range w/ far-speaker
    set_pos(db_path, rc["id"], 29, 40)   # Chebyshev 19 — out of range

    r = voice(client, ka, "shout", "far and wide", idem_key=fresh_key())
    assert r.status_code == 201, r.text
    assert r.json()["radius"] == 18

    assert [m["text"] for m in feed(client, kb).json()] == ["far and wide"]
    assert feed(client, kc).json() == []


def test_voice_requires_spawn(comms):
    client, _, _ = comms
    ka = make_key()
    register(client, "comms-unspawned", ka)
    for kind in ("whisper", "talk", "shout", "relay"):
        r = voice(client, ka, kind, "hello", idem_key=fresh_key())
        assert r.status_code == 403, (kind, r.text)
    r = feed(client, ka)
    assert r.status_code == 403, r.text


def test_voice_text_validation(comms):
    client, _, _ = comms
    ka = make_key()
    register(client, "comms-validate", ka)
    spawn(client, ka)
    r = voice(client, ka, "talk", "", idem_key=fresh_key())
    assert r.status_code == 400, r.text
    r = voice(client, ka, "talk", "x" * 4001, idem_key=fresh_key())
    assert r.status_code == 400, r.text


def test_voice_rate_limits_exact(comms):
    # §11: voice 1/2s, shout 1/30s, relay 1/300s.
    client, db_path, _ = comms
    ka = make_key()
    ra = register(client, "comms-ratelimit", ka)
    spawn(client, ka)
    place_relay(db_path, pubkey_hex(ka), 10, 10)
    set_pos(db_path, ra["id"], 10, 10)

    r1 = voice(client, ka, "talk", "one", idem_key=fresh_key())
    assert r1.status_code == 201, r1.text
    r2 = voice(client, ka, "talk", "two", idem_key=fresh_key())
    assert r2.status_code == 429, r2.text
    assert r2.headers.get("Retry-After")

    r3 = voice(client, ka, "shout", "loud", idem_key=fresh_key())
    assert r3.status_code == 201, r3.text
    r4 = voice(client, ka, "shout", "louder", idem_key=fresh_key())
    assert r4.status_code == 429, r4.text

    r5 = voice(client, ka, "relay", "far", idem_key=fresh_key())
    assert r5.status_code == 201, r5.text
    r6 = voice(client, ka, "relay", "farther", idem_key=fresh_key())
    assert r6.status_code == 429, r6.text


def test_voice_idempotency_replay_no_recharge(comms):
    # Replay-before-rate-limit: same Idempotency-Key returns the stored
    # response without re-executing — no AP or rate-limit re-charge.
    client, _, _ = comms
    ka = make_key()
    register(client, "comms-idem", ka)
    spawn(client, ka)
    key = fresh_key()

    r1 = voice(client, ka, "shout", "once", idem_key=key)
    assert r1.status_code == 201, r1.text
    assert r1.json()["ap"] == 46

    # Immediate replay: would 429 on the shout bucket if re-executed.
    r2 = voice(client, ka, "shout", "once", idem_key=key)
    assert r2.status_code == 201, r2.text
    assert r2.json() == r1.json()

    # Different key on the same endpoint: the shout bucket is consumed.
    r3 = voice(client, ka, "shout", "twice", idem_key=fresh_key())
    assert r3.status_code == 429, r3.text


def test_voice_feed_filters(comms):
    client, db_path, _ = comms
    ka, kb = make_key(), make_key()
    ra = register(client, "comms-feed-a", ka)
    rb = register(client, "comms-feed-b", kb)
    spawn(client, ka)
    spawn(client, kb)
    set_pos(db_path, ra["id"], 5, 5)
    set_pos(db_path, rb["id"], 5, 5)

    voice(client, ka, "whisper", "w1", idem_key=fresh_key())
    # whisper bucket 1/2s — use a second agent for the shout to avoid the wait.
    kc = make_key()
    rc = register(client, "comms-feed-c", kc)
    spawn(client, kc)
    set_pos(db_path, rc["id"], 5, 5)
    voice(client, kc, "shout", "s1", idem_key=fresh_key())

    all_msgs = feed(client, kb).json()
    assert [m["kind"] for m in all_msgs] == ["whisper", "shout"]
    only_shout = feed(client, kb, kind="shout").json()
    assert [m["text"] for m in only_shout] == ["s1"]
    r = feed(client, kb, kind="bogus")
    assert r.status_code == 400, r.text

    since = all_msgs[0]["id"]
    page2 = feed(client, kb, since=since).json()
    assert [m["text"] for m in page2] == ["s1"]
    limited = feed(client, kb, limit=1).json()
    assert len(limited) == 1


def test_voice_retention_pruned_on_send(comms):
    # VOICE_RETENTION_SECONDS=604800: rows older than 7 days are pruned
    # lazily inside the send transaction.
    client, db_path, _ = comms
    ka = make_key()
    ra = register(client, "comms-retain", ka)
    spawn(client, ka)
    set_pos(db_path, ra["id"], 7, 7)
    conn = db(db_path)
    try:
        conn.execute(
            "INSERT INTO voice_messages (agent_id, kind, text, send_x, send_y,"
            " radius, tower_path, ts, signature)"
            " VALUES (?, 'talk', 'ancient', 7, 7, 3, NULL, ?, 'sig')",
            (ra["id"], time.time() - 604800 - 10),
        )
        conn.commit()
        before = conn.execute("SELECT COUNT(*) FROM voice_messages").fetchone()[0]
        assert before == 1
    finally:
        conn.close()

    r = voice(client, ka, "talk", "fresh", idem_key=fresh_key())
    assert r.status_code == 201, r.text

    conn = db(db_path)
    try:
        texts = [row["text"] for row in conn.execute(
            "SELECT text FROM voice_messages").fetchall()]
    finally:
        conn.close()
    assert texts == ["fresh"]


def test_shout_insufficient_ap(comms):
    client, db_path, _ = comms
    ka = make_key()
    ra = register(client, "comms-poor", ka)
    spawn(client, ka)
    conn = db(db_path)
    try:
        conn.execute("UPDATE agent_world SET ap = 0 WHERE agent_id = ?",
                     (ra["id"],))
        conn.commit()
    finally:
        conn.close()
    r = voice(client, ka, "shout", "nope", idem_key=fresh_key())
    assert r.status_code == 402, r.text
    # Free voice still works at 0 AP.
    r = voice(client, ka, "talk", "free", idem_key=fresh_key())
    assert r.status_code == 201, r.text


# ---------------------------------------------------------------- S10: relay

def test_relay_basic_chain_and_cost(comms):
    client, db_path, _ = comms
    ka, kb, kc = make_key(), make_key(), make_key()
    ra = register(client, "comms-relay-a", ka)
    rb = register(client, "comms-relay-b", kb)
    rc = register(client, "comms-relay-c", kc)
    spawn(client, ka)
    spawn(client, kb)
    spawn(client, kc)
    pk = pubkey_hex(ka)
    # Sender at (10,10); tower T1 at (20,10) is within hop 15 of sender;
    # tower T2 at (32,10) is within hop 15 of T1 but not of the sender.
    place_relay(db_path, pk, 20, 10)
    place_relay(db_path, pk, 32, 10)
    set_pos(db_path, ra["id"], 10, 10)
    set_pos(db_path, rb["id"], 32, 12)   # within catch 3 of T2
    set_pos(db_path, rc["id"], 60, 60)   # nowhere near

    r = voice(client, ka, "relay", "tower mail", idem_key=fresh_key())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["kind"] == "relay"
    assert body["towers_used"] == 2
    assert body["ap_cost"] == 5  # 3 + 1/tower
    assert body["ap"] == 45      # 50 - 5
    assert body["tower_path"] == [{"x": 20, "y": 10}, {"x": 32, "y": 10}]

    heard = feed(client, kb).json()
    assert len(heard) == 1
    assert heard[0]["tower_path"] == [{"x": 20, "y": 10}, {"x": 32, "y": 10}]
    # Observer-visible: the path is in the feed record.
    assert feed(client, kc).json() == []


def test_relay_sender_catch_radius(comms):
    # The sender's own tile is a delivery point (catch radius 3), so a
    # single-tower relay also reaches agents standing near the sender.
    client, db_path, _ = comms
    ka, kb = make_key(), make_key()
    ra = register(client, "comms-relaycatch-a", ka)
    rb = register(client, "comms-relaycatch-b", kb)
    spawn(client, ka)
    spawn(client, kb)
    pk = pubkey_hex(ka)
    place_relay(db_path, pk, 20, 10)
    set_pos(db_path, ra["id"], 10, 10)
    set_pos(db_path, rb["id"], 12, 11)   # within catch 3 of sender

    r = voice(client, ka, "relay", "local echo", idem_key=fresh_key())
    assert r.status_code == 201, r.text
    assert r.json()["towers_used"] == 1
    assert r.json()["ap_cost"] == 4
    assert [m["text"] for m in feed(client, kb).json()] == ["local echo"]


def test_relay_max_10_towers(comms):
    client, db_path, _ = comms
    ka = make_key()
    ra = register(client, "comms-relaymax", ka)
    spawn(client, ka)
    pk = pubkey_hex(ka)
    # 12 towers in a line, 10 apart — all chainable, but capped at 10.
    for i in range(12):
        place_relay(db_path, pk, 10 + (i + 1) * 10, 10)
    set_pos(db_path, ra["id"], 10, 10)
    # 50 AP is not enough for 3+10=13; top up via DB for this mechanics test.
    conn = db(db_path)
    try:
        conn.execute("UPDATE agent_world SET ap = 100 WHERE agent_id = ?",
                     (ra["id"],))
        conn.commit()
    finally:
        conn.close()

    r = voice(client, ka, "relay", "long haul", idem_key=fresh_key())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["towers_used"] == 10
    assert body["ap_cost"] == 13
    assert len(body["tower_path"]) == 10


def test_relay_no_tower_in_range_400(comms):
    client, db_path, _ = comms
    ka = make_key()
    ra = register(client, "comms-relaynone", ka)
    spawn(client, ka)
    place_relay(db_path, pubkey_hex(ka), 60, 60)  # far from sender
    set_pos(db_path, ra["id"], 10, 10)

    r = voice(client, ka, "relay", "nowhere", idem_key=fresh_key())
    assert r.status_code == 400, r.text
    assert "hop" in r.json()["detail"]


def test_relay_skips_derelict_towers(comms):
    client, db_path, _ = comms
    ka, kb = make_key(), make_key()
    ra = register(client, "comms-relayd-a", ka)
    rb = register(client, "comms-relayd-b", kb)
    spawn(client, ka)
    spawn(client, kb)
    pk = pubkey_hex(ka)
    # Near tower is derelict (5+ weeks behind); far tower is kept up.
    place_relay(db_path, pk, 15, 10, derelict=True)
    place_relay(db_path, pk, 24, 10)
    set_pos(db_path, ra["id"], 10, 10)
    set_pos(db_path, rb["id"], 24, 12)

    r = voice(client, ka, "relay", "skip the ruin", idem_key=fresh_key())
    assert r.status_code == 201, r.text
    body = r.json()
    # Only the kept-up tower carries the signal (chain starts at sender,
    # 24,10 is within hop 15 of (10,10)).
    assert body["tower_path"] == [{"x": 24, "y": 10}]
    assert body["towers_used"] == 1
    assert [m["text"] for m in feed(client, kb).json()] == ["skip the ruin"]


def test_relay_idempotency(comms):
    client, db_path, _ = comms
    ka = make_key()
    ra = register(client, "comms-relayidem", ka)
    spawn(client, ka)
    place_relay(db_path, pubkey_hex(ka), 15, 10)
    set_pos(db_path, ra["id"], 10, 10)
    key = fresh_key()

    r1 = voice(client, ka, "relay", "once", idem_key=key)
    assert r1.status_code == 201, r1.text
    assert r1.json()["ap"] == 46  # 50 - (3+1)
    r2 = voice(client, ka, "relay", "once", idem_key=key)
    assert r2.status_code == 201, r2.text
    assert r2.json() == r1.json()

    # Relay bucket (1/300s) is consumed by the first send.
    r3 = voice(client, ka, "relay", "again", idem_key=fresh_key())
    assert r3.status_code == 429, r3.text


# ------------------------------------------------------- S11: herald

def _insert_disclosures(db_path, recruit_db_id, n, day_offset=0, x_offset=0):
    conn = db(db_path)
    try:
        base = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                             time.gmtime(time.time() - day_offset * 86400))
        for i in range(n):
            conn.execute(
                "INSERT OR IGNORE INTO public_map (x, y, terrain, disclosed_by,"
                " disclosed_at) VALUES (?, ?, 'plains', ?, ?)",
                ((i + x_offset) % 64, (i * 7 + day_offset) % 64,
                 recruit_db_id, base),
            )
        conn.commit()
    finally:
        conn.close()


def _insert_messages(db_path, recruit_db_id, n_voice, n_chat, day_offsets=(0,)):
    # Spread across the given day offsets so active-day counting works.
    conn = db(db_path)
    try:
        for i in range(n_voice):
            off = day_offsets[i % len(day_offsets)]
            conn.execute(
                "INSERT INTO voice_messages (agent_id, kind, text, send_x,"
                " send_y, radius, tower_path, ts, signature)"
                " VALUES (?, 'talk', ?, 0, 0, 3, NULL, ?, 'sig')",
                (recruit_db_id, f"v{i}", time.time() - off * 86400),
            )
        for i in range(n_chat):
            off = day_offsets[i % len(day_offsets)]
            ts = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                               time.gmtime(time.time() - off * 86400))
            conn.execute(
                "INSERT INTO messages (agent_id, room, text, ts, signature)"
                " VALUES (?, 'general', ?, ?, 'sig')",
                (recruit_db_id, f"c{i}", ts),
            )
        conn.commit()
    finally:
        conn.close()


def test_register_referred_by_name_and_pubkey(comms):
    client, db_path, _ = comms
    ki, kr = make_key(), make_key()
    inviter = register(client, "comms-inviter", ki)

    r1 = client.post("/register", json={
        "name": "comms-recruit-name", "pubkey": pubkey_hex(kr),
        "referred_by": "comms-inviter",
    })
    assert r1.status_code == 201, r1.text
    assert r1.json()["referred_by_agent_id"] == inviter["id"]

    kr2 = make_key()
    r2 = client.post("/register", json={
        "name": "comms-recruit-pk", "pubkey": pubkey_hex(kr2),
        "referred_by": pubkey_hex(ki),
    })
    assert r2.status_code == 201, r2.text
    assert r2.json()["referred_by_agent_id"] == inviter["id"]

    conn = db(db_path)
    try:
        rows = {row["agent_id"]: row["referred_by_agent_id"] for row in conn.execute(
            "SELECT agent_id, referred_by_agent_id FROM heralds").fetchall()}
    finally:
        conn.close()
    assert rows[r1.json()["id"]] == inviter["id"]
    assert rows[r2.json()["id"]] == inviter["id"]


def test_register_referred_by_invalid(comms):
    client, _, _ = comms
    ki = make_key()
    register(client, "comms-inviter2", ki)

    # Unknown referrer -> 400, never silently dropped.
    r = client.post("/register", json={
        "name": "comms-recruit-bad", "pubkey": pubkey_hex(make_key()),
        "referred_by": "nobody-here",
    })
    assert r.status_code == 400, r.text

    # Self-referral -> 400.
    kself = make_key()
    r = client.post("/register", json={
        "name": "comms-selfref", "pubkey": pubkey_hex(kself),
        "referred_by": "comms-selfref",
    })
    assert r.status_code == 400, r.text

    # Backward compatible: no referred_by still registers fine.
    r = client.post("/register", json={
        "name": "comms-loner", "pubkey": pubkey_hex(make_key()),
    })
    assert r.status_code == 201, r.text
    assert "referred_by_agent_id" not in r.json()


def test_herald_vesting_lazy_and_thresholds(comms):
    client, db_path, _ = comms
    ki = make_key()
    inviter = register(client, "comms-herald", ki)
    kr = make_key()
    recruit = client.post("/register", json={
        "name": "comms-vesting", "pubkey": pubkey_hex(kr),
        "referred_by": "comms-herald",
    }).json()
    rid = recruit["id"]

    # 25 disclosed + 10 messages across 2 UTC days -> vested.
    _insert_disclosures(db_path, rid, 25, day_offset=0)
    _insert_disclosures(db_path, rid, 1, day_offset=1)  # 26 total, 2nd day
    _insert_messages(db_path, rid, 6, 4, day_offsets=(0, 1))

    # Before the leaderboard read, the cached flag is still 0 (lazy).
    conn = db(db_path)
    try:
        flag_before = conn.execute(
            "SELECT vested FROM heralds WHERE agent_id = ?", (rid,)).fetchone()[0]
    finally:
        conn.close()
    assert flag_before == 0

    lb = client.get("/heralds/leaderboard").json()
    assert len(lb) == 1
    entry = lb[0]
    assert entry["inviter_name"] == "comms-herald"
    assert entry["recruits_total"] == 1
    assert entry["vested_count"] == 1
    assert entry["is_herald"] is False  # needs 3 vested

    # The lazy evaluation cached the flag.
    conn = db(db_path)
    try:
        flag_after = conn.execute(
            "SELECT vested FROM heralds WHERE agent_id = ?", (rid,)).fetchone()[0]
    finally:
        conn.close()
    assert flag_after == 1


def test_herald_vesting_partial_not_vested(comms):
    client, db_path, _ = comms
    ki = make_key()
    register(client, "comms-herald2", ki)
    kr = make_key()
    recruit = client.post("/register", json={
        "name": "comms-partial", "pubkey": pubkey_hex(kr),
        "referred_by": "comms-herald2",
    }).json()
    rid = recruit["id"]

    # 24 disclosed (one short), 10 messages, 2 active days.
    _insert_disclosures(db_path, rid, 24, day_offset=0)
    _insert_messages(db_path, rid, 10, 0, day_offsets=(0, 1))

    lb = client.get("/heralds/leaderboard").json()
    assert lb[0]["vested_count"] == 0
    assert lb[0]["is_herald"] is False


def test_herald_feast_buffs_do_not_count(comms):
    # Feast buffs are AP-cap buffs, not actions: they must never move a
    # recruit toward vesting. A recruit with ONLY a feast buff stays unvested.
    client, db_path, _ = comms
    ki = make_key()
    register(client, "comms-herald3", ki)
    kr = make_key()
    recruit = client.post("/register", json={
        "name": "comms-feastonly", "pubkey": pubkey_hex(kr),
        "referred_by": "comms-herald3",
    }).json()
    conn = db(db_path)
    try:
        conn.execute(
            "INSERT INTO feast_buffs (agent_pubkey, settlement_id, granted_at,"
            " expires_at) VALUES (?, 0, ?, ?)",
            (pubkey_hex(kr), time.time(), time.time() + 7 * 86400),
        )
        conn.commit()
    finally:
        conn.close()

    lb = client.get("/heralds/leaderboard").json()
    assert lb[0]["vested_count"] == 0


def test_herald_threshold_three_vested(comms):
    client, db_path, _ = comms
    ki = make_key()
    inviter = register(client, "comms-herald4", ki)
    for i in range(3):
        kr = make_key()
        recruit = client.post("/register", json={
            "name": f"comms-vest{i}", "pubkey": pubkey_hex(kr),
            "referred_by": "comms-herald4",
        }).json()
        rid = recruit["id"]
        # x_offset keeps each recruit's tiles distinct: public_map is
        # PRIMARY KEY(x, y), shared across all agents.
        _insert_disclosures(db_path, rid, 25, day_offset=0, x_offset=i * 25)
        _insert_messages(db_path, rid, 10, 0, day_offsets=(0, 1))

    lb = client.get("/heralds/leaderboard").json()
    assert len(lb) == 1
    assert lb[0]["recruits_total"] == 3
    assert lb[0]["vested_count"] == 3
    assert lb[0]["is_herald"] is True
    assert lb[0]["herald_threshold"] == 3


def test_herald_leaderboard_ordering(comms):
    client, db_path, _ = comms
    ka, kb = make_key(), make_key()
    register(client, "comms-hb-a", ka)
    register(client, "comms-hb-b", kb)
    # A gets one vested recruit; B gets two unvested recruits.
    kr = make_key()
    r = client.post("/register", json={
        "name": "comms-hb-av", "pubkey": pubkey_hex(kr),
        "referred_by": "comms-hb-a"}).json()
    _insert_disclosures(db_path, r["id"], 25, day_offset=0)
    _insert_messages(db_path, r["id"], 10, 0, day_offsets=(0, 1))
    for i in range(2):
        k = make_key()
        client.post("/register", json={
            "name": f"comms-hb-bu{i}", "pubkey": pubkey_hex(k),
            "referred_by": "comms-hb-b"})

    lb = client.get("/heralds/leaderboard").json()
    assert [e["inviter_name"] for e in lb] == ["comms-hb-a", "comms-hb-b"]
    assert lb[0]["vested_count"] == 1
    assert lb[1]["vested_count"] == 0


# ------------------------------------------------- spawn anti-isolation

def test_spawn_near_radius(comms):
    # Bible §11 SPAWN_NEAR_RADIUS=20: after the first agent, newcomers spawn
    # within Chebyshev 20 of at least one spawned agent — never stranded
    # permanently out of voice range with no path to the others.
    client, db_path, _ = comms
    k0 = make_key()
    r0 = register(client, "comms-spawn0", k0)
    spawn(client, k0)
    set_pos(db_path, r0["id"], 0, 0)
    prior = [(0, 0)]

    for i in range(5):
        k = make_key()
        r = register(client, f"comms-spawn{i + 1}", k)
        spawn(client, k)
        x, y = get_pos(db_path, r["id"])
        # Within Chebyshev 20 of AT LEAST ONE already-spawned agent — the
        # population may drift as a chain, which is the intended design.
        assert any(max(abs(x - px), abs(y - py)) <= 20 for px, py in prior), (x, y)
        prior.append((x, y))


def test_spawn_first_agent_unconstrained(comms):
    client, db_path, _ = comms
    k = make_key()
    register(client, "comms-spawnfirst", k)
    r = spawn(client, k)
    assert r["x"] is not None and r["y"] is not None
    # No other agents exist; the near pool is empty so any free land tile
    # is eligible — the spawn simply succeeds.
    assert r["ap"] == 50


# ------------------------------------------------- the aether (legacy /chat)

def test_aether_chat_still_global(comms):
    # The Bible names no trigger for quieting the aether, so legacy /chat
    # stays fully functional: global, room-scoped, position-independent.
    client, db_path, _ = comms
    ka, kb = make_key(), make_key()
    register(client, "comms-aether-a", ka)
    register(client, "comms-aether-b", kb)
    spawn(client, ka)
    spawn(client, kb)
    # Far apart — beyond even far-speaker shout range.
    ra_id = agent_row(db_path, ka)["id"]
    rb_id = agent_row(db_path, kb)["id"]
    set_pos(db_path, ra_id, 0, 0)
    set_pos(db_path, rb_id, 63, 63)

    body = json.dumps({"room": "general", "text": "aether carries all"},
                      separators=(",", ":"))
    headers = sign(ka, "POST", "/chat", body)
    headers["Idempotency-Key"] = fresh_key()
    r = client.post("/chat", content=body.encode("utf-8"), headers=headers)
    assert r.status_code == 201, r.text

    # B reads it despite the distance: /chat is global (the aether).
    body2 = json.dumps({}, separators=(",", ":"))
    h2 = sign(kb, "GET", "/chat", body2)
    msgs = client.get("/chat", params={"room": "general"},
                       headers=h2).json()
    assert any(m["text"] == "aether carries all" for m in msgs)

    # And voice at that distance is NOT audible — the two channels differ.
    assert feed(client, kb).json() == []


# ------------------------------------------------- docs surface

def test_openapi_covers_comms_routes(comms):
    client, _, _ = comms
    paths = client.get("/openapi.json").json()["paths"]
    expected = {
        "/voice/whisper": ["post"],
        "/voice/talk": ["post"],
        "/voice/shout": ["post"],
        "/voice/relay": ["post"],
        "/voice/feed": ["get"],
        "/heralds/leaderboard": ["get"],
    }
    for path, methods in expected.items():
        assert path in paths, f"missing from OpenAPI: {path}"
        for method in methods:
            assert method in paths[path], f"{path} missing {method}"
    # The aether is still served.
    assert "post" in paths["/chat"]


def test_agents_txt_documents_voice_and_aether(comms):
    client, _, _ = comms
    txt = client.get("/agents.txt").text
    for token in ("/voice/whisper", "/voice/talk", "/voice/shout",
                  "/voice/relay", "/voice/feed", "aether",
                  "/heralds/leaderboard", "referred_by", "far-speaker"):
        assert token in txt, f"agents.txt missing: {token}"
