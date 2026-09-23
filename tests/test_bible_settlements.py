"""Bible ch.8 — settlements (Systems Bible §9).

Covers: formation (50 AP, 8-tile founder radius, land center, 20-tile
separation), joining (10 AP, 8-tile radius, stewards distinct), one-time
naming (steward-only; Atlas convention TODO flagged), treasury
contributions, two-steward disbursements (self-approval refused),
feasts (10 grain + 5 fruit → +10 AP cap 24h for all stewards,
non-stacking), collective projects on unclaimed land (escrow,
completion raises a settlement_asset structure), and the public ledger.
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
def b8(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("AC_DB_PATH", db_path)
    op_key = make_key()
    monkeypatch.setenv("AC_OPERATOR_PUBKEY", pubkey_hex(op_key))
    import server.app as appmod
    importlib.reload(appmod)
    client = TestClient(appmod.app)
    keys = [make_key() for _ in range(4)]
    for i, k in enumerate(keys):
        register(client, f"bible-settle-{i}", k)
    return client, keys, db_path, appmod


def db(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def teleport(db_path, pubkey, x, y):
    conn = db(db_path)
    try:
        conn.execute(
            "UPDATE agent_world SET x = ?, y = ? WHERE agent_id ="
            " (SELECT id FROM agents WHERE pubkey = ?)",
            (x, y, pubkey),
        )
        conn.commit()
    finally:
        conn.close()


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


def set_ap(db_path, pubkey, ap):
    conn = db(db_path)
    try:
        conn.execute(
            "UPDATE agent_world SET ap = ? WHERE agent_id ="
            " (SELECT id FROM agents WHERE pubkey = ?)",
            (float(ap), pubkey),
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


def land_tile_within(db_path, x, y, lo, hi):
    """A land tile whose Chebyshev distance from (x,y) is in [lo, hi]."""
    conn = db(db_path)
    try:
        row = conn.execute(
            "SELECT x, y FROM world_tiles WHERE terrain != 'ocean'"
            " AND MAX(ABS(x - ?), ABS(y - ?)) BETWEEN ? AND ?"
            " AND NOT (x = ? AND y = ?) LIMIT 1",
            (x, y, lo, hi, x, y),
        ).fetchone()
        assert row is not None, "no suitable land tile"
        return (row["x"], row["y"])
    finally:
        conn.close()


def form_here(client, keys, key_idx=0):
    me = signed_request(client, keys[key_idx], "GET", "/world/me", {}).json()
    r = signed_request(client, keys[key_idx], "POST", "/world/settlements/form",
                       {"x": me["x"], "y": me["y"]})
    assert r.status_code == 200, r.text
    return r.json()["id"], me


# ---------------------------------------------------------------- formation

def test_form_settlement(b8, monkeypatch):
    client, keys, db_path, appmod = b8
    monkeypatch.setitem(appmod.RATE_LIMITS, "settlements", (100, 60))
    me = spawn(client, keys[0])
    r = signed_request(client, keys[0], "POST", "/world/settlements/form",
                       {"x": me["x"], "y": me["y"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ap"] == me["ap"] - 50
    view = signed_request(client, keys[0], "GET",
                          f"/world/settlements/{body['id']}", {}).json()
    assert view["name"] is None
    assert view["center"] == {"x": me["x"], "y": me["y"]}
    assert view["stewards"] == [pubkey_hex(keys[0])]
    assert view["treasury"] == {}


def test_form_ocean_400(b8, monkeypatch):
    client, keys, db_path, appmod = b8
    monkeypatch.setitem(appmod.RATE_LIMITS, "settlements", (100, 60))
    me = spawn(client, keys[0])
    conn = db(db_path)
    try:
        ocean = conn.execute(
            "SELECT x, y FROM world_tiles WHERE terrain = 'ocean' LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    teleport(db_path, pubkey_hex(keys[0]), ocean["x"], ocean["y"])
    # Teleporting onto ocean for the test: the center check still refuses.
    r = signed_request(client, keys[0], "POST", "/world/settlements/form",
                       {"x": ocean["x"], "y": ocean["y"]})
    assert r.status_code == 400, r.text
    assert "land center" in r.json()["detail"]


def test_form_too_close_to_existing_400(b8, monkeypatch):
    client, keys, db_path, appmod = b8
    monkeypatch.setitem(appmod.RATE_LIMITS, "settlements", (100, 60))
    spawn(client, keys[0])
    sid, me = form_here(client, keys)
    # A land tile within the 8-tile founder radius is necessarily within
    # the 20-tile separation → the separation rule fires.
    tx, ty = land_tile_within(db_path, me["x"], me["y"], 1, 8)
    r = signed_request(client, keys[0], "POST", "/world/settlements/form",
                       {"x": tx, "y": ty})
    assert r.status_code == 400, r.text
    assert "too close" in r.json()["detail"]


def test_form_insufficient_ap_402(b8, monkeypatch):
    client, keys, db_path, appmod = b8
    monkeypatch.setitem(appmod.RATE_LIMITS, "settlements", (100, 60))
    me = spawn(client, keys[0])
    set_ap(db_path, pubkey_hex(keys[0]), 49)
    r = signed_request(client, keys[0], "POST", "/world/settlements/form",
                       {"x": me["x"], "y": me["y"]})
    assert r.status_code == 402, r.text


# ---------------------------------------------------------------- join + name

def test_join_settlement(b8, monkeypatch):
    client, keys, db_path, appmod = b8
    monkeypatch.setitem(appmod.RATE_LIMITS, "settlements", (100, 60))
    spawn(client, keys[0])
    spawn(client, keys[1])
    sid, me = form_here(client, keys, key_idx=0)
    teleport(db_path, pubkey_hex(keys[1]), me["x"] + 1, me["y"])
    me1 = signed_request(client, keys[1], "GET", "/world/me", {}).json()
    r = signed_request(client, keys[1], "POST", "/world/settlements/join",
                       {"settlement_id": sid})
    assert r.status_code == 200, r.text
    assert r.json()["ap"] == me1["ap"] - 10
    view = signed_request(client, keys[0], "GET",
                          f"/world/settlements/{sid}", {}).json()
    assert sorted(view["stewards"]) == sorted(
        [pubkey_hex(keys[0]), pubkey_hex(keys[1])])


def test_join_twice_400(b8, monkeypatch):
    client, keys, db_path, appmod = b8
    monkeypatch.setitem(appmod.RATE_LIMITS, "settlements", (100, 60))
    spawn(client, keys[0])
    sid, _ = form_here(client, keys, key_idx=0)
    r = signed_request(client, keys[0], "POST", "/world/settlements/join",
                       {"settlement_id": sid})
    assert r.status_code == 400, r.text
    assert "already a steward" in r.json()["detail"]


def test_join_far_400(b8, monkeypatch):
    client, keys, db_path, appmod = b8
    monkeypatch.setitem(appmod.RATE_LIMITS, "settlements", (100, 60))
    spawn(client, keys[0])
    spawn(client, keys[1])
    sid, me = form_here(client, keys, key_idx=0)
    tx, ty = land_tile_within(db_path, me["x"], me["y"], 30, 60)
    teleport(db_path, pubkey_hex(keys[1]), tx, ty)
    r = signed_request(client, keys[1], "POST", "/world/settlements/join",
                       {"settlement_id": sid})
    assert r.status_code == 400, r.text
    assert "within 8 tiles" in r.json()["detail"]


def test_name_settlement_once(b8, monkeypatch):
    client, keys, db_path, appmod = b8
    monkeypatch.setitem(appmod.RATE_LIMITS, "settlements", (100, 60))
    spawn(client, keys[0])
    spawn(client, keys[2])
    sid, me = form_here(client, keys, key_idx=0)
    # Non-steward cannot name.
    r = signed_request(client, keys[2], "POST", "/world/settlements/name",
                       {"settlement_id": sid, "name": "Hubris"})
    assert r.status_code == 400, r.text
    r = signed_request(client, keys[0], "POST", "/world/settlements/name",
                       {"settlement_id": sid, "name": "First Light"})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "First Light"
    view = signed_request(client, keys[0], "GET",
                          f"/world/settlements/{sid}", {}).json()
    assert view["name"] == "First Light"
    # Names are permanent.
    r = signed_request(client, keys[0], "POST", "/world/settlements/name",
                       {"settlement_id": sid, "name": "Second Try"})
    assert r.status_code == 400, r.text
    assert "already named" in r.json()["detail"]


# ---------------------------------------------------------------- treasury

def test_contribute_and_ledger(b8, monkeypatch):
    client, keys, db_path, appmod = b8
    monkeypatch.setitem(appmod.RATE_LIMITS, "settlements", (100, 60))
    spawn(client, keys[0])
    sid, _ = form_here(client, keys, key_idx=0)
    pk = pubkey_hex(keys[0])
    set_inventory(db_path, pk, {"grain": 20, "fruit": 7})
    r = signed_request(client, keys[0], "POST",
                       "/world/settlements/contribute",
                       {"settlement_id": sid, "item": "grain", "qty": 12})
    assert r.status_code == 200, r.text
    assert r.json()["treasury_qty"] == 12
    assert inventory_of(db_path, pk) == {"grain": 8, "fruit": 7}
    view = signed_request(client, keys[0], "GET",
                          f"/world/settlements/{sid}", {}).json()
    assert view["treasury"] == {"grain": 12}
    ledger = signed_request(client, keys[0], "GET",
                            f"/world/settlements/{sid}/ledger", {}).json()
    kinds = [e["kind"] for e in ledger["entries"]]
    assert kinds == ["formed", "contribute"]
    assert ledger["entries"][1]["qty"] == 12


def test_contribute_non_steward_400(b8, monkeypatch):
    client, keys, db_path, appmod = b8
    monkeypatch.setitem(appmod.RATE_LIMITS, "settlements", (100, 60))
    spawn(client, keys[0])
    spawn(client, keys[1])
    sid, _ = form_here(client, keys, key_idx=0)
    set_inventory(db_path, pubkey_hex(keys[1]), {"grain": 5})
    r = signed_request(client, keys[1], "POST",
                       "/world/settlements/contribute",
                       {"settlement_id": sid, "item": "grain", "qty": 5})
    assert r.status_code == 400, r.text
    assert "only settlement stewards" in r.json()["detail"]


# ---------------------------------------------------------------- disbursement

def _two_steward_settlement(client, keys, db_path):
    sid, me = form_here(client, keys, key_idx=0)
    teleport(db_path, pubkey_hex(keys[1]), me["x"] + 1, me["y"])
    r = signed_request(client, keys[1], "POST", "/world/settlements/join",
                       {"settlement_id": sid})
    assert r.status_code == 200, r.text
    return sid, me


def test_disburse_needs_two_stewards(b8, monkeypatch):
    client, keys, db_path, appmod = b8
    monkeypatch.setitem(appmod.RATE_LIMITS, "settlements", (100, 60))
    spawn(client, keys[0])
    spawn(client, keys[1])
    spawn(client, keys[2])
    sid, _ = _two_steward_settlement(client, keys, db_path)
    pk0 = pubkey_hex(keys[0])
    set_inventory(db_path, pk0, {"grain": 10})
    r = signed_request(client, keys[0], "POST",
                       "/world/settlements/contribute",
                       {"settlement_id": sid, "item": "grain", "qty": 10})
    assert r.status_code == 200, r.text
    to = pubkey_hex(keys[2])
    r = signed_request(client, keys[0], "POST",
                       "/world/settlements/disburse",
                       {"settlement_id": sid, "to_pubkey": to,
                        "item": "grain", "qty": 3})
    assert r.status_code == 200, r.text
    did = r.json()["id"]
    assert r.json()["status"] == "proposed"
    # The proposer cannot approve their own proposal.
    r = signed_request(client, keys[0], "POST",
                       "/world/settlements/disburse/approve",
                       {"disbursal_id": did})
    assert r.status_code == 400, r.text
    assert "second steward" in r.json()["detail"]
    # A distinct steward approves: treasury → recipient.
    r = signed_request(client, keys[1], "POST",
                       "/world/settlements/disburse/approve",
                       {"disbursal_id": did})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"
    assert inventory_of(db_path, to) == {"grain": 3}
    view = signed_request(client, keys[0], "GET",
                          f"/world/settlements/{sid}", {}).json()
    assert view["treasury"] == {"grain": 7}
    ledger = signed_request(client, keys[0], "GET",
                            f"/world/settlements/{sid}/ledger", {}).json()
    kinds = [e["kind"] for e in ledger["entries"]]
    assert "disburse_proposed" in kinds and "disburse_approved" in kinds


def test_disburse_insufficient_treasury_400(b8, monkeypatch):
    client, keys, db_path, appmod = b8
    monkeypatch.setitem(appmod.RATE_LIMITS, "settlements", (100, 60))
    spawn(client, keys[0])
    spawn(client, keys[1])
    spawn(client, keys[2])
    sid, _ = _two_steward_settlement(client, keys, db_path)
    set_inventory(db_path, pubkey_hex(keys[0]), {"grain": 2})
    assert signed_request(client, keys[0], "POST",
                          "/world/settlements/contribute",
                          {"settlement_id": sid, "item": "grain",
                           "qty": 2}).status_code == 200
    r = signed_request(client, keys[0], "POST",
                       "/world/settlements/disburse",
                       {"settlement_id": sid, "to_pubkey": pubkey_hex(keys[2]),
                        "item": "grain", "qty": 50})
    assert r.status_code == 200, r.text
    r = signed_request(client, keys[1], "POST",
                       "/world/settlements/disburse/approve",
                       {"disbursal_id": r.json()["id"]})
    assert r.status_code == 400, r.text
    assert "insufficient treasury" in r.json()["detail"]


# ---------------------------------------------------------------- feast

def test_feast_buffs_all_stewards(b8, monkeypatch):
    client, keys, db_path, appmod = b8
    monkeypatch.setitem(appmod.RATE_LIMITS, "settlements", (100, 60))
    spawn(client, keys[0])
    spawn(client, keys[1])
    sid, _ = _two_steward_settlement(client, keys, db_path)
    set_inventory(db_path, pubkey_hex(keys[0]), {"grain": 10, "fruit": 5})
    assert signed_request(client, keys[0], "POST",
                          "/world/settlements/contribute",
                          {"settlement_id": sid, "item": "grain",
                           "qty": 10}).status_code == 200
    assert signed_request(client, keys[0], "POST",
                          "/world/settlements/contribute",
                          {"settlement_id": sid, "item": "fruit",
                           "qty": 5}).status_code == 200
    r = signed_request(client, keys[0], "POST", "/world/settlements/feast",
                       {"settlement_id": sid})
    assert r.status_code == 200, r.text
    assert r.json()["buffed"] == 2
    for i in (0, 1):
        me = signed_request(client, keys[i], "GET", "/world/me", {}).json()
        assert me["ap_cap"] == 110, me
    view = signed_request(client, keys[0], "GET",
                          f"/world/settlements/{sid}", {}).json()
    assert view["treasury"] == {}
    # Broke treasury: no second feast.
    r = signed_request(client, keys[0], "POST", "/world/settlements/feast",
                       {"settlement_id": sid})
    assert r.status_code == 400, r.text


def test_feast_non_stacking(b8, monkeypatch):
    client, keys, db_path, appmod = b8
    monkeypatch.setitem(appmod.RATE_LIMITS, "settlements", (100, 60))
    spawn(client, keys[0])
    sid, _ = form_here(client, keys, key_idx=0)
    set_inventory(db_path, pubkey_hex(keys[0]), {"grain": 20, "fruit": 10})
    for _ in range(2):
        assert signed_request(client, keys[0], "POST",
                              "/world/settlements/contribute",
                              {"settlement_id": sid, "item": "grain",
                               "qty": 10}).status_code == 200
        assert signed_request(client, keys[0], "POST",
                              "/world/settlements/contribute",
                              {"settlement_id": sid, "item": "fruit",
                               "qty": 5}).status_code == 200
        r = signed_request(client, keys[0], "POST",
                           "/world/settlements/feast",
                           {"settlement_id": sid})
        assert r.status_code == 200, r.text
    me = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    assert me["ap_cap"] == 110  # replaced, not stacked to 120
    conn = db(db_path)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM feast_buffs WHERE agent_pubkey = ?",
            (pubkey_hex(keys[0]),),
        ).fetchone()[0]
        assert n == 1
    finally:
        conn.close()


# ---------------------------------------------------------------- projects

def _unclaimed_land_near(db_path, x, y):
    conn = db(db_path)
    try:
        row = conn.execute(
            """SELECT t.x, t.y FROM world_tiles t
               WHERE t.terrain != 'ocean'
                 AND NOT EXISTS (SELECT 1 FROM claims c WHERE c.x = t.x AND c.y = t.y)
                 AND NOT EXISTS (SELECT 1 FROM structures s WHERE s.x = t.x AND s.y = t.y)
               ORDER BY ABS(t.x - ?) + ABS(t.y - ?) LIMIT 1""",
            (x, y),
        ).fetchone()
        assert row is not None
        return (row["x"], row["y"])
    finally:
        conn.close()


def test_project_lifecycle(b8, monkeypatch):
    client, keys, db_path, appmod = b8
    monkeypatch.setitem(appmod.RATE_LIMITS, "settlements", (100, 60))
    spawn(client, keys[0])
    spawn(client, keys[1])
    sid, me = _two_steward_settlement(client, keys, db_path)
    px, py = _unclaimed_land_near(db_path, me["x"], me["y"])
    r = signed_request(client, keys[0], "POST", "/world/settlements/projects",
                       {"settlement_id": sid, "kind": "shelter",
                        "x": px, "y": py})
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    # Two stewards escrow the 8 timber together.
    set_inventory(db_path, pubkey_hex(keys[0]), {"timber": 5})
    set_inventory(db_path, pubkey_hex(keys[1]), {"timber": 3})
    for i, qty in ((0, 5), (1, 3)):
        r = signed_request(client, keys[i], "POST",
                           "/world/settlements/projects/contribute",
                           {"project_id": pid, "item": "timber", "qty": qty})
        assert r.status_code == 200, r.text
    set_ap(db_path, pubkey_hex(keys[1]), 50)  # completer pays the 8 AP
    r = signed_request(client, keys[1], "POST",
                       "/world/settlements/projects/complete",
                       {"project_id": pid})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "complete"
    struct_id = r.json()["structure_id"]
    conn = db(db_path)
    try:
        srow = conn.execute("SELECT * FROM structures WHERE id = ?",
                            (struct_id,)).fetchone()
        assert srow["kind"] == "shelter"
        assert srow["x"] == px and srow["y"] == py
        assert int(srow["settlement_asset"]) == 1
        # Escrow consumed exactly; project closed to new contributions.
        left = conn.execute(
            "SELECT COALESCE(SUM(qty), 0) FROM project_contributions"
            " WHERE project_id = ?",
            (pid,),
        ).fetchone()[0]
        assert int(left) == 0
        plots = conn.execute(
            "SELECT COUNT(*) FROM farm_plots WHERE structure_id = ?",
            (struct_id,),
        ).fetchone()[0]
        assert plots == 4
    finally:
        conn.close()
    r = signed_request(client, keys[0], "POST",
                       "/world/settlements/projects/contribute",
                       {"project_id": pid, "item": "timber", "qty": 1})
    assert r.status_code == 400, r.text


def test_project_on_claimed_land_400(b8, monkeypatch):
    client, keys, db_path, appmod = b8
    monkeypatch.setitem(appmod.RATE_LIMITS, "settlements", (100, 60))
    monkeypatch.setitem(appmod.RATE_LIMITS, "claim", (100, 60))
    spawn(client, keys[0])
    sid, me = form_here(client, keys, key_idx=0)
    set_ap(db_path, pubkey_hex(keys[0]), 50)
    r = signed_request(client, keys[0], "POST", "/world/claim",
                       {"x": me["x"], "y": me["y"]})
    assert r.status_code == 200, r.text
    r = signed_request(client, keys[0], "POST", "/world/settlements/projects",
                       {"settlement_id": sid, "kind": "mill",
                        "x": me["x"], "y": me["y"]})
    assert r.status_code == 400, r.text
    assert "unclaimed land" in r.json()["detail"]


def test_project_complete_underfunded_400(b8, monkeypatch):
    client, keys, db_path, appmod = b8
    monkeypatch.setitem(appmod.RATE_LIMITS, "settlements", (100, 60))
    spawn(client, keys[0])
    sid, me = form_here(client, keys, key_idx=0)
    px, py = _unclaimed_land_near(db_path, me["x"], me["y"])
    r = signed_request(client, keys[0], "POST", "/world/settlements/projects",
                       {"settlement_id": sid, "kind": "furnace",
                        "x": px, "y": py})
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    set_inventory(db_path, pubkey_hex(keys[0]), {"stone": 9})  # need 10
    assert signed_request(client, keys[0], "POST",
                          "/world/settlements/projects/contribute",
                          {"project_id": pid, "item": "stone",
                           "qty": 9}).status_code == 200
    set_ap(db_path, pubkey_hex(keys[0]), 50)
    r = signed_request(client, keys[0], "POST",
                       "/world/settlements/projects/complete",
                       {"project_id": pid})
    assert r.status_code == 400, r.text
    assert "not fully contributed" in r.json()["detail"]


def test_settlement_views_404(b8):
    client, keys, _, _ = b8
    spawn(client, keys[0])
    assert signed_request(client, keys[0], "GET", "/world/settlements/9999",
                          {}).status_code == 404
    assert signed_request(client, keys[0], "GET",
                          "/world/settlements/9999/ledger", {}).status_code == 404
