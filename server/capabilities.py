"""Capability Registry catalog — the v0 capability namespaces.

A capability is something a citizen may be granted the right to do or use.
Capabilities are inert until granted via a Capability Lease (see
``server/leases.py``). This module holds the catalog: what capabilities
exist, which namespace they belong to, and whether the namespace is open,
chartered, or closed.

Namespace posture (per CAPABILITY_LEASES.md §9):
- ``world.*``  — baseline citizenship verbs (movement, chat, gather, craft…)
- ``econ.*``   — trade, transfers, tithes, settlement economy
- ``object.*`` — per-object grants (furnace.use, relay.use…; in-world only)
- ``gov.*``    — proposals, endorsements, settlements, voting
- ``mind.*``   — citizen mind-memory store (budgeted in bytes)
- ``org.*``    — chartered-organization powers (asset issuance is
  achievement-gated; see CITIZEN_PROTOCOL.md §8)
- ``tool.*``   — external system access. CLOSED until the real-economy rails
  are approved. No lease may be issued for a closed namespace, not even by
  the world authority.
"""

from __future__ import annotations

import json
import sqlite3

# (name, namespace, description, status, routes)
# status: open | chartered | closed
CATALOG: list[tuple[str, str, str, str, list[str]]] = [
    # world.* — baseline citizenship
    ("world.chat", "world", "Send chat messages", "open", ["POST /chat"]),
    ("world.voice", "world", "Send proximity voice (whisper/talk/shout/relay)", "open",
     ["POST /voice/whisper", "POST /voice/talk", "POST /voice/shout", "POST /voice/relay"]),
    ("world.spawn", "world", "Spawn into the world at a starting tile", "open", ["POST /world/spawn"]),
    ("world.move", "world", "Move across tiles (AP-budgeted)", "open", ["POST /world/move"]),
    ("world.disclose", "world", "Disclose explored tiles to the public map", "open", ["POST /world/disclose"]),
    ("world.gather", "world", "Gather resources from tiles (AP-budgeted)", "open", ["POST /world/gather"]),
    ("world.craft", "world", "Craft items and experiment for hidden recipes", "open",
     ["POST /world/craft", "POST /world/experiment"]),
    ("world.claim", "world", "Claim unowned tiles", "open", ["POST /world/claim"]),
    ("world.build", "world", "Raise structures on claimed tiles", "open", ["POST /world/build"]),
    ("world.demolish", "world", "Demolish structures (own, or per world law)", "open", ["POST /world/demolish"]),
    ("world.refine", "world", "Refine raw resources at a furnace", "open", ["POST /world/refine"]),
    ("world.farm", "world", "Till, plant, and harvest farm plots", "open", ["POST /world/farm"]),
    ("world.eat", "world", "Eat food to restore AP", "open", ["POST /eat"]),
    ("world.identity", "world", "Present or update the Citizen Card", "open", ["POST /citizens/card"]),
    # econ.* — economy
    ("econ.trade", "econ", "Create, accept, and cancel trade offers", "open",
     ["POST /trade/offers", "POST /trade/offers/{offer_id}/accept", "POST /trade/offers/{offer_id}/cancel"]),
    ("econ.transfer", "econ", "Transfer structures or assets to another citizen", "open", ["POST /world/transfer"]),
    ("econ.tithe", "econ", "Pay structure tithes", "open", ["POST /world/tithe"]),
    ("econ.settlement", "econ", "Contribute chits to settlements and projects", "open",
     ["POST /world/settlements/contribute", "POST /world/settlements/projects/contribute"]),
    # gov.* — governance
    ("gov.propose", "gov", "Create and withdraw proposals", "open",
     ["POST /proposals", "DELETE /proposals/{proposal_id}"]),
    ("gov.comment", "gov", "Comment on proposals", "open", ["POST /proposals/{proposal_id}/comments"]),
    ("gov.endorse", "gov", "Endorse or un-endorse proposals", "open",
     ["POST /proposals/{proposal_id}/endorse", "DELETE /proposals/{proposal_id}/endorse"]),
    ("gov.settlement", "gov", "Name settlements, manage disbursals and projects", "open",
     ["POST /world/settlements/name", "POST /world/settlements/disburse",
      "POST /world/settlements/disburse/approve", "POST /world/settlements/projects",
      "POST /world/settlements/projects/complete"]),
    ("gov.vote", "gov", "Vote in governance decisions", "chartered", []),
    ("gov.charter", "gov", "Charter organizations", "chartered", []),
    # mind.* — lifetime memory (subjective layer)
    ("mind.memory", "mind", "Read/write the citizen's private mind-memory store (byte-budgeted)", "open",
     ["POST /mind/entries", "GET /mind/entries", "PUT /mind/entries/{entry_id}",
      "DELETE /mind/entries/{entry_id}"]),
    # object.* — in-world object grants (data-driven successors of today's
    # hardcoded per-verb checks; issued by object owners or world authority)
    ("object.furnace.use", "object", "Use a furnace object for refining", "open", []),
    ("object.relay.use", "object", "Use a voice-relay tower", "open", []),
    # org.* — chartered organization powers
    ("org.hire", "org", "Employ citizens under an organization charter", "chartered", []),
    ("org.issue", "org", "Issue org assets (achievement-gated, never a birthright)", "chartered", []),
    # tool.* — external systems. CLOSED.
    ("tool.egress", "tool", "External network egress", "closed", []),
    ("tool.compute", "tool", "External compute execution", "closed", []),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS policy_capabilities(
  name TEXT PRIMARY KEY,
  namespace TEXT NOT NULL,
  description TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  routes TEXT NOT NULL DEFAULT '[]'
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def seed_catalog(conn: sqlite3.Connection) -> int:
    """Insert catalog rows (INSERT OR IGNORE — idempotent). Returns the
    number of rows newly inserted."""
    inserted = 0
    for name, namespace, description, status, routes in CATALOG:
        cur = conn.execute(
            "INSERT OR IGNORE INTO policy_capabilities"
            " (name, namespace, description, status, routes)"
            " VALUES (?, ?, ?, ?, ?)",
            (name, namespace, description, status, json.dumps(routes)),
        )
        inserted += cur.rowcount
    return inserted


def get_capability(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM policy_capabilities WHERE name = ?", (name,)
    ).fetchone()


def list_capabilities(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM policy_capabilities ORDER BY namespace, name"
    ).fetchall()


def namespace_status(conn: sqlite3.Connection, namespace: str) -> str | None:
    row = conn.execute(
        "SELECT status FROM policy_capabilities WHERE namespace = ? LIMIT 1",
        (namespace,),
    ).fetchone()
    return row["status"] if row else None


def is_issuable(conn: sqlite3.Connection, capability: str) -> tuple[bool, str]:
    """Can a lease be issued for this capability at all? Closed namespaces
    (tool.*) are never issuable — not even by the world authority."""
    cap = get_capability(conn, capability)
    if cap is None:
        return False, f"unknown capability '{capability}'"
    if cap["status"] == "closed":
        return False, f"capability namespace '{cap['namespace']}' is closed"
    return True, ""
