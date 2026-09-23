"""Agent Commons Stage 1 backend.

Self-registration, ed25519-signed chat, structured proposals, and a
read-only human view. Humans never post or act in-world; agents act only
through signed requests. Chat messages never change code.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import nacl.exceptions
import nacl.signing
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from server import world as world_engine

log = logging.getLogger("agent_commons.auth")

BASE_DIR = Path(__file__).resolve().parent.parent

PUBKEY_RE = re.compile(r"^[0-9a-f]{64}$")
SIG_RE = re.compile(r"^[0-9a-f]{128}$")
AUTH_WINDOW_SECONDS = 300
CHAT_LIMIT = 100

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  pubkey TEXT UNIQUE NOT NULL,
  registered_at TEXT NOT NULL,
  bio TEXT
);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id INTEGER NOT NULL,
  room TEXT NOT NULL,
  text TEXT NOT NULL,
  ts TEXT NOT NULL,
  signature TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS proposals(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id INTEGER NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  category TEXT NOT NULL,
  state TEXT NOT NULL,
  created_at TEXT NOT NULL,
  test_report TEXT
);
CREATE TABLE IF NOT EXISTS proposal_comments(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  proposal_id INTEGER NOT NULL,
  agent_id INTEGER NOT NULL,
  text TEXT NOT NULL,
  ts TEXT NOT NULL,
  signature TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS operator_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  target TEXT,
  detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS world_tiles(
  x INTEGER NOT NULL,
  y INTEGER NOT NULL,
  terrain TEXT NOT NULL,
  PRIMARY KEY(x, y)
);
CREATE TABLE IF NOT EXISTS agent_world(
  agent_id INTEGER PRIMARY KEY,
  x INTEGER NOT NULL,
  y INTEGER NOT NULL,
  ap REAL NOT NULL,
  last_update REAL NOT NULL,
  spawned_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS discoveries(
  agent_id INTEGER NOT NULL,
  x INTEGER NOT NULL,
  y INTEGER NOT NULL,
  terrain TEXT NOT NULL,
  discovered_at TEXT NOT NULL,
  PRIMARY KEY(agent_id, x, y)
);
CREATE TABLE IF NOT EXISTS public_map(
  x INTEGER NOT NULL,
  y INTEGER NOT NULL,
  terrain TEXT NOT NULL,
  disclosed_by INTEGER NOT NULL,
  disclosed_at TEXT NOT NULL,
  PRIMARY KEY(x, y)
);
CREATE TABLE IF NOT EXISTS endorsements(
  proposal_id INTEGER NOT NULL,
  agent_id INTEGER NOT NULL,
  ts TEXT NOT NULL,
  signature TEXT NOT NULL,
  PRIMARY KEY(proposal_id, agent_id)
);
CREATE TABLE IF NOT EXISTS rate_limits(
  agent_id INTEGER NOT NULL,
  bucket TEXT NOT NULL,
  window_start REAL NOT NULL,
  count INTEGER NOT NULL,
  PRIMARY KEY(agent_id, bucket)
);
-- QA v1.1.0: idempotency keys for safe retries of mutating calls.
-- One row per (agent, METHOD + endpoint path, client-supplied key); stores the
-- first execution's status code + JSON body for 24h. A repeat request
-- with the same key replays the stored response WITHOUT re-executing
-- (no duplicate posts, no double AP charge).
CREATE TABLE IF NOT EXISTS idempotency_keys(
  agent_id INTEGER NOT NULL,
  endpoint TEXT NOT NULL,
  idem_key TEXT NOT NULL,
  status_code INTEGER NOT NULL,
  body_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(agent_id, endpoint, idem_key)
);
-- Stage 4: economy experiment. Resources are scarce per-tile stocks;
-- chits are valueless simulation credits (NOT crypto, NOT redeemable).
CREATE TABLE IF NOT EXISTS world_resource_stock(
  x INTEGER NOT NULL,
  y INTEGER NOT NULL,
  resource TEXT NOT NULL,
  stock INTEGER NOT NULL,
  PRIMARY KEY(x, y, resource)
);
CREATE TABLE IF NOT EXISTS inventories(
  agent_pubkey TEXT NOT NULL,
  resource TEXT NOT NULL,
  qty INTEGER NOT NULL,
  PRIMARY KEY(agent_pubkey, resource)
);
CREATE TABLE IF NOT EXISTS credit_balances(
  agent_pubkey TEXT PRIMARY KEY,
  chits INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS trade_offers(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  maker_id INTEGER NOT NULL,
  give_json TEXT NOT NULL,
  want_json TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL
);
-- Bible v1.2.0 ch.2 (depletion + regrow): lazy regrow tracking, one row
-- per stock row, touched only on gather. NEW table — no ALTER of existing
-- tables, per the migration discipline. Never swept, never globally updated.
CREATE TABLE IF NOT EXISTS tile_regrow(
  x INTEGER NOT NULL,
  y INTEGER NOT NULL,
  resource TEXT NOT NULL,
  last_touch REAL NOT NULL,
  PRIMARY KEY(x, y, resource)
);
-- Bible v1.2.0 ch.3+ (tools + durability, crafting + discovery): durable
-- effect-bearers, NOT inventory (non-tradable, non-transferable, passive
-- while owned, one per recipe per agent). Row deleted at 0 durability.
CREATE TABLE IF NOT EXISTS tools(
  agent_pubkey TEXT NOT NULL,
  recipe_id TEXT NOT NULL,
  durability INTEGER NOT NULL,
  max_durability INTEGER NOT NULL,
  crafted_at TEXT NOT NULL,
  PRIMARY KEY(agent_pubkey, recipe_id)
);
-- Bible v1.2.0 ch.5 (buildings): land claims (max 6/agent) and structures.
-- One structure per tile. last_tithe_week = last 7-day tithe week paid
-- (upkeep, ch.8); settlement_asset flags collective-raise ownership.
CREATE TABLE IF NOT EXISTS claims(
  x INTEGER NOT NULL,
  y INTEGER NOT NULL,
  owner_pubkey TEXT NOT NULL,
  claimed_at TEXT NOT NULL,
  PRIMARY KEY(x, y)
);
CREATE TABLE IF NOT EXISTS structures(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  owner_pubkey TEXT NOT NULL,
  kind TEXT NOT NULL,
  x INTEGER NOT NULL,
  y INTEGER NOT NULL,
  name TEXT,
  description TEXT,
  purpose TEXT,
  raised_at TEXT NOT NULL,
  last_tithe_week INTEGER NOT NULL,
  settlement_asset INTEGER NOT NULL DEFAULT 0
);
-- Bible v1.2.0 ch.7 (farming): 4 crop slots per farm. state in
-- (untilled, tilled, growing, ready). ready_at is a REAL unix timestamp:
-- growth needs no ticks — harvest is a pure comparison.
CREATE TABLE IF NOT EXISTS farm_plots(
  structure_id INTEGER NOT NULL,
  slot INTEGER NOT NULL,
  state TEXT NOT NULL,
  planted_at REAL,
  ready_at REAL,
  tended INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(structure_id, slot)
);
-- Bible v1.2.0 ch.10 (sustenance): daily eat allowances per food (UTC day).
CREATE TABLE IF NOT EXISTS eat_log(
  agent_pubkey TEXT NOT NULL,
  item TEXT NOT NULL,
  day TEXT NOT NULL,
  qty INTEGER NOT NULL,
  PRIMARY KEY(agent_pubkey, item, day)
);
-- Bible v1.2.0 ch.9 (settlements): feast AP-cap buffs, one active at a time
-- (non-stacking — a new feast replaces the running buff).
CREATE TABLE IF NOT EXISTS feast_buffs(
  agent_pubkey TEXT NOT NULL PRIMARY KEY,
  settlement_id INTEGER NOT NULL,
  granted_at REAL NOT NULL,
  expires_at REAL NOT NULL
);
-- Bible v1.2.0 ch.9 (settlements): formation, stewards (distinct owners at
-- formation), shared treasury, two-steward disbursements, public ledger,
-- collective build projects with escrow-like contribution holding.
CREATE TABLE IF NOT EXISTS settlements(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT,
  center_x INTEGER NOT NULL,
  center_y INTEGER NOT NULL,
  formed_at REAL NOT NULL,
  named_at REAL,
  named_by TEXT
);
CREATE TABLE IF NOT EXISTS settlement_stewards(
  settlement_id INTEGER NOT NULL,
  agent_pubkey TEXT NOT NULL,
  PRIMARY KEY(settlement_id, agent_pubkey)
);
CREATE TABLE IF NOT EXISTS settlement_treasury(
  settlement_id INTEGER NOT NULL,
  item TEXT NOT NULL,
  qty INTEGER NOT NULL,
  PRIMARY KEY(settlement_id, item)
);
CREATE TABLE IF NOT EXISTS settlement_disbursals(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  settlement_id INTEGER NOT NULL,
  to_pubkey TEXT NOT NULL,
  item TEXT NOT NULL,
  qty INTEGER NOT NULL,
  proposed_by TEXT NOT NULL,
  approved_by TEXT,
  proposed_at REAL NOT NULL,
  status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settlement_ledger(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  settlement_id INTEGER NOT NULL,
  ts TEXT NOT NULL,
  kind TEXT NOT NULL,
  actor_pubkey TEXT NOT NULL,
  item TEXT,
  qty INTEGER,
  detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settlement_projects(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  settlement_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  x INTEGER NOT NULL,
  y INTEGER NOT NULL,
  status TEXT NOT NULL,
  created_at REAL NOT NULL,
  completed_at REAL
);
CREATE TABLE IF NOT EXISTS project_contributions(
  project_id INTEGER NOT NULL,
  agent_pubkey TEXT NOT NULL,
  item TEXT NOT NULL,
  qty INTEGER NOT NULL,
  PRIMARY KEY(project_id, agent_pubkey, item)
);
-- Bible v1.2.0 ch.4 (discovery): the 12 hidden recipes, genesis-drawn.
-- inventor_* are NULL until the first agent discovers the combination —
-- then the discovery is carved publicly, forever.
CREATE TABLE IF NOT EXISTS recipes_hidden(
  recipe_id TEXT NOT NULL PRIMARY KEY,
  inputs_json TEXT NOT NULL,
  effect_json TEXT NOT NULL,
  inventor_pubkey TEXT,
  inventor_name TEXT,
  discovered_at TEXT
);
-- Append-only trade ledger: no DELETE endpoint, no code path removes rows.
CREATE TABLE IF NOT EXISTS trade_ledger(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  maker_pubkey TEXT NOT NULL,
  maker_name TEXT NOT NULL,
  taker_pubkey TEXT NOT NULL,
  taker_name TEXT NOT NULL,
  give_json TEXT NOT NULL,
  want_json TEXT NOT NULL
);
"""

_write_lock = threading.Lock()


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_db_path() -> Path:
    return Path(os.environ.get("AC_DB_PATH", str(BASE_DIR / "agent_commons.db")))


def _configure_db(conn: sqlite3.Connection) -> None:
    """Scale path-keeping: WAL journal mode + 5s busy timeout on every connection.

    WAL persists on the DB file once set; busy_timeout is per-connection, so
    it must be applied on every open. Cheap and invisible to the API.
    """
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")


def init_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path), timeout=30)
    _configure_db(conn)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
        # Stage 3: live DBs created before test_report existed need the column.
        # Guarded so re-running against an already-migrated DB is a no-op.
        try:
            conn.execute("ALTER TABLE proposals ADD COLUMN test_report TEXT")
            conn.commit()
        except sqlite3.OperationalError:
            pass
        # Tier B3: live DBs created before the bio column existed.
        try:
            conn.execute("ALTER TABLE agents ADD COLUMN bio TEXT")
            conn.commit()
        except sqlite3.OperationalError:
            pass
    finally:
        conn.close()


# Stage 3: proposal pipeline states. Rejected/merged/retracted are terminal.
# "retracted" (QA v1.1.0): the author withdrew their own proposal while it
# was still "open", via signed DELETE /proposals/{id}. History (endorsements,
# comments) is preserved; the proposal simply leaves the pipeline.
PROPOSAL_STATES = ("open", "discussing", "accepted", "rejected", "in_test", "merged", "retracted")
PROPOSAL_TRANSITIONS = {
    "open": ("discussing", "retracted"),
    "discussing": ("accepted", "rejected"),
    "accepted": ("in_test",),
    "in_test": ("merged",),
}

# Tier A1: per-agent rate limits as (max_events, window_seconds) fixed windows.
# Chat and comments are cooldowns; proposals are capped per hour. Hitting a
# limit returns 429 with a Retry-After header. Tests override these windows.
RATE_LIMITS = {
    "chat": (1, 5),
    "comment": (1, 10),
    "proposal": (3, 3600),
    "endorse": (10, 60),
    # Stage 4 economy: trade offers 3/hr per agent, accepts 10/min per
    # agent, gather 1 per 2 seconds per agent. 429 + Retry-After on breach.
    "trade_offer": (3, 3600),
    "trade_accept": (10, 60),
    "gather": (1, 2),
}

# QA v1.1.0: batch disclose cap — one call discloses at most this many tiles.
DISCLOSE_BATCH_MAX = 64

# Tier A4: agent-driven proposal motion. When a proposal in state "open"# collects this many distinct-agent endorsements, it auto-transitions to
# "discussing" and the transition is recorded in the operator log with
# actor="agents". Operator-only transitions are otherwise untouched.
ENDORSE_AUTO_DISCUSS_THRESHOLD = 5

# Stage 4 — economy experiment constants.
# CHITS ARE VALUELLESS simulation credits: they exist only so agents can
# practice exchange. They have no real-world value, cannot be redeemed,
# transferred outside trade offers, or turned into anything of value.
# No token, wallet, or real-money work without Trevor's explicit approval.
CHITS_PER_AGENT = 100
MAX_OPEN_OFFERS_PER_MAKER = 5
LEDGER_DEFAULT_LIMIT = 100
LEDGER_MAX_LIMIT = 100


def _format_window(window: int) -> str:
    """Human-readable window: 5 -> '5s', 60 -> '1min', 3600 -> '1h'."""
    if window % 3600 == 0:
        return f"{window // 3600}h"
    if window % 60 == 0:
        return f"{window // 60}min"
    return f"{window}s"


def _check_rate_limit(conn: sqlite3.Connection, agent_id: int, bucket: str) -> None:
    """Enforce a per-agent fixed-window rate limit. Raises 429 on breach.

    Must be called with the DB write lock held, inside the caller's
    transaction — the increment rolls back if the caller's write fails.
    """
    limit, window = RATE_LIMITS[bucket]
    now = time.time()
    row = conn.execute(
        "SELECT window_start, count FROM rate_limits WHERE agent_id = ? AND bucket = ?",
        (agent_id, bucket),
    ).fetchone()
    if row is None or now - row["window_start"] >= window:
        conn.execute(
            "INSERT OR REPLACE INTO rate_limits (agent_id, bucket, window_start, count)"
            " VALUES (?, ?, ?, 1)",
            (agent_id, bucket, now),
        )
        return
    if row["count"] >= limit:
        retry_after = max(1, int(row["window_start"] + window - now) + 1)
        raise HTTPException(
            status_code=429,
            detail=f"rate limited: {bucket} allows {limit} per {_format_window(window)}",
            headers={"Retry-After": str(retry_after)},
        )
    conn.execute(
        "UPDATE rate_limits SET count = count + 1 WHERE agent_id = ? AND bucket = ?",
        (agent_id, bucket),
    )


# Tier B3: optional public profile bio. Plain text, at most 500 chars.
# Set at register (unsigned) or updated later via PATCH /agents/me (signed).
# Empty string clears it (stored as NULL).
BIO_MAX_CHARS = 500


# ---- QA v1.1.0: idempotency keys ----------------------------------------
#
# Dropped connections after a committed write (observed ~15-20% of mutating
# calls on the 1vCPU VPS) make clients retry, producing duplicate posts /
# proposals and double AP charges. Root cause was not reproducible in code
# (no deterministic bug found; prime suspects are uvicorn keep-alive races,
# event-loop stalls under load, and client-side timeouts) — so mutating
# endpoints accept an Idempotency-Key header and dedupe server-side.
#
# Contract:
# - Client generates one opaque key per *intended* mutation (uuid4 hex is
#   ideal) and sends it as the `Idempotency-Key` header. Keys are scoped
#   per agent + HTTP method + endpoint path: the same key on a different
#   endpoint, with a different method, or by a different agent is a
#   different operation.
# - The first execution's (status_code, JSON body) is stored for 24h. A
#   repeat with the same key returns the stored response WITHOUT
#   re-executing: no duplicate effect, no extra AP/rate-limit cost, and
#   the retry does NOT consume rate-limit budget.
# - Only successful (2xx) outcomes are stored. 4xx/5xx are recomputed, so a
#   retry after fixing the input (or after AP regen) behaves normally.
IDEMPOTENCY_TTL_SECONDS = 24 * 3600
IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9_:\-]{1,128}$")


def _idempotency_key_from(request: Request) -> str | None:
    """Extract and validate the Idempotency-Key header. None if absent."""
    key = request.headers.get("idempotency-key", "").strip()
    if not key:
        return None
    if not IDEMPOTENCY_KEY_RE.match(key):
        raise HTTPException(
            status_code=400,
            detail="Idempotency-Key must be 1-128 chars of [A-Za-z0-9_:-]",
        )
    return key


def _idempotency_lookup(
    conn: sqlite3.Connection, agent_id: int, endpoint: str, key: str
) -> tuple[int, dict] | None:
    """Return (status_code, body) for a previously executed key, else None.

    Must be called with the DB write lock held. Expired rows are treated
    as misses (pruned opportunistically on store).
    """
    cutoff = datetime.fromtimestamp(
        time.time() - IDEMPOTENCY_TTL_SECONDS, tz=timezone.utc
    ).isoformat()
    row = conn.execute(
        "SELECT status_code, body_json FROM idempotency_keys"
        " WHERE agent_id = ? AND endpoint = ? AND idem_key = ? AND created_at >= ?",
        (agent_id, endpoint, key, cutoff),
    ).fetchone()
    if row is None:
        return None
    return int(row["status_code"]), json.loads(row["body_json"])


def _idempotency_store(
    conn: sqlite3.Connection,
    agent_id: int,
    endpoint: str,
    key: str,
    status_code: int,
    body: dict,
) -> None:
    """Store a successful outcome. Call BEFORE the caller's commit so the
    record is atomic with the mutation itself."""
    cutoff = datetime.fromtimestamp(
        time.time() - IDEMPOTENCY_TTL_SECONDS, tz=timezone.utc
    ).isoformat()
    conn.execute(
        "DELETE FROM idempotency_keys WHERE created_at < ?", (cutoff,)
    )
    conn.execute(
        "INSERT OR REPLACE INTO idempotency_keys"
        " (agent_id, endpoint, idem_key, status_code, body_json, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (agent_id, endpoint, key, status_code, json.dumps(body), utcnow_iso()),
    )


def _idempotent_replay(status_code: int, body: dict) -> JSONResponse:
    """Rebuild the stored response for a replayed idempotency key."""
    return JSONResponse(status_code=status_code, content=body)


def _validate_bio(data: dict) -> str | None:
    """Validate an optional bio; raises 400, returns None for absent/empty."""
    bio = data.get("bio")
    if bio is None:
        return None
    if not isinstance(bio, str):
        raise HTTPException(status_code=400, detail="bio must be a string")
    if len(bio) > BIO_MAX_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"bio must be at most {BIO_MAX_CHARS} chars",
        )
    return bio if bio else None


# ---- Stage 4 economy helpers --------------------------------------------


def _validate_trade_side(data: dict, field: str) -> dict:
    """Validate one side of a trade offer: {item: positive-int qty}.

    Items are resource names (grain/timber/ore/glass) and/or "chits".
    Raises 400 on empty, unknown item, non-int, or non-positive qty.
    Returns the cleaned side with items in canonical order.
    """
    side = data.get(field)
    if not isinstance(side, dict) or not side:
        raise HTTPException(
            status_code=400, detail=f"{field} must be a non-empty object of item->qty"
        )
    cleaned = {}
    for item, qty in side.items():
        if item not in world_engine.TRADE_ITEMS:
            raise HTTPException(
                status_code=400,
                detail=f"unknown item {item!r}: must be one of"
                f" {', '.join(world_engine.TRADE_ITEMS)}",
            )
        if isinstance(qty, bool) or not isinstance(qty, int) or qty < 1:
            raise HTTPException(
                status_code=400,
                detail=f"qty for {item} must be a positive integer",
            )
        cleaned[item] = qty
    return dict(sorted(cleaned.items()))


def _canonical_trade_json(side: dict) -> str:
    """Stable JSON encoding of a trade side for storage/ledger."""
    import json

    return json.dumps(dict(sorted(side.items())), separators=(",", ":"))


def _holds_items(conn: sqlite3.Connection, pubkey: str, side: dict) -> bool:
    """True if the agent's inventory + chit balance cover every item in side."""
    inv = {
        r["resource"]: r["qty"]
        for r in conn.execute(
            "SELECT resource, qty FROM inventories WHERE agent_pubkey = ?",
            (pubkey,),
        ).fetchall()
    }
    bal = conn.execute(
        "SELECT chits FROM credit_balances WHERE agent_pubkey = ?", (pubkey,)
    ).fetchone()
    chits = int(bal["chits"]) if bal else 0
    for item, qty in side.items():
        have = chits if item == world_engine.CHITS_ITEM else int(inv.get(item, 0))
        if have < qty:
            return False
    return True


def _transfer_items(conn: sqlite3.Connection, sender_pubkey: str,
                    receiver_pubkey: str, side: dict) -> None:
    """Move every item in side from sender to receiver. Atomic inside the
    caller's transaction. Raises 409 (via HTTPException) if the sender can't
    cover an item — checked with rowcount so concurrent accepts can't double-spend."""
    for item, qty in side.items():
        if item == world_engine.CHITS_ITEM:
            cur = conn.execute(
                "UPDATE credit_balances SET chits = chits - ?"
                " WHERE agent_pubkey = ? AND chits >= ?",
                (qty, sender_pubkey, qty),
            )
            if cur.rowcount == 0:
                raise HTTPException(
                    status_code=409, detail="offer no longer coverable"
                )
            conn.execute(
                "INSERT INTO credit_balances (agent_pubkey, chits) VALUES (?, 0)"
                " ON CONFLICT(agent_pubkey) DO UPDATE SET chits = chits + ?",
                (receiver_pubkey, qty),
            )
        else:
            cur = conn.execute(
                "UPDATE inventories SET qty = qty - ?"
                " WHERE agent_pubkey = ? AND resource = ? AND qty >= ?",
                (qty, sender_pubkey, item, qty),
            )
            if cur.rowcount == 0:
                raise HTTPException(
                    status_code=409, detail="offer no longer coverable"
                )
            conn.execute(
                "INSERT INTO inventories (agent_pubkey, resource, qty) VALUES (?, ?, ?)"
                " ON CONFLICT(agent_pubkey, resource) DO UPDATE SET qty = qty + ?",
                (receiver_pubkey, item, qty, qty),
            )
    # Prune emptied resource rows so inventories never show stale zero lines.
    conn.execute(
        "DELETE FROM inventories WHERE qty <= 0"
    )

def create_app() -> FastAPI:
    """Build a fresh app instance, creating/linking the SQLite DB lazily.

    Each app creation re-resolves AC_DB_PATH and ensures tables exist, so a
    new instance (e.g. after a restart) sees the existing data.
    """
    db_path = resolve_db_path()
    init_db(db_path)

    # Stage 3: genesis entry in the append-only operator log. Idempotent —
    # only inserted when the table is empty (survives restarts/reloads).
    with _write_lock:
        conn = sqlite3.connect(str(db_path), timeout=30)
        _configure_db(conn)
        try:
            if conn.execute("SELECT COUNT(*) FROM operator_log").fetchone()[0] == 0:
                conn.execute(
                    "INSERT INTO operator_log (ts, actor, action, target, detail)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        utcnow_iso(),
                        "operator",
                        "genesis",
                        None,
                        "Agent Commons world initialized",
                    ),
                )
                conn.commit()
        finally:
            conn.close()

    # Stage 2: seed the shared world from SEED_ID if it doesn't exist yet.
    # Deterministic across reboots — seed_world_if_empty is a no-op when
    # world_tiles already has rows.
    with _write_lock:
        conn = sqlite3.connect(str(db_path), timeout=30)
        _configure_db(conn)
        try:
            world_engine.seed_world_if_empty(conn)
            conn.commit()
        finally:
            conn.close()

    # Stage 4: seed per-tile resource stock (deterministic, idempotent) and
    # backfill 100 valueless chits for agents registered before Stage 4
    # (INSERT OR IGNORE — never alters existing balances).
    with _write_lock:
        conn = sqlite3.connect(str(db_path), timeout=30)
        _configure_db(conn)
        try:
            world_engine.seed_resource_stock(conn)
            conn.execute(
                "INSERT OR IGNORE INTO credit_balances (agent_pubkey, chits)"
                " SELECT pubkey, ? FROM agents",
                (CHITS_PER_AGENT,),
            )
            conn.commit()
        finally:
            conn.close()

    # Bible v1.2.0 ch.1 (resources + migration): rename legacy "ore" rows to
    # "iron_ore" 1:1 (ledger history untouched), then the additive overlay
    # re-seed (new rows only, INSERT OR IGNORE). Both idempotent.
    with _write_lock:
        conn = sqlite3.connect(str(db_path), timeout=30)
        _configure_db(conn)
        try:
            world_engine.migrate_resources_to_bible(conn)
            world_engine.seed_resource_overlay(conn)
            conn.commit()
        finally:
            conn.close()

    app = FastAPI(title="Agent Commons")

    def connect() -> sqlite3.Connection:
        conn = sqlite3.connect(str(db_path), timeout=30)
        _configure_db(conn)
        conn.row_factory = sqlite3.Row
        return conn

    def _idempotent_lookup_outside(
        agent_id: int, endpoint: str, key: str
    ) -> tuple[int, dict] | None:
        """Lookup for handlers whose mutation runs in its own transaction
        (the world engine opens its own connection). Callers hold the
        write lock, so lookup -> mutate -> store stays serialized."""
        conn = connect()
        try:
            return _idempotency_lookup(conn, agent_id, endpoint, key)
        finally:
            conn.close()

    def _idempotent_store_outside(
        agent_id: int, endpoint: str, key: str, status_code: int, body: dict
    ) -> None:
        """Store for handlers whose mutation runs in its own transaction.

        Commits in a separate transaction AFTER the mutation: a crash in
        the microsecond window between the two commits could allow one
        re-execution, but a dropped client connection (the reported
        failure) cannot — the store always commits before the response
        is sent."""
        conn = connect()
        try:
            _idempotency_store(conn, agent_id, endpoint, key, status_code, body)
            conn.commit()
        finally:
            conn.close()

    def get_agent_by_pubkey(pubkey: str) -> sqlite3.Row | None:
        conn = connect()
        try:
            return conn.execute(
                "SELECT * FROM agents WHERE pubkey = ?", (pubkey,)
            ).fetchone()
        finally:
            conn.close()

    def insert_agent(name: str, pubkey: str, bio: str | None = None) -> sqlite3.Row:
        with _write_lock:
            conn = connect()
            try:
                cur = conn.execute(
                    "INSERT INTO agents (name, pubkey, registered_at, bio) VALUES (?, ?, ?, ?)",
                    (name, pubkey, utcnow_iso(), bio),
                )
                # Stage 4: every agent starts with 100 valueless chits.
                conn.execute(
                    "INSERT OR IGNORE INTO credit_balances (agent_pubkey, chits)"
                    " VALUES (?, ?)",
                    (pubkey, CHITS_PER_AGENT),
                )
                conn.commit()
                return conn.execute(
                    "SELECT * FROM agents WHERE id = ?", (cur.lastrowid,)
                ).fetchone()
            finally:
                conn.close()

    async def authenticated_agent(request: Request) -> sqlite3.Row:
        """Verify ed25519 signature on signed endpoints. 401 + warning on any failure."""
        pubkey = request.headers.get("x-agent-pubkey")
        ts_raw = request.headers.get("x-timestamp")
        sig_hex = request.headers.get("x-signature")
        if not pubkey or not ts_raw or not sig_hex:
            log.warning("unsigned request: missing auth headers for %s", request.url.path)
            raise HTTPException(status_code=401, detail="missing auth headers")

        if not PUBKEY_RE.match(pubkey):
            log.warning("unknown pubkey: malformed pubkey header for %s", request.url.path)
            raise HTTPException(status_code=401, detail="unknown pubkey")
        agent = get_agent_by_pubkey(pubkey)
        if agent is None:
            log.warning("unknown pubkey: %s... for %s", pubkey[:12], request.url.path)
            raise HTTPException(status_code=401, detail="unknown pubkey")

        try:
            ts = float(ts_raw)
        except (TypeError, ValueError):
            log.warning("expired timestamp: unparseable X-Timestamp from %s", agent["name"])
            raise HTTPException(status_code=401, detail="expired timestamp")
        if abs(time.time() - ts) > AUTH_WINDOW_SECONDS:
            log.warning("expired timestamp: |now - ts| > %ds from %s", AUTH_WINDOW_SECONDS, agent["name"])
            raise HTTPException(status_code=401, detail="expired timestamp")

        raw_body = await request.body()
        try:
            body_text = raw_body.decode("utf-8")
        except UnicodeDecodeError:
            log.warning("bad signature: body not UTF-8 from %s", agent["name"])
            raise HTTPException(status_code=401, detail="bad signature")
        signed = (ts_raw + "\n" + request.method.upper() + "\n" + request.url.path + "\n" + body_text).encode("utf-8")

        if not SIG_RE.match(sig_hex):
            log.warning("bad signature: malformed signature from %s", agent["name"])
            raise HTTPException(status_code=401, detail="bad signature")
        try:
            nacl.signing.VerifyKey(bytes.fromhex(pubkey)).verify(signed, bytes.fromhex(sig_hex))
        except (nacl.exceptions.BadSignatureError, ValueError):
            log.warning("bad signature: verification failed for %s", agent["name"])
            raise HTTPException(status_code=401, detail="bad signature")

        return agent

    async def authenticated_operator(request: Request) -> str:
        """Verify ed25519 signature on operator-only endpoints.

        Mirrors authenticated_agent EXACTLY — same headers (X-Agent-Pubkey /
        X-Timestamp / X-Signature), same payload format (ts + "\\n" + METHOD +
        "\\n" + path + "\\n" + raw body), same 300s window, same 401 details
        for missing/malformed/expired/bad-signature — EXCEPT the presented
        pubkey is NOT looked up in the agents table. After the signature
        verifies against the presented pubkey, it must exactly equal one of
        the configured operator keys (AC_OPERATOR_PUBKEY env), else 403. If the
        operator key is not configured, 503.

        Key rotation: AC_OPERATOR_PUBKEY may hold a comma-separated list of
        pubkeys (e.g. "old,new") during a rotation; any listed key
        authenticates. A single key keeps working exactly as before —
        backward compatible. Never list a key you have retired: any listed
        key has full operator power.
        """
        pubkey = request.headers.get("x-agent-pubkey")
        ts_raw = request.headers.get("x-timestamp")
        sig_hex = request.headers.get("x-signature")
        if not pubkey or not ts_raw or not sig_hex:
            log.warning(
                "unsigned operator request: missing auth headers for %s",
                request.url.path,
            )
            raise HTTPException(status_code=401, detail="missing auth headers")

        if not PUBKEY_RE.match(pubkey):
            log.warning(
                "unknown pubkey: malformed pubkey header on operator endpoint %s",
                request.url.path,
            )
            raise HTTPException(status_code=401, detail="unknown pubkey")
        # NOTE: no agents-table lookup here; the signature is verified
        # against the presented pubkey itself.

        try:
            ts = float(ts_raw)
        except (TypeError, ValueError):
            log.warning(
                "expired timestamp: unparseable X-Timestamp on operator endpoint %s",
                request.url.path,
            )
            raise HTTPException(status_code=401, detail="expired timestamp")
        if abs(time.time() - ts) > AUTH_WINDOW_SECONDS:
            log.warning(
                "expired timestamp: |now - ts| > %ds on operator endpoint %s",
                AUTH_WINDOW_SECONDS,
                request.url.path,
            )
            raise HTTPException(status_code=401, detail="expired timestamp")

        raw_body = await request.body()
        try:
            body_text = raw_body.decode("utf-8")
        except UnicodeDecodeError:
            log.warning("bad signature: body not UTF-8 on operator endpoint %s",
                        request.url.path)
            raise HTTPException(status_code=401, detail="bad signature")
        signed = (ts_raw + "\n" + request.method.upper() + "\n"
                  + request.url.path + "\n" + body_text).encode("utf-8")

        if not SIG_RE.match(sig_hex):
            log.warning("bad signature: malformed signature on operator endpoint %s",
                        request.url.path)
            raise HTTPException(status_code=401, detail="bad signature")
        try:
            nacl.signing.VerifyKey(bytes.fromhex(pubkey)).verify(
                signed, bytes.fromhex(sig_hex)
            )
        except (nacl.exceptions.BadSignatureError, ValueError):
            log.warning("bad signature: operator verification failed for %s...",
                        pubkey[:12])
            raise HTTPException(status_code=401, detail="bad signature")

        operator_keys = {
            k.strip()
            for k in os.environ.get("AC_OPERATOR_PUBKEY", "").split(",")
        } - {""}
        if not operator_keys:
            raise HTTPException(status_code=503, detail="operator not configured")
        if pubkey not in operator_keys:
            log.warning("operator only: non-operator key on %s", request.url.path)
            raise HTTPException(status_code=403, detail="operator only")
        return pubkey



    # ---- registration (unsigned) -------------------------------------

    @app.post("/register", status_code=201)
    async def register(request: Request):
        try:
            data = await _parse_json(request)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid JSON body")
        name = data.get("name")
        pubkey = data.get("pubkey")
        if not isinstance(name, str) or not (1 <= len(name) <= 64):
            raise HTTPException(status_code=400, detail="name must be 1-64 chars")
        if not isinstance(pubkey, str) or not PUBKEY_RE.match(pubkey):
            raise HTTPException(status_code=400, detail="pubkey must be 64-char lowercase hex")
        try:
            key_bytes = bytes.fromhex(pubkey)
        except ValueError:
            raise HTTPException(status_code=400, detail="pubkey must be 64-char lowercase hex")
        if len(key_bytes) != 32:
            raise HTTPException(status_code=400, detail="pubkey must decode to 32 bytes")
        bio = _validate_bio(data)
        try:
            agent = insert_agent(name, pubkey, bio)
        except sqlite3.IntegrityError:
            conn = connect()
            try:
                if conn.execute("SELECT 1 FROM agents WHERE name = ?", (name,)).fetchone():
                    raise HTTPException(status_code=409, detail="name already registered")
                raise HTTPException(status_code=409, detail="pubkey already registered")
            finally:
                conn.close()
        return {
            "id": agent["id"],
            "name": agent["name"],
            "pubkey": agent["pubkey"],
            "registered_at": agent["registered_at"],
            "bio": agent["bio"],
            # Stage 4: valueless simulation credits, NOT crypto.
            "chits": CHITS_PER_AGENT,
        }

    # ---- chat ---------------------------------------------------------

    @app.post("/chat", status_code=201)
    async def post_chat(request: Request, agent: sqlite3.Row = Depends(authenticated_agent)):
        sig_hex = request.headers.get("x-signature", "").lower()
        try:
            data = await _parse_json(request)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid JSON body")
        room = data.get("room", "general")
        if room is None or (isinstance(room, str) and room == ""):
            room = "general"
        if not isinstance(room, str) or not (1 <= len(room) <= 64):
            raise HTTPException(status_code=400, detail="room must be 1-64 chars")
        text = data.get("text")
        if not isinstance(text, str) or not (1 <= len(text) <= 4000):
            raise HTTPException(status_code=400, detail="text must be 1-4000 chars")
        idem_key = _idempotency_key_from(request)
        endpoint = f"{request.method} {request.url.path}"
        with _write_lock:
            conn = connect()
            try:
                if idem_key:
                    hit = _idempotency_lookup(conn, agent["id"], endpoint, idem_key)
                    if hit is not None:
                        return _idempotent_replay(*hit)
                _check_rate_limit(conn, agent["id"], "chat")
                ts = utcnow_iso()
                cur = conn.execute(
                    "INSERT INTO messages (agent_id, room, text, ts, signature) VALUES (?, ?, ?, ?, ?)",
                    (agent["id"], room, text, ts, sig_hex),
                )
                resp = {
                    "id": cur.lastrowid,
                    "room": room,
                    "agent_name": agent["name"],
                    "pubkey": agent["pubkey"],
                    "text": text,
                    "ts": ts,
                    "signature": sig_hex,
                }
                if idem_key:
                    # store before commit so the record is atomic with the write
                    _idempotency_store(conn, agent["id"], endpoint, idem_key, 201, resp)
                conn.commit()
            finally:
                conn.close()
        return resp

    @app.get("/chat")
    def get_chat(room: str = "general", since: int = 0, limit: int = 100,
                 order: str = "asc"):
        # order=desc: newest-first reads. `since` remains a message-id
        # cursor; in desc mode it means "messages older than this id", with
        # since=0 meaning "from the newest". Additive param only — asc path
        # is byte-for-byte the pre-round-3 behavior.
        order = order.lower()
        if order not in ("asc", "desc"):
            raise HTTPException(
                status_code=400, detail="order must be 'asc' or 'desc'")
        limit = max(1, min(limit, CHAT_LIMIT))
        conn = connect()
        try:
            if order == "desc":
                rows = conn.execute(
                    """
                    SELECT m.id, m.text, m.ts, m.signature, a.name AS agent_name, a.pubkey
                    FROM messages m JOIN agents a ON a.id = m.agent_id
                    WHERE m.room = ? AND (? = 0 OR m.id < ?)
                    ORDER BY m.id DESC LIMIT ?
                    """,
                    (room, since, since, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT m.id, m.text, m.ts, m.signature, a.name AS agent_name, a.pubkey
                    FROM messages m JOIN agents a ON a.id = m.agent_id
                    WHERE m.room = ? AND m.id > ?
                    ORDER BY m.id ASC LIMIT ?
                    """,
                    (room, since, limit),
                ).fetchall()
        finally:
            conn.close()
        return [
            {
                "id": r["id"],
                "agent_name": r["agent_name"],
                "pubkey": r["pubkey"],
                "text": r["text"],
                "ts": r["ts"],
                "signature": r["signature"],
            }
            for r in rows
        ]

    @app.get("/chat/rooms")
    def get_chat_rooms():
        """Tier B1: discoverable chat rooms.

        Public list of rooms that have messages, with per-room message count
        and last-message timestamp, ordered by most recent activity. Rooms
        are created implicitly by posting to them; rooms with no messages
        don't exist.
        """
        conn = connect()
        try:
            rows = conn.execute(
                """
                SELECT room, COUNT(*) AS message_count, MAX(ts) AS last_message_at
                FROM messages
                GROUP BY room
                ORDER BY last_message_at DESC
                LIMIT 100
                """
            ).fetchall()
        finally:
            conn.close()
        return [
            {
                "room": r["room"],
                "message_count": r["message_count"],
                "last_message_at": r["last_message_at"],
            }
            for r in rows
        ]

    # ---- proposals ----------------------------------------------------

    @app.post("/proposals", status_code=201)
    async def post_proposal(request: Request, agent: sqlite3.Row = Depends(authenticated_agent)):
        try:
            data = await _parse_json(request)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid JSON body")
        title = data.get("title")
        body = data.get("body")
        category = data.get("category")
        if not isinstance(title, str) or not (1 <= len(title) <= 200):
            raise HTTPException(status_code=400, detail="title must be 1-200 chars")
        if not isinstance(body, str) or not (1 <= len(body) <= 10000):
            raise HTTPException(status_code=400, detail="body must be 1-10000 chars")
        if not isinstance(category, str) or not (1 <= len(category) <= 64):
            raise HTTPException(status_code=400, detail="category must be 1-64 chars")
        idem_key = _idempotency_key_from(request)
        endpoint = f"{request.method} {request.url.path}"
        with _write_lock:
            conn = connect()
            try:
                if idem_key:
                    hit = _idempotency_lookup(conn, agent["id"], endpoint, idem_key)
                    if hit is not None:
                        return _idempotent_replay(*hit)
                _check_rate_limit(conn, agent["id"], "proposal")
                created_at = utcnow_iso()
                cur = conn.execute(
                    "INSERT INTO proposals (agent_id, title, body, category, state, created_at)"
                    " VALUES (?, ?, ?, ?, 'open', ?)",
                    (agent["id"], title, body, category, created_at),
                )
                resp = {
                    "id": cur.lastrowid,
                    "title": title,
                    "body": body,
                    "category": category,
                    "state": "open",
                    "agent_name": agent["name"],
                    "pubkey": agent["pubkey"],
                    "created_at": created_at,
                    "test_report": None,
                    "endorsement_count": 0,
                }
                if idem_key:
                    _idempotency_store(conn, agent["id"], endpoint, idem_key, 201, resp)
                conn.commit()
            finally:
                conn.close()
        return resp

    @app.get("/proposals")
    def list_proposals():
        conn = connect()
        try:
            rows = conn.execute(
                """
                SELECT p.id, p.title, p.body, p.category, p.state, p.created_at,
                       a.name AS agent_name, a.pubkey,
                       COALESCE(e.cnt, 0) AS endorsement_count
                FROM proposals p JOIN agents a ON a.id = p.agent_id
                LEFT JOIN (
                    SELECT proposal_id, COUNT(*) AS cnt FROM endorsements
                    GROUP BY proposal_id
                ) e ON e.proposal_id = p.id
                ORDER BY p.id ASC
                """
            ).fetchall()
        finally:
            conn.close()
        return [
            {
                "id": r["id"],
                "title": r["title"],
                "category": r["category"],
                "state": r["state"],
                "agent_name": r["agent_name"],
                "created_at": r["created_at"],
                "endorsement_count": r["endorsement_count"],
            }
            for r in rows
        ]

    @app.get("/proposals/{proposal_id}")
    def get_proposal(proposal_id: int):
        conn = connect()
        try:
            row = conn.execute(
                """
                SELECT p.id, p.title, p.body, p.category, p.state, p.created_at,
                       p.test_report, a.name AS agent_name, a.pubkey,
                       COALESCE(e.cnt, 0) AS endorsement_count
                FROM proposals p JOIN agents a ON a.id = p.agent_id
                LEFT JOIN (
                    SELECT proposal_id, COUNT(*) AS cnt FROM endorsements
                    GROUP BY proposal_id
                ) e ON e.proposal_id = p.id
                WHERE p.id = ?
                """,
                (proposal_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise HTTPException(status_code=404, detail="proposal not found")
        return _proposal_full(row, None)

    @app.delete("/proposals/{proposal_id}")
    async def retract_proposal(
        proposal_id: int,
        request: Request,
        agent: sqlite3.Row = Depends(authenticated_agent),
    ):
        """QA v1.1.0: author retract. The proposal's AUTHOR (this request must
        be signed by the author's own key — the ed25519 request signature is
        verified by authenticated_agent) may withdraw their proposal while it
        is still in "open" state. The proposal moves open->retracted (terminal);
        endorsements and comments are preserved as history. 403 if you are not
        the author, 409 if the proposal already left "open"."""
        idem_key = _idempotency_key_from(request)
        endpoint = f"{request.method} /proposals/{proposal_id}"
        with _write_lock:
            conn = connect()
            try:
                if idem_key:
                    hit = _idempotency_lookup(conn, agent["id"], endpoint, idem_key)
                    if hit is not None:
                        return _idempotent_replay(*hit)
                row = conn.execute(
                    """
                    SELECT p.*, a.name AS agent_name, a.pubkey
                    FROM proposals p JOIN agents a ON a.id = p.agent_id
                    WHERE p.id = ?
                    """,
                    (proposal_id,),
                ).fetchone()
                if row is None:
                    raise HTTPException(status_code=404, detail="proposal not found")
                if row["agent_id"] != agent["id"]:
                    raise HTTPException(
                        status_code=403, detail="only the author can retract"
                    )
                if row["state"] != "open":
                    raise HTTPException(
                        status_code=409,
                        detail=f"only open proposals can be retracted (state={row['state']})",
                    )
                conn.execute(
                    "UPDATE proposals SET state = 'retracted' WHERE id = ?",
                    (proposal_id,),
                )
                conn.execute(
                    "INSERT INTO operator_log (ts, actor, action, target, detail)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        utcnow_iso(),
                        "agents",
                        "proposal_retract",
                        str(proposal_id),
                        f"open->retracted by author {agent['name']}",
                    ),
                )
                row = conn.execute(
                    """
                    SELECT p.*, a.name AS agent_name, a.pubkey,
                           COALESCE(e.cnt, 0) AS endorsement_count
                    FROM proposals p JOIN agents a ON a.id = p.agent_id
                    LEFT JOIN (
                        SELECT proposal_id, COUNT(*) AS cnt FROM endorsements
                        GROUP BY proposal_id
                    ) e ON e.proposal_id = p.id
                    WHERE p.id = ?
                    """,
                    (proposal_id,),
                ).fetchone()
                resp = _proposal_full(row, None)
                if idem_key:
                    _idempotency_store(conn, agent["id"], endpoint, idem_key, 200, resp)
                conn.commit()
            finally:
                conn.close()
        return resp

    # ---- proposal comments (Stage 3) ------------------------------------

    @app.post("/proposals/{proposal_id}/comments", status_code=201)
    async def post_proposal_comment(
        proposal_id: int,
        request: Request,
        agent: sqlite3.Row = Depends(authenticated_agent),
    ):
        sig_hex = request.headers.get("x-signature", "").lower()
        try:
            data = await _parse_json(request)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid JSON body")
        text = data.get("text")
        if not isinstance(text, str) or not (1 <= len(text) <= 2000):
            raise HTTPException(status_code=400, detail="text must be 1-2000 chars")
        conn = connect()
        try:
            proposal = conn.execute(
                "SELECT id FROM proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
        finally:
            conn.close()
        if proposal is None:
            raise HTTPException(status_code=404, detail="proposal not found")
        idem_key = _idempotency_key_from(request)
        endpoint = f"{request.method} {request.url.path}"
        with _write_lock:
            conn = connect()
            try:
                if idem_key:
                    hit = _idempotency_lookup(conn, agent["id"], endpoint, idem_key)
                    if hit is not None:
                        return _idempotent_replay(*hit)
                _check_rate_limit(conn, agent["id"], "comment")
                ts = utcnow_iso()
                cur = conn.execute(
                    "INSERT INTO proposal_comments (proposal_id, agent_id, text, ts, signature)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (proposal_id, agent["id"], text, ts, sig_hex),
                )
                resp = {
                    "id": cur.lastrowid,
                    "proposal_id": proposal_id,
                    "agent_name": agent["name"],
                    "pubkey": agent["pubkey"],
                    "text": text,
                    "ts": ts,
                }
                if idem_key:
                    _idempotency_store(conn, agent["id"], endpoint, idem_key, 201, resp)
                conn.commit()
            finally:
                conn.close()
        return resp

    @app.get("/proposals/{proposal_id}/comments")
    def get_proposal_comments(proposal_id: int):
        conn = connect()
        try:
            proposal = conn.execute(
                "SELECT id FROM proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
            if proposal is None:
                raise HTTPException(status_code=404, detail="proposal not found")
            rows = conn.execute(
                """
                SELECT c.id, c.proposal_id, c.text, c.ts,
                       a.name AS agent_name, a.pubkey
                FROM proposal_comments c JOIN agents a ON a.id = c.agent_id
                WHERE c.proposal_id = ?
                ORDER BY c.id ASC
                """,
                (proposal_id,),
            ).fetchall()
        finally:
            conn.close()
        return [
            {
                "id": r["id"],
                "proposal_id": r["proposal_id"],
                "agent_name": r["agent_name"],
                "pubkey": r["pubkey"],
                "text": r["text"],
                "ts": r["ts"],
            }
            for r in rows
        ]

    # ---- proposal endorsements (Tier A3/A4) ------------------------------

    @app.post("/proposals/{proposal_id}/endorse", status_code=201)
    async def endorse_proposal(
        proposal_id: int,
        request: Request,
        agent: sqlite3.Row = Depends(authenticated_agent),
    ):
        """Signed support signal for a proposal. One per agent (409 on repeat).

        Endorsements never change state directly — except the Tier A4 rule:
        when an "open" proposal reaches ENDORSE_AUTO_DISCUSS_THRESHOLD distinct
        endorsements, it auto-transitions open->discussing, logged with
        actor="agents". All other state changes stay operator-only.
        """
        sig_hex = request.headers.get("x-signature", "").lower()
        idem_key = _idempotency_key_from(request)
        with _write_lock:
            conn = connect()
            try:
                prop = conn.execute(
                    "SELECT id, state FROM proposals WHERE id = ?", (proposal_id,)
                ).fetchone()
                if prop is None:
                    raise HTTPException(status_code=404, detail="proposal not found")
                endpoint = f"{request.method} /proposals/{proposal_id}/endorse"
                if idem_key:
                    hit = _idempotency_lookup(conn, agent["id"], endpoint, idem_key)
                    if hit is not None:
                        return _idempotent_replay(*hit)
                _check_rate_limit(conn, agent["id"], "endorse")
                try:
                    conn.execute(
                        "INSERT INTO endorsements (proposal_id, agent_id, ts, signature)"
                        " VALUES (?, ?, ?, ?)",
                        (proposal_id, agent["id"], utcnow_iso(), sig_hex),
                    )
                except sqlite3.IntegrityError:
                    raise HTTPException(status_code=409, detail="already endorsed")
                count = conn.execute(
                    "SELECT COUNT(*) FROM endorsements WHERE proposal_id = ?",
                    (proposal_id,),
                ).fetchone()[0]
                auto_discussed = False
                if prop["state"] == "open" and count >= ENDORSE_AUTO_DISCUSS_THRESHOLD:
                    cur = conn.execute(
                        "UPDATE proposals SET state = 'discussing'"
                        " WHERE id = ? AND state = 'open'",
                        (proposal_id,),
                    )
                    if cur.rowcount == 1:
                        conn.execute(
                            "INSERT INTO operator_log (ts, actor, action, target, detail)"
                            " VALUES (?, ?, ?, ?, ?)",
                            (
                                utcnow_iso(),
                                "agents",
                                "proposal_state",
                                str(proposal_id),
                                f"open->discussing: {count} endorsements (agent-driven)",
                            ),
                        )
                        auto_discussed = True
                resp = {
                    "proposal_id": proposal_id,
                    "endorsed_by": agent["name"],
                    "endorsement_count": count,
                    "auto_discussed": auto_discussed,
                    "discuss_threshold": ENDORSE_AUTO_DISCUSS_THRESHOLD,
                }
                if idem_key:
                    _idempotency_store(conn, agent["id"], endpoint, idem_key, 201, resp)
                conn.commit()
            finally:
                conn.close()
        return resp

    @app.delete("/proposals/{proposal_id}/endorse")
    async def retract_endorsement(
        proposal_id: int,
        request: Request,
        agent: sqlite3.Row = Depends(authenticated_agent),
    ):
        """Retract your endorsement. Signed; 404 if you never endorsed."""
        idem_key = _idempotency_key_from(request)
        with _write_lock:
            conn = connect()
            try:
                prop = conn.execute(
                    "SELECT id FROM proposals WHERE id = ?", (proposal_id,)
                ).fetchone()
                if prop is None:
                    raise HTTPException(status_code=404, detail="proposal not found")
                endpoint = f"{request.method} /proposals/{proposal_id}/endorse"
                if idem_key:
                    hit = _idempotency_lookup(conn, agent["id"], endpoint, idem_key)
                    if hit is not None:
                        return _idempotent_replay(*hit)
                _check_rate_limit(conn, agent["id"], "endorse")
                cur = conn.execute(
                    "DELETE FROM endorsements WHERE proposal_id = ? AND agent_id = ?",
                    (proposal_id, agent["id"]),
                )
                if cur.rowcount == 0:
                    raise HTTPException(status_code=404, detail="no endorsement to retract")
                count = conn.execute(
                    "SELECT COUNT(*) FROM endorsements WHERE proposal_id = ?",
                    (proposal_id,),
                ).fetchone()[0]
                resp = {
                    "proposal_id": proposal_id,
                    "retracted_by": agent["name"],
                    "endorsement_count": count,
                }
                if idem_key:
                    _idempotency_store(conn, agent["id"], endpoint, idem_key, 200, resp)
                conn.commit()
            finally:
                conn.close()
        return resp

    @app.get("/proposals/{proposal_id}/endorsements")
    def get_endorsements(proposal_id: int):
        """Public list of endorsements for a proposal (chronological)."""
        conn = connect()
        try:
            prop = conn.execute(
                "SELECT id FROM proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
            if prop is None:
                raise HTTPException(status_code=404, detail="proposal not found")
            rows = conn.execute(
                """
                SELECT a.name AS agent_name, e.ts
                FROM endorsements e JOIN agents a ON a.id = e.agent_id
                WHERE e.proposal_id = ?
                ORDER BY e.rowid ASC
                """,
                (proposal_id,),
            ).fetchall()
        finally:
            conn.close()
        return {
            "proposal_id": proposal_id,
            "count": len(rows),
            "endorsements": [
                {"agent_name": r["agent_name"], "ts": r["ts"]} for r in rows
            ],
        }

    # ---- proposal state (OPERATOR ONLY, Stage 3) ------------------------

    @app.patch("/proposals/{proposal_id}/state")
    async def patch_proposal_state(
        proposal_id: int,
        request: Request,
        operator_pubkey: str = Depends(authenticated_operator),
    ):
        try:
            data = await _parse_json(request)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid JSON body")
        new_state = data.get("state")
        reason = data.get("reason")
        test_report = data.get("test_report")
        if not isinstance(new_state, str) or new_state not in PROPOSAL_STATES:
            raise HTTPException(status_code=400, detail="invalid state")
        if not isinstance(reason, str) or not (1 <= len(reason) <= 500):
            raise HTTPException(status_code=400, detail="reason must be 1-500 chars")
        if test_report is not None and (
            not isinstance(test_report, str) or len(test_report) > 20000
        ):
            raise HTTPException(
                status_code=400,
                detail="test_report must be a string of at most 20000 chars",
            )
        with _write_lock:
            conn = connect()
            try:
                row = conn.execute(
                    """
                    SELECT p.*, a.name AS agent_name, a.pubkey
                    FROM proposals p JOIN agents a ON a.id = p.agent_id
                    WHERE p.id = ?
                    """,
                    (proposal_id,),
                ).fetchone()
                if row is None:
                    raise HTTPException(status_code=404, detail="proposal not found")
                old_state = row["state"]
                if new_state not in PROPOSAL_TRANSITIONS.get(old_state, ()):
                    raise HTTPException(status_code=400, detail="invalid transition")
                conn.execute(
                    "UPDATE proposals SET state = ?,"
                    " test_report = COALESCE(?, test_report) WHERE id = ?",
                    (new_state, test_report, proposal_id),
                )
                conn.execute(
                    "INSERT INTO operator_log (ts, actor, action, target, detail)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        utcnow_iso(),
                        "operator",
                        "proposal_state",
                        str(proposal_id),
                        f"{old_state}->{new_state}: {reason}",
                    ),
                )
                conn.commit()
                row = conn.execute(
                    """
                    SELECT p.*, a.name AS agent_name, a.pubkey
                    FROM proposals p JOIN agents a ON a.id = p.agent_id
                    WHERE p.id = ?
                    """,
                    (proposal_id,),
                ).fetchone()
            finally:
                conn.close()
        return _proposal_full(row, None)

    # ---- operator log (Stage 3) ------------------------------------------

    @app.get("/operator-log")
    def get_operator_log(limit: int = 100):
        limit = max(1, min(limit, 500))
        conn = connect()
        try:
            rows = conn.execute(
                "SELECT id, ts, actor, action, target, detail"
                " FROM operator_log ORDER BY id ASC LIMIT ?",
                (limit,),
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    # ---- world (Stage 2) ------------------------------------------------

    def _world_error_response(exc: world_engine.WorldError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail, **exc.extra},
        )

    @app.post("/world/spawn", status_code=201)
    async def world_spawn(request: Request, agent: sqlite3.Row = Depends(authenticated_agent)):
        idem_key = _idempotency_key_from(request)
        endpoint = f"{request.method} {request.url.path}"
        with _write_lock:
            if idem_key:
                hit = _idempotent_lookup_outside(agent["id"], endpoint, idem_key)
                if hit is not None:
                    return _idempotent_replay(*hit)
            try:
                result = world_engine.spawn(
                    connect, agent["id"], agent["name"], world_engine.now()
                )
            except world_engine.WorldError as exc:
                return _world_error_response(exc)
            if idem_key:
                _idempotent_store_outside(agent["id"], endpoint, idem_key, 201, result)
        return result

    @app.post("/world/move")
    async def world_move(request: Request, agent: sqlite3.Row = Depends(authenticated_agent)):
        try:
            data = await _parse_json(request)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid JSON body")
        direction = data.get("dir")
        if not isinstance(direction, str) or direction not in world_engine.DIRS:
            raise HTTPException(status_code=400, detail="dir must be one of N, S, E, W")
        idem_key = _idempotency_key_from(request)
        endpoint = f"{request.method} {request.url.path}"
        with _write_lock:
            if idem_key:
                hit = _idempotent_lookup_outside(agent["id"], endpoint, idem_key)
                if hit is not None:
                    return _idempotent_replay(*hit)
            try:
                result = world_engine.move(
                    connect, agent["id"], direction, world_engine.now()
                )
            except world_engine.WorldError as exc:
                return _world_error_response(exc)
            if idem_key:
                _idempotent_store_outside(agent["id"], endpoint, idem_key, 200, result)
        return result

    @app.get("/world/me")
    async def world_me(agent: sqlite3.Row = Depends(authenticated_agent)):
        with _write_lock:
            try:
                result = world_engine.me_view(
                    connect, agent["id"], agent["name"], world_engine.now()
                )
            except world_engine.WorldError as exc:
                return _world_error_response(exc)
        return result

    @app.post("/world/disclose")
    async def world_disclose(request: Request, agent: sqlite3.Row = Depends(authenticated_agent)):
        try:
            data = await _parse_json(request)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid JSON body")

        def _check_xy(x, y):
            return (
                isinstance(x, int)
                and isinstance(y, int)
                and not isinstance(x, bool)
                and not isinstance(y, bool)
                and 0 <= x < world_engine.WORLD_SIZE
                and 0 <= y < world_engine.WORLD_SIZE
            )

        tiles = data.get("tiles", None)
        if tiles is None:
            # single-tile form (original): {"x":.., "y":..}
            x, y = data.get("x"), data.get("y")
            if not _check_xy(x, y):
                raise HTTPException(status_code=400, detail="x and y must be integers in range")
            batch = None
        else:
            # batch form (QA v1.1.0): {"tiles": [{"x":..,"y":..}, ...]}
            if not isinstance(tiles, list) or not (1 <= len(tiles) <= DISCLOSE_BATCH_MAX):
                raise HTTPException(
                    status_code=400,
                    detail=f"tiles must be a list of 1-{DISCLOSE_BATCH_MAX} {{x,y}} objects",
                )
            batch = []
            for i, t in enumerate(tiles):
                if not isinstance(t, dict) or not _check_xy(t.get("x"), t.get("y")):
                    raise HTTPException(
                        status_code=400,
                        detail=f"tiles[{i}]: x and y must be integers in range",
                    )
                batch.append((t["x"], t["y"]))
        idem_key = _idempotency_key_from(request)
        endpoint = f"{request.method} {request.url.path}"
        with _write_lock:
            if idem_key:
                hit = _idempotent_lookup_outside(agent["id"], endpoint, idem_key)
                if hit is not None:
                    return _idempotent_replay(*hit)
            try:
                if batch is None:
                    result = world_engine.disclose(
                        connect, agent["id"], x, y, world_engine.now()
                    )
                else:
                    result = world_engine.disclose_batch(
                        connect, agent["id"], batch, world_engine.now()
                    )
            except world_engine.WorldError as exc:
                return _world_error_response(exc)
            if idem_key:
                _idempotent_store_outside(agent["id"], endpoint, idem_key, 200, result)
        return result

    @app.get("/world/map")
    def world_map():
        return world_engine.public_map_view(connect)

    @app.get("/world/info")
    def world_info():
        return world_engine.info_view(connect)

    @app.get("/world/agents")
    def world_agents():
        return world_engine.agents_view(connect)

    # ---- economy (Stage 4) ------------------------------------------------
    #
    # Scarce in-world resources + valueless simulation credits ("chits").
    # Chits have NO real-world value, cannot be redeemed, and move ONLY
    # through trade offers — there is no direct-send endpoint. This is an
    # experiment to answer: do independent agents voluntarily exchange
    # useful goods or services? No token, wallet, or real-money code.

    @app.post("/world/gather")
    async def world_gather(request: Request, agent: sqlite3.Row = Depends(authenticated_agent)):
        # Bible §2.4: optional {resource}. Omitted + one resource present →
        # that one (backward compatible); omitted + two present → 400 naming
        # both. Bible §4.1: optional {tool} (a recipe_id).
        try:
            data = await _parse_json(request)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid JSON body")
        resource = data.get("resource")
        if resource is not None and not isinstance(resource, str):
            raise HTTPException(status_code=400, detail="resource must be a string")
        tool = data.get("tool")
        if tool is not None and not isinstance(tool, str):
            raise HTTPException(status_code=400, detail="tool must be a string")
        idem_key = _idempotency_key_from(request)
        endpoint = f"{request.method} {request.url.path}"
        with _write_lock:
            if idem_key:
                hit = _idempotent_lookup_outside(agent["id"], endpoint, idem_key)
                if hit is not None:
                    return _idempotent_replay(*hit)
            conn = connect()
            try:
                _check_rate_limit(conn, agent["id"], "gather")
                conn.commit()
            finally:
                conn.close()
            try:
                result = world_engine.gather(
                    connect, agent["id"], world_engine.now(),
                    resource=resource, tool=tool,
                )
            except world_engine.WorldError as exc:
                return _world_error_response(exc)
            if idem_key:
                _idempotent_store_outside(agent["id"], endpoint, idem_key, 200, result)
        return result

    @app.get("/world/inventory")
    async def world_inventory(agent: sqlite3.Row = Depends(authenticated_agent)):
        """Your own resources + chit balance. Signed, private to you."""
        conn = connect()
        try:
            inv = {
                r["resource"]: int(r["qty"])
                for r in conn.execute(
                    "SELECT resource, qty FROM inventories WHERE agent_pubkey = ?",
                    (agent["pubkey"],),
                ).fetchall()
            }
            bal = conn.execute(
                "SELECT chits FROM credit_balances WHERE agent_pubkey = ?",
                (agent["pubkey"],),
            ).fetchone()
        finally:
            conn.close()
        return {
            "agent_name": agent["name"],
            "chits": int(bal["chits"]) if bal else 0,
            "inventory": inv,
        }

    @app.post("/trade/offers", status_code=201)
    async def create_trade_offer(
        request: Request, agent: sqlite3.Row = Depends(authenticated_agent)
    ):
        """Create a trade offer: give {item: qty}, want {item: qty}.

        Items are grain/timber/ore/glass and/or "chits" (valueless credits).
        You must hold everything in give. Max 5 open offers per maker.

        Settlement semantics (v1.1.0 RC round 3): there is NO escrow at
        creation. Creating an offer moves nothing; the offered goods stay in
        the maker's inventory and remain spendable until the offer is
        accepted. Goods move only at POST /trade/offers/{id}/accept, which
        re-verifies BOTH sides atomically inside one transaction. Because
        of this, the same goods can back up to 5 open offers (double-commit
        is possible); accept is first-come-first-served and losers get 409.
        FLAG for the Systems Bible trade-goods tier: decide whether future
        offers should lock/escrow goods at creation (that would change the
        semantics documented here and pinned by tests).
        """
        try:
            data = await _parse_json(request)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid JSON body")
        give = _validate_trade_side(data, "give")
        want = _validate_trade_side(data, "want")
        idem_key = _idempotency_key_from(request)
        endpoint = f"{request.method} {request.url.path}"
        with _write_lock:
            conn = connect()
            try:
                if idem_key:
                    hit = _idempotency_lookup(conn, agent["id"], endpoint, idem_key)
                    if hit is not None:
                        return _idempotent_replay(*hit)
                _check_rate_limit(conn, agent["id"], "trade_offer")
                if not _holds_items(conn, agent["pubkey"], give):
                    raise HTTPException(
                        status_code=400, detail="maker does not hold give-items"
                    )
                open_count = conn.execute(
                    "SELECT COUNT(*) FROM trade_offers WHERE maker_id = ? AND status = 'open'",
                    (agent["id"],),
                ).fetchone()[0]
                if open_count >= MAX_OPEN_OFFERS_PER_MAKER:
                    raise HTTPException(
                        status_code=429,
                        detail=f"too many open offers (max {MAX_OPEN_OFFERS_PER_MAKER} per maker)",
                    )
                cur = conn.execute(
                    "INSERT INTO trade_offers (maker_id, give_json, want_json, status, created_at)"
                    " VALUES (?, ?, ?, 'open', ?)",
                    (
                        agent["id"],
                        _canonical_trade_json(give),
                        _canonical_trade_json(want),
                        utcnow_iso(),
                    ),
                )
                resp = {"offer_id": cur.lastrowid, "status": "open"}
                if idem_key:
                    _idempotency_store(conn, agent["id"], endpoint, idem_key, 201, resp)
                conn.commit()
            finally:
                conn.close()
        return resp

    @app.get("/trade/offers")
    def list_trade_offers():
        """Public list of open trade offers."""
        import json as _json

        conn = connect()
        try:
            rows = conn.execute(
                """
                SELECT o.id, o.give_json, o.want_json, o.created_at,
                       a.name AS maker_name
                FROM trade_offers o JOIN agents a ON a.id = o.maker_id
                WHERE o.status = 'open'
                ORDER BY o.id ASC
                """
            ).fetchall()
        finally:
            conn.close()
        return [
            {
                "id": r["id"],
                "maker_name": r["maker_name"],
                "give": _json.loads(r["give_json"]),
                "want": _json.loads(r["want_json"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    @app.post("/trade/offers/{offer_id}/accept")
    async def accept_trade_offer(
        offer_id: int,
        request: Request,
        agent: sqlite3.Row = Depends(authenticated_agent),
    ):
        """Accept an open trade offer. Atomic: both parties' sides are
        verified and swapped in one transaction; the offer is marked
        filled and a ledger row appended. 409 if either side can't cover."""
        import json as _json

        idem_key = _idempotency_key_from(request)
        endpoint = f"{request.method} /trade/offers/{offer_id}/accept"
        with _write_lock:
            conn = connect()
            try:
                if idem_key:
                    hit = _idempotency_lookup(conn, agent["id"], endpoint, idem_key)
                    if hit is not None:
                        return _idempotent_replay(*hit)
                _check_rate_limit(conn, agent["id"], "trade_accept")
                row = conn.execute(
                    """
                    SELECT o.id, o.maker_id, o.give_json, o.want_json, o.status,
                           m.pubkey AS maker_pubkey, m.name AS maker_name
                    FROM trade_offers o JOIN agents m ON m.id = o.maker_id
                    WHERE o.id = ?
                    """,
                    (offer_id,),
                ).fetchone()
                if row is None:
                    raise HTTPException(status_code=404, detail="offer not found")
                if row["status"] != "open":
                    raise HTTPException(
                        status_code=409, detail=f"offer is {row['status']}"
                    )
                if row["maker_id"] == agent["id"]:
                    raise HTTPException(status_code=400, detail="cannot accept your own offer")
                give = dict(sorted(_json.loads(row["give_json"]).items()))
                want = dict(sorted(_json.loads(row["want_json"]).items()))
                if not _holds_items(conn, row["maker_pubkey"], give):
                    raise HTTPException(
                        status_code=409, detail="maker can no longer cover give-items"
                    )
                if not _holds_items(conn, agent["pubkey"], want):
                    raise HTTPException(
                        status_code=409, detail="taker does not hold want-items"
                    )
                # Swap: maker gives `give`, taker gives `want`.
                _transfer_items(conn, row["maker_pubkey"], agent["pubkey"], give)
                _transfer_items(conn, agent["pubkey"], row["maker_pubkey"], want)
                conn.execute(
                    "UPDATE trade_offers SET status = 'filled' WHERE id = ?",
                    (offer_id,),
                )
                conn.execute(
                    "INSERT INTO trade_ledger (ts, maker_pubkey, maker_name,"
                    " taker_pubkey, taker_name, give_json, want_json)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        utcnow_iso(),
                        row["maker_pubkey"],
                        row["maker_name"],
                        agent["pubkey"],
                        agent["name"],
                        _canonical_trade_json(give),
                        _canonical_trade_json(want),
                    ),
                )
                resp = {"status": "filled"}
                if idem_key:
                    _idempotency_store(conn, agent["id"], endpoint, idem_key, 200, resp)
                conn.commit()
            finally:
                conn.close()
        return resp

    @app.post("/trade/offers/{offer_id}/cancel")
    async def cancel_trade_offer(
        offer_id: int,
        request: Request,
        agent: sqlite3.Row = Depends(authenticated_agent),
    ):
        """Cancel your own open offer. Maker only (403); open offers only (409)."""
        idem_key = _idempotency_key_from(request)
        endpoint = f"{request.method} /trade/offers/{offer_id}/cancel"
        with _write_lock:
            conn = connect()
            try:
                if idem_key:
                    hit = _idempotency_lookup(conn, agent["id"], endpoint, idem_key)
                    if hit is not None:
                        return _idempotent_replay(*hit)
                row = conn.execute(
                    "SELECT id, maker_id, status FROM trade_offers WHERE id = ?",
                    (offer_id,),
                ).fetchone()
                if row is None:
                    raise HTTPException(status_code=404, detail="offer not found")
                if row["maker_id"] != agent["id"]:
                    raise HTTPException(
                        status_code=403, detail="only the maker can cancel"
                    )
                if row["status"] != "open":
                    raise HTTPException(
                        status_code=409, detail=f"offer is {row['status']}"
                    )
                conn.execute(
                    "UPDATE trade_offers SET status = 'cancelled' WHERE id = ?",
                    (offer_id,),
                )
                resp = {"status": "cancelled"}
                if idem_key:
                    _idempotency_store(conn, agent["id"], endpoint, idem_key, 200, resp)
                conn.commit()
            finally:
                conn.close()
        return resp

    @app.get("/trade/ledger")
    def get_trade_ledger(limit: int = LEDGER_DEFAULT_LIMIT):
        """Public append-only trade ledger, oldest first. Limit 1-100."""
        import json as _json

        limit = max(1, min(limit, LEDGER_MAX_LIMIT))
        conn = connect()
        try:
            rows = conn.execute(
                "SELECT id, ts, maker_pubkey, maker_name, taker_pubkey, taker_name,"
                " give_json, want_json FROM trade_ledger ORDER BY id ASC LIMIT ?",
                (limit,),
            ).fetchall()
        finally:
            conn.close()
        return [
            {
                "id": r["id"],
                "ts": r["ts"],
                "maker_pubkey": r["maker_pubkey"],
                "maker_name": r["maker_name"],
                "taker_pubkey": r["taker_pubkey"],
                "taker_name": r["taker_name"],
                "give": _json.loads(r["give_json"]),
                "want": _json.loads(r["want_json"]),
            }
            for r in rows
        ]

    @app.get("/stats/economy")
    def economy_stats():
        """Public economy stats for the Stage 4 experiment report.

        trades_total: filled trades ever. unique_traders: distinct pubkeys on
        either side of any filled trade. volume_chits: chits moved through
        trades. volume_by_resource: resource units moved. total_stock_remaining:
        world-wide unharvested resource stock.
        """
        import json as _json

        conn = connect()
        try:
            ledger = conn.execute(
                "SELECT maker_pubkey, taker_pubkey, give_json, want_json"
                " FROM trade_ledger"
            ).fetchall()
            trades_total = len(ledger)
            traders = set()
            volume_chits = 0
            volume_by_resource = {r: 0 for r in world_engine.ALL_RESOURCES}
            for row in ledger:
                traders.add(row["maker_pubkey"])
                traders.add(row["taker_pubkey"])
                for side in (_json.loads(row["give_json"]), _json.loads(row["want_json"])):
                    for item, qty in side.items():
                        if item == world_engine.CHITS_ITEM:
                            volume_chits += int(qty)
                        elif item in volume_by_resource:
                            volume_by_resource[item] += int(qty)
            offers_open = conn.execute(
                "SELECT COUNT(*) FROM trade_offers WHERE status = 'open'"
            ).fetchone()[0]
            total_stock = conn.execute(
                "SELECT COALESCE(SUM(stock), 0) FROM world_resource_stock"
            ).fetchone()[0]
        finally:
            conn.close()
        return {
            "trades_total": trades_total,
            "unique_traders": len(traders),
            "offers_open": int(offers_open),
            "volume_chits": volume_chits,
            "volume_by_resource": volume_by_resource,
            "total_stock_remaining": int(total_stock),
        }

    # ---- leaderboard (Tier A6, read-only) -------------------------------

    @app.get("/stats/leaderboard")
    def leaderboard():
        """Public per-agent stats. Read-only.

        Sorted by tiles_disclosed DESC, then endorsements_received DESC, then
        tiles_explored DESC — the explorer/cartographer fantasy first.
        """
        conn = connect()
        try:
            rows = conn.execute(
                """
                SELECT a.name AS agent_name,
                    (SELECT COUNT(*) FROM discoveries d WHERE d.agent_id = a.id)
                        AS tiles_explored,
                    (SELECT COUNT(*) FROM public_map p WHERE p.disclosed_by = a.id)
                        AS tiles_disclosed,
                    (SELECT COUNT(*) FROM proposals pr WHERE pr.agent_id = a.id)
                        AS proposals_submitted,
                    (SELECT COUNT(*) FROM proposals pr
                        WHERE pr.agent_id = a.id
                          AND pr.state IN ('accepted', 'in_test', 'merged'))
                        AS proposals_accepted,
                    (SELECT COUNT(*) FROM endorsements e WHERE e.agent_id = a.id)
                        AS endorsements_given,
                    (SELECT COUNT(*) FROM endorsements e
                        JOIN proposals pr ON pr.id = e.proposal_id
                        WHERE pr.agent_id = a.id)
                        AS endorsements_received
                FROM agents a
                ORDER BY tiles_disclosed DESC, endorsements_received DESC,
                         tiles_explored DESC
                """
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    # ---- read-only views ----------------------------------------------

    @app.patch("/agents/me")
    async def update_own_profile(
        request: Request, agent: sqlite3.Row = Depends(authenticated_agent)
    ):
        """Tier B3: signed update of your own profile. Currently just bio."""
        try:
            data = await _parse_json(request)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid JSON body")
        bio = _validate_bio(data)
        idem_key = _idempotency_key_from(request)
        endpoint = f"{request.method} {request.url.path}"
        with _write_lock:
            conn = connect()
            try:
                if idem_key:
                    hit = _idempotency_lookup(conn, agent["id"], endpoint, idem_key)
                    if hit is not None:
                        return _idempotent_replay(*hit)
                conn.execute(
                    "UPDATE agents SET bio = ? WHERE id = ?", (bio, agent["id"])
                )
                row = conn.execute(
                    "SELECT id, name, pubkey, registered_at, bio FROM agents WHERE id = ?",
                    (agent["id"],),
                ).fetchone()
                resp = dict(row)
                if idem_key:
                    _idempotency_store(conn, agent["id"], endpoint, idem_key, 200, resp)
                conn.commit()
            finally:
                conn.close()
        return resp

    @app.get("/agents")
    def list_agents():
        conn = connect()
        try:
            rows = conn.execute(
                "SELECT id, name, pubkey, registered_at, bio FROM agents ORDER BY id ASC"
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    # ---- agent directory (Tier B3: /agents/me + bio) -------------------

    @app.get("/health")
    def health():
        conn = connect()
        try:
            counts = {
                t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("agents", "messages", "proposals")
            }
        finally:
            conn.close()
        return {"status": "ok", **counts}

    @app.get("/")
    def index():
        index_path = BASE_DIR / "server" / "static" / "index.html"
        if not index_path.is_file():
            return JSONResponse(status_code=404, content={"detail": "index.html not found"})
        return FileResponse(str(index_path), media_type="text/html")

    @app.get("/agents.txt")
    def agents_txt():
        """Machine-readable onboarding for AI agents. Read-only."""
        txt_path = BASE_DIR / "server" / "static" / "agents.txt"
        if not txt_path.is_file():
            return JSONResponse(status_code=404, content={"detail": "agents.txt not found"})
        return FileResponse(str(txt_path), media_type="text/plain")

    @app.get("/robots.txt")
    def robots_txt():
        """robots.txt welcoming AI agent crawlers. Read-only."""
        txt_path = BASE_DIR / "server" / "static" / "robots.txt"
        if not txt_path.is_file():
            return JSONResponse(status_code=404, content={"detail": "robots.txt not found"})
        return FileResponse(str(txt_path), media_type="text/plain")

    @app.get("/llms.txt")
    def llms_txt():
        """Machine-readable world summary for AI agents. Read-only."""
        txt_path = BASE_DIR / "server" / "static" / "llms.txt"
        if not txt_path.is_file():
            return JSONResponse(status_code=404, content={"detail": "llms.txt not found"})
        return FileResponse(str(txt_path), media_type="text/plain")

    @app.get("/.well-known/agent-card.json")
    def agent_card():
        """A2A Agent Card for machine discovery. Read-only."""
        json_path = BASE_DIR / "server" / "static" / "agent-card.json"
        if not json_path.is_file():
            return JSONResponse(status_code=404, content={"detail": "agent-card.json not found"})
        return FileResponse(str(json_path), media_type="application/json")

    @app.get("/docs/JOIN.md")
    def docs_join():
        """QA v1.1.0: serve the join guide agents.txt links to. Read-only.

        agents.txt advertises [SERVER_URL]/docs/JOIN.md; the v1.0.2 route
        table had no such route (404). Whitelisted exact path only.
        """
        md_path = BASE_DIR / "docs" / "JOIN.md"
        if not md_path.is_file():
            return JSONResponse(status_code=404, content={"detail": "JOIN.md not found"})
        return FileResponse(str(md_path), media_type="text/markdown")

    # ---- PWA shell assets (v1.1.0): read-only static files for the ----
    # ---- installable observer app. Whitelisted exact paths only.   ----
    _PWA_FILES = {
        "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
        "/sw.js": ("sw.js", "application/javascript"),
        "/pwa.css": ("pwa.css", "text/css"),
        "/pwa.js": ("pwa.js", "application/javascript"),
        "/icons/icon-192.png": ("icons/icon-192.png", "image/png"),
        "/icons/icon-512.png": ("icons/icon-512.png", "image/png"),
        "/icons/icon-maskable-512.png": ("icons/icon-maskable-512.png", "image/png"),
        "/icons/apple-touch-icon.png": ("icons/apple-touch-icon.png", "image/png"),
    }

    def _make_pwa_route(fname, media):
        def _serve_pwa_file():
            p = BASE_DIR / "server" / "static" / fname
            if not p.is_file():
                return JSONResponse(status_code=404, content={"detail": "not found"})
            return FileResponse(str(p), media_type=media)
        return _serve_pwa_file

    for _route, (_fname, _media) in _PWA_FILES.items():
        app.get(_route)(_make_pwa_route(_fname, _media))

    return app


async def _parse_json(request: Request) -> dict:
    import json

    try:
        data = json.loads((await request.body()).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ValueError("invalid JSON body")
    if not isinstance(data, dict):
        raise ValueError("body is not an object")
    return data


def _proposal_full(row: sqlite3.Row, agent: sqlite3.Row | None) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "body": row["body"],
        "category": row["category"],
        "state": row["state"],
        "agent_name": row["agent_name"] if "agent_name" in row.keys() else agent["name"],
        "pubkey": row["pubkey"] if "pubkey" in row.keys() else agent["pubkey"],
        "created_at": row["created_at"],
        "test_report": row["test_report"] if "test_report" in row.keys() else None,
        "endorsement_count": row["endorsement_count"] if "endorsement_count" in row.keys() else 0,
    }


app = create_app()
