"""Bible ch.6 — farming (Systems Bible §7).

Covers: shelter plot creation (4 untilled slots), the till → plant →
tend → harvest cycle with exact AP costs (2/2/1/1), 1 grain per plant,
2-hour growth as a pure timestamp comparison (no ticks), 4 base yield /
6 with plow, harvest resetting to untilled, tending a ready crop wasting
the action, and the error cases (wrong state, bad slot, чужой shelter,
non-shelter structure, insufficient grain/AP).
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


def grain_of(db_path, pubkey):
    conn = db(db_path)
    try:
        row = conn.execute(
            "SELECT qty FROM inventories WHERE agent_pubkey = ? AND resource = 'grain'",
            (pubkey,),
        ).fetchone()
        return int(row["qty"]) if row else 0
    finally:
        conn.close()


def build_shelter(client, keys, db_path, key_idx=0):
    me = signed_request(client, keys[key_idx], "GET", "/world/me", {}).json()
    pk = pubkey_hex(keys[key_idx])
    assert signed_request(client, keys[key_idx], "POST", "/world/claim",
                          {"x": me["x"], "y": me["y"]}).status_code == 200
    set_inventory(db_path, pk, {"timber": 8})
    r = signed_request(client, keys[key_idx], "POST", "/world/build",
                       {"kind": "shelter", "x": me["x"], "y": me["y"]})
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


# ---------------------------------------------------------------- tests

def test_shelter_gets_four_untilled_plots(b6, monkeypatch):
    client, keys, db_path, appmod = b6
    monkeypatch.setitem(appmod.RATE_LIMITS, "claim", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "build", (100, 60))
    spawn(client, keys[0])
    sid = build_shelter(client, keys, db_path)
    for slot in range(4):
        plot = plot_state(db_path, sid, slot)
        assert plot is not None
        assert plot["state"] == "untilled"


def test_till_plant_tend_harvest_cycle(b6, monkeypatch):
    client, keys, db_path, appmod = b6
    monkeypatch.setitem(appmod.RATE_LIMITS, "claim", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "build", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "farm", (100, 60))
    spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    sid = build_shelter(client, keys, db_path)
    set_inventory(db_path, pk, {"grain": 1})

    me = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    r = farm(client, keys[0], sid, "till", 0)
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "tilled"
    assert r.json()["ap"] == me["ap"] - 2

    me = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    r = farm(client, keys[0], sid, "plant", 0)
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "growing"
    assert r.json()["ap"] == me["ap"] - 2
    assert grain_of(db_path, pk) == 0  # the seed grain is consumed
    plot = plot_state(db_path, sid, 0)
    assert abs(float(plot["ready_at"]) - (time.time() + 7200)) < 30

    me = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    r = farm(client, keys[0], sid, "tend", 0)
    assert r.status_code == 200, r.text
    assert r.json()["ap"] == me["ap"] - 1
    assert plot_state(db_path, sid, 0)["tended"] == 1

    # Not ready yet: harvest refuses.
    r = farm(client, keys[0], sid, "harvest", 0)
    assert r.status_code == 400, r.text
    assert "not ready" in r.json()["detail"]

    # Two hours pass (simulated) — harvest is a pure timestamp comparison.
    force_ready(db_path, sid, 0)
    me = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    r = farm(client, keys[0], sid, "harvest", 0)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["yielded"] == 4  # base yield, no plow
    assert body["state"] == "untilled"
    assert body["ap"] == me["ap"] - 1
    assert grain_of(db_path, pk) == 4
    plot = plot_state(db_path, sid, 0)
    assert plot["state"] == "untilled" and plot["tended"] == 0


def test_plow_yield_six(b6, monkeypatch):
    client, keys, db_path, appmod = b6
    monkeypatch.setitem(appmod.RATE_LIMITS, "claim", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "build", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "farm", (100, 60))
    spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    sid = build_shelter(client, keys, db_path)
    conn = db(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO tools (agent_pubkey, recipe_id, durability,"
            " max_durability, crafted_at) VALUES (?, 'plow', 300, 300, ?)",
            (pk, "2026-09-23T00:00:00Z"),
        )
        conn.commit()
    finally:
        conn.close()
    set_inventory(db_path, pk, {"grain": 1})
    assert farm(client, keys[0], sid, "till", 1).status_code == 200
    assert farm(client, keys[0], sid, "plant", 1).status_code == 200
    force_ready(db_path, sid, 1)
    r = farm(client, keys[0], sid, "harvest", 1)
    assert r.status_code == 200, r.text
    assert r.json()["yielded"] == 6
    assert grain_of(db_path, pk) == 6


def test_tend_ready_crop_wastes_action(b6, monkeypatch):
    client, keys, db_path, appmod = b6
    monkeypatch.setitem(appmod.RATE_LIMITS, "claim", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "build", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "farm", (100, 60))
    spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    sid = build_shelter(client, keys, db_path)
    set_inventory(db_path, pk, {"grain": 1})
    assert farm(client, keys[0], sid, "till", 2).status_code == 200
    assert farm(client, keys[0], sid, "plant", 2).status_code == 200
    force_ready(db_path, sid, 2)
    me = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    r = farm(client, keys[0], sid, "tend", 2)
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "ready"
    assert r.json()["ap"] == me["ap"] - 1  # AP spent...
    assert r.json()["yielded"] == 0  # ...for nothing
    assert plot_state(db_path, sid, 2)["tended"] == 0


def test_till_wrong_state_400(b6, monkeypatch):
    client, keys, db_path, appmod = b6
    monkeypatch.setitem(appmod.RATE_LIMITS, "claim", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "build", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "farm", (100, 60))
    spawn(client, keys[0])
    sid = build_shelter(client, keys, db_path)
    assert farm(client, keys[0], sid, "till", 0).status_code == 200
    r = farm(client, keys[0], sid, "till", 0)
    assert r.status_code == 400, r.text
    r = farm(client, keys[0], sid, "plant", 1)  # untilled, not tilled
    assert r.status_code == 400, r.text
    r = farm(client, keys[0], sid, "harvest", 1)  # nothing growing
    assert r.status_code == 400, r.text


def test_plant_without_grain_400(b6, monkeypatch):
    client, keys, db_path, appmod = b6
    monkeypatch.setitem(appmod.RATE_LIMITS, "claim", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "build", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "farm", (100, 60))
    spawn(client, keys[0])
    sid = build_shelter(client, keys, db_path)
    assert farm(client, keys[0], sid, "till", 0).status_code == 200
    r = farm(client, keys[0], sid, "plant", 0)
    assert r.status_code == 400, r.text
    assert "insufficient grain" in r.json()["detail"]


def test_bad_slot_400(b6, monkeypatch):
    client, keys, db_path, appmod = b6
    monkeypatch.setitem(appmod.RATE_LIMITS, "claim", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "build", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "farm", (100, 60))
    spawn(client, keys[0])
    sid = build_shelter(client, keys, db_path)
    for slot in (4, -1):
        r = farm(client, keys[0], sid, "till", slot)
        assert r.status_code == 400, (slot, r.text)
    r = signed_request(client, keys[0], "POST", "/world/farm",
                       {"structure_id": sid, "action": "till"})
    assert r.status_code == 400, r.text  # slot required


def test_farm_other_agents_shelter_400(b6, monkeypatch):
    client, keys, db_path, appmod = b6
    monkeypatch.setitem(appmod.RATE_LIMITS, "claim", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "build", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "farm", (100, 60))
    spawn(client, keys[0])
    spawn(client, keys[1])
    sid = build_shelter(client, keys, db_path, key_idx=0)
    r = farm(client, keys[1], sid, "till", 0)
    assert r.status_code == 400, r.text


def test_farm_non_shelter_400(b6, monkeypatch):
    client, keys, db_path, appmod = b6
    monkeypatch.setitem(appmod.RATE_LIMITS, "claim", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "build", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "farm", (100, 60))
    me = spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    assert signed_request(client, keys[0], "POST", "/world/claim",
                          {"x": me["x"], "y": me["y"]}).status_code == 200
    set_inventory(db_path, pk, {"stone": 10})
    r = signed_request(client, keys[0], "POST", "/world/build",
                       {"kind": "furnace", "x": me["x"], "y": me["y"]})
    assert r.status_code == 200, r.text
    fid = r.json()["id"]
    r = farm(client, keys[0], fid, "till", 0)
    assert r.status_code == 400, r.text
    assert "not a shelter" in r.json()["detail"]


def test_farm_insufficient_ap_402(b6, monkeypatch):
    client, keys, db_path, appmod = b6
    monkeypatch.setitem(appmod.RATE_LIMITS, "claim", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "build", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "farm", (100, 60))
    spawn(client, keys[0])
    pk = pubkey_hex(keys[0])
    sid = build_shelter(client, keys, db_path)
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
    r = farm(client, keys[0], sid, "till", 0)
    assert r.status_code == 402, r.text
