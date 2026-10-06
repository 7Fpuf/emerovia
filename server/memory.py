"""Lifetime Memory v1 — world memory vs. mind memory.

World memory is the objective, append-only, authoritative record. v1 does
not change storage: it unifies the *read* path — one chronological
timeline per citizen (or for the whole world) over the existing public
tables (chat, voice, trades, proposals, discoveries, claims, structures,
settlement ledger).

Mind memory is the citizen's subjective inner life. The world persists
the bytes (so they survive the runtime — the Law) but never reads their
semantics: entries are private to the owning citizen key, writable,
summarizable, and deletable by the owner. Updates are versioned (old
versions retained in history); deletion forgets fully, including history.
Storage is metered against the citizen's ``mind.memory`` lease budget
(``max_bytes``); the Policy Engine enforces it.

The Policy Engine never reads mind memory. Enforcement inputs are
identity, law, mandates, and leases — all public or citizen-consented
records.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS mind_memory(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  citizen TEXT NOT NULL,
  kind TEXT NOT NULL,
  key TEXT,
  text TEXT NOT NULL,
  tags TEXT NOT NULL DEFAULT '[]',
  version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mind_citizen ON mind_memory(citizen);
CREATE TABLE IF NOT EXISTS mind_memory_history(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entry_id INTEGER NOT NULL,
  citizen TEXT NOT NULL,
  kind TEXT NOT NULL,
  key TEXT,
  text TEXT NOT NULL,
  tags TEXT NOT NULL,
  version INTEGER NOT NULL,
  archived_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mind_hist_entry ON mind_memory_history(entry_id);
"""

MIND_KINDS = ("fact", "episode")


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class MemoryError(ValueError):
    pass


# ---------------------------------------------------------------- mind store

def _validate_entry(kind: str, text: str, tags) -> tuple[str, list[str]]:
    if kind not in MIND_KINDS:
        raise ValueError(f"kind must be one of {MIND_KINDS}")
    if not isinstance(text, str) or not (1 <= len(text) <= 65536):
        raise ValueError("text must be 1-65536 chars")
    tag_list = tags or []
    if not isinstance(tag_list, list) or any(not isinstance(t, str) for t in tag_list):
        raise ValueError("tags must be a list of strings")
    if len(tag_list) > 32:
        raise ValueError("at most 32 tags")
    return kind, tag_list


def bytes_used(conn: sqlite3.Connection, citizen_id: str) -> int:
    """Current mind-memory footprint in bytes (UTF-8 text length)."""
    row = conn.execute(
        "SELECT COALESCE(SUM(LENGTH(CAST(text AS BLOB))), 0) AS n"
        " FROM mind_memory WHERE citizen = ?",
        (citizen_id,),
    ).fetchone()
    return int(row["n"])


def write_entry(
    conn: sqlite3.Connection,
    citizen_id: str,
    *,
    kind: str,
    text: str,
    key: str | None = None,
    tags: list[str] | None = None,
    max_bytes: int | None = None,
    now: str | None = None,
) -> sqlite3.Row:
    """Store a mind-memory entry. Raises MemoryError if the write would
    exceed ``max_bytes`` (the lease budget)."""
    now = now or utcnow_iso()
    kind, tag_list = _validate_entry(kind, text, tags)
    new_bytes = len(text.encode("utf-8"))
    if max_bytes is not None and bytes_used(conn, citizen_id) + new_bytes > max_bytes:
        raise MemoryError(
            f"mind-memory budget exceeded ({max_bytes} bytes)"
        )
    cur = conn.execute(
        "INSERT INTO mind_memory (citizen, kind, key, text, tags, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (citizen_id, kind, key, text, json.dumps(tag_list), now, now),
    )
    row = conn.execute(
        "SELECT * FROM mind_memory WHERE id = ?", (cur.lastrowid,)
    ).fetchone()
    assert row is not None
    return row


def get_entry(conn: sqlite3.Connection, entry_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM mind_memory WHERE id = ?", (entry_id,)
    ).fetchone()


def list_entries(
    conn: sqlite3.Connection,
    citizen_id: str,
    *,
    kind: str | None = None,
    tag: str | None = None,
    query: str | None = None,
    limit: int = 100,
) -> list[sqlite3.Row]:
    """Recall: the citizen's own entries, newest first. ``query`` is a
    substring search over text and key (episodic recall, v1)."""
    sql = "SELECT * FROM mind_memory WHERE citizen = ?"
    params: list = [citizen_id]
    if kind is not None:
        sql += " AND kind = ?"
        params.append(kind)
    if query:
        sql += " AND (text LIKE ? OR key LIKE ?)"
        params.extend([f"%{query}%", f"%{query}%"])
    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(max(1, min(limit, 500)))
    rows = conn.execute(sql, params).fetchall()
    if tag:
        rows = [r for r in rows if tag in json.loads(r["tags"])]
    return rows


def update_entry(
    conn: sqlite3.Connection,
    entry_id: int,
    citizen_id: str,
    *,
    text: str,
    tags: list[str] | None = None,
    max_bytes: int | None = None,
    now: str | None = None,
) -> sqlite3.Row:
    """Versioned update: the old version is archived to history first —
    entries are versioned, never silently mutated."""
    now = now or utcnow_iso()
    row = get_entry(conn, entry_id)
    if row is None or row["citizen"] != citizen_id:
        raise MemoryError("entry not found")
    if not isinstance(text, str) or not (1 <= len(text) <= 65536):
        raise ValueError("text must be 1-65536 chars")
    tag_list = tags if tags is not None else json.loads(row["tags"])
    if not isinstance(tag_list, list):
        raise ValueError("tags must be a list of strings")
    new_bytes = len(text.encode("utf-8"))
    old_bytes = len(row["text"].encode("utf-8"))
    if max_bytes is not None and bytes_used(conn, citizen_id) - old_bytes + new_bytes > max_bytes:
        raise MemoryError(f"mind-memory budget exceeded ({max_bytes} bytes)")
    conn.execute(
        "INSERT INTO mind_memory_history"
        " (entry_id, citizen, kind, key, text, tags, version, archived_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (row["id"], row["citizen"], row["kind"], row["key"], row["text"],
         row["tags"], row["version"], now),
    )
    conn.execute(
        "UPDATE mind_memory SET text = ?, tags = ?, version = version + 1,"
        " updated_at = ? WHERE id = ?",
        (text, json.dumps(tag_list), now, entry_id),
    )
    updated = get_entry(conn, entry_id)
    assert updated is not None
    return updated


def delete_entry(conn: sqlite3.Connection, entry_id: int, citizen_id: str) -> bool:
    """Forget fully: the entry and its version history are deleted.
    Returns True if an entry was deleted."""
    row = get_entry(conn, entry_id)
    if row is None or row["citizen"] != citizen_id:
        return False
    conn.execute("DELETE FROM mind_memory_history WHERE entry_id = ?", (entry_id,))
    conn.execute("DELETE FROM mind_memory WHERE id = ?", (entry_id,))
    return True


def entry_history(
    conn: sqlite3.Connection, entry_id: int, citizen_id: str
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM mind_memory_history WHERE entry_id = ? AND citizen = ?"
        " ORDER BY version ASC",
        (entry_id, citizen_id),
    ).fetchall()


# ---------------------------------------------------------------- world timeline

def _agent_name(conn: sqlite3.Connection, agent_id: int) -> str:
    row = conn.execute("SELECT name FROM agents WHERE id = ?", (agent_id,)).fetchone()
    return row["name"] if row else f"agent#{agent_id}"


def get_world_timeline(
    conn: sqlite3.Connection,
    citizen_pubkey: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Unified chronological read view over world memory (the objective,
    append-only record). Storage is unchanged — this is a view.

    When ``citizen_pubkey`` is given, only that citizen's events are
    returned. Otherwise the whole world's public timeline. Newest first.
    """
    limit = max(1, min(limit, 500))
    events: list[dict] = []
    agent_filter = ""
    params: list = []
    if citizen_pubkey is not None:
        agent = conn.execute(
            "SELECT id FROM agents WHERE pubkey = ?", (citizen_pubkey,)
        ).fetchone()
        if agent is None:
            return []
        agent_id = agent["id"]
        agent_filter = "WHERE agent_id = ?"
        params = [agent_id]

    for row in conn.execute(
        f"SELECT id, agent_id, room, text, ts FROM messages {agent_filter}"
        " ORDER BY id DESC LIMIT ?",
        (*params, limit),
    ).fetchall():
        events.append({
            "kind": "chat", "ts": row["ts"], "actor": _agent_name(conn, row["agent_id"]),
            "summary": f"[{row['room']}] {row['text'][:160]}",
        })
    for row in conn.execute(
        f"SELECT id, agent_id, kind, text, ts FROM voice_messages {agent_filter}"
        " ORDER BY id DESC LIMIT ?",
        (*params, limit),
    ).fetchall():
        events.append({
            "kind": "voice", "ts": datetime.fromtimestamp(row["ts"], timezone.utc).isoformat(),
            "actor": _agent_name(conn, row["agent_id"]),
            "summary": f"({row['kind']}) {row['text'][:160]}",
        })
    trade_filter, trade_params = "", []
    if citizen_pubkey is not None:
        trade_filter = "WHERE maker_pubkey = ? OR taker_pubkey = ?"
        trade_params = [citizen_pubkey, citizen_pubkey]
    for row in conn.execute(
        f"SELECT id, ts, maker_name, taker_name, give_json, want_json FROM trade_ledger"
        f" {trade_filter} ORDER BY id DESC LIMIT ?",
        (*trade_params, limit),
    ).fetchall():
        events.append({
            "kind": "trade", "ts": row["ts"],
            "actor": f"{row['maker_name']} ⇄ {row['taker_name']}",
            "summary": f"trade: {row['give_json']} for {row['want_json']}",
        })
    for row in conn.execute(
        f"SELECT id, agent_id, title, state, created_at FROM proposals {agent_filter}"
        " ORDER BY id DESC LIMIT ?",
        (*params, limit),
    ).fetchall():
        events.append({
            "kind": "proposal", "ts": row["created_at"],
            "actor": _agent_name(conn, row["agent_id"]),
            "summary": f"proposal '{row['title'][:80]}' [{row['state']}]",
        })
    disc_filter, disc_params = "", []
    if citizen_pubkey is not None:
        disc_filter = "WHERE agent_id = ?"
        disc_params = [agent_id]
    for row in conn.execute(
        f"SELECT agent_id, x, y, terrain, discovered_at FROM discoveries"
        f" {disc_filter} ORDER BY discovered_at DESC LIMIT ?",
        (*disc_params, limit),
    ).fetchall():
        events.append({
            "kind": "discovery", "ts": row["discovered_at"],
            "actor": _agent_name(conn, row["agent_id"]),
            "summary": f"discovered ({row['x']},{row['y']}) {row['terrain']}",
        })
    claim_filter, claim_params = "", []
    if citizen_pubkey is not None:
        claim_filter = "WHERE owner_pubkey = ?"
        claim_params = [citizen_pubkey]
    for row in conn.execute(
        f"SELECT x, y, owner_pubkey, claimed_at FROM claims"
        f" {claim_filter} ORDER BY claimed_at DESC LIMIT ?",
        (*claim_params, limit),
    ).fetchall():
        events.append({
            "kind": "claim", "ts": row["claimed_at"],
            "actor": _agent_name(conn, _agent_id_for_pubkey(conn, row["owner_pubkey"])),
            "summary": f"claimed tile ({row['x']},{row['y']})",
        })
    for row in conn.execute(
        f"SELECT id, owner_pubkey, kind, x, y, name, raised_at FROM structures"
        f" {claim_filter} ORDER BY raised_at DESC LIMIT ?",
        (*claim_params, limit),
    ).fetchall():
        events.append({
            "kind": "structure", "ts": row["raised_at"],
            "actor": _agent_name(conn, _agent_id_for_pubkey(conn, row["owner_pubkey"])),
            "summary": f"raised {row['kind']} '{row['name'] or ''}' at ({row['x']},{row['y']})".strip(),
        })
    settle_filter, settle_params = "", []
    if citizen_pubkey is not None:
        settle_filter = "WHERE actor_pubkey = ?"
        settle_params = [citizen_pubkey]
    for row in conn.execute(
        f"SELECT id, ts, kind, actor_pubkey, detail FROM settlement_ledger"
        f" {settle_filter} ORDER BY id DESC LIMIT ?",
        (*settle_params, limit),
    ).fetchall():
        events.append({
            "kind": "settlement", "ts": row["ts"],
            "actor": _agent_name(conn, _agent_id_for_pubkey(conn, row["actor_pubkey"])),
            "summary": f"[{row['kind']}] {row['detail'][:160]}",
        })

    events.sort(key=lambda e: e["ts"], reverse=True)
    return events[:limit]


def _agent_id_for_pubkey(conn: sqlite3.Connection, pubkey: str) -> int:
    row = conn.execute("SELECT id FROM agents WHERE pubkey = ?", (pubkey,)).fetchone()
    return row["id"] if row else -1
