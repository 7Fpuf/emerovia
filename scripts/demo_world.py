#!/usr/bin/env python3
"""Agent Commons Stage 2 demo: world exploration.

Two fresh agents register, spawn into the shared 64x64 world, wander for
several steps (routing around ocean tiles), disclose a few discovered tiles,
then print their summaries plus an ASCII excerpt of the public map.

Run against a live server:

    scripts/run.sh & sleep 2; .venv/bin/python scripts/demo_world.py

Env: AC_DEMO_URL (default http://127.0.0.1:8765)

NOTE: do not run this against a production database you care about -- it
registers new agents and mutates world state.
"""
from __future__ import annotations

import json
import os
import sys
import time

import httpx
from nacl.signing import SigningKey

BASE_URL = os.environ.get("AC_DEMO_URL", "http://127.0.0.1:8765")

# Direction vocabulary per the world API contract (single-letter, uppercase).
DIRS = ("N", "S", "E", "W")
DELTA = {"N": (0, -1), "S": (0, 1), "E": (1, 0), "W": (-1, 0)}

TERRAIN_GLYPH = {
    "ocean": "~",
    "plains": ".",
    "forest": "F",
    "desert": "d",
    "mountain": "^",
}


def sign(key: SigningKey, ts: str, method: str, path: str, body: bytes) -> str:
    """Exact-bytes ed25519 signature per the Agent Commons contract."""
    msg = (ts + "\n" + method + "\n" + path + "\n" + body.decode("utf-8")).encode("utf-8")
    return key.sign(msg).signature.hex()


def auth_headers(key: SigningKey, method: str, path: str, body: bytes) -> dict:
    ts = str(time.time())
    return {
        "X-Agent-Pubkey": key.verify_key.encode().hex(),
        "X-Timestamp": ts,
        "X-Signature": sign(key, ts, method, path, body),
        "Content-Type": "application/json",
    }


def signed_post(client: httpx.Client, key: SigningKey, path: str, payload: dict) -> httpx.Response:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return client.post(BASE_URL + path, content=body,
                       headers=auth_headers(key, "POST", path, body))


def signed_get(client: httpx.Client, key: SigningKey, path: str) -> httpx.Response:
    return client.get(BASE_URL + path, headers=auth_headers(key, "GET", path, b""))


def register(client: httpx.Client, name: str, key: SigningKey) -> dict:
    r = client.post(
        BASE_URL + "/register",
        json={"name": name, "pubkey": key.verify_key.encode().hex()},
    )
    if r.status_code == 409:
        print(f"!! register {name}: 409 already registered -- aborting", file=sys.stderr)
        sys.exit(1)
    r.raise_for_status()
    return r.json()


def wander(client: httpx.Client, key: SigningKey, name: str, label: str,
           preferred: tuple[str, ...], steps: int = 8) -> list[dict]:
    """Walk up to `steps` moves, routing around blocked tiles. Returns visited tiles."""
    visited: list[dict] = []
    order = list(preferred) + [d for d in DIRS if d not in preferred]
    for _ in range(steps):
        moved = False
        for d in order:
            r = signed_post(client, key, "/world/move", {"dir": d})
            if r.status_code == 200:
                tile = r.json()
                visited.append(tile)
                print(f"  {label} moved {d:5s} -> ({tile['x']},{tile['y']}) "
                      f"{tile['terrain']:8s} ap={tile['ap']} cost={tile['cost']}")
                moved = True
                break
            if r.status_code == 402:
                print(f"  {label} out of AP ({r.json().get('detail')}) -- stopping")
                return visited
            # 400: ocean / off-map / invalid -- try the next direction
        if not moved:
            print(f"  {label} boxed in (all directions blocked) -- stopping")
            return visited
    return visited


def main() -> None:
    stamp = int(time.time())
    specs = [
        (f"world-alice-{stamp}", "A", ("E", "S", "W", "N")),
        (f"world-bob-{stamp}", "B", ("S", "W", "N", "E")),
    ]

    print("== Agent Commons Stage 2 demo: world exploration ==")
    print(f"server: {BASE_URL}\n")

    # trust_env=False: localhost must not be affected by the host's proxy
    # environment (whose no_proxy IPv6 entries httpx chokes on).
    with httpx.Client(timeout=10.0, trust_env=False) as client:
        info = client.get(BASE_URL + "/world/info").json()
        print(f"world: {info['width']}x{info['height']} seed={info['seed_id']}")
        print(f"legend: {info['legend']}\n")

        agents = []
        for name, label, preferred in specs:
            key = SigningKey.generate()
            reg = register(client, name, key)
            print(f"-- {name} registered (id={reg['id']}) --")
            sp = signed_post(client, key, "/world/spawn", {})
            if sp.status_code != 201:
                print(f"!! spawn failed: {sp.status_code} {sp.text}", file=sys.stderr)
                sys.exit(1)
            s = sp.json()
            print(f"  spawned at ({s['x']},{s['y']}) terrain={s['terrain']} "
                  f"ap={s['ap']}/{s['ap_cap']}")
            visited = [{"x": s["x"], "y": s["y"], "terrain": s["terrain"]}] \
                + wander(client, key, name, label, preferred)
            # disclose a few discovered tiles (spawn tile + last two stops)
            disclosed = 0
            for tile in [visited[0]] + visited[-2:]:
                dr = signed_post(client, key, "/world/disclose",
                                 {"x": tile["x"], "y": tile["y"]})
                if dr.status_code == 200:
                    disclosed += 1
                    print(f"  {label} disclosed ({tile['x']},{tile['y']}) "
                          f"{dr.json()['terrain']}")
                else:
                    print(f"  {label} disclose failed: {dr.status_code} {dr.text}")
            print(f"  {label} disclosed {disclosed} tiles\n")
            agents.append((name, label, key))

        print("-- agent summaries (/world/me) --")
        positions = []
        for name, label, key in agents:
            me = signed_get(client, key, "/world/me")
            me.raise_for_status()
            m = me.json()
            positions.append((m["x"], m["y"], label))
            print(f"  [{label}] {m['agent_name']}: pos=({m['x']},{m['y']}) "
                  f"terrain={m['terrain']} ap={m['ap']}/{m['ap_cap']} "
                  f"next_ap_in={m['seconds_until_next_ap']}s "
                  f"private_discoveries={m['private_discoveries']}")

        print("\n-- public map excerpt (~20x12, centered on first agent) --")
        world_map = client.get(BASE_URL + "/world/map").json()
        tiles = {(t["x"], t["y"]): t["terrain"] for t in world_map["tiles"]}
        cx, cy = positions[0][0], positions[0][1]  # center on first agent
        x0, x1 = max(0, cx - 10), min(info["width"] - 1, cx + 9)
        y0, y1 = max(0, cy - 6), min(info["height"] - 1, cy + 5)
        agent_at = {(x, y): label for x, y, label in positions}
        for y in range(y0, y1 + 1):
            row = ""
            for x in range(x0, x1 + 1):
                if (x, y) in agent_at:
                    row += agent_at[(x, y)]
                elif (x, y) in tiles:
                    row += TERRAIN_GLYPH.get(tiles[(x, y)], "?")
                else:
                    row += "?"
            print(f"  {row}")
        print("  legend: ~=ocean .=plains F=forest d=desert ^=mountain ?=unknown "
              "(A/B=agents)")
        print(f"  public_count={world_map['public_count']} "
              f"(tiles disclosed so far)")

    print("\ndemo complete: 2 agents spawned, wandered, and disclosed tiles.")


if __name__ == "__main__":
    main()
