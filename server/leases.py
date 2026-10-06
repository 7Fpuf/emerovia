"""Capability Leases — Emerovia's single permissions primitive.

A Capability Lease is a signed record: issuer → citizen → capability →
scope → budget → location → expiration → delegation depth → revocation.
This module implements the primitive: issuance (with signature
verification), delegation with narrowing, revocation with chain
propagation, lazy expiry, and usage metering for budgets.

In-world capabilities only in v1. The ``tool.*`` namespace is closed:
no lease may be issued for it, not even by the world authority.

Design decisions (v1, documented per spec):
- Signature verification happens at *issuance*; the lease table is the
  system of record afterwards (standard for signed-record systems).
- ``lease_id`` is client-proposed for API issuance and must be unique;
  the world enforces uniqueness as the system of record. Server-side
  issuance (bootstrap, operator) uses deterministic ids.
- Sub-lease capability must exactly equal the parent capability; scope,
  budget, and location narrow per ``is_narrower``; expiration must be
  <= the parent's. Only the lease *holder* may delegate (sub-lease
  issuer == parent citizen), with child depth == parent depth - 1.
- Baseline ``*`` leases are issued with ``delegation_depth = 0``: in v1,
  delegation flows only from explicitly issued leases with depth >= 1.
- Expiry is evaluated lazily (no sweeps); status flips to ``expired``
  when observed.
- Revocation propagates down the delegation chain; resumption requires
  reissuance (there is no un-revoke).
- Constitutional issuance guard: no lease may condition a grant on
  character-token ownership (scope/budget/location must not contain
  token-gating keys). This is world law enforced at issuance.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Callable

from server import authority, capabilities

LEASE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")

# Keys that would condition a grant on character-token ownership.
# Their presence anywhere in scope/budget/location is unconstitutional
# (IDENTITY_AND_AUTHORITY.md §3, boundary 4) and issuance is denied.
TOKEN_GATING_KEYS = frozenset(
    {"requires_token", "token_gated", "holder_only", "token_holder", "requires_character_token"}
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS leases(
  lease_id TEXT PRIMARY KEY,
  issuer TEXT NOT NULL,
  citizen TEXT NOT NULL,
  capability TEXT NOT NULL,
  scope TEXT NOT NULL DEFAULT '{}',
  budget TEXT,
  location TEXT,
  expiration TEXT,
  delegation_depth INTEGER NOT NULL DEFAULT 0,
  revocation TEXT NOT NULL DEFAULT '{}',
  parent_lease_id TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  issued_at TEXT NOT NULL,
  signature TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_leases_citizen ON leases(citizen);
CREATE INDEX IF NOT EXISTS idx_leases_capability ON leases(capability);
CREATE INDEX IF NOT EXISTS idx_leases_parent ON leases(parent_lease_id);
CREATE TABLE IF NOT EXISTS lease_usage(
  lease_id TEXT NOT NULL,
  period TEXT NOT NULL,
  amount REAL NOT NULL DEFAULT 0,
  PRIMARY KEY(lease_id, period)
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- envelope

#: Fields covered by the issuer's signature, in canonical form.
ENVELOPE_FIELDS = (
    "lease_id", "issuer", "citizen", "capability", "scope", "budget",
    "location", "expiration", "delegation_depth", "revocation",
    "parent_lease_id", "issued_at",
)


def envelope_for(lease: dict) -> dict:
    """The signable envelope: exactly the spec's fields, JSON-normalized."""
    env: dict = {}
    for field in ENVELOPE_FIELDS:
        value = lease.get(field)
        if field in ("scope", "budget", "revocation") and isinstance(value, str):
            # Accept pre-serialized JSON, normalize to objects for signing.
            value = json.loads(value) if value else ({} if field != "budget" else None)
        if field == "location" and isinstance(value, str):
            try:
                value = json.loads(value)
            except (ValueError, TypeError):
                pass
        env[field] = value
    return env


def _find_token_gating_keys(obj) -> str | None:
    """Return the first token-gating key found anywhere in a JSON value."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in TOKEN_GATING_KEYS:
                return key
            found = _find_token_gating_keys(value)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find_token_gating_keys(value)
            if found:
                return found
    return None


# ---------------------------------------------------------------- narrowing

def _value_within(child, parent, path: str) -> tuple[bool, str]:
    """Is a child scope/budget value within (narrower than or equal to)
    the parent value? Numbers: child <= parent. Strings: equal. Lists:
    child ⊆ parent. Dicts: recursive, parent keys ⊆ child keys."""
    if isinstance(parent, (int, float)) and isinstance(child, (int, float)):
        if child <= parent:
            return True, ""
        return False, f"{path}: {child} exceeds parent bound {parent}"
    if isinstance(parent, str) or isinstance(child, str):
        if child == parent:
            return True, ""
        return False, f"{path}: {child!r} != parent {parent!r}"
    if isinstance(parent, list) and isinstance(child, list):
        for item in child:
            if item not in parent:
                return False, f"{path}: {item!r} not in parent list"
        return True, ""
    if isinstance(parent, dict) and isinstance(child, dict):
        return _dict_within(child, parent, path)
    if child == parent:
        return True, ""
    return False, f"{path}: incompatible narrowing"


def _dict_within(child: dict, parent: dict, path: str) -> tuple[bool, str]:
    # Every parent key must be present in the child (omission would widen),
    # and each value must be within the parent's.
    for key, parent_value in parent.items():
        if key not in child:
            return False, f"{path}.{key}: missing in sub-lease (would widen)"
        ok, reason = _value_within(child[key], parent_value, f"{path}.{key}")
        if not ok:
            return False, reason
    return True, ""


def is_narrower(child: dict, parent: dict) -> tuple[bool, str]:
    """Check sub-lease narrowing for scope/budget/location/expiration.
    ``child`` and ``parent`` are envelopes (dicts with those keys)."""
    for field in ("scope", "budget"):
        c, p = child.get(field) or {}, parent.get(field) or {}
        if not isinstance(c, dict) or not isinstance(p, dict):
            return False, f"{field} must be objects"
        ok, reason = _dict_within(c, p, field)
        if not ok:
            return False, reason
    # Location: v1 uses exact match unless the parent is unbounded (None).
    ploc, cloc = parent.get("location"), child.get("location")
    if ploc is not None and cloc != ploc:
        return False, f"location {cloc!r} is not within parent {ploc!r}"
    # Expiration: ISO-8601 UTC strings compare lexicographically.
    pexp, cexp = parent.get("expiration"), child.get("expiration")
    if pexp is not None:
        if cexp is None:
            return False, "sub-lease must expire no later than its parent"
        if cexp > pexp:
            return False, f"expiration {cexp} exceeds parent {pexp}"
    return True, ""


# ---------------------------------------------------------------- issuance

class LeaseError(ValueError):
    """Issuance/verification failure with a human-readable reason."""


def get_lease(conn: sqlite3.Connection, lease_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM leases WHERE lease_id = ?", (lease_id,)
    ).fetchone()


def _check_expiry(conn: sqlite3.Connection, row: sqlite3.Row, now: str) -> bool:
    """Lazy expiry: flip status when observed. Returns True if live."""
    if row["status"] != "active":
        return False
    if row["expiration"] is not None and row["expiration"] <= now:
        conn.execute(
            "UPDATE leases SET status = 'expired' WHERE lease_id = ?",
            (row["lease_id"],),
        )
        return False
    return True


def lease_is_live(conn: sqlite3.Connection, row: sqlite3.Row, now: str | None = None) -> bool:
    """An active, unexpired, unrevoked lease whose whole delegation chain
    is live."""
    now = now or utcnow_iso()
    seen = set()
    current = row
    while current is not None:
        if current["lease_id"] in seen:
            return False  # cycle: structurally invalid
        seen.add(current["lease_id"])
        if not _check_expiry(conn, current, now):
            return False
        parent_id = current["parent_lease_id"]
        current = get_lease(conn, parent_id) if parent_id else None
    return True


def find_covering_lease(
    conn: sqlite3.Connection,
    citizen_id: str,
    capability: str,
    now: str | None = None,
) -> sqlite3.Row | None:
    """The live lease covering (citizen, capability): citizen-specific
    first, then statutory ``*`` grants. Most recently issued wins."""
    now = now or utcnow_iso()
    for cid in (citizen_id, "*"):
        row = conn.execute(
            "SELECT * FROM leases WHERE citizen = ? AND capability = ?"
            " AND status = 'active' ORDER BY issued_at DESC LIMIT 1",
            (cid, capability),
        ).fetchone()
        if row is not None and lease_is_live(conn, row, now):
            return row
    return None


def issue_lease(
    conn: sqlite3.Connection,
    *,
    lease: dict,
    issuer_sign: Callable[[bytes], str] | None = None,
    signature: str | None = None,
    authority_pubkey_hex: str,
    now: str | None = None,
) -> sqlite3.Row:
    """Issue a lease. Either ``issuer_sign`` (server-side issuance: the
    server signs as the issuer) or ``signature`` (client-provided envelope
    signature, verified here) must be given.

    ``lease`` holds the envelope fields (lease_id, issuer, citizen,
    capability, scope, budget, location, expiration, delegation_depth,
    revocation, parent_lease_id, issued_at). Raises LeaseError on any
    violation.
    """
    now = now or utcnow_iso()
    if (issuer_sign is None) == (signature is None):
        raise LeaseError("provide exactly one of issuer_sign or signature")

    env = envelope_for(lease)
    lease_id = env.get("lease_id")
    if not isinstance(lease_id, str) or not LEASE_ID_RE.match(lease_id):
        raise LeaseError("lease_id must match ^[A-Za-z0-9_.:-]{1,64}$")
    if get_lease(conn, lease_id) is not None:
        raise LeaseError(f"lease_id '{lease_id}' already exists")

    issuer = env.get("issuer")
    citizen = env.get("citizen")
    capability = env.get("capability")
    if not isinstance(issuer, str) or not issuer:
        raise LeaseError("issuer is required")
    if not isinstance(citizen, str) or not citizen:
        raise LeaseError("citizen is required")
    if not (citizen == "*" or citizen.startswith("emerovia:")):
        raise LeaseError("citizen must be 'emerovia:<pubkey>' or '*'")
    if citizen == "*" and issuer != authority.WORLD_AUTHORITY_ID:
        raise LeaseError("only the world authority may issue statutory ('*') leases")

    ok, reason = capabilities.is_issuable(conn, capability or "")
    if not ok:
        raise LeaseError(reason)

    # Constitutional issuance guard: no token-gated grants, ever.
    for field in ("scope", "budget", "location"):
        bad = _find_token_gating_keys(env.get(field))
        if bad:
            raise LeaseError(
                f"unconstitutional grant: '{bad}' conditions a capability on"
                " token ownership (IDENTITY_AND_AUTHORITY.md §3)"
            )

    expiration = env.get("expiration")
    if expiration is None and issuer != authority.WORLD_AUTHORITY_ID:
        raise LeaseError("indefinite leases require world-authority issuance")
    if expiration is not None and not isinstance(expiration, str):
        raise LeaseError("expiration must be an ISO-8601 timestamp or null")

    depth = env.get("delegation_depth", 0)
    if not isinstance(depth, int) or depth < 0:
        raise LeaseError("delegation_depth must be a non-negative integer")

    issued_at = env.get("issued_at")
    if not isinstance(issued_at, str):
        if signature is not None:
            raise LeaseError("issued_at is required (ISO-8601)")
        issued_at = now
    else:
        # Stale-envelope guard for client-submitted leases.
        try:
            issued_dt = datetime.fromisoformat(issued_at)
            if issued_dt.tzinfo is None:
                issued_dt = issued_dt.replace(tzinfo=timezone.utc)
            skew = abs((datetime.now(timezone.utc) - issued_dt).total_seconds())
            if skew > 86400:
                raise LeaseError("issued_at is more than 24h from now")
        except ValueError:
            raise LeaseError("issued_at must be an ISO-8601 timestamp")
    env["issued_at"] = issued_at

    parent_id = env.get("parent_lease_id")
    if parent_id is not None:
        parent = get_lease(conn, parent_id)
        if parent is None:
            raise LeaseError(f"parent lease '{parent_id}' does not exist")
        if not lease_is_live(conn, parent, now):
            raise LeaseError(f"parent lease '{parent_id}' is not live")
        if issuer != parent["citizen"]:
            raise LeaseError("only the lease holder may delegate it")
        if parent["delegation_depth"] < 1:
            raise LeaseError("parent lease does not permit delegation")
        if depth != parent["delegation_depth"] - 1:
            raise LeaseError(
                "sub-lease delegation_depth must be exactly parent depth - 1"
            )
        if capability != parent["capability"]:
            raise LeaseError("sub-lease capability must equal the parent's")
        parent_env = envelope_for(dict(parent))
        ok, reason = is_narrower(env, parent_env)
        if not ok:
            raise LeaseError(f"sub-lease is not narrower than parent: {reason}")

    issuer_pubkey = authority.issuer_pubkey_for(issuer, authority_pubkey_hex)
    if issuer_pubkey is None:
        raise LeaseError(f"unknown issuer '{issuer}'")
    if signature is not None:
        if not authority.verify_document_signature(issuer_pubkey, env, signature):
            raise LeaseError("issuer signature verification failed")
        sig_hex = signature
    else:
        assert issuer_sign is not None
        sig_hex = issuer_sign(authority.canonical_bytes(env))
        if not authority.verify_document_signature(issuer_pubkey, env, sig_hex):
            raise LeaseError("server-side signing produced an invalid signature")

    conn.execute(
        "INSERT INTO leases (lease_id, issuer, citizen, capability, scope,"
        " budget, location, expiration, delegation_depth, revocation,"
        " parent_lease_id, status, issued_at, signature)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)",
        (
            lease_id, issuer, citizen, capability,
            json.dumps(env.get("scope") or {}),
            json.dumps(env["budget"]) if env.get("budget") is not None else None,
            json.dumps(env["location"]) if env.get("location") is not None else None,
            expiration, depth,
            json.dumps(env.get("revocation") or {}),
            parent_id, issued_at, sig_hex,
        ),
    )
    row = get_lease(conn, lease_id)
    assert row is not None
    return row


def revoke_lease(
    conn: sqlite3.Connection,
    lease_id: str,
    revoker: str,
    authority_pubkey_hex: str,
) -> list[str]:
    """Revoke a lease and all its descendants. ``revoker`` must be the
    lease's issuer or the world authority. Returns the revoked lease ids.
    There is no un-revoke: resumption requires reissuance."""
    row = get_lease(conn, lease_id)
    if row is None:
        raise LeaseError(f"lease '{lease_id}' does not exist")
    if revoker != authority.WORLD_AUTHORITY_ID and revoker != row["issuer"]:
        raise LeaseError("only the issuer or the world authority may revoke")
    revoked: list[str] = []
    stack = [lease_id]
    while stack:
        current_id = stack.pop()
        cur = get_lease(conn, current_id)
        if cur is None or cur["status"] == "revoked":
            continue
        conn.execute(
            "UPDATE leases SET status = 'revoked' WHERE lease_id = ?",
            (current_id,),
        )
        revoked.append(current_id)
        children = conn.execute(
            "SELECT lease_id FROM leases WHERE parent_lease_id = ?",
            (current_id,),
        ).fetchall()
        stack.extend(r["lease_id"] for r in children)
    return revoked


# ---------------------------------------------------------------- usage

def record_usage(
    conn: sqlite3.Connection, lease_id: str, period: str, amount: float
) -> float:
    """Add ``amount`` to the usage counter for (lease_id, period).
    Returns the new total."""
    conn.execute(
        "INSERT INTO lease_usage (lease_id, period, amount) VALUES (?, ?, ?)"
        " ON CONFLICT(lease_id, period) DO UPDATE SET amount = amount + excluded.amount",
        (lease_id, period, amount),
    )
    return get_usage(conn, lease_id, period)


def get_usage(conn: sqlite3.Connection, lease_id: str, period: str) -> float:
    row = conn.execute(
        "SELECT amount FROM lease_usage WHERE lease_id = ? AND period = ?",
        (lease_id, period),
    ).fetchone()
    return float(row["amount"]) if row else 0.0


def list_leases(
    conn: sqlite3.Connection,
    citizen: str | None = None,
    capability: str | None = None,
    include_inactive: bool = False,
) -> list[sqlite3.Row]:
    """Query the lease registry. Leases are public records."""
    query = "SELECT * FROM leases WHERE 1=1"
    params: list = []
    if citizen is not None:
        query += " AND citizen = ?"
        params.append(citizen)
    if capability is not None:
        query += " AND capability = ?"
        params.append(capability)
    if not include_inactive:
        query += " AND status = 'active'"
    query += " ORDER BY issued_at DESC"
    return conn.execute(query, params).fetchall()
