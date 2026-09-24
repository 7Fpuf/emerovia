"""Observer wave-2 — public read views for the Systems Bible build.

Covers the four additive read-only endpoints the wave-2 observer UI is
built against (all unsigned GETs, no world-state changes possible):
- GET /world/settlements — settlements index (spec from
  docs/observer-api-specs.md)
- GET /world/structures — structure census with farm growth stages
- GET /world/relays — relay network towers + recent relay chains
- GET /heralds/leaderboard — already public (regression cover)

Plus: new routes appear in /openapi.json and are documented in
server/static/agents.txt (docs-completeness discipline).
"""
from __future__ import annotations

import importlib
import os
import sqlite3
import sys
import time
from pathlib import Path

from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _make_client(tmp_path, monkeypatch):
    monkeypatch.setenv("AC_DB_PATH", str(tmp_path / "wave2.db"))
    monkeypatch.setenv("AC_OPERATOR_PUBKEY", "ab" * 32)
    import server.app as appmod
    importlib.reload(appmod)
    client = TestClient(appmod.app)
    return client, appmod


def _db(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "wave2.db"))
    conn.row_factory = sqlite3.Row
    return conn


def _seed_base(tmp_path):
    """Two agents, one settlement, one farm, one relay tower, one feast."""
    now = time.time()
    conn = _db(tmp_path)
    conn.execute(
        "INSERT INTO agents(id,name,pubkey,registered_at) VALUES (1,'Aporia','aa','2026-09-24T00:00:00')")
    conn.execute(
        "INSERT INTO agents(id,name,pubkey,registered_at) VALUES (2,'Vesper','bb','2026-09-24T00:00:00')")
    conn.execute(
        "INSERT INTO settlements(id,name,center_x,center_y,formed_at)"
        " VALUES (1,'Thornvale',41,22,?)", (now - 86400,))
    conn.execute("INSERT INTO settlement_stewards VALUES (1,'aa'),(1,'bb')")
    # farm: last_tithe_week far in the future = kept up
    conn.execute(
        "INSERT INTO structures(id,owner_pubkey,kind,x,y,name,raised_at,"
        " last_tithe_week,settlement_asset)"
        " VALUES (1,'aa','farm',40,22,'Green Acres','2026-09-24T00:00:00Z',999999,0)")
    # slot 0: planted 1h ago, ready in 1h (growth_pct ~50); slot 1 empty.
    conn.execute(
        "INSERT INTO farm_plots VALUES (1,0,'growing',?,?,0)",
        (now - 3600, now + 3600))
    conn.execute("INSERT INTO farm_plots VALUES (1,1,'empty',NULL,NULL,0)")
    # relay tower with a recent chain
    conn.execute(
        "INSERT INTO structures(id,owner_pubkey,kind,x,y,name,raised_at,"
        " last_tithe_week,settlement_asset)"
        " VALUES (2,'bb','relay',45,25,'Highspire','2026-09-24T00:00:00Z',999999,0)")
    conn.execute(
        "INSERT INTO voice_messages(agent_id,kind,text,send_x,send_y,"
        " tower_path,ts,signature)"
        " VALUES (2,'relay','hello world',45,25,'[{\"x\":45,\"y\":25}]',?,'sig')",
        (now - 60,))
    # feast: one active, one expired
    conn.execute("INSERT INTO feast_buffs VALUES ('aa',1,?,?)", (now - 100, now + 604700))
    conn.execute("INSERT INTO feast_buffs VALUES ('bb',1,?,?)", (now - 2000000, now - 1000))
    conn.commit()
    conn.close()
    return now


# ---------------------------------------------------------------- settlements

def test_settlements_index_empty_world(tmp_path, monkeypatch):
    client, _ = _make_client(tmp_path, monkeypatch)
    r = client.get("/world/settlements")
    assert r.status_code == 200, r.text
    assert r.json() == []  # 200, not 404


def test_settlements_index_shape(tmp_path, monkeypatch):
    client, _ = _make_client(tmp_path, monkeypatch)
    _seed_base(tmp_path)
    r = client.get("/world/settlements")
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 1
    s = rows[0]
    assert s["id"] == 1
    assert s["name"] == "Thornvale"
    assert s["center_x"] == 41 and s["center_y"] == 22
    assert s["steward_count"] == 2
    assert s["formed_at"].endswith("Z")  # ISO-8601 UTC per spec


def test_settlements_index_ordered_by_formed_at(tmp_path, monkeypatch):
    client, _ = _make_client(tmp_path, monkeypatch)
    now = _seed_base(tmp_path)
    conn = _db(tmp_path)
    conn.execute(
        "INSERT INTO settlements(id,name,center_x,center_y,formed_at)"
        " VALUES (2,'Newhaven',10,10,?)", (now,))
    conn.commit()
    conn.close()
    ids = [s["id"] for s in client.get("/world/settlements").json()]
    assert ids == [1, 2]


# ---------------------------------------------------------------- structures

def test_structures_census(tmp_path, monkeypatch):
    client, _ = _make_client(tmp_path, monkeypatch)
    _seed_base(tmp_path)
    r = client.get("/world/structures")
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 2
    farm = rows[0]
    assert farm["kind"] == "farm"
    assert farm["x"] == 40 and farm["y"] == 22
    assert farm["owner_name"] == "Aporia"
    assert farm["derelict"] is False
    assert farm["tithe_weeks_behind"] == 0
    relay = rows[1]
    assert relay["kind"] == "relay"
    assert relay["plots"] is None  # only farms carry plots


def test_structures_farm_growth_stages(tmp_path, monkeypatch):
    client, _ = _make_client(tmp_path, monkeypatch)
    _seed_base(tmp_path)
    farm = client.get("/world/structures?kind=farm").json()[0]
    plots = farm["plots"]
    assert len(plots) == 4
    p0 = plots[0]
    assert p0["state"] == "growing"
    assert 40.0 < p0["growth_pct"] < 60.0
    assert p0["ready_at"].endswith("Z")
    assert plots[1]["state"] == "empty"
    assert plots[1]["growth_pct"] == 0.0
    # slots 2,3 have no rows: read-only backfill presents them as empty
    assert plots[2]["state"] == "empty"
    assert plots[3]["state"] == "empty"


def test_structures_farm_ready_derived(tmp_path, monkeypatch):
    client, _ = _make_client(tmp_path, monkeypatch)
    now = _seed_base(tmp_path)
    conn = _db(tmp_path)
    # slot 2 planted 3h ago: ready_at passed -> derived "ready"
    conn.execute("INSERT INTO farm_plots VALUES (1,2,'growing',?,?,0)",
                 (now - 10800, now - 3600))
    conn.commit()
    conn.close()
    plots = client.get("/world/structures?kind=farm").json()[0]["plots"]
    assert plots[2]["state"] == "ready"
    assert plots[2]["growth_pct"] == 100.0


def test_structures_derelict_flag(tmp_path, monkeypatch):
    client, _ = _make_client(tmp_path, monkeypatch)
    _seed_base(tmp_path)
    conn = _db(tmp_path)
    # last_tithe_week 0 = deep in arrears -> derelict
    conn.execute("UPDATE structures SET last_tithe_week = 0 WHERE id = 2")
    conn.commit()
    conn.close()
    relay = [s for s in client.get("/world/structures").json() if s["kind"] == "relay"][0]
    assert relay["derelict"] is True
    assert relay["tithe_weeks_behind"] >= 4


def test_structures_kind_filter_validation(tmp_path, monkeypatch):
    client, _ = _make_client(tmp_path, monkeypatch)
    _seed_base(tmp_path)
    r = client.get("/world/structures?kind=farm")
    assert r.status_code == 200 and len(r.json()) == 1
    r = client.get("/world/structures?kind=bogus")
    assert r.status_code == 400


# ---------------------------------------------------------------- relays

def test_relays_network(tmp_path, monkeypatch):
    client, _ = _make_client(tmp_path, monkeypatch)
    _seed_base(tmp_path)
    r = client.get("/world/relays")
    assert r.status_code == 200, r.text
    data = r.json()
    assert len(data["towers"]) == 1
    t = data["towers"][0]
    assert t["x"] == 45 and t["y"] == 25
    assert t["owner_name"] == "Vesper"
    assert t["active"] is True
    assert len(data["chains"]) == 1
    ch = data["chains"][0]
    assert ch["sender_name"] == "Vesper"
    assert ch["tower_path"] == [{"x": 45, "y": 25}]
    assert ch["text"] == "hello world"
    assert ch["sent_at"].endswith("Z")


def test_relays_derelict_tower_inactive(tmp_path, monkeypatch):
    client, _ = _make_client(tmp_path, monkeypatch)
    _seed_base(tmp_path)
    conn = _db(tmp_path)
    conn.execute("UPDATE structures SET last_tithe_week = 0 WHERE kind = 'relay'")
    conn.commit()
    conn.close()
    towers = client.get("/world/relays").json()["towers"]
    assert towers[0]["active"] is False


def test_relays_limit_clamped(tmp_path, monkeypatch):
    client, _ = _make_client(tmp_path, monkeypatch)
    _seed_base(tmp_path)
    assert client.get("/world/relays?limit=500").status_code == 200
    assert client.get("/world/relays?limit=0").status_code == 200


# ---------------------------------------------------------------- feasts

def test_feasts_active_only_grouped(tmp_path, monkeypatch):
    client, _ = _make_client(tmp_path, monkeypatch)
    _seed_base(tmp_path)
    r = client.get("/world/feasts")
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 1  # expired buff omitted
    g = rows[0]
    assert g["settlement_id"] == 1
    assert g["settlement_name"] == "Thornvale"
    assert len(g["buffed"]) == 1  # only the active one
    assert g["buffed"][0]["agent_name"] == "Aporia"
    assert g["buffed"][0]["expires_at"].endswith("Z")


def test_feasts_empty_world(tmp_path, monkeypatch):
    client, _ = _make_client(tmp_path, monkeypatch)
    assert client.get("/world/feasts").json() == []


# ---------------------------------------------------------------- docs

def test_wave2_routes_unsigned_and_documented(tmp_path, monkeypatch):
    client, appmod = _make_client(tmp_path, monkeypatch)
    _seed_base(tmp_path)
    for path in ("/world/settlements", "/world/structures",
                 "/world/relays", "/world/feasts"):
        r = client.get(path)  # no auth headers: must not 401
        assert r.status_code != 401, path
        assert r.status_code == 200, path
    paths = client.get("/openapi.json").json()["paths"]
    for path in ("/world/settlements", "/world/structures",
                 "/world/relays", "/world/feasts"):
        assert path in paths, f"{path} missing from /openapi.json"
        assert "get" in paths[path], f"{path} missing GET in /openapi.json"
    agents_txt = (REPO / "server" / "static" / "agents.txt").read_text()
    for path in ("GET /world/settlements", "GET /world/structures",
                 "GET /world/relays", "GET /world/feasts"):
        assert path in agents_txt, f"{path} missing from agents.txt"


def test_wave2_routes_are_get_only(tmp_path, monkeypatch):
    """Wave-2 observer views must expose GET and nothing else."""
    client, appmod = _make_client(tmp_path, monkeypatch)
    for path in ("/world/settlements", "/world/structures",
                 "/world/relays", "/world/feasts"):
        route = next(rt for rt in appmod.app.routes
                     if getattr(rt, "path", None) == path)
        assert set(route.methods) == {"GET"}, f"{path}: {route.methods}"
