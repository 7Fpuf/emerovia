"""Stage 4 economy experiment tests for Emerovia.

Covers: resource stock seeding (deterministic, idempotent), gathering
(terrain->resource, AP cost, stock decrement, inventory), gather edge cases
(ocean, unspawned, depleted, cap, AP, rate limit), chits grant + backfill,
trade offers (create/validate/cap), accept (atomic swap, ledger, self,
double, uncoverable), cancel (auth, state), ledger shape + limit clamp,
economy stats, and the trade rate limits.

Same isolated-app fixture pattern as the other suites: AC_DB_PATH ->
tmp_path, fresh operator keypair, importlib.reload(server.app). Rate-limit
windows are overridden per-test via the module-level RATE_LIMITS dict.
"""
from __future__ import annotations

import importlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from nacl.signing import SigningKey

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

TERRAIN_RESOURCE = {"plains": "grain", "forest": "timber",
                    "mountain": "iron_ore", "desert": "glass"}


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


def spawn(client: TestClient, key: SigningKey):
    r = signed_request(client, key, "POST", "/world/spawn", {})
    assert r.status_code == 201, r.text
    return r.json()


def gather(client: TestClient, key: SigningKey, resource=None):
    body = {} if resource is None else {"resource": resource}
    return signed_request(client, key, "POST", "/world/gather", body)


def offer(client: TestClient, key: SigningKey, give: dict, want: dict):
    return signed_request(client, key, "POST", "/trade/offers",
                          {"give": give, "want": want})


@pytest.fixture()
def s4(tmp_path, monkeypatch):
    """Fresh app + isolated DB; returns (client, keys, db_path, appmod)."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("AC_DB_PATH", db_path)
    op_key = make_key()
    monkeypatch.setenv("AC_OPERATOR_PUBKEY", pubkey_hex(op_key))
    import server.app as appmod
    importlib.reload(appmod)
    client = TestClient(appmod.app)
    keys = [make_key() for _ in range(8)]
    for i, k in enumerate(keys):
        register(client, f"eco-agent-{i}", k)
    return client, keys, db_path, appmod


def db(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def set_inventory(db_path, key, resource, qty):
    conn = db(db_path)
    try:
        conn.execute(
            "INSERT INTO inventories (agent_pubkey, resource, qty) VALUES (?, ?, ?)"
            " ON CONFLICT(agent_pubkey, resource) DO UPDATE SET qty = ?",
            (pubkey_hex(key), resource, qty, qty),
        )
        conn.commit()
    finally:
        conn.close()


def set_chits(db_path, key, chits):
    conn = db(db_path)
    try:
        conn.execute(
            "INSERT INTO credit_balances (agent_pubkey, chits) VALUES (?, ?)"
            " ON CONFLICT(agent_pubkey) DO UPDATE SET chits = ?",
            (pubkey_hex(key), chits, chits),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- chits

def test_register_grants_100_chits(s4):
    client, keys, _, _ = s4
    r = signed_request(client, keys[0], "GET", "/world/inventory", {})
    # GET with {} body: sign over empty-ish body; endpoint ignores body.
    # Inventory endpoint is signed-only so we reuse the signed helper.
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["chits"] == 100
    assert body["inventory"] == {}


def test_chits_backfill_for_pre_stage4_agents(s4, monkeypatch):
    """Agents inserted directly (bypassing /register) still get 100 chits
    when the app boots — the idempotent create_app backfill."""
    client, keys, db_path, appmod = s4
    legacy_key = make_key()
    conn = db(db_path)
    try:
        conn.execute(
            "INSERT INTO agents (name, pubkey, registered_at) VALUES (?, ?, ?)",
            ("legacy-agent", pubkey_hex(legacy_key), "2026-01-01T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()
    importlib.reload(appmod)  # create_app() runs the backfill again
    client = TestClient(appmod.app)
    r = signed_request(client, legacy_key, "GET", "/world/inventory", {})
    assert r.status_code == 200, r.text
    assert r.json()["chits"] == 100


def test_chits_backfill_never_clobbers_existing_balance(s4):
    client, keys, db_path, _ = s4
    set_chits(db_path, keys[0], 42)
    import server.app as appmod
    importlib.reload(appmod)
    client = TestClient(appmod.app)
    r = signed_request(client, keys[0], "GET", "/world/inventory", {})
    assert r.json()["chits"] == 42


# ---------------------------------------------------------------- stock seeding

def test_resource_stock_seeded_deterministic_idempotent(s4):
    _, _, db_path, _ = s4
    conn = db(db_path)
    try:
        land = conn.execute(
            "SELECT COUNT(*) FROM world_tiles WHERE terrain != 'ocean'"
        ).fetchone()[0]
        rows = conn.execute("SELECT COUNT(*) FROM world_resource_stock").fetchone()[0]
        # Bible ch.1: legacy + overlay rows — exactly two per land tile.
        assert rows == 2 * land
        lo, hi = conn.execute(
            "SELECT MIN(stock), MAX(stock) FROM world_resource_stock"
        ).fetchone()
        assert 5 <= lo <= hi <= 12  # legacy 5-10, overlay bulk 6-12
        ocean_rows = conn.execute(
            """SELECT COUNT(*) FROM world_resource_stock s
               JOIN world_tiles t ON t.x = s.x AND t.y = s.y
               WHERE t.terrain = 'ocean'"""
        ).fetchone()[0]
        assert ocean_rows == 0
        before = conn.execute(
            "SELECT x, y, resource, stock FROM world_resource_stock ORDER BY x, y, resource"
        ).fetchall()
    finally:
        conn.close()
    # Re-seeding is a no-op and values are deterministic (no change).
    import server.world as world_engine
    conn = db(db_path)
    try:
        assert world_engine.seed_resource_stock(conn) == 0
        conn.commit()
        after = conn.execute(
            "SELECT x, y, resource, stock FROM world_resource_stock ORDER BY x, y, resource"
        ).fetchall()
        assert [tuple(r) for r in after] == [tuple(r) for r in before]
    finally:
        conn.close()


# ---------------------------------------------------------------- gather

def test_gather_yields_terrain_resource_and_costs(s4, monkeypatch):
    client, keys, db_path, appmod = s4
    monkeypatch.setitem(appmod.RATE_LIMITS, "gather", (100, 60))
    spawn(client, keys[0])
    me = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    terrain = me["terrain"]
    ap_before = me["ap"]
    expected = TERRAIN_RESOURCE[terrain]
    conn = db(db_path)
    try:
        stock_before = conn.execute(
            "SELECT stock FROM world_resource_stock WHERE x = ? AND y = ? AND resource = ?",
            (me["x"], me["y"], expected),
        ).fetchone()["stock"]
    finally:
        conn.close()
    # Bible ch.1: tiles bear legacy + overlay resources, so the gather names
    # its resource explicitly (omitted + two present -> 400, tested below).
    r = gather(client, keys[0], expected)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["resource"] == expected
    assert body["gained"] == 1
    # Bible ch.3: bare hands cost 4 AP for 1 (the flat 2 AP era ended).
    assert body["ap"] == ap_before - 4
    assert body["tooled"] is False
    assert body["stock_remaining"] == stock_before - 1
    inv = signed_request(client, keys[0], "GET", "/world/inventory", {}).json()
    assert inv["inventory"] == {expected: 1}


def test_gather_ocean_400(s4, monkeypatch):
    client, keys, db_path, appmod = s4
    monkeypatch.setitem(appmod.RATE_LIMITS, "gather", (100, 60))
    spawn(client, keys[0])
    conn = db(db_path)
    try:
        ocean = conn.execute(
            "SELECT x, y FROM world_tiles WHERE terrain = 'ocean' LIMIT 1"
        ).fetchone()
        agent_id = conn.execute(
            "SELECT id FROM agents WHERE pubkey = ?", (pubkey_hex(keys[0]),)
        ).fetchone()["id"]
        conn.execute(
            "UPDATE agent_world SET x = ?, y = ? WHERE agent_id = ?",
            (ocean["x"], ocean["y"], agent_id),
        )
        conn.commit()
    finally:
        conn.close()
    r = gather(client, keys[0])
    assert r.status_code == 400, r.text


def test_gather_unspawned_400(s4):
    client, keys, _, _ = s4
    r = gather(client, keys[0])
    assert r.status_code == 400, r.text
    assert "not spawned" in r.json()["detail"]


def test_gather_depleted_tile_400(s4, monkeypatch):
    client, keys, db_path, appmod = s4
    monkeypatch.setitem(appmod.RATE_LIMITS, "gather", (100, 60))
    spawn(client, keys[0])
    me = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    conn = db(db_path)
    try:
        conn.execute(
            "UPDATE world_resource_stock SET stock = 1 WHERE x = ? AND y = ?",
            (me["x"], me["y"]),
        )
        conn.commit()
    finally:
        conn.close()
    me = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    res = TERRAIN_RESOURCE[me["terrain"]]
    assert gather(client, keys[0], res).status_code == 200
    r = gather(client, keys[0], res)
    assert r.status_code == 400, r.text
    assert "depleted" in r.json()["detail"]


def test_gather_inventory_cap_99_400(s4, monkeypatch):
    client, keys, db_path, appmod = s4
    monkeypatch.setitem(appmod.RATE_LIMITS, "gather", (100, 60))
    spawn(client, keys[0])
    me = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    set_inventory(db_path, keys[0], TERRAIN_RESOURCE[me["terrain"]], 99)
    r = gather(client, keys[0], TERRAIN_RESOURCE[me["terrain"]])
    assert r.status_code == 400, r.text
    assert "cap" in r.json()["detail"]


def test_gather_insufficient_ap_402(s4, monkeypatch):
    client, keys, db_path, appmod = s4
    monkeypatch.setitem(appmod.RATE_LIMITS, "gather", (100, 60))
    spawn(client, keys[0])
    conn = db(db_path)
    try:
        agent_id = conn.execute(
            "SELECT id FROM agents WHERE pubkey = ?", (pubkey_hex(keys[0]),)
        ).fetchone()["id"]
        conn.execute("UPDATE agent_world SET ap = 1 WHERE agent_id = ?", (agent_id,))
        conn.commit()
    finally:
        conn.close()
    me = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    r = gather(client, keys[0], TERRAIN_RESOURCE[me["terrain"]])
    assert r.status_code == 402, r.text


def test_gather_rate_limit_429(s4, monkeypatch):
    client, keys, _, appmod = s4
    monkeypatch.setitem(appmod.RATE_LIMITS, "gather", (1, 60))
    spawn(client, keys[0])
    me = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    res = TERRAIN_RESOURCE[me["terrain"]]
    assert gather(client, keys[0], res).status_code == 200
    r = gather(client, keys[0], res)
    assert r.status_code == 429, r.text
    assert "retry-after" in r.headers
    assert "rate limited" in r.json()["detail"]


# ---------------------------------------------------------------- trade offers

def test_create_offer_ok_and_listed(s4):
    client, keys, db_path, _ = s4
    set_inventory(db_path, keys[0], "grain", 5)
    r = offer(client, keys[0], {"grain": 2}, {"chits": 10})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "open"
    listed = client.get("/trade/offers").json()
    assert len(listed) == 1
    o = listed[0]
    assert o["id"] == body["offer_id"]
    assert o["maker_name"] == "eco-agent-0"
    assert o["give"] == {"grain": 2}
    assert o["want"] == {"chits": 10}
    assert "created_at" in o


def test_create_offer_maker_lacks_items_400(s4):
    client, keys, _, _ = s4
    r = offer(client, keys[0], {"iron_ore": 3}, {"chits": 5})
    assert r.status_code == 400, r.text
    r = offer(client, keys[0], {"chits": 1000}, {"grain": 1})
    assert r.status_code == 400, r.text


def test_create_offer_validation_400(s4):
    client, keys, db_path, _ = s4
    set_inventory(db_path, keys[0], "grain", 5)
    assert offer(client, keys[0], {}, {"chits": 5}).status_code == 400
    assert offer(client, keys[0], {"grain": 1}, {}).status_code == 400
    assert offer(client, keys[0], {"gold": 1}, {"chits": 5}).status_code == 400
    assert offer(client, keys[0], {"grain": 0}, {"chits": 5}).status_code == 400
    assert offer(client, keys[0], {"grain": -2}, {"chits": 5}).status_code == 400
    assert offer(client, keys[0], {"grain": True}, {"chits": 5}).status_code == 400


def test_create_offer_max_five_open_429(s4, monkeypatch):
    client, keys, db_path, appmod = s4
    monkeypatch.setitem(appmod.RATE_LIMITS, "trade_offer", (100, 60))
    set_inventory(db_path, keys[0], "grain", 99)
    for i in range(5):
        r = offer(client, keys[0], {"grain": 1}, {"chits": i + 1})
        assert r.status_code == 201, r.text
    r = offer(client, keys[0], {"grain": 1}, {"chits": 99})
    assert r.status_code == 429, r.text


def test_create_offer_rate_limit_429(s4, monkeypatch):
    client, keys, db_path, appmod = s4
    monkeypatch.setitem(appmod.RATE_LIMITS, "trade_offer", (1, 60))
    set_inventory(db_path, keys[0], "grain", 99)
    assert offer(client, keys[0], {"grain": 1}, {"chits": 1}).status_code == 201
    r = offer(client, keys[0], {"grain": 1}, {"chits": 2})
    assert r.status_code == 429, r.text
    assert "retry-after" in r.headers


# ---------------------------------------------------------------- accept

def _filled_trade(client, keys, db_path):
    """Maker eco-agent-0 offers 2 grain for 10 chits; eco-agent-1 accepts."""
    set_inventory(db_path, keys[0], "grain", 5)
    oid = offer(client, keys[0], {"grain": 2}, {"chits": 10}).json()["offer_id"]
    r = signed_request(client, keys[1], "POST", f"/trade/offers/{oid}/accept", {})
    assert r.status_code == 200, r.text
    return oid


def test_accept_offer_swaps_atomically_and_ledgers(s4):
    client, keys, db_path, _ = s4
    oid = _filled_trade(client, keys, db_path)
    conn = db(db_path)
    try:
        maker_inv = conn.execute(
            "SELECT qty FROM inventories WHERE agent_pubkey = ? AND resource = 'grain'",
            (pubkey_hex(keys[0]),),
        ).fetchone()["qty"]
        taker_inv = conn.execute(
            "SELECT qty FROM inventories WHERE agent_pubkey = ? AND resource = 'grain'",
            (pubkey_hex(keys[1]),),
        ).fetchone()["qty"]
        maker_chits = conn.execute(
            "SELECT chits FROM credit_balances WHERE agent_pubkey = ?",
            (pubkey_hex(keys[0]),),
        ).fetchone()["chits"]
        taker_chits = conn.execute(
            "SELECT chits FROM credit_balances WHERE agent_pubkey = ?",
            (pubkey_hex(keys[1]),),
        ).fetchone()["chits"]
        status = conn.execute(
            "SELECT status FROM trade_offers WHERE id = ?", (oid,)
        ).fetchone()["status"]
        ledger = conn.execute("SELECT * FROM trade_ledger").fetchall()
    finally:
        conn.close()
    assert maker_inv == 3      # 5 - 2
    assert taker_inv == 2      # 0 + 2
    assert maker_chits == 110  # 100 + 10
    assert taker_chits == 90   # 100 - 10
    assert status == "filled"
    assert len(ledger) == 1
    row = ledger[0]
    assert row["maker_name"] == "eco-agent-0"
    assert row["taker_name"] == "eco-agent-1"
    assert json.loads(row["give_json"]) == {"grain": 2}
    assert json.loads(row["want_json"]) == {"chits": 10}


def test_accept_self_400(s4):
    client, keys, db_path, _ = s4
    set_inventory(db_path, keys[0], "grain", 5)
    oid = offer(client, keys[0], {"grain": 2}, {"chits": 10}).json()["offer_id"]
    r = signed_request(client, keys[0], "POST", f"/trade/offers/{oid}/accept", {})
    assert r.status_code == 400, r.text


def test_accept_twice_409(s4):
    client, keys, db_path, _ = s4
    oid = _filled_trade(client, keys, db_path)
    r = signed_request(client, keys[2], "POST", f"/trade/offers/{oid}/accept", {})
    assert r.status_code == 409, r.text


def test_accept_taker_cannot_cover_409(s4):
    client, keys, db_path, _ = s4
    set_inventory(db_path, keys[0], "grain", 5)
    oid = offer(client, keys[0], {"grain": 1}, {"chits": 10000}).json()["offer_id"]
    r = signed_request(client, keys[1], "POST", f"/trade/offers/{oid}/accept", {})
    assert r.status_code == 409, r.text
    # Failed accept leaves everything untouched: offer still open, no ledger.
    listed = client.get("/trade/offers").json()
    assert any(o["id"] == oid for o in listed)
    assert client.get("/trade/ledger").json() == []


def test_accept_maker_drained_409(s4):
    client, keys, db_path, _ = s4
    set_inventory(db_path, keys[0], "grain", 5)
    oid = offer(client, keys[0], {"grain": 5}, {"chits": 10}).json()["offer_id"]
    set_inventory(db_path, keys[0], "grain", 0)  # maker spent the grain elsewhere
    r = signed_request(client, keys[1], "POST", f"/trade/offers/{oid}/accept", {})
    assert r.status_code == 409, r.text
    assert client.get("/trade/ledger").json() == []


# ---------------------------------------------------------------- cancel

def test_cancel_non_maker_403_and_maker_ok(s4):
    client, keys, db_path, _ = s4
    set_inventory(db_path, keys[0], "grain", 5)
    oid = offer(client, keys[0], {"grain": 1}, {"chits": 5}).json()["offer_id"]
    r = signed_request(client, keys[1], "POST", f"/trade/offers/{oid}/cancel", {})
    assert r.status_code == 403, r.text
    r = signed_request(client, keys[0], "POST", f"/trade/offers/{oid}/cancel", {})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "cancelled"
    assert client.get("/trade/offers").json() == []


def test_cancel_filled_offer_409(s4):
    client, keys, db_path, _ = s4
    oid = _filled_trade(client, keys, db_path)
    r = signed_request(client, keys[0], "POST", f"/trade/offers/{oid}/cancel", {})
    assert r.status_code == 409, r.text


# ---------------------------------------------------------------- ledger + stats

def test_ledger_shape_limit_and_append_only(s4):
    client, keys, db_path, _ = s4
    _filled_trade(client, keys, db_path)
    set_inventory(db_path, keys[2], "timber", 4)
    oid2 = offer(client, keys[2], {"timber": 4}, {"iron_ore": 1}).json()["offer_id"]
    set_inventory(db_path, keys[3], "iron_ore", 2)
    assert signed_request(client, keys[3], "POST", f"/trade/offers/{oid2}/accept", {}).status_code == 200
    ledger = client.get("/trade/ledger").json()
    assert len(ledger) == 2
    row = ledger[0]
    for field in ("id", "ts", "maker_pubkey", "maker_name", "taker_pubkey",
                  "taker_name", "give", "want"):
        assert field in row, field
    assert isinstance(row["give"], dict) and isinstance(row["want"], dict)
    assert ledger[0]["id"] < ledger[1]["id"]  # oldest first
    assert len(client.get("/trade/ledger", params={"limit": 1}).json()) == 1
    assert len(client.get("/trade/ledger", params={"limit": 0}).json()) == 1   # clamped
    assert len(client.get("/trade/ledger", params={"limit": 500}).json()) == 2  # clamped


def test_economy_stats_numbers(s4, monkeypatch):
    client, keys, db_path, appmod = s4
    monkeypatch.setitem(appmod.RATE_LIMITS, "gather", (100, 60))
    stock_before = client.get("/stats/economy").json()["total_stock_remaining"]
    spawn(client, keys[0])
    me0 = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    assert gather(client, keys[0], TERRAIN_RESOURCE[me0["terrain"]]).status_code == 200
    _filled_trade(client, keys, db_path)
    set_inventory(db_path, keys[0], "grain", 99)  # extra open offer stays open
    offer(client, keys[0], {"grain": 1}, {"chits": 1})
    stats = client.get("/stats/economy").json()
    assert stats["trades_total"] == 1
    assert stats["unique_traders"] == 2
    assert stats["offers_open"] == 1
    assert stats["volume_chits"] == 10
    vol = stats["volume_by_resource"]
    assert vol["grain"] == 2 and vol["timber"] == 0 and vol["iron_ore"] == 0 \
        and vol["glass"] == 0
    assert len(vol) == 17  # all Bible raw + refined resources
    assert stats["total_stock_remaining"] == stock_before - 1


def test_stats_economy_empty_world(s4):
    client, _, _, _ = s4
    # fresh DB: no trades, no offers, stock fully seeded
    stats = client.get("/stats/economy").json()
    assert stats["trades_total"] == 0
    assert stats["unique_traders"] == 0
    assert stats["offers_open"] == 0
    assert stats["volume_chits"] == 0
    assert stats["total_stock_remaining"] > 0


def test_inventory_view_shape(s4):
    client, keys, db_path, _ = s4
    set_inventory(db_path, keys[0], "grain", 7)
    set_inventory(db_path, keys[0], "iron_ore", 3)
    body = signed_request(client, keys[0], "GET", "/world/inventory", {}).json()
    assert body["agent_name"] == "eco-agent-0"
    assert body["chits"] == 100
    assert body["inventory"] == {"grain": 7, "iron_ore": 3}
