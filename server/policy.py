"""Policy Engine — the single decision point for every mutation.

Pipeline (first failure denies; evaluation stops at denial)::

    REQUEST
       ↓  Is this really Nova?                    (identity)
       ↓  Does world law permit it?               (world law)
       ↓  Does Nova's mandate permit it?          (mandate)
       ↓  Does Nova possess the required lease?    (capability leases)
       ↓  Are budget / location / time /
          delegation constraints satisfied?      (lease fields)
       ↓
    WORLD CORE EXECUTES
       ↓
    LEDGER RECORDS RESULT

The engine is deterministic and auditable: given the lease registry, the
mandate set, world law, and the action, the decision is reproducible.
Denials are appended to ``policy_denials`` (public). The engine never
reads mind memory; enforcement inputs are identity, law, mandates, and
leases only.

Integration: the FastAPI app wraps ``authenticated_agent`` with
``authorized_agent``, which resolves the request's capability from
``ROUTE_CAPABILITIES`` and calls ``evaluate``. Any signed mutation
(POST/PUT/DELETE/PATCH) without a capability mapping is denied by
default. Registration (``POST /register``) is the explicit bootstrap
exception: it is unsigned and permissionless, and it is where the default
world-authority mandate is issued.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from nacl.signing import SigningKey

from server import authority, capabilities, leases

SCHEMA = """
CREATE TABLE IF NOT EXISTS policy_mandates(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  citizen TEXT NOT NULL,
  issuer TEXT NOT NULL,
  bounds TEXT NOT NULL,
  issued_at TEXT NOT NULL,
  expires_at TEXT,
  revoked INTEGER NOT NULL DEFAULT 0,
  superseded_by INTEGER,
  signature TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mandates_citizen ON policy_mandates(citizen);
CREATE TABLE IF NOT EXISTS policy_statutes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  description TEXT NOT NULL,
  rule_kind TEXT NOT NULL,
  rule TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS policy_denials(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  citizen TEXT,
  capability TEXT NOT NULL,
  path TEXT,
  failed_check TEXT NOT NULL,
  reason TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_denials_ts ON policy_denials(ts);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- actions

@dataclass(frozen=True)
class Action:
    """A normalized mutation the engine is asked to decide on."""
    capability: str
    location: str | None = None
    amount_bytes: int | None = None  # for byte-budgeted capabilities


@dataclass(frozen=True)
class Decision:
    allowed: bool
    failed_check: str | None  # identity|world_law|mandate|lease|constraints
    reason: str
    lease_id: str | None = None
    mandate_id: int | None = None


# ---------------------------------------------------------------- mandates

MANDATE_ENVELOPE_FIELDS = ("citizen", "issuer", "bounds", "issued_at", "expires_at")


def mandate_envelope(mandate: dict) -> dict:
    env: dict = {}
    for field in MANDATE_ENVELOPE_FIELDS:
        value = mandate.get(field)
        if field == "bounds" and isinstance(value, str):
            value = json.loads(value)
        env[field] = value
    return env


def default_mandate_bounds() -> dict:
    """The default least-privilege mandate issued by the world authority
    to permissionless registrants (and backfilled for existing citizens).

    v1 decision: the baseline preserves every verb citizens already hold
    (all 386 existing tests stay green). Least-privilege tightening is
    future governance, not a silent behavior change. ``object.*`` is
    included: in-world object use (furnace, relay) is baseline
    citizenship today."""
    return {
        "capability_prefixes": ["world.", "econ.", "gov.", "mind.", "object."],
        "note": "default world-authority mandate v1",
    }


class MandateError(ValueError):
    pass


def get_active_mandate(
    conn: sqlite3.Connection, citizen_id: str, now: str | None = None
) -> sqlite3.Row | None:
    now = now or utcnow_iso()
    row = conn.execute(
        "SELECT * FROM policy_mandates WHERE citizen = ? AND revoked = 0"
        " AND superseded_by IS NULL"
        " AND (expires_at IS NULL OR expires_at > ?)"
        " ORDER BY issued_at DESC LIMIT 1",
        (citizen_id, now),
    ).fetchone()
    return row


def issue_mandate(
    conn: sqlite3.Connection,
    *,
    mandate: dict,
    issuer_sign: Callable[[bytes], str] | None = None,
    signature: str | None = None,
    authority_pubkey_hex: str,
    now: str | None = None,
) -> sqlite3.Row:
    """Issue a mandate. The citizen never signs its own mandate: issuer
    must differ from citizen. Either ``issuer_sign`` (server-side) or
    ``signature`` (verified here) is required. A new mandate supersedes
    the citizen's previous active one."""
    now = now or utcnow_iso()
    if (issuer_sign is None) == (signature is None):
        raise MandateError("provide exactly one of issuer_sign or signature")
    env = mandate_envelope(mandate)
    citizen, issuer = env.get("citizen"), env.get("issuer")
    if not isinstance(citizen, str) or not citizen.startswith("emerovia:"):
        raise MandateError("mandate citizen must be 'emerovia:<pubkey>'")
    if issuer not in (authority.WORLD_AUTHORITY_ID,) and not (
        isinstance(issuer, str) and issuer.startswith("emerovia:")
    ):
        raise MandateError(f"unknown mandate issuer '{issuer}'")
    if issuer == citizen:
        raise MandateError("the citizen cannot issue its own mandate")
    bounds = env.get("bounds")
    if not isinstance(bounds, dict) or not bounds.get("capability_prefixes"):
        raise MandateError("mandate bounds must list capability_prefixes")
    issued_at = env.get("issued_at")
    if not isinstance(issued_at, str):
        if signature is not None:
            raise MandateError("issued_at is required (ISO-8601)")
        issued_at = now
    env["issued_at"] = issued_at

    issuer_pubkey = authority.issuer_pubkey_for(issuer, authority_pubkey_hex)
    if issuer_pubkey is None:
        raise MandateError(f"unknown issuer '{issuer}'")
    if signature is not None:
        if not authority.verify_document_signature(issuer_pubkey, env, signature):
            raise MandateError("issuer signature verification failed")
        sig_hex = signature
    else:
        assert issuer_sign is not None
        sig_hex = issuer_sign(authority.canonical_bytes(env))

    previous = get_active_mandate(conn, citizen, now)
    cur = conn.execute(
        "INSERT INTO policy_mandates (citizen, issuer, bounds, issued_at,"
        " expires_at, signature) VALUES (?, ?, ?, ?, ?, ?)",
        (citizen, issuer, json.dumps(bounds), env["issued_at"],
         env.get("expires_at"), sig_hex),
    )
    new_id = cur.lastrowid
    if previous is not None:
        conn.execute(
            "UPDATE policy_mandates SET superseded_by = ? WHERE id = ?",
            (new_id, previous["id"]),
        )
    row = conn.execute(
        "SELECT * FROM policy_mandates WHERE id = ?", (new_id,)
    ).fetchone()
    assert row is not None
    return row


def revoke_mandate(
    conn: sqlite3.Connection, mandate_id: int, revoker: str
) -> None:
    row = conn.execute(
        "SELECT * FROM policy_mandates WHERE id = ?", (mandate_id,)
    ).fetchone()
    if row is None:
        raise MandateError(f"mandate {mandate_id} does not exist")
    if revoker != authority.WORLD_AUTHORITY_ID and revoker != row["issuer"]:
        raise MandateError("only the issuer or the world authority may revoke")
    conn.execute(
        "UPDATE policy_mandates SET revoked = 1 WHERE id = ?", (mandate_id,)
    )


# ---------------------------------------------------------------- world law

class StatuteError(ValueError):
    pass


def add_statute(
    conn: sqlite3.Connection,
    *,
    name: str,
    description: str,
    rule_kind: str,
    rule: dict,
    now: str | None = None,
) -> sqlite3.Row:
    """Record world law. Only the world authority enacts statutes (the API
    layer restricts this to operator-signed requests)."""
    now = now or utcnow_iso()
    if rule_kind not in ("deny_capability",):
        raise StatuteError(f"unknown rule_kind '{rule_kind}'")
    if rule_kind == "deny_capability" and not rule.get("capability"):
        raise StatuteError("deny_capability requires rule.capability")
    try:
        cur = conn.execute(
            "INSERT INTO policy_statutes (name, description, rule_kind, rule, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (name, description, rule_kind, json.dumps(rule), now),
        )
    except sqlite3.IntegrityError:
        raise StatuteError(f"statute '{name}' already exists")
    row = conn.execute(
        "SELECT * FROM policy_statutes WHERE id = ?", (cur.lastrowid,)
    ).fetchone()
    assert row is not None
    return row


def _statute_matches(rule: dict, capability: str) -> bool:
    target = rule.get("capability", "")
    if target.endswith(".*"):
        return capability.startswith(target[:-1])
    return capability == target


def check_world_law(
    conn: sqlite3.Connection, capability: str
) -> tuple[bool, str]:
    """Check 2 of the pipeline. Returns (permitted, reason)."""
    namespace = capability.split(".")[0] if "." in capability else capability
    status_row = conn.execute(
        "SELECT status FROM policy_capabilities WHERE namespace = ? LIMIT 1",
        (namespace,),
    ).fetchone()
    if status_row is not None and status_row["status"] == "closed":
        return False, (
            f"world law: capability namespace '{namespace}.*' is closed"
        )
    statutes = conn.execute(
        "SELECT * FROM policy_statutes WHERE rule_kind = 'deny_capability'"
        " AND active = 1"
    ).fetchall()
    for statute in statutes:
        rule = json.loads(statute["rule"])
        if _statute_matches(rule, capability):
            citizen_sel = rule.get("citizen")
            # Citizen-scoped statutes are matched by the caller passing
            # citizen context; v1 statutes are universal (citizen null).
            if citizen_sel is None:
                return False, (
                    f"world law: statute '{statute['name']}' forbids"
                    f" '{capability}'"
                )
    return True, ""


# ---------------------------------------------------------------- evaluation

def log_denial(    conn: sqlite3.Connection,
    *,
    citizen: str | None,
    capability: str,
    path: str | None,
    failed_check: str,
    reason: str,
    now: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO policy_denials (ts, citizen, capability, path, failed_check, reason)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (now or utcnow_iso(), citizen, capability, path, failed_check, reason),
    )


def evaluate(
    conn: sqlite3.Connection,
    citizen_pubkey: str,
    action: Action,
    path: str | None = None,
    log_denials: bool = True,
) -> Decision:
    """Run the five-check pipeline. Deterministic: the decision is a pure
    function of (lease registry, mandate set, world law, action).

    Check 1 (identity) here asserts a valid citizen record; request
    signature verification is performed by the authentication layer
    before the engine is called."""
    citizen_id = authority.citizen_id_for_pubkey(citizen_pubkey)
    now = utcnow_iso()

    def deny(failed_check: str, reason: str) -> Decision:
        if log_denials:
            log_denial(
                conn, citizen=citizen_id, capability=action.capability,
                path=path, failed_check=failed_check, reason=reason, now=now,
            )
        return Decision(False, failed_check, reason)

    # 1. identity
    agent = conn.execute(
        "SELECT id FROM agents WHERE pubkey = ?", (citizen_pubkey,)
    ).fetchone()
    if agent is None:
        return deny("identity", "unknown citizen")

    # 2. world law
    permitted, reason = check_world_law(conn, action.capability)
    if not permitted:
        return deny("world_law", reason)

    # 3. mandate
    mandate = get_active_mandate(conn, citizen_id, now)
    if mandate is None:
        return deny("mandate", "no active mandate")
    try:
        prefixes = json.loads(mandate["bounds"]).get("capability_prefixes", [])
    except (ValueError, AttributeError):
        prefixes = []
    if not any(
        action.capability == p.rstrip(".") or action.capability.startswith(p)
        for p in prefixes
    ):
        return deny(
            "mandate",
            f"mandate {mandate['id']} does not cover '{action.capability}'",
        )

    # 4. capability lease
    lease = leases.find_covering_lease(conn, citizen_id, action.capability, now)
    if lease is None:
        return deny(
            "lease",
            f"no active lease covers '{action.capability}'",
        )

    # 5. lease constraints
    try:
        budget = json.loads(lease["budget"]) if lease["budget"] else {}
    except ValueError:
        budget = {}
    max_bytes = budget.get("max_bytes")
    if max_bytes is not None and action.amount_bytes is not None:
        used = leases.get_usage(conn, lease["lease_id"], "total")
        if used + action.amount_bytes > max_bytes:
            return deny(
                "constraints",
                f"lease budget exceeded: {used + action.amount_bytes} > {max_bytes} bytes",
            )
    if lease["location"] and action.location:
        try:
            lease_loc = json.loads(lease["location"])
        except ValueError:
            lease_loc = lease["location"]
        if lease_loc != action.location:
            return deny(
                "constraints",
                f"lease location {lease_loc!r} does not cover {action.location!r}",
            )

    return Decision(True, None, "allowed", lease_id=lease["lease_id"],
                    mandate_id=mandate["id"])


def charge_budget(
    conn: sqlite3.Connection,
    citizen_pubkey: str,
    capability: str,
    amount_bytes: int,
) -> None:
    """Meter byte-budgeted usage *after* a successful write. The
    Policy Engine's constraint check gates the write; this records it."""
    lease = leases.find_covering_lease(
        conn, authority.citizen_id_for_pubkey(citizen_pubkey), capability
    )
    if lease is not None:
        leases.record_usage(conn, lease["lease_id"], "total", float(amount_bytes))


# ---------------------------------------------------------------- route map

# (HTTP method, path template, capability). Signed mutations without a
# mapping are denied by default.
ROUTE_CAPABILITIES: list[tuple[str, str, str]] = [
    ("POST", "/chat", "world.chat"),
    ("POST", "/voice/whisper", "world.voice"),
    ("POST", "/voice/talk", "world.voice"),
    ("POST", "/voice/shout", "world.voice"),
    ("POST", "/voice/relay", "world.voice"),
    ("POST", "/proposals", "gov.propose"),
    ("DELETE", "/proposals/{proposal_id}", "gov.propose"),
    ("POST", "/proposals/{proposal_id}/comments", "gov.comment"),
    ("POST", "/proposals/{proposal_id}/endorse", "gov.endorse"),
    ("DELETE", "/proposals/{proposal_id}/endorse", "gov.endorse"),
    ("POST", "/world/spawn", "world.spawn"),
    ("POST", "/world/move", "world.move"),
    ("POST", "/world/disclose", "world.disclose"),
    ("POST", "/world/gather", "world.gather"),
    ("POST", "/world/craft", "world.craft"),
    ("POST", "/world/experiment", "world.craft"),
    ("POST", "/world/claim", "world.claim"),
    ("POST", "/world/build", "world.build"),
    ("POST", "/world/demolish", "world.demolish"),
    ("POST", "/world/transfer", "econ.transfer"),
    ("POST", "/world/refine", "world.refine"),
    ("POST", "/world/farm", "world.farm"),
    ("POST", "/world/tithe", "econ.tithe"),
    ("POST", "/eat", "world.eat"),
    ("POST", "/world/settlements/name", "gov.settlement"),
    ("POST", "/world/settlements/contribute", "econ.settlement"),
    ("POST", "/world/settlements/disburse", "gov.settlement"),
    ("POST", "/world/settlements/disburse/approve", "gov.settlement"),
    ("POST", "/world/settlements/projects", "gov.settlement"),
    ("POST", "/world/settlements/projects/contribute", "econ.settlement"),
    ("POST", "/world/settlements/projects/complete", "gov.settlement"),
    ("POST", "/trade/offers", "econ.trade"),
    ("POST", "/trade/offers/{offer_id}/accept", "econ.trade"),
    ("POST", "/trade/offers/{offer_id}/cancel", "econ.trade"),
    ("POST", "/citizens/card", "world.identity"),
    ("POST", "/mind/entries", "mind.memory"),
    ("PUT", "/mind/entries/{entry_id}", "mind.memory"),
    ("DELETE", "/mind/entries/{entry_id}", "mind.memory"),
    ("PATCH", "/agents/me", "world.identity"),
]

_COMPILED_ROUTES: list[tuple[str, re.Pattern, str]] | None = None


def _compiled_routes() -> list[tuple[str, re.Pattern, str]]:
    global _COMPILED_ROUTES
    if _COMPILED_ROUTES is None:
        _COMPILED_ROUTES = []
        for method, template, capability in ROUTE_CAPABILITIES:
            pattern = "^" + re.sub(r"\{[^}]+\}", r"[^/]+", template) + "$"
            _COMPILED_ROUTES.append((method, re.compile(pattern), capability))
    return _COMPILED_ROUTES


def resolve_capability(method: str, path: str) -> str | None:
    """Map an inbound request to its capability. None = unmapped."""
    method = method.upper()
    for route_method, pattern, capability in _compiled_routes():
        if route_method == method and pattern.match(path):
            return capability
    return None


# ---------------------------------------------------------------- bootstrap

#: Baseline world-authority → * leases. v1 preserves every verb citizens
#: already hold; delegation_depth=0 (no delegation of baseline verbs).
BASELINE_LEASES: list[tuple[str, dict | None]] = [
    ("world.chat", None),
    ("world.voice", None),
    ("world.spawn", None),
    ("world.move", None),
    ("world.disclose", None),
    ("world.gather", None),
    ("world.craft", None),
    ("world.claim", None),
    ("world.build", None),
    ("world.demolish", None),
    ("world.refine", None),
    ("world.farm", None),
    ("world.eat", None),
    ("world.identity", None),
    ("econ.trade", None),
    ("econ.transfer", None),
    ("econ.tithe", None),
    ("econ.settlement", None),
    ("gov.propose", None),
    ("gov.comment", None),
    ("gov.endorse", None),
    ("gov.settlement", None),
    ("mind.memory", {"max_bytes": 262144}),
]


def bootstrap(
    conn: sqlite3.Connection,
    authority_key: SigningKey,
    authority_pubkey_hex: str,
) -> dict:
    """Idempotent policy bootstrap: capability catalog, policy schema,
    baseline ``*`` leases, and default mandates for agents lacking one.
    Safe to run on every app start."""
    capabilities.ensure_schema(conn)
    capabilities.seed_catalog(conn)
    ensure_schema(conn)
    leases.ensure_schema(conn)
    report = {"baseline_leases_issued": 0, "mandates_issued": 0}

    def issuer_sign(data: bytes) -> str:
        return authority_key.sign(data).signature.hex()

    for capability, budget in BASELINE_LEASES:
        lease_id = f"baseline:{capability}"
        if leases.get_lease(conn, lease_id) is not None:
            continue
        leases.issue_lease(
            conn,
            lease={
                "lease_id": lease_id,
                "issuer": authority.WORLD_AUTHORITY_ID,
                "citizen": "*",
                "capability": capability,
                "scope": {},
                "budget": budget,
                "location": None,
                "expiration": None,
                "delegation_depth": 0,
                "revocation": {"notice": "immediate"},
                "parent_lease_id": None,
                "issued_at": utcnow_iso(),
            },
            issuer_sign=issuer_sign,
            authority_pubkey_hex=authority_pubkey_hex,
        )
        report["baseline_leases_issued"] += 1

    for agent in conn.execute("SELECT pubkey FROM agents").fetchall():
        citizen_id = authority.citizen_id_for_pubkey(agent["pubkey"])
        if get_active_mandate(conn, citizen_id) is not None:
            continue
        issue_mandate(
            conn,
            mandate={
                "citizen": citizen_id,
                "issuer": authority.WORLD_AUTHORITY_ID,
                "bounds": default_mandate_bounds(),
                "issued_at": utcnow_iso(),
                "expires_at": None,
            },
            issuer_sign=issuer_sign,
            authority_pubkey_hex=authority_pubkey_hex,
        )
        report["mandates_issued"] += 1
    return report
