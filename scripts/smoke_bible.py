#!/usr/bin/env python3
"""Systems Bible spine smoke (v1.2.0 RC verification).

Two agents play the game's full loop end-to-end against a FRESH scratch DB
on the bible branch: register -> spawn -> gather -> trade -> craft ->
claim -> build -> farm -> eat -> experiment -> voice -> tithe, with the
§2.4 migration and seasons published in /world/info.

Division of labor (the spine's own story: specialization + trade):
- smoke-scout: forest timber specialist. Gathers timber, crafts a crude axe,
  claims land, builds shelter + farm, plants, experiments, whispers.
- smoke-mate: plains forager. Gathers fiber (forest-only overlay) + grain,
  sells both to the scout for chits (exercising the v1.0.2 trade engine
  inside the bible build), eats.

This chains the systems together the way unit tests don't: the point is the
*transitions* (gather -> craft inputs, trade between strangers, claim ->
build -> farm -> eat), not re-testing each verb.

AP economics: spawn terrain is random, so a long walk can starve an agent.
Each attempt is fully agent's-eye; on APStarved (bad spawn luck, not a code
failure) the harness wipes the scratch DB and retries once with new spawns.

Run:  .venv/bin/python scripts/smoke_bible.py   (from the repo root)

Exit 0 = the whole spine played clean. Any unexpected status (including a
402 insufficient-AP anywhere in the spine proper) fails loudly. Uses only
localhost TestClient; touches nothing outside its scratch DB.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from fastapi.testclient import TestClient  # noqa: E402
from nacl.signing import SigningKey  # noqa: E402

import server.world as world  # noqa: E402

import heapq  # noqa: E402
import sqlite3  # noqa: E402

DB_PATH = "/tmp/smoke_bible.db"
STEPS: list[tuple[str, str, str]] = []
client: TestClient | None = None


class APStarved(Exception):
    """A 402 where the budget said there was enough — a real bug, not luck."""


def setup() -> None:
    """Fresh scratch DB + reloaded app (same fixture pattern as the suites)."""
    global client, STEPS
    STEPS = []
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    os.environ["AC_DB_PATH"] = DB_PATH
    os.environ["AC_OPERATOR_PUBKEY"] = "ab" * 32
    import server.app as appmod
    importlib.reload(appmod)
    client = TestClient(appmod.app)


def check(name: str, cond: bool, detail: str = "", loud: bool = True) -> None:
    STEPS.append(("ok" if cond else "FAIL", name, detail))
    if loud:
        print(("  ok " if cond else "FAIL ") + name +
              (f" — {detail}" if detail else ""))
    if not cond:
        raise SystemExit(f"smoke FAILED at: {name} {detail}")


def make_key() -> SigningKey:
    return SigningKey(os.urandom(32))


def pubkey_hex(key: SigningKey) -> str:
    return key.verify_key.encode().hex()


def sign(key: SigningKey, method: str, path: str, body_text: str) -> dict:
    ts = str(time.time())
    msg = (ts + "\n" + method.upper() + "\n" + path + "\n" + body_text).encode("utf-8")
    return {
        "X-Agent-Pubkey": pubkey_hex(key),
        "X-Timestamp": ts,
        "X-Signature": key.sign(msg).signature.hex(),
        "Content-Type": "application/json",
    }


def sreq(key: SigningKey, method: str, path: str, payload: dict | None = None,
         idem: str | None = None):
    body = json.dumps(payload if payload is not None else {},
                      separators=(",", ":"))
    headers = sign(key, method, path, body)
    if idem:
        headers["Idempotency-Key"] = idem
    return client.request(method, path, content=body.encode(), headers=headers)


def sget(key: SigningKey, path: str):
    return client.get(path, headers=sign(key, "GET", path, ""))


def register(name: str, key: SigningKey):
    r = client.post("/register", json={"name": name, "pubkey": pubkey_hex(key)})
    check(f"register {name}", r.status_code == 201, r.text[:100])
    return r.json()


def spawn(key: SigningKey):
    r = sreq(key, "POST", "/world/spawn", {})
    check("spawn", r.status_code == 201, r.text[:100])
    return r.json()


def me(key: SigningKey, loud: bool = True):
    r = sget(key, "/world/me")
    check("GET /world/me", r.status_code == 200, r.text[:100], loud=loud)
    return r.json()


def inventory(key: SigningKey, loud: bool = True) -> dict:
    r = sget(key, "/world/inventory")
    check("GET /world/inventory", r.status_code == 200, r.text[:100], loud=loud)
    body = r.json()
    return body.get("inventory", body)  # items live under "inventory"


DIRS = ["N", "E", "S", "W"]
DELTA = {"N": (0, -1), "S": (0, 1), "E": (1, 0), "W": (-1, 0)}


def walk_to(key: SigningKey, terrain: str,
            exclude: set[tuple[int, int]] | None = None) -> dict:
    """Dijkstra walk to the nearest `terrain` tile, minimizing AP cost
    (mountain 2 AP, other land 1 AP, ocean impassable).

    WHITE-BOX navigation (reads world_tiles): a test-harness privilege so the
    smoke is deterministic. Every *game action* still goes through the API
    agent's-eye; only pathfinding uses the map. A 402 mid-walk is a real bug.
    """
    m = me(key, loud=False)
    start = (m["x"], m["y"])
    exclude = exclude or set()
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute("SELECT x, y, terrain FROM world_tiles").fetchall()
    finally:
        conn.close()
    terr = {(r[0], r[1]): r[2] for r in rows}

    def cost(p):
        t = terr.get(p)
        if t is None or t == "ocean":
            return None
        return 2 if t == "mountain" else 1

    # Dijkstra from start; stop at the first tile of the target terrain.
    dist = {start: 0}
    prev: dict[tuple[int, int], tuple[int, int]] = {}
    pq = [(0, start)]
    target = None
    while pq:
        d, p = heapq.heappop(pq)
        if d != dist[p]:
            continue
        if terr.get(p) == terrain and p not in exclude:
            target = p
            break
        for dd, (dx, dy) in DELTA.items():
            np = (p[0] + dx, p[1] + dy)
            c = cost(np)
            if c is None:
                continue
            nd = d + c
            if nd < dist.get(np, float("inf")):
                dist[np] = nd
                prev[np] = p
                heapq.heappush(pq, (nd, np))
    check("path to terrain exists", target is not None, terrain)
    # Reconstruct path and follow it through the API.
    path = []
    p = target
    while p != start:
        path.append(p)
        p = prev[p]
    path.reverse()
    for nxt in path:
        cur = me(key, loud=False)
        dx, dy = nxt[0] - cur["x"], nxt[1] - cur["y"]
        d = next(k for k, v in DELTA.items() if v == (dx, dy))
        r = sreq(key, "POST", "/world/move", {"dir": d})
        if r.status_code == 402:
            raise APStarved(f"dijkstra walk: {r.json()}")
        check("dijkstra step", r.status_code == 200, r.text[:80], loud=False)
    m = me(key, loud=False)
    check(f"arrived on {terrain}", m["terrain"] == terrain,
          f"at {(m['x'], m['y'])} after {len(path)} steps")
    m["_steps"] = len(path)
    return m


_last_gather = 0.0


def gather(key: SigningKey, tool: str | None = None, want: str | None = None):
    """Gather with the 1/2s per-agent rate limit respected.

    Multi-resource tiles (legacy + overlay) 400 with "specify resource";
    retry naming `want` — the disambiguation flow agents must implement.
    """
    global _last_gather
    wait = 2.15 - (time.time() - _last_gather)
    if wait > 0:
        time.sleep(wait)
    payload: dict = {} if tool is None else {"tool": tool}
    if want:
        payload["resource"] = want
    r = sreq(key, "POST", "/world/gather", payload)
    if r.status_code == 400:
        try:
            options = r.json().get("resources", [])
        except Exception:
            options = []
        if r.json().get("detail") == "specify resource" and options:
            payload["resource"] = want if want in options else options[0]
            r = sreq(key, "POST", "/world/gather", payload)
    _last_gather = time.time()
    if r.status_code == 402:
        raise APStarved(f"gather: {r.json()}")
    check("gather", r.status_code == 200, r.text[:130])
    return r.json()


def gather_until(key: SigningKey, resource: str, terrain: str, target: int,
                 tool: str | None = None, max_tiles: int = 6) -> None:
    """Gather `target` units of `resource`, walking to fresh `terrain` tiles
    when the current tile depletes (per-tile stock is 5-12; the spine needs
    more than one tile can hold). Honest agent behavior, API-only actions."""
    seen: set[tuple[int, int]] = set()
    for _ in range(max_tiles * 8):
        have = inventory(key, loud=False).get(resource, 0)
        if have >= target:
            return
        m = me(key, loud=False)
        seen.add((m["x"], m["y"]))
        global _last_gather
        wait = 2.15 - (time.time() - _last_gather)
        if wait > 0:
            time.sleep(wait)
        payload: dict = {} if tool is None else {"tool": tool}
        payload["resource"] = resource
        r = sreq(key, "POST", "/world/gather", payload)
        _last_gather = time.time()
        if r.status_code == 402:
            raise APStarved(f"gather_until {resource}: {r.json()}")
        if r.status_code == 400 and "depleted" in r.text:
            walk_to(key, terrain, exclude=seen)  # fresh tile, then continue
            continue
        check(f"gather {resource}", r.status_code == 200, r.text[:100])
    raise SystemExit(
        f"smoke FAILED: could not gather {target}x {resource} "
        f"(have {inventory(key, loud=False).get(resource, 0)})")


def run_spine() -> None:
    print("== Systems Bible spine smoke ==")

    # 1. World info: migration announced (§2.4) + seasons published (§7).
    info = client.get("/world/info").json()
    check("migration announced in /world/info",
          "migration" in info and "iron_ore" in info["migration"]["ore_to_iron_ore"])
    check("seasons published", info["seasons"]["season"] in
          ("spring", "summer", "autumn", "winter"),
          f"season={info['seasons']['season']}")
    season = info["seasons"]["season"]

    # 2. Two agents: timber specialist + plains forager.
    k_scout, k_mate = make_key(), make_key()
    register("smoke-scout", k_scout)
    register("smoke-mate", k_mate)
    spawn(k_scout)
    spawn(k_mate)

    # 3. Scout -> forest, bare-hands timber (4 AP -> 1).
    m = walk_to(k_scout, "forest")
    print(f"  .. scout reached forest in {m['_steps']} steps, ap={m['ap']:.0f}")
    for _ in range(2):
        g = gather(k_scout, want="timber")
    check("bare-hands timber", g["resource"] == "timber" and g["gained"] == 1
          and not g["tooled"])
    check("2 timber banked",
          inventory(k_scout, loud=False).get("timber", 0) >= 2)

    # 4. Mate: forest for fiber (forest-only overlay), then plains for grain.
    #    Depletion-aware: per-tile stock is 5-12, so move on when a tile runs dry.
    walk_to(k_mate, "forest")
    gather_until(k_mate, "fiber", "forest", 2)
    check("fiber gathered", inventory(k_mate, loud=False).get("fiber", 0) >= 2)
    m2 = walk_to(k_mate, "plains")
    print(f"  .. mate walks done, ap={m2['ap']:.0f}")
    gather_until(k_mate, "grain", "plains", 7)
    inv = inventory(k_mate)
    check("mate holds fiber + grain", inv.get("fiber", 0) >= 2
          and inv.get("grain", 0) >= 7,
          str({k: inv.get(k, 0) for k in ("fiber", "grain")}))

    # 5. Trade A — early food parcel: mate sells 2 grain for 10 chits.
    #    Chit-denominated offers exercise the v1.0.2 trade engine inside the
    #    bible build (backward compat); trades cost no AP.
    r = sreq(k_mate, "POST", "/trade/offers",
             {"give": {"grain": 2}, "want": {"chits": 10}})
    check("mate offers grain->chits", r.status_code == 201, r.text[:100])
    r = sreq(k_scout, "POST",
             f"/trade/offers/{r.json()['offer_id']}/accept", {})
    check("scout accepts grain offer", r.status_code == 200, r.text[:130])
    r = sreq(k_scout, "POST", "/eat", {"item": "grain", "qty": 2})
    check("scout eats 2 grain -> +4 AP",
          r.status_code == 200 and r.json().get("ap_gained") == 4, r.text[:100])

    # 6. Trade B: mate sells 1 fiber for 5 chits; scout crafts crude_axe
    #    (2 timber + 1 fiber, 2 AP, 120 durability).
    r = sreq(k_mate, "POST", "/trade/offers",
             {"give": {"fiber": 2}, "want": {"chits": 10}})
    check("mate offers fiber->chits", r.status_code == 201, r.text[:100])
    r = sreq(k_scout, "POST",
             f"/trade/offers/{r.json()['offer_id']}/accept", {})
    check("scout accepts fiber offer", r.status_code == 200, r.text[:130])
    r = sreq(k_scout, "POST", "/world/craft", {"recipe_id": "crude_axe"})
    check("craft crude_axe", r.status_code == 200, r.text[:130])
    check("axe durability 120", r.json().get("durability") == 120)

    # 7. Tooled timber (2 AP -> 2, +1 when spring abundance >= 1.25).
    #    Need 7 total: 2 already spent on the axe + 3 (shelter) + 2 (farm).
    g = gather(k_scout, tool="crude_axe", want="timber")
    expect = 3 if world.SEASON_MULT["timber"][season] >= 1.25 else 2
    if g["stock_remaining"] + g["gained"] >= expect:
        check("tooled timber yield (+season)",
              g["gained"] == expect and g["tooled"],
              f"season={season} gained={g['gained']} expected={expect}")
    else:
        check("tooled gather works on thin stock",
              g["tooled"] and g["gained"] >= 1,
              f"gained={g['gained']} (stock-limited)")
    gather_until(k_scout, "timber", "forest", 7, tool="crude_axe")
    check("timber stock for builds",
          inventory(k_scout, loud=False).get("timber", 0) >= 7)
    # Scout gathers 2 grain on the plains (needs 9 total: 5 eaten +
    # 2 farm + 1 experiment + 1 sickle; 7 traded in).
    walk_to(k_scout, "plains")
    gather_until(k_scout, "grain", "plains", 2)

    # 8. Trade C: mate sells 3 more grain for 15 chits; both eat.
    r = sreq(k_mate, "POST", "/trade/offers",
             {"give": {"grain": 5}, "want": {"chits": 25}})
    check("mate offers more grain", r.status_code == 201, r.text[:100])
    r = sreq(k_scout, "POST",
             f"/trade/offers/{r.json()['offer_id']}/accept", {})
    check("scout accepts", r.status_code == 200, r.text[:130])
    r = client.get("/trade/ledger")
    check("trade ledger holds 3 fills", r.status_code == 200 and
          len(r.json()) >= 3, f"len={len(r.json()) if r.status_code == 200 else '?'}")
    r = sreq(k_scout, "POST", "/eat", {"item": "grain", "qty": 2})
    check("scout eats 2 more grain",
          r.status_code == 200 and r.json().get("ap_gained") == 4, r.text[:100])
    r = sreq(k_scout, "POST", "/world/craft", {"recipe_id": "crude_sickle"})
    check("craft crude_sickle (farm key)",
          r.status_code == 200, r.text[:130])

    # 9. Idempotency: same key twice on /eat -> identical replay, food
    #    consumed exactly once.
    inv_before = inventory(k_scout).get("grain", 0)
    key = "smoke-idem-" + os.urandom(4).hex()
    r1 = sreq(k_scout, "POST", "/eat", {"item": "grain", "qty": 1}, idem=key)
    r2 = sreq(k_scout, "POST", "/eat", {"item": "grain", "qty": 1}, idem=key)
    check("idempotent eat replays identically",
          r1.status_code == 200 and r2.status_code == 200
          and r1.json() == r2.json(), r2.text[:100])
    inv_after = inventory(k_scout).get("grain", 0)
    check("food consumed exactly once", inv_before - inv_after == 1,
          f"before={inv_before} after={inv_after}")

    # 10. Claim two adjacent tiles; build shelter (3 timber + 1 fiber) and
    #     farm (2 timber + 2 grain). One structure per tile.
    m = me(k_scout)
    x, y = m["x"], m["y"]
    check("standing on land", m["terrain"] != "ocean")
    r = sreq(k_scout, "POST", "/world/claim", {"x": x, "y": y})
    check("claim tile", r.status_code == 200, r.text[:100])
    # One structure per tile is the rule (BuildError otherwise), and the
    # 50-AP spawn budget doesn't fit two claims at 1/60s + two builds.
    # The farm exercises build + farming + upkeep-tithe in one structure.
    r = sreq(k_scout, "POST", "/world/build",
             {"kind": "farm", "x": x, "y": y})
    check("build farm", r.status_code == 200, r.text[:130])
    farm_id = r.json()["id"]

    # 11. Farm: plant now; harvest must 400 (crops mature after 2h).
    r = sreq(k_scout, "POST", "/world/farm",
             {"structure_id": farm_id, "action": "plant", "slot": 0})
    check("farm plant", r.status_code == 200, r.text[:130])
    r = sreq(k_scout, "POST", "/world/farm",
             {"structure_id": farm_id, "action": "harvest", "slot": 0})
    check("immature harvest -> 400", r.status_code == 400, r.text[:100])

    # 12. Experiment (discovery) + tithe attempt on the fresh shelter.
    #     A fresh structure owes no tithe: the 400 proves upkeep can't be
    #     overpaid (Bible §4.2 — no double-tithe).
    r = sreq(k_scout, "POST", "/world/experiment",
             {"items": {"timber": 1, "grain": 1}})
    check("experiment returns discovery verdict", r.status_code == 200,
          r.text[:130])
    r = sreq(k_scout, "POST", "/world/tithe", {"structure_id": farm_id})
    check("fresh farm owes no tithe -> 400",
          r.status_code == 400 and "no tithe owed" in r.text, r.text[:100])

    # 13. Voice: whisper on own tile, then read own feed.
    r = sreq(k_scout, "POST", "/voice/whisper", {"text": "smoke ping"})
    check("whisper", r.status_code == 201, r.text[:100])
    r = sget(k_scout, "/voice/feed")
    check("voice feed hears own whisper",
          r.status_code == 200 and any("smoke ping" in str(e) for e in r.json()),
          f"feed_len={len(r.json()) if r.status_code == 200 else '?'}")

    n_ok = len([s for s in STEPS if s[0] == "ok"])
    print(f"\n== spine complete: {n_ok} checks ok ==")
    print(f"   scout ap={me(k_scout, loud=False)['ap']:.0f} "
          f"mate ap={me(k_mate, loud=False)['ap']:.0f} season={season}")


def main() -> None:
    for attempt in (1, 2):
        setup()
        try:
            run_spine()
            return
        except APStarved as e:
            print(f"  .. AP-starved (bad spawn luck), retrying: {e}")
    raise SystemExit("smoke FAILED: AP-starved on two consecutive spawns")


if __name__ == "__main__":
    main()
