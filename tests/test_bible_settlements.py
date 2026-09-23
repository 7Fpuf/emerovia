"""Bible ch.5 — settlements (Systems Bible §5).

Covers: AUTOMATIC formation on build/raise (≥5 structures within
Chebyshev 8, ≥3 distinct owners; no form/join endpoints), fixed
stewards vs dynamic residents, one-time naming by the triggering agent
within 7 days (Atlas convention is social, not mechanical — any
non-empty name ≤64 chars is accepted), resident treasury contributions
(resources or chits), two-key disbursement with 7-day approval expiry,
collective projects (relay/mill/furnace/feast; residents contribute;
any steward executes; claim must be settlement-held or the executor's),
the feast as a collective project (20 food / ≥3 types → +10 AP cap for
7 days, contributors only, non-stacking), and the public ledger.
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
def sx(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("AC_DB_PATH", db_path)
    op_key = make_key()
    monkeypatch.setenv("AC_OPERATOR_PUBKEY", pubkey_hex(op_key))
    import server.app as appmod
    importlib.reload(appmod)
    client = TestClient(appmod.app)
    keys = [make_key() for _ in range(5)]
    for i, k in enumerate(keys):
        register(client, f"bible-settle-{i}", k)
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


def set_chits(db_path, pubkey, qty):
    conn = db(db_path)
    try:
        conn.execute(
            "INSERT INTO credit_balances (agent_pubkey, chits) VALUES (?, ?)"
            " ON CONFLICT(agent_pubkey) DO UPDATE SET chits = ?",
            (pubkey, qty, qty),
        )
        conn.commit()
    finally:
        conn.close()


def land_tiles_near(db_path, cx, cy, n, min_dist=1, max_dist=8):
    """n distinct land tiles within Chebyshev [min_dist, max_dist] of (cx, cy)."""
    conn = db(db_path)
    try:
        rows = conn.execute(
            "SELECT x, y FROM world_tiles WHERE terrain != 'ocean'"
            " AND MAX(ABS(x - ?), ABS(y - ?)) BETWEEN ? AND ?"
            " ORDER BY x, y LIMIT ?",
            (cx, cy, min_dist, max_dist, n),
        ).fetchall()
        assert len(rows) >= n, "not enough land tiles nearby"
        return [(r["x"], r["y"]) for r in rows]
    finally:
        conn.close()


def free_tile_near(db_path, cx, cy, min_dist=1, max_dist=7):
    """A land tile with no claim and no structure (for project tests)."""
    conn = db(db_path)
    try:
        row = conn.execute(
            "SELECT x, y FROM world_tiles WHERE terrain != 'ocean'"
            " AND MAX(ABS(x - ?), ABS(y - ?)) BETWEEN ? AND ?"
            " AND NOT EXISTS (SELECT 1 FROM claims WHERE claims.x = world_tiles.x"
            " AND claims.y = world_tiles.y)"
            " AND NOT EXISTS (SELECT 1 FROM structures WHERE structures.x = world_tiles.x"
            " AND structures.y = world_tiles.y)"
            " ORDER BY x, y LIMIT 1",
            (cx, cy, min_dist, max_dist),
        ).fetchone()
        assert row is not None, "no free tile nearby"
        return (row["x"], row["y"])
    finally:
        conn.close()


def plant_claim_only(db_path, pubkey, x, y):
    conn = db(db_path)
    try:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        conn.execute(
            "INSERT OR IGNORE INTO claims (x, y, owner_pubkey, claimed_at)"
            " VALUES (?, ?, ?, ?)",
            (x, y, pubkey, now),
        )
        conn.commit()
    finally:
        conn.close()


def plant_structure(db_path, pubkey, x, y, kind="shelter"):
    """Insert a claim + structure directly (test setup)."""
    plant_claim_only(db_path, pubkey, x, y)
    conn = db(db_path)
    try:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        cur = conn.execute(
            "INSERT INTO structures (owner_pubkey, kind, x, y, raised_at,"
            " last_tithe_week, settlement_asset)"
            " VALUES (?, ?, ?, ?, ?, ?, 0)",
            (pubkey, kind, x, y, now, int(time.time() // 604800)),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def raise_via_http(client, key, kind, x, y):
    r = signed_request(client, key, "POST", "/world/build",
                       {"kind": kind, "x": x, "y": y})
    assert r.status_code == 200, r.text
    return r.json()


def list_settlements(db_path):
    conn = db(db_path)
    try:
        return conn.execute("SELECT * FROM settlements").fetchall()
    finally:
        conn.close()


def form_settlement(client, keys, db_path, n_structures=5, n_owners=3,
                    expect_form=True):
    """Form a settlement via the automatic path.

    Picks a trigger tile near spawn, plants n_structures-1 shelters on
    tiles clustered around the TRIGGER tile (so all are within radius
    8 of it), then raises the last one over HTTP (triggering detection).
    Returns (settlement_id, center, owner_pubkeys).
    """
    for k in keys[:n_owners]:
        spawn(client, k)
    me = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    cx, cy = me["x"], me["y"]
    tx, ty = land_tiles_near(db_path, cx, cy, 1, 1, 7)[0]
    others = [t for t in land_tiles_near(db_path, tx, ty, n_structures, 1, 7)
              if t != (tx, ty)][:n_structures - 1]
    assert len(others) == n_structures - 1, "not enough tiles near trigger"
    pubs = [pubkey_hex(k) for k in keys[:n_owners]]
    for i, (x, y) in enumerate(others):
        plant_structure(db_path, pubs[i % n_owners], x, y)
    trigger = keys[(n_structures - 1) % n_owners]
    tpub = pubkey_hex(trigger)
    plant_claim_only(db_path, tpub, tx, ty)
    set_inventory(db_path, tpub, {"timber": 10, "fiber": 10})
    set_ap(db_path, tpub, 100)
    raise_via_http(client, trigger, "shelter", tx, ty)
    settlements = list_settlements(db_path)
    if expect_form:
        assert len(settlements) == 1, "expected exactly one settlement to form"
        s = settlements[0]
        return s["id"], (tx, ty), pubs
    return None, (tx, ty), pubs


def settlement_view(client, keys, sid, key_idx=0):
    r = signed_request(client, keys[key_idx], "GET",
                       f"/world/settlements/{sid}", {})
    assert r.status_code == 200, r.text
    return r.json()


def contribute(client, key, sid, item, qty):
    return signed_request(client, key, "POST", "/world/settlements/contribute",
                          {"settlement_id": sid, "item": item, "qty": qty})


# ---------------------------------------------------------------- formation

def test_auto_formation_on_fifth_raise(sx):
    client, keys, db_path, appmod = sx
    sid, center, pubs = form_settlement(client, keys, db_path)
    view = settlement_view(client, keys, sid)
    assert view["center"] == {"x": center[0], "y": center[1]}
    assert sorted(view["stewards"]) == sorted(pubs[:3])
    assert sorted(view["residents"]) == sorted(pubs[:3])
    assert view["name"] is None
    assert view["triggered_by"] == pubs[(5 - 1) % 3]
    assert view["name_window_ends"] is not None
    assert view["name_window_ends"] > time.time()


def test_formation_needs_five_structures(sx):
    client, keys, db_path, appmod = sx
    form_settlement(client, keys, db_path, n_structures=4, n_owners=3,
                    expect_form=False)
    assert list_settlements(db_path) == []


def test_formation_needs_three_owners(sx):
    client, keys, db_path, appmod = sx
    form_settlement(client, keys, db_path, n_structures=5, n_owners=2,
                    expect_form=False)
    assert list_settlements(db_path) == []


def test_formation_radius_bounded(sx):
    client, keys, db_path, appmod = sx
    for k in keys[:3]:
        spawn(client, k)
    me = signed_request(client, keys[0], "GET", "/world/me", {}).json()
    cx, cy = me["x"], me["y"]
    pubs = [pubkey_hex(k) for k in keys[:3]]
    tx, ty = land_tiles_near(db_path, cx, cy, 1, 1, 7)[0]
    near = [t for t in land_tiles_near(db_path, tx, ty, 4, 1, 7)
            if t != (tx, ty)][:3]
    assert len(near) == 3
    # One structure 20+ tiles out: even with 5 structures / 3 owners, no form.
    far = land_tiles_near(db_path, tx, ty, 1, 20, 30)[0]
    plant_structure(db_path, pubs[0], *near[0])
    plant_structure(db_path, pubs[1], *near[1])
    plant_structure(db_path, pubs[2], *near[2])
    plant_structure(db_path, pubs[0], *far)
    plant_claim_only(db_path, pubs[1], tx, ty)
    set_inventory(db_path, pubs[1], {"timber": 10, "fiber": 10})
    set_ap(db_path, pubs[1], 100)
    raise_via_http(client, keys[1], "shelter", tx, ty)
    assert list_settlements(db_path) == []


def test_no_manual_form_or_join_routes(sx):
    client, keys, db_path, appmod = sx
    spawn(client, keys[0])
    r = signed_request(client, keys[0], "POST", "/world/settlements/form",
                       {"x": 1, "y": 1})
    assert r.status_code in (404, 405)
    r = signed_request(client, keys[0], "POST", "/world/settlements/join",
                       {"settlement_id": 1})
    assert r.status_code in (404, 405)


def test_no_direct_feast_route(sx):
    client, keys, db_path, appmod = sx
    spawn(client, keys[0])
    r = signed_request(client, keys[0], "POST", "/world/settlements/feast",
                       {"settlement_id": 1})
    assert r.status_code in (404, 405)


def test_stewards_fixed_residents_dynamic(sx):
    client, keys, db_path, appmod = sx
    sid, center, pubs = form_settlement(client, keys, db_path)
    spawn(client, keys[3])
    fourth = pubkey_hex(keys[3])
    tile = free_tile_near(db_path, center[0], center[1])
    plant_structure(db_path, fourth, *tile)
    view = settlement_view(client, keys, sid)
    assert sorted(view["stewards"]) == sorted(pubs[:3])
    assert fourth in view["residents"]
    conn = db(db_path)
    try:
        conn.execute("DELETE FROM structures WHERE owner_pubkey = ?",
                     (pubs[0],))
        conn.commit()
    finally:
        conn.close()
    view = settlement_view(client, keys, sid)
    assert pubs[0] not in view["residents"]
    assert pubs[0] in view["stewards"]


def test_overlap_suppresses_second_settlement(sx):
    """Judgment call: a second formation whose center lands within
    radius 8 of an existing center is suppressed (Bible is silent)."""
    client, keys, db_path, appmod = sx
    sid, center, pubs = form_settlement(client, keys, db_path)
    for k in keys[2:5]:
        try:
            spawn(client, k)
        except AssertionError:
            pass  # keys[2] already spawned by form_settlement
    pubs2 = [pubkey_hex(k) for k in keys[2:5]]
    # Second cluster: trigger within 7 of the first center (so overlap
    # suppression fires), other structures clustered around the trigger.
    # All tiles must be free of existing structures/claims.
    conn = db(db_path)
    try:
        cand = conn.execute(
            "SELECT x, y FROM world_tiles WHERE terrain != 'ocean'"
            " AND MAX(ABS(x - ?), ABS(y - ?)) BETWEEN 4 AND 7"
            " AND NOT EXISTS (SELECT 1 FROM claims WHERE claims.x = world_tiles.x"
            " AND claims.y = world_tiles.y)"
            " AND NOT EXISTS (SELECT 1 FROM structures WHERE structures.x = world_tiles.x"
            " AND structures.y = world_tiles.y)"
            " ORDER BY x, y LIMIT 1",
            (center[0], center[1]),
        ).fetchone()
        assert cand is not None, "no free tile near first settlement"
        tx2, ty2 = cand["x"], cand["y"]
    finally:
        conn.close()
    others2 = []
    for _ in range(4):
        x, y = free_tile_near(db_path, tx2, ty2)
        plant_claim_only(db_path, "reserve", x, y)  # hold the tile
        others2.append((x, y))
    conn = db(db_path)
    try:
        conn.execute("DELETE FROM claims WHERE owner_pubkey = 'reserve'")
        conn.commit()
    finally:
        conn.close()
    for i, (x, y) in enumerate(others2):
        plant_structure(db_path, pubs2[i % 3], x, y)
    plant_claim_only(db_path, pubs2[0], tx2, ty2)
    set_inventory(db_path, pubs2[0], {"timber": 10, "fiber": 10})
    set_ap(db_path, pubs2[0], 100)
    raise_via_http(client, keys[2], "shelter", tx2, ty2)
    assert len(list_settlements(db_path)) == 1


# ---------------------------------------------------------------- naming

def name_settlement(client, key, sid, name):
    return signed_request(client, key, "POST", "/world/settlements/name",
                          {"settlement_id": sid, "name": name})


def test_triggering_agent_names(sx):
    client, keys, db_path, appmod = sx
    sid, center, pubs = form_settlement(client, keys, db_path)
    trigger_idx = (5 - 1) % 3
    r = name_settlement(client, keys[trigger_idx], sid, "Glasshaven")
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Glasshaven"
    assert settlement_view(client, keys, sid)["name"] == "Glasshaven"


def test_non_trigger_cannot_name(sx):
    client, keys, db_path, appmod = sx
    sid, center, pubs = form_settlement(client, keys, db_path)
    trigger_idx = (5 - 1) % 3
    other = (trigger_idx + 1) % 3  # a fellow steward, but not the trigger
    r = name_settlement(client, keys[other], sid, "Othername")
    assert r.status_code == 400, r.text


def test_naming_window_expiry(sx):
    client, keys, db_path, appmod = sx
    sid, center, pubs = form_settlement(client, keys, db_path)
    trigger_idx = (5 - 1) % 3
    conn = db(db_path)
    try:
        conn.execute("UPDATE settlements SET name_window_ends = ? WHERE id = ?",
                     (time.time() - 1, sid))
        conn.commit()
    finally:
        conn.close()
    r = name_settlement(client, keys[trigger_idx], sid, "Late Name")
    assert r.status_code == 400, r.text
    assert "governance" in r.json()["detail"]


def test_naming_once(sx):
    client, keys, db_path, appmod = sx
    sid, center, pubs = form_settlement(client, keys, db_path)
    trigger_idx = (5 - 1) % 3
    r = name_settlement(client, keys[trigger_idx], sid, "Firstname")
    assert r.status_code == 200, r.text
    r = name_settlement(client, keys[trigger_idx], sid, "Secondname")
    assert r.status_code == 400, r.text


def test_naming_accepts_any_content(sx):
    """Atlas convention is SOCIAL (proposal #3, Vesper): the endpoint
    enforces only trigger + window + length — no content policing, so a
    possessive name the social convention discourages is still accepted."""
    client, keys, db_path, appmod = sx
    sid, center, pubs = form_settlement(client, keys, db_path)
    trigger_idx = (5 - 1) % 3
    r = name_settlement(client, keys[trigger_idx], sid, "Vesper's Desert")
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Vesper's Desert"


# ---------------------------------------------------------------- treasury

def test_resident_can_contribute(sx):
    client, keys, db_path, appmod = sx
    sid, center, pubs = form_settlement(client, keys, db_path)
    # pubs[0] is a steward; make keys[3] a non-steward resident.
    spawn(client, keys[3])
    fourth = pubkey_hex(keys[3])
    tile = free_tile_near(db_path, center[0], center[1])
    plant_structure(db_path, fourth, *tile)
    set_inventory(db_path, fourth, {"timber": 10})
    r = contribute(client, keys[3], sid, "timber", 4)
    assert r.status_code == 200, r.text
    assert r.json()["treasury_qty"] == 4
    view = settlement_view(client, keys, sid)
    assert view["treasury"] == {"timber": 4}


def test_nonresident_cannot_contribute(sx):
    client, keys, db_path, appmod = sx
    sid, center, pubs = form_settlement(client, keys, db_path)
    spawn(client, keys[3])
    fourth = pubkey_hex(keys[3])
    set_inventory(db_path, fourth, {"timber": 10})
    r = contribute(client, keys[3], sid, "timber", 4)
    assert r.status_code == 400, r.text


def test_contribute_chits(sx):
    client, keys, db_path, appmod = sx
    sid, center, pubs = form_settlement(client, keys, db_path)
    set_chits(db_path, pubs[0], 100)
    r = contribute(client, keys[0], sid, "chits", 25)
    assert r.status_code == 200, r.text
    assert r.json()["treasury_qty"] == 25
    conn = db(db_path)
    try:
        bal = conn.execute("SELECT chits FROM credit_balances WHERE agent_pubkey = ?",
                           (pubs[0],)).fetchone()["chits"]
    finally:
        conn.close()
    assert bal == 75


def test_disburse_two_keys(sx):
    client, keys, db_path, appmod = sx
    sid, center, pubs = form_settlement(client, keys, db_path)
    set_inventory(db_path, pubs[0], {"grain": 20})
    r = contribute(client, keys[0], sid, "grain", 20)
    assert r.status_code == 200, r.text
    r = signed_request(client, keys[0], "POST", "/world/settlements/disburse",
                       {"settlement_id": sid, "to_pubkey": pubs[1],
                        "item": "grain", "qty": 7})
    assert r.status_code == 200, r.text
    did = r.json()["id"]
    # Self-approval refused.
    r = signed_request(client, keys[0], "POST",
                       "/world/settlements/disburse/approve",
                       {"disbursal_id": did})
    assert r.status_code == 400, r.text
    # A different steward approves: resources move.
    r = signed_request(client, keys[1], "POST",
                       "/world/settlements/disburse/approve",
                       {"disbursal_id": did})
    assert r.status_code == 200, r.text
    view = settlement_view(client, keys, sid)
    assert view["treasury"] == {"grain": 13}
    conn = db(db_path)
    try:
        got = conn.execute("SELECT qty FROM inventories WHERE agent_pubkey = ?"
                           " AND resource = 'grain'", (pubs[1],)).fetchone()["qty"]
    finally:
        conn.close()
    assert got == 7


def test_disburse_approval_expires(sx):
    client, keys, db_path, appmod = sx
    sid, center, pubs = form_settlement(client, keys, db_path)
    set_inventory(db_path, pubs[0], {"grain": 20})
    assert contribute(client, keys[0], sid, "grain", 20).status_code == 200
    r = signed_request(client, keys[0], "POST", "/world/settlements/disburse",
                       {"settlement_id": sid, "to_pubkey": pubs[1],
                        "item": "grain", "qty": 7})
    did = r.json()["id"]
    conn = db(db_path)
    try:
        conn.execute("UPDATE settlement_disbursals SET proposed_at = ? WHERE id = ?",
                     (time.time() - 8 * 86400, did))
        conn.commit()
    finally:
        conn.close()
    r = signed_request(client, keys[1], "POST",
                       "/world/settlements/disburse/approve",
                       {"disbursal_id": did})
    assert r.status_code == 400, r.text
    assert "7 days" in r.json()["detail"]


# ---------------------------------------------------------------- projects

def create_project(client, key, sid, kind, x, y):
    return signed_request(client, key, "POST", "/world/settlements/projects",
                          {"settlement_id": sid, "kind": kind, "x": x, "y": y})


def test_project_kinds(sx):
    client, keys, db_path, appmod = sx
    sid, center, pubs = form_settlement(client, keys, db_path)
    for kind in ("relay", "mill", "furnace", "feast"):
        x, y = free_tile_near(db_path, center[0], center[1])
        plant_claim_only(db_path, pubs[0], x, y)
        r = create_project(client, keys[0], sid, kind, x, y)
        assert r.status_code == 200, (kind, r.text)
    x, y = free_tile_near(db_path, center[0], center[1])
    plant_claim_only(db_path, pubs[0], x, y)
    r = create_project(client, keys[0], sid, "shelter", x, y)
    assert r.status_code == 400, r.text


def test_project_tile_rules(sx):
    client, keys, db_path, appmod = sx
    sid, center, pubs = form_settlement(client, keys, db_path)
    # Unclaimed tile → 400.
    x, y = free_tile_near(db_path, center[0], center[1])
    r = create_project(client, keys[0], sid, "mill", x, y)
    assert r.status_code == 400, r.text
    # Claimed by an outsider → 400.
    spawn(client, keys[3])
    outsider = pubkey_hex(keys[3])
    plant_claim_only(db_path, outsider, x, y)
    r = create_project(client, keys[0], sid, "mill", x, y)
    assert r.status_code == 400, r.text
    # Outside the settlement radius → 400.
    fx, fy = land_tiles_near(db_path, center[0], center[1], 1, 20, 30)[0]
    plant_claim_only(db_path, pubs[0], fx, fy)
    r = create_project(client, keys[0], sid, "mill", fx, fy)
    assert r.status_code == 400, r.text


def fund_project(client, key, pid, items: dict):
    for item, qty in items.items():
        r = signed_request(client, key, "POST",
                           "/world/settlements/projects/contribute",
                           {"project_id": pid, "item": item, "qty": qty})
        assert r.status_code == 200, (item, r.text)


def test_project_complete_build(sx):
    client, keys, db_path, appmod = sx
    sid, center, pubs = form_settlement(client, keys, db_path)
    x, y = free_tile_near(db_path, center[0], center[1])
    plant_claim_only(db_path, pubs[0], x, y)
    pid = create_project(client, keys[0], sid, "relay", x, y).json()["id"]
    # Residents (not just stewards) contribute.
    spawn(client, keys[3])
    fourth = pubkey_hex(keys[3])
    tile = free_tile_near(db_path, center[0], center[1])
    plant_structure(db_path, fourth, *tile)
    set_inventory(db_path, pubs[0], {"lumber": 6, "copper": 2, "glass": 2})
    set_inventory(db_path, fourth, {"fiber": 2})
    fund_project(client, keys[0], pid, {"lumber": 6, "copper": 2, "glass": 2})
    fund_project(client, keys[3], pid, {"fiber": 2})
    set_ap(db_path, pubs[1], 100)  # steward 1 executes
    r = signed_request(client, keys[1], "POST",
                       "/world/settlements/projects/complete",
                       {"project_id": pid})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "complete"
    assert body["ap"] == 92  # relay = 8 AP
    conn = db(db_path)
    try:
        st = conn.execute("SELECT * FROM structures WHERE id = ?",
                          (body["structure_id"],)).fetchone()
    finally:
        conn.close()
    assert st["kind"] == "relay"
    assert st["owner_pubkey"] == pubs[1]
    assert st["settlement_asset"] == 1


def test_project_complete_claim_must_be_steward_or_executor(sx):
    """Bible §5.4: the raise tile's claim must be settlement-held (read
    as a steward's) or the executor's. A resident non-steward's claim
    fails completion."""
    client, keys, db_path, appmod = sx
    sid, center, pubs = form_settlement(client, keys, db_path)
    spawn(client, keys[3])
    fourth = pubkey_hex(keys[3])
    ftile = free_tile_near(db_path, center[0], center[1])
    plant_structure(db_path, fourth, *ftile)  # fourth is a resident
    x, y = free_tile_near(db_path, center[0], center[1])
    plant_claim_only(db_path, fourth, x, y)
    pid = create_project(client, keys[3], sid, "furnace", x, y).json()["id"]
    set_inventory(db_path, pubs[0],
                  {"stone": 4, "clay": 2, "timber": 2})
    fund_project(client, keys[0], pid, {"stone": 4, "clay": 2, "timber": 2})
    set_ap(db_path, pubs[0], 100)
    r = signed_request(client, keys[0], "POST",
                       "/world/settlements/projects/complete",
                       {"project_id": pid})
    assert r.status_code == 400, r.text
    assert "settlement-held" in r.json()["detail"]


def test_feast_project_buffs_contributors(sx):
    client, keys, db_path, appmod = sx
    sid, center, pubs = form_settlement(client, keys, db_path)
    x, y = free_tile_near(db_path, center[0], center[1])
    plant_claim_only(db_path, pubs[0], x, y)
    pid = create_project(client, keys[0], sid, "feast", x, y).json()["id"]
    # Non-food is refused for feasts.
    r = signed_request(client, keys[0], "POST",
                       "/world/settlements/projects/contribute",
                       {"project_id": pid, "item": "timber", "qty": 5})
    assert r.status_code == 400, r.text
    # Two contributors, three food types, 20 units.
    spawn(client, keys[3])
    fourth = pubkey_hex(keys[3])
    ftile = free_tile_near(db_path, center[0], center[1])
    plant_structure(db_path, fourth, *ftile)
    set_inventory(db_path, pubs[0], {"grain": 10, "fruit": 4})
    set_inventory(db_path, fourth, {"flour": 6})
    fund_project(client, keys[0], pid, {"grain": 10, "fruit": 4})
    fund_project(client, keys[3], pid, {"flour": 6})
    before = time.time()
    r = signed_request(client, keys[1], "POST",
                       "/world/settlements/projects/complete",
                       {"project_id": pid})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["buffed"] == 2
    assert body["skipped_active_buff"] == 0
    assert before + 7 * 86400 - 5 <= body["expires_at"] <= time.time() + 7 * 86400
    # Both contributors hold the buff; the non-contributing steward does not.
    conn = db(db_path)
    try:
        for pk in (pubs[0], fourth):
            row = conn.execute("SELECT * FROM feast_buffs WHERE agent_pubkey = ?",
                               (pk,)).fetchone()
            assert row is not None, pk
        assert conn.execute("SELECT 1 FROM feast_buffs WHERE agent_pubkey = ?",
                            (pubs[1],)).fetchone() is None
        # Escrowed food is consumed.
        left = conn.execute("SELECT COALESCE(SUM(qty),0) FROM project_contributions"
                            " WHERE project_id = ?", (pid,)).fetchone()[0]
        assert int(left) == 0
    finally:
        conn.close()


def test_feast_nonstacking(sx):
    """A contributor who already holds an active feast buff keeps it —
    no refresh, no second buff."""
    client, keys, db_path, appmod = sx
    sid, center, pubs = form_settlement(client, keys, db_path)
    x, y = free_tile_near(db_path, center[0], center[1])
    plant_claim_only(db_path, pubs[0], x, y)
    pid = create_project(client, keys[0], sid, "feast", x, y).json()["id"]
    set_inventory(db_path, pubs[0], {"grain": 10, "fruit": 5, "herbs": 5})
    fund_project(client, keys[0], pid, {"grain": 10, "fruit": 5, "herbs": 5})
    # pubs[2] already feasting: active buff granted an hour ago.
    conn = db(db_path)
    try:
        conn.execute(
            "INSERT INTO feast_buffs (agent_pubkey, settlement_id, granted_at,"
            " expires_at) VALUES (?, ?, ?, ?)",
            (pubs[2], sid, time.time() - 3600, time.time() + 7 * 86400 - 3600),
        )
        conn.commit()
        old_expires = conn.execute(
            "SELECT expires_at FROM feast_buffs WHERE agent_pubkey = ?",
            (pubs[2],)).fetchone()["expires_at"]
    finally:
        conn.close()
    # pubs[2] contributes too (20 units / 3 types already met, add more).
    set_inventory(db_path, pubs[2], {"grain": 5})
    fund_project(client, keys[2], pid, {"grain": 5})
    r = signed_request(client, keys[1], "POST",
                       "/world/settlements/projects/complete",
                       {"project_id": pid})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["buffed"] == 1
    assert body["skipped_active_buff"] == 1
    conn = db(db_path)
    try:
        new_expires = conn.execute(
            "SELECT expires_at FROM feast_buffs WHERE agent_pubkey = ?",
            (pubs[2],)).fetchone()["expires_at"]
    finally:
        conn.close()
    assert new_expires == old_expires


def test_feast_needs_20_units_3_types(sx):
    client, keys, db_path, appmod = sx
    sid, center, pubs = form_settlement(client, keys, db_path)
    x, y = free_tile_near(db_path, center[0], center[1])
    plant_claim_only(db_path, pubs[0], x, y)
    pid = create_project(client, keys[0], sid, "feast", x, y).json()["id"]
    set_inventory(db_path, pubs[0], {"grain": 30})
    fund_project(client, keys[0], pid, {"grain": 20})  # 20 units, 1 type
    r = signed_request(client, keys[1], "POST",
                       "/world/settlements/projects/complete",
                       {"project_id": pid})
    assert r.status_code == 400, r.text


def test_project_ledger_records(sx):
    client, keys, db_path, appmod = sx
    sid, center, pubs = form_settlement(client, keys, db_path)
    x, y = free_tile_near(db_path, center[0], center[1])
    plant_claim_only(db_path, pubs[0], x, y)
    pid = create_project(client, keys[0], sid, "furnace", x, y).json()["id"]
    set_inventory(db_path, pubs[0], {"stone": 4, "clay": 2, "timber": 2})
    fund_project(client, keys[0], pid, {"stone": 4, "clay": 2, "timber": 2})
    set_ap(db_path, pubs[0], 100)
    r = signed_request(client, keys[0], "POST",
                       "/world/settlements/projects/complete",
                       {"project_id": pid})
    assert r.status_code == 200, r.text
    r = signed_request(client, keys[0], "GET",
                       f"/world/settlements/{sid}/ledger", {})
    kinds = [e["kind"] for e in r.json()["entries"]]
    assert "project_created" in kinds
    assert "project_contributed" in kinds
    assert "project_completed" in kinds
