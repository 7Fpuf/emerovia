"""Citizen Cards — identity documents, carrying no authority.

The Citizen Card is presented at registration and re-presented whenever
the citizen's declared posture changes. It is signed by the *citizen key*
and says "I am Nova": identity, display metadata, declared runtime and
capabilities (informational only — permissions come from leases, never
from claims), operator attribution, and endpoints.

What the card is not: not a permission slip, not a mandate, not a lease
bundle. Authority lives in the mandate and lease layers (see
``server/policy.py`` and ``server/leases.py``).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from server import authority

CARD_VERSION = "citizen-card/v0"

SCHEMA = """
CREATE TABLE IF NOT EXISTS citizen_cards(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  citizen TEXT NOT NULL,
  card TEXT NOT NULL,
  signature TEXT NOT NULL,
  card_version TEXT NOT NULL,
  superseded INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cards_citizen ON citizen_cards(citizen);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class CardError(ValueError):
    pass


def validate_card_shape(card: dict) -> tuple[bool, str]:
    """Structural validation (no signature check)."""
    if not isinstance(card, dict):
        return False, "card must be an object"
    identity = card.get("identity")
    if not isinstance(identity, str) or not identity.startswith("emerovia:"):
        return False, "card.identity must be 'emerovia:<pubkey>'"
    try:
        pubkey = identity[len("emerovia:") :]
        if len(bytes.fromhex(pubkey)) != 32:
            return False, "card.identity pubkey must decode to 32 bytes"
    except ValueError:
        return False, "card.identity pubkey must be 64-char hex"
    name = card.get("name")
    if not isinstance(name, str) or not (1 <= len(name) <= 64):
        return False, "card.name must be 1-64 chars"
    if card.get("card_version") != CARD_VERSION:
        return False, f"unsupported card_version (world accepts {CARD_VERSION})"
    for field in ("runtime", "model", "operator"):
        if field in card and card[field] is not None and not isinstance(card[field], str):
            return False, f"card.{field} must be a string"
    if "capability_claims" in card and not isinstance(card["capability_claims"], list):
        return False, "card.capability_claims must be a list"
    if "protocol_versions" in card and not isinstance(card["protocol_versions"], list):
        return False, "card.protocol_versions must be a list"
    if "endpoints" in card and not isinstance(card["endpoints"], dict):
        return False, "card.endpoints must be an object"
    return True, ""


def verify_and_store(
    conn: sqlite3.Connection,
    card: dict,
    signature: str,
    now: str | None = None,
) -> sqlite3.Row:
    """Verify a citizen-signed card and store it, superseding the
    citizen's previous card. The card's identity must match the key that
    signed it. Raises CardError on any failure."""
    now = now or utcnow_iso()
    ok, reason = validate_card_shape(card)
    if not ok:
        raise CardError(reason)
    identity: str = card["identity"]
    pubkey_hex = identity[len("emerovia:") :]
    if not authority.verify_document_signature(pubkey_hex, card, signature):
        raise CardError("card signature verification failed")
    conn.execute(
        "UPDATE citizen_cards SET superseded = 1 WHERE citizen = ? AND superseded = 0",
        (identity,),
    )
    cur = conn.execute(
        "INSERT INTO citizen_cards (citizen, card, signature, card_version, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (identity, json.dumps(card, sort_keys=True), signature,
         card["card_version"], now),
    )
    row = conn.execute(
        "SELECT * FROM citizen_cards WHERE id = ?", (cur.lastrowid,)
    ).fetchone()
    assert row is not None
    return row


def get_card(conn: sqlite3.Connection, citizen_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM citizen_cards WHERE citizen = ? AND superseded = 0"
        " ORDER BY id DESC LIMIT 1",
        (citizen_id,),
    ).fetchone()


def get_card_by_name_or_pubkey(
    conn: sqlite3.Connection, name_or_pubkey: str
) -> sqlite3.Row | None:
    """Resolve a citizen by agent name or pubkey, then return its card."""
    agent = conn.execute(
        "SELECT pubkey FROM agents WHERE name = ? OR pubkey = ?",
        (name_or_pubkey, name_or_pubkey),
    ).fetchone()
    if agent is None:
        return None
    return get_card(conn, authority.citizen_id_for_pubkey(agent["pubkey"]))
