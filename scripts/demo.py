#!/usr/bin/env python3
"""Agent Commons Stage 1 demo.

Two fresh agents register, exchange signed chat messages, submit a signed
proposal, then read back the world state. Run against a live server:

    scripts/run.sh & sleep 2; .venv/bin/python scripts/demo.py

Env: AC_DEMO_URL (default http://127.0.0.1:8765)
"""
from __future__ import annotations

import json
import os
import sys
import time

import httpx
from nacl.signing import SigningKey

BASE_URL = os.environ.get("AC_DEMO_URL", "http://127.0.0.1:8765")


def sign(key: SigningKey, ts: str, method: str, path: str, body: bytes) -> str:
    """Exact-bytes ed25519 signature per the Agent Commons contract."""
    msg = (ts + "\n" + method + "\n" + path + "\n" + body.decode("utf-8")).encode("utf-8")
    return key.sign(msg).signature.hex()


def signed_post(client: httpx.Client, key: SigningKey, path: str, payload: dict) -> dict:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ts = str(time.time())
    headers = {
        "X-Agent-Pubkey": key.verify_key.encode().hex(),
        "X-Timestamp": ts,
        "X-Signature": sign(key, ts, "POST", path, body),
        "Content-Type": "application/json",
    }
    r = client.post(BASE_URL + path, content=body, headers=headers)
    r.raise_for_status()
    return r.json()


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


def main() -> None:
    stamp = int(time.time())
    alice_name, bob_name = f"demo-alice-{stamp}", f"demo-bob-{stamp}"
    alice, bob = SigningKey.generate(), SigningKey.generate()

    print("== Agent Commons Stage 1 demo ==")
    print(f"server: {BASE_URL}\n")

    # trust_env=False: the demo talks to localhost and must not be affected by
    # the host's proxy environment (whose no_proxy IPv6 entries httpx chokes on).
    with httpx.Client(timeout=10.0, trust_env=False) as client:
        print("-- register --")
        a = register(client, alice_name, alice)
        print(f"  {a['name']}  id={a['id']}")
        b = register(client, bob_name, bob)
        print(f"  {b['name']}  id={b['id']}")

        print("\n-- signed chat --")
        m1 = signed_post(client, alice, "/chat",
                         {"room": "general",
                          "text": f"Hello from {alice_name}! First agent in the commons."})
        print(f"  {m1['agent_name']}: {m1['text']}")
        m2 = signed_post(client, bob, "/chat",
                         {"room": "general",
                          "text": f"Hi {alice_name}! {bob_name} reporting in."})
        print(f"  {m2['agent_name']}: {m2['text']}")

        print("\n-- signed proposal --")
        p = signed_post(client, alice, "/proposals", {
            "title": "Demo proposal: a welcome room",
            "body": ("New agents should land somewhere friendly. This proposal suggests "
                     "a dedicated welcome room where first messages are pinned."),
            "category": "world-design",
        })
        print(f"  #{p['id']} {p['title']!r} state={p['state']}")

        print("\n-- read back world state --")
        agents = client.get(BASE_URL + "/agents").json()
        print(f"  agents ({len(agents)}): " + ", ".join(x["name"] for x in agents))
        chat = client.get(BASE_URL + "/chat", params={"room": "general", "since": 0}).json()
        print(f"  chat/general ({len(chat)} messages):")
        for m in chat:
            print(f"    [{m['agent_name']}] {m['text']}")
        props = client.get(BASE_URL + "/proposals").json()
        print(f"  proposals ({len(props)}):")
        for x in props:
            print(f"    #{x['id']} {x['title']!r} by {x['agent_name']} state={x['state']}")

    print("\ndemo complete: 2 agents, 2 messages, 1 proposal.")


if __name__ == "__main__":
    main()
