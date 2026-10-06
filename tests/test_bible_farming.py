"""Bible ch.6 — farming (Systems Bible §2.3/§11).

Covers: farm-structure plot creation (4 empty slots), the plant →
harvest cycle with exact Bible AP costs (plant 2, harvest 2), 2-hour
growth as a pure timestamp comparison (no ticks), 3 base yield /
plow variant (plant 1 AP, 4 yield), harvest resetting to empty, no
seed cost (the Bible names none), farmed grain being seasonless (§7),
and the error cases (unknown action, wrong state, bad slot, чужой farm,
non-farm structure, insufficient AP, inventory cap).
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


@pytest.fixture()
def b6(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("AC_DB_PATH", db_path)
    op_key = make_key()
    monkeypatch.setenv("AC_OPERATOR_PUBKEY", pubkey_hex(op_key))
    import server.app as appmod
    importlib.reload(appmod)
    client = TestClient(appmod.app)
    keys = [make_key() for _ in range(4)]
    for i, k in enumerate(keys):
        register(client, f"bible-farm-{i}", k)
    return client, keys, db_path, appmod


def db(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def set_inventory(db_path, pubkey, items: dict):
    conn = db(db_path)
    try:
        for item, qty in items.items():
            conn.execute(
                "INSERT INTO inventories (agent_pubkey, resource, qty) VALUES (?, ?, ?)"
                " ON CONFLICT(agent_pubkey, resource) DO UPDATE SET qty = ?",
                (pubkey, item, qty, qty),
            )
        conn.commit()
    finally:
        conn.close()


def inventory_of(db_path, pubkey):
    conn = db(db_path)
    try:
        return {
            r["resource"]: int(r["qty"])
            for r in conn.execute(
                "SELECT resource, qty FROM inventories WHERE agent_pubkey = ?",
                (pubkey,),
            ).fetchall()
        }
    finally:
        conn.close()


def grant_tool(db_path, pubkey, recipe_id, durability=120):
    conn = db(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO tools (agent_pubkey, recipe_id, durability,"
            " max_durability, crafted_at) VALUES (?, ?, ?, ?, ?)",
            (pubkey, recipe_id, durability, durability,
             "2026-09-23T00:00:00Z"),
        )
        conn.commit()
    finally:
        conn.close()


def build_farm(client, keys, db_path, key_idx=0):
    # Bible §11: farm = 4 AP + 2 timber + 2 grain, owned crude_sickle key.
    me = signed_request(client, keys[key_idx], "GET", "/world/me", {}).json()
    pk = pubkey_hex(keys[key_idx])
    assert signed_request(client, keys[key_idx], "POST", "/world/claim",
                          {"x": me["x"], "y": me["y"]}).status_code == 200
    grant_tool(db_path, pk, "crude_sickle")
    set_inventory(db_path, pk, {"timber": 2, "grain": 2})
    r = signed_request(client, keys[key_idx], "POST", "/world/build",
                       {"kind": "farm", "x": me["x"], "y": me["y"]})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def farm(client, key, structure_id, action, slot=None):
    payload = {"structure_id": structure_id, "action": action}
    if slot is not None:
        payload["slot"] = slot
    return signed_request(client, key, "POST", "/world/farm", payload)


def plot_state(db_path, structure_id, slot):
    conn = db(db_path)
    try:
        return conn.execute(
            "SELECT * FROM farm_plots WHERE structure_id = ? AND slot = ?",
            (structure_id, slot),
        ).fetchone()
    finally:
        conn.close()


def force_ready(db_path, structure_id, slot):
    conn = db(db_path)
    try:
        conn.execute(
            "UPDATE farm_plots SET ready_at = ? WHERE structure_id = ? AND slot = ?",
            (time.time() - 1, structure_id, slot),
        )
        conn.commit()
    finally:
        conn.close()


def set_genesis_days_ago(db_path, days):
    conn = db(db_path)
    try:
        conn.execute(
            "INSERT INTO world_meta (key, value) VALUES ('genesis_ts', ?)"
            " ON CONFLICT(key) DO UPDATE SET value = ?",
            (str(time.time() - days * 86400),) * 2,
        )
        conn.commit()
    finally:
        conn.close()


def open_buckets(appmod, monkeypatch):
    for bucket in ("claim", "build", "plant", "harvest"):
        monkeypatch.setitem(appmod.RATE_LIMITS, bucket, (100, 60))


# ---------------------------------------------------------------- tests

def test_farm_gets_four_empty_plots(b6, monkeypatch):
    client, keys, db_path, appmod = b6
    open_buckets(appmod, monkeypatch)
    spawn(client, keys[0])
    fid = build_farm(client, keys, db_path)
    for slot in range(4):
        plot = plot_state(db_path, fid, slot)
        assert plot is not None
        assert plot["state"] == "empty"


def test_plant_harvest_cycle(b6, monkeypatch):
    client, keys, db_path, appmod = b6
    open_buckets(appmod, monkeypatch)
    spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    fid = build_farm(client, keys, db_path)

    me = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    r = farm(client, keys[0], fid, "plant", 0)
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "growing"
    assert r.json()["ap"] == me["ap"] - 2  # Bible §11 PLANT_COST_AP
    assert inventory_of(db_path, pk) == {}  # no seed cost — the Bible names none
    plot = plot_state(db_path, fid, 0)
    assert abs(float(plot["ready_at"]) - (time.time() + 7200)) < 30

    # Planting a growing slot refuses.
    r = farm(client, keys[0], fid, "plant", 0)
    assert r.status_code == 400, r.text

    # Not ready yet: harvest refuses.
    r = farm(client, keys[0], fid, "harvest", 0)
    assert r.status_code == 400, r.text
    assert "not ready" in r.json()["detail"]

    # Two hours pass (simulated) — harvest is a pure timestamp comparison.
    force_ready(db_path, fid, 0)
    me = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    r = farm(client, keys[0], fid, "harvest", 0)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["yielded"] == 3  # Bible §11 HARVEST_YIELD
    assert body["state"] == "empty"
    assert body["ap"] == me["ap"] - 2  # Bible §11 HARVEST_COST_AP
    assert inventory_of(db_path, pk) == {"grain": 3}
    plot = plot_state(db_path, fid, 0)
    assert plot["state"] == "empty"


def test_plow_variant(b6, monkeypatch):
    # Bible §11: +plow: plant 1 AP, harvest 2 AP, yield 4.
    client, keys, db_path, appmod = b6
    open_buckets(appmod, monkeypatch)
    spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    fid = build_farm(client, keys, db_path)
    grant_tool(db_path, pk, "plow", durability=300)

    me = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    r = farm(client, keys[0], fid, "plant", 1)
    assert r.status_code == 200, r.text
    assert r.json()["ap"] == me["ap"] - 1

    force_ready(db_path, fid, 1)
    r = farm(client, keys[0], fid, "harvest", 1)
    assert r.status_code == 200, r.text
    assert r.json()["yielded"] == 4
    assert inventory_of(db_path, pk) == {"grain": 4}


def test_farmed_grain_is_seasonless(b6, monkeypatch):
    # Bible §7: farming never consults the season table. Pin winter, when
    # gathered grain is ×0.25 — farmed yield must still be exactly 3.
    client, keys, db_path, appmod = b6
    open_buckets(appmod, monkeypatch)
    spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    fid = build_farm(client, keys, db_path)
    set_genesis_days_ago(db_path, 42)  # (42//14)%4 == 3 → winter
    assert farm(client, keys[0], fid, "plant", 2).status_code == 200
    force_ready(db_path, fid, 2)
    r = farm(client, keys[0], fid, "harvest", 2)
    assert r.status_code == 200, r.text
    assert r.json()["yielded"] == 3


def test_unknown_farm_action_400(b6, monkeypatch):
    # till/tend are not Bible verbs — they are refused, not no-ops.
    client, keys, db_path, appmod = b6
    open_buckets(appmod, monkeypatch)
    spawn(client, keys[0])
    fid = build_farm(client, keys, db_path)
    for action in ("till", "tend", "water", ""):
        r = farm(client, keys[0], fid, action, 0)
        assert r.status_code == 400, (action, r.text)
        assert "unknown farm action" in r.json()["detail"]


def test_bad_slot_400(b6, monkeypatch):
    client, keys, db_path, appmod = b6
    open_buckets(appmod, monkeypatch)
    spawn(client, keys[0])
    fid = build_farm(client, keys, db_path)
    for slot in (4, -1):
        r = farm(client, keys[0], fid, "plant", slot)
        assert r.status_code == 400, (slot, r.text)
    r = signed_request(client, keys[0], "POST", "/world/farm",
                       {"structure_id": fid, "action": "plant"})
    assert r.status_code == 400, r.text  # slot required


def test_farm_other_agents_farm_400(b6, monkeypatch):
    client, keys, db_path, appmod = b6
    open_buckets(appmod, monkeypatch)
    spawn(client, keys[0])
    spawn(client, keys[1])
    fid = build_farm(client, keys, db_path, key_idx=0)
    r = farm(client, keys[1], fid, "plant", 0)
    assert r.status_code == 400, r.text


def test_farm_non_farm_structure_400(b6, monkeypatch):
    client, keys, db_path, appmod = b6
    open_buckets(appmod, monkeypatch)
    me = spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    assert signed_request(client, keys[0], "POST", "/world/claim",
                          {"x": me["x"], "y": me["y"]}).status_code == 200
    grant_tool(db_path, pk, "crude_pick")
    set_inventory(db_path, pk, {"stone": 4, "clay": 2, "timber": 2})
    r = signed_request(client, keys[0], "POST", "/world/build",
                       {"kind": "furnace", "x": me["x"], "y": me["y"]})
    assert r.status_code == 200, r.text
    r = farm(client, keys[0], r.json()["id"], "plant", 0)
    assert r.status_code == 400, r.text
    assert "not a farm" in r.json()["detail"]


def test_farm_insufficient_ap_402(b6, monkeypatch):
    client, keys, db_path, appmod = b6
    open_buckets(appmod, monkeypatch)
    spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    fid = build_farm(client, keys, db_path)
    conn = db(db_path)
    try:
        conn.execute(
            "UPDATE agent_world SET ap = 1 WHERE agent_id ="
            " (SELECT id FROM agents WHERE pubkey = ?)",
            (pk,),
        )
        conn.commit()
    finally:
        conn.close()
    r = farm(client, keys[0], fid, "plant", 0)
    assert r.status_code == 402, r.text


def test_harvest_at_cap_400(b6, monkeypatch):
    # At-cap harvest is 400 and consumes nothing — the crop stays growing.
    client, keys, db_path, appmod = b6
    open_buckets(appmod, monkeypatch)
    spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    fid = build_farm(client, keys, db_path)
    assert farm(client, keys[0], fid, "plant", 3).status_code == 200
    force_ready(db_path, fid, 3)
    set_inventory(db_path, pk, {"grain": 98})  # 98 + 3 > 99
    me_before = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    r = farm(client, keys[0], fid, "harvest", 3)
    assert r.status_code == 400, r.text
    assert inventory_of(db_path, pk) == {"grain": 98}
    me_after = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    assert me_after["ap"] == me_before["ap"]
    assert plot_state(db_path, fid, 3)["state"] == "growing"
