"""Stage 2 acceptance tests for Agent Commons.

Covers the world API contract:
  deterministic seeded terrain generation, AP costs and regeneration,
  spawning, movement rules (ocean / off-map / invalid-dir blocking,
  mountain surcharge), private discoveries vs the public map, and the
  read-only world views.

Each test gets a fresh app instance backed by an isolated SQLite file
(AC_DB_PATH -> tmp_path), reloaded via importlib -- the same pattern as
tests/test_stage1.py. The world engine module is resolved as
``server.world`` first, falling back to ``server.app``.
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
    """Deterministic keypair so tests are reproducible."""
    seed = bytes((seed_byte + i) % 256 for i in range(32))
    return SigningKey(seed)


def pubkey_hex(key: SigningKey) -> str:
    return key.verify_key.encode().hex()


def signed_headers(key: SigningKey, ts: str, method: str, path: str, body: bytes) -> dict:
    """Headers for a signed request, using the exact-bytes signing scheme."""
    msg = (ts + "\n" + method + "\n" + path + "\n" + body.decode("utf-8")).encode("utf-8")
    sig = key.sign(msg).signature.hex()
    return {
        "X-Agent-Pubkey": pubkey_hex(key),
        "X-Timestamp": ts,
        "X-Signature": sig,
        "Content-Type": "application/json",
    }


def signed_post(client: TestClient, key: SigningKey, path: str, payload: dict,
                ts: str | None = None):
    """POST with exact-bytes body; never uses the `json=` kwarg."""
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if ts is None:
        ts = str(time.time())
    headers = signed_headers(key, ts, "POST", path, body)
    return client.post(path, content=body, headers=headers)


def signed_get(client: TestClient, key: SigningKey, path: str):
    """Signed GET with an empty exact-bytes body."""
    ts = str(time.time())
    headers = signed_headers(key, ts, "GET", path, b"")
    return client.get(path, headers=headers)


def do_register(client: TestClient, name: str, pubkey: str):
    # /register is unsigned, so the json= kwarg is fine here.
    return client.post("/register", json={"name": name, "pubkey": pubkey})


def wmod():
    """World engine module: server.world preferred, server.app fallback."""
    try:
        import server.world as w
        return w
    except ImportError:
        import server.app as a  # already reloaded by the fixture
        return a


def wconst(name: str, fallback):
    return getattr(wmod(), name, fallback)


SEED = wconst("SEED_ID", "agent-commons-genesis-v1")
SIZE = wconst("WORLD_SIZE", 64)
TERRAINS = ("ocean", "plains", "forest", "desert", "mountain")
DIR_DELTAS = {"N": (0, -1), "S": (0, 1), "E": (1, 0), "W": (-1, 0)}


def terrain_map() -> dict:
    return wmod().generate_world_terrain(SEED)


@pytest.fixture()
def tworld(tmp_path, monkeypatch):
    """Fresh app + isolated SQLite DB (stage-1 fixture pattern).

    Returns (client, db_path) so tests can also poke the DB directly.
    """
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("AC_DB_PATH", db_path)
    import server.app as appmod
    importlib.reload(appmod)
    return TestClient(appmod.app), db_path


def register_spawn(client: TestClient, key: SigningKey, name: str):
    """Register an agent and spawn it; returns (agent_id, spawn_body)."""
    r = do_register(client, name, pubkey_hex(key))
    assert r.status_code == 201, r.text
    agent_id = r.json()["id"]
    r = signed_post(client, key, "/world/spawn", {})
    assert r.status_code == 201, r.text
    return agent_id, r.json()


def get_me(client: TestClient, key: SigningKey) -> dict:
    r = signed_get(client, key, "/world/me")
    assert r.status_code == 200, r.text
    return r.json()


def set_agent_state(db_path: str, agent_id: int, **fields) -> None:
    """Direct DB write to agent_world (x, y, ap, last_update)."""
    assert fields, "nothing to set"
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            f"UPDATE agent_world SET {cols} WHERE agent_id = ?",
            (*fields.values(), agent_id),
        )
        assert cur.rowcount == 1, f"no agent_world row for agent {agent_id}"
        conn.commit()
    finally:
        conn.close()


def find_land_adjacent(terrain: dict, target: str,
                       agent_avoid: tuple = ("ocean",)):
    """Find (agent_pos, target_pos, dir) with agent on land next to `target`."""
    for (tx, ty), t in terrain.items():
        if t != target:
            continue
        for d, (dx, dy) in DIR_DELTAS.items():
            ax, ay = tx - dx, ty - dy
            if 0 <= ax < SIZE and 0 <= ay < SIZE \
                    and terrain[(ax, ay)] not in agent_avoid:
                return (ax, ay), (tx, ty), d
    raise AssertionError(f"no {target!r} tile with a land neighbor exists")


def dir_toward(frm: tuple, to: tuple) -> str:
    dx, dy = to[0] - frm[0], to[1] - frm[1]
    for d, (ddx, ddy) in DIR_DELTAS.items():
        if (dx, dy) == (ddx, ddy):
            return d
    raise AssertionError(f"{frm} -> {to} is not a single step")


def try_steps(client: TestClient, key: SigningKey, n: int = 3) -> list:
    """Attempt up to n moves, routing around blocked tiles. Returns successes."""
    done = []
    for _ in range(n):
        for d in ("N", "S", "E", "W"):
            r = signed_post(client, key, "/world/move", {"dir": d})
            if r.status_code == 200:
                done.append(r.json())
                break
            assert r.status_code in (400, 402), r.text
            if r.status_code == 402:
                return done
    return done


# ---------------------------------------------------------------- tests

def test_terrain_generation_is_deterministic():
    gen = wmod().generate_world_terrain
    t1 = gen(SEED)
    t2 = gen(SEED)
    assert t1 == t2, "same seed must yield the identical map"
    assert len(t1) == SIZE * SIZE
    assert set(t1.values()) <= set(TERRAINS)
    # every tile of the 64x64 grid is present
    assert set(t1.keys()) == {(x, y) for y in range(SIZE) for x in range(SIZE)}

    t3 = gen(SEED + "-different")
    assert t3 != t1, "a different seed must yield a different map"


def test_spawn_lands_on_land_and_is_unique(tworld):
    client, _ = tworld
    key = make_key(101)

    agent_id, sp = register_spawn(client, key, "explorer")
    assert sp["agent_name"] == "explorer"
    assert sp["terrain"] != "ocean", "spawn must land on land"
    assert sp["terrain"] in TERRAINS
    assert 0 <= sp["x"] < SIZE and 0 <= sp["y"] < SIZE
    assert sp["ap"] == wconst("AP_START", 50)
    assert sp["ap_cap"] == wconst("AP_CAP", 100)
    assert sp["terrain"] == terrain_map()[(sp["x"], sp["y"])]

    # second spawn -> 409
    r = signed_post(client, key, "/world/spawn", {})
    assert r.status_code == 409, r.text

    # the spawn tile counts as the first private discovery
    me = get_me(client, key)
    assert me["private_discoveries"] == 1
    assert (me["x"], me["y"]) == (sp["x"], sp["y"])


def test_ocean_move_blocked_without_cost(tworld):
    client, db_path = tworld
    key = make_key(102)
    agent_id, sp = register_spawn(client, key, "sailor")

    terrain = terrain_map()
    agent_pos, ocean_pos, d = find_land_adjacent(terrain, "ocean")
    set_agent_state(db_path, agent_id, x=agent_pos[0], y=agent_pos[1],
                    ap=50.0, last_update=time.time())
    before = get_me(client, key)

    r = signed_post(client, key, "/world/move", {"dir": d})
    assert r.status_code == 400, r.text

    after = get_me(client, key)
    assert (after["x"], after["y"]) == (before["x"], before["y"]), \
        "blocked move must not change position"
    assert after["ap"] == before["ap"], "blocked move must not cost AP"


def test_mountain_move_costs_two_ap(tworld):
    client, db_path = tworld
    key = make_key(103)
    agent_id, sp = register_spawn(client, key, "climber")

    terrain = terrain_map()
    agent_pos, mountain_pos, d = find_land_adjacent(terrain, "mountain")
    set_agent_state(db_path, agent_id, x=agent_pos[0], y=agent_pos[1],
                    ap=50.0, last_update=time.time())
    before = get_me(client, key)

    r = signed_post(client, key, "/world/move", {"dir": d})
    assert r.status_code == 200, r.text
    mv = r.json()
    assert mv["cost"] == 2, "mountain move must cost 2 AP"
    assert mv["terrain"] == "mountain"
    assert (mv["x"], mv["y"]) == mountain_pos
    assert mv["ap"] == before["ap"] - 2, "AP must decrease by exactly 2"


def test_plain_move_costs_one_ap(tworld):
    client, db_path = tworld
    key = make_key(104)
    agent_id, sp = register_spawn(client, key, "walker")

    terrain = terrain_map()
    agent_pos, plains_pos, d = find_land_adjacent(terrain, "plains")
    set_agent_state(db_path, agent_id, x=agent_pos[0], y=agent_pos[1],
                    ap=50.0, last_update=time.time())
    ap_before = get_me(client, key)["ap"]

    r = signed_post(client, key, "/world/move", {"dir": d})
    assert r.status_code == 200, r.text
    assert r.json()["cost"] == 1

    ap_after = get_me(client, key)["ap"]
    assert ap_after == ap_before - 1, "plain move must cost exactly 1 AP"


def test_move_with_no_ap_returns_402(tworld):
    client, db_path = tworld
    key = make_key(105)
    agent_id, sp = register_spawn(client, key, "exhausted")

    terrain = terrain_map()
    agent_pos, land_pos, d = find_land_adjacent(terrain, "plains")
    set_agent_state(db_path, agent_id, x=agent_pos[0], y=agent_pos[1],
                    ap=0.0, last_update=time.time())

    r = signed_post(client, key, "/world/move", {"dir": d})
    assert r.status_code == 402, r.text
    body = r.json()
    assert "deficit" in body, "402 body must include deficit"
    assert body["deficit"] >= 1
    assert body["ap"] == 0

    me = get_me(client, key)
    assert (me["x"], me["y"]) == agent_pos, "position must be unchanged on 402"
    assert me["ap"] == 0


def test_ap_regen_pure_helper():
    regen = wmod().ap_after_regen
    now = time.time()
    # pinned example: 50 AP, cap 100, 125s elapsed -> +2 = 52
    assert regen(50, 100, now - 125, now) == 52
    # capped at cap
    assert regen(99, 100, now - 600, now) == 100
    assert regen(100, 100, now - 600, now) == 100
    # no partial-minute credit
    assert regen(50, 100, now - 59, now) == 50
    # clock skew never drops AP
    assert regen(50, 100, now + 30, now) == 50


def test_ap_regen_integration(tworld):
    client, db_path = tworld
    key = make_key(106)
    agent_id, sp = register_spawn(client, key, "sleeper")

    ap_before = get_me(client, key)["ap"]
    assert ap_before < wconst("AP_CAP", 100)

    set_agent_state(db_path, agent_id, last_update=time.time() - 61)
    me = get_me(client, key)
    assert me["ap"] == ap_before + 1, "61s of regen must grant exactly 1 AP"


def test_discovery_privacy_and_disclose(tworld):
    client, db_path = tworld
    key = make_key(107)
    agent_id, sp = register_spawn(client, key, "cartographer")
    try_steps(client, key, n=3)

    # nothing is public until disclosed
    world_map = client.get("/world/map").json()
    assert world_map["public_count"] == 0
    assert world_map["tiles"] == []

    # disclosing the spawn tile (privately discovered) works
    r = signed_post(client, key, "/world/disclose",
                    {"x": sp["x"], "y": sp["y"]})
    assert r.status_code == 200, r.text
    disc = r.json()
    assert disc["already_public"] is False
    assert (disc["x"], disc["y"]) == (sp["x"], sp["y"])
    assert disc["terrain"] == terrain_map()[(sp["x"], sp["y"])]

    world_map = client.get("/world/map").json()
    assert world_map["public_count"] == 1
    assert {"x": sp["x"], "y": sp["y"],
            "terrain": terrain_map()[(sp["x"], sp["y"])],
            "disclosed_by": "cartographer"} in world_map["tiles"]

    # disclosing a tile the agent never discovered -> 404
    ux, uy = (sp["x"] + 32) % SIZE, (sp["y"] + 32) % SIZE
    r = signed_post(client, key, "/world/disclose", {"x": ux, "y": uy})
    assert r.status_code == 404, r.text


def test_disclose_already_public_charges_once(tworld):
    client, db_path = tworld
    key = make_key(108)
    agent_id, sp = register_spawn(client, key, "herald")

    ap0 = get_me(client, key)["ap"]

    r = signed_post(client, key, "/world/disclose",
                    {"x": sp["x"], "y": sp["y"]})
    assert r.status_code == 200, r.text
    assert r.json()["already_public"] is False
    ap1 = get_me(client, key)["ap"]
    assert ap1 == ap0 - 1, "first disclose must cost 1 AP"

    r = signed_post(client, key, "/world/disclose",
                    {"x": sp["x"], "y": sp["y"]})
    assert r.status_code == 200, r.text
    assert r.json()["already_public"] is True
    ap2 = get_me(client, key)["ap"]
    assert ap2 == ap1, "re-disclose must not charge AP again"


def test_move_rejections(tworld):
    client, db_path = tworld
    key = make_key(109)
    agent_id, sp = register_spawn(client, key, "edgewalker")

    # off-map: park at the west edge, walk further west
    set_agent_state(db_path, agent_id, x=0, y=0,
                    ap=50.0, last_update=time.time())
    r = signed_post(client, key, "/world/move", {"dir": "W"})
    assert r.status_code == 400, r.text
    r = signed_post(client, key, "/world/move", {"dir": "N"})
    assert r.status_code == 400, r.text
    me = get_me(client, key)
    assert (me["x"], me["y"]) == (0, 0)

    # invalid direction
    r = signed_post(client, key, "/world/move", {"dir": "sideways"})
    assert r.status_code == 400, r.text
    r = signed_post(client, key, "/world/move", {"dir": "north"})
    assert r.status_code == 400, r.text  # vocab is N/S/E/W, not words

    # unspawned agent cannot move
    stranger = make_key(110)
    assert do_register(client, "ghost", pubkey_hex(stranger)).status_code == 201
    r = signed_post(client, stranger, "/world/move", {"dir": "N"})
    assert r.status_code == 400, r.text


def test_world_info_shape(tworld):
    client, _ = tworld
    r = client.get("/world/info")
    assert r.status_code == 200, r.text
    info = r.json()
    assert info["width"] == 64
    assert info["height"] == 64
    assert info["seed_id"] == SEED
    assert set(info["legend"].keys()) == set(TERRAINS), \
        "legend must cover all five terrains"
    rules = info["ap_rules"]
    for k in ("start", "cap", "move_cost_land", "move_cost_mountain",
              "disclose_cost"):
        assert k in rules, f"ap_rules missing {k!r}"
    assert rules["start"] == wconst("AP_START", 50)
    assert rules["cap"] == wconst("AP_CAP", 100)
    assert "agents_in_world" in info


def test_world_agents_lists_spawned(tworld):
    client, _ = tworld
    key_a, key_b = make_key(111), make_key(112)
    _, sp_a = register_spawn(client, key_a, "alice-w")
    _, sp_b = register_spawn(client, key_b, "bob-w")

    r = client.get("/world/agents")
    assert r.status_code == 200, r.text
    agents = {a["agent_name"]: a for a in r.json()}
    assert set(agents) == {"alice-w", "bob-w"}
    for name, sp in (("alice-w", sp_a), ("bob-w", sp_b)):
        a = agents[name]
        assert (a["x"], a["y"]) == (sp["x"], sp["y"])
        assert a["terrain"] == sp["terrain"]
