"""Scale-plan path-keeping tests for Emerovia.

Two deliberately cheap day-one items (the other two scale items —
db.py centralization and region_id — were deferred as refactor/schema
churn with zero day-one value):

  1. SQLite WAL + busy_timeout: journal_mode=wal persists on the DB file
     and every connection gets a 5000ms busy timeout (invisible to API).
  2. Operator-key rotation: AC_OPERATOR_PUBKEY accepts a comma-separated
     list of pubkeys (backward compatible with a single key), and
     scripts/rotate_operator_key.py generates a fresh pair, writes the
     private key to a 600-mode file only, and prints the public key plus
     the rotation ceremony — never the private key.

Same isolated-app fixture pattern as the other test modules.
"""
from __future__ import annotations

import importlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from nacl.signing import SigningKey

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- helpers

def make_key() -> SigningKey:
    return SigningKey(os.urandom(32))


def pubkey_hex(key: SigningKey) -> str:
    return key.verify_key.encode().hex()


def sign(key: SigningKey, method: str, path: str, body_text: str, ts: str | None = None) -> dict:
    if ts is None:
        ts = str(time.time())
    msg = (ts + "\n" + method.upper() + "\n" + path + "\n" + body_text).encode("utf-8")
    sig = key.sign(msg).signature.hex()
    return {
        "X-Agent-Pubkey": pubkey_hex(key),
        "X-Timestamp": ts,
        "X-Signature": sig,
        "Content-Type": "application/json",
    }


def signed_request(client: TestClient, key: SigningKey, method: str, path: str,
                   payload: dict, ts: str | None = None):
    body_text = json.dumps(payload, separators=(",", ":"))
    headers = sign(key, method, path, body_text, ts=ts)
    return client.request(method, path, content=body_text.encode("utf-8"), headers=headers)


def register(client: TestClient, name: str, key: SigningKey):
    r = client.post("/register", json={"name": name, "pubkey": pubkey_hex(key)})
    assert r.status_code == 201, r.text
    return r.json()


def patch_state(client: TestClient, op_key: SigningKey, proposal_id: int,
                state: str, reason: str):
    return signed_request(
        client, op_key, "PATCH", f"/proposals/{proposal_id}/state",
        {"state": state, "reason": reason},
    )


@pytest.fixture()
def sp(tmp_path, monkeypatch):
    """Fresh app + isolated DB; single operator key. Returns (client, keys, op_key, appmod, db_path)."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("AC_DB_PATH", db_path)
    op_key = make_key()
    monkeypatch.setenv("AC_OPERATOR_PUBKEY", pubkey_hex(op_key))
    import server.app as appmod
    importlib.reload(appmod)
    client = TestClient(appmod.app)
    keys = [make_key() for _ in range(2)]
    register(client, "sp-agent-a", keys[0])
    register(client, "sp-agent-b", keys[1])
    return client, keys, op_key, appmod, db_path


def make_proposal(client, key, title="rotation proposal"):
    r = signed_request(client, key, "POST", "/proposals",
                       {"title": title, "body": "body", "category": "world"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ---------------------------------------------------------------- 1. WAL + busy_timeout

def test_journal_mode_is_wal(sp):
    _, _, _, _, db_path = sp
    mode = sqlite3.connect(db_path).execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"


def test_every_connection_gets_busy_timeout(sp):
    _, _, _, appmod, _ = sp
    conn = sqlite3.connect(":memory:")
    appmod._configure_db(conn)
    timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert timeout == 5000
    conn.close()


# ---------------------------------------------------------------- 2. operator key rotation

def test_single_operator_key_still_works_and_strangers_rejected(sp, monkeypatch):
    client, keys, op_key, _, _ = sp
    pid = make_proposal(client, keys[0])
    r = patch_state(client, op_key, pid, "discussing", "single key works")
    assert r.status_code == 200, r.text
    stranger = make_key()
    pid2 = make_proposal(client, keys[1])
    r = patch_state(client, stranger, pid2, "discussing", "stranger")
    assert r.status_code == 403, r.text


def test_comma_separated_list_accepts_both_keys(sp, monkeypatch):
    client, keys, old_key, _, _ = sp
    new_key = make_key()
    monkeypatch.setenv(
        "AC_OPERATOR_PUBKEY",
        f"{pubkey_hex(old_key)},{pubkey_hex(new_key)}",
    )
    pid1 = make_proposal(client, keys[0])
    r = patch_state(client, old_key, pid1, "discussing", "old key during rotation")
    assert r.status_code == 200, r.text
    pid2 = make_proposal(client, keys[1])
    r = patch_state(client, new_key, pid2, "discussing", "new key during rotation")
    assert r.status_code == 200, r.text


def test_removed_operator_key_is_rejected_after_rotation(sp, monkeypatch):
    client, keys, old_key, _, _ = sp
    new_key = make_key()
    # full ceremony in one test: both listed, then only the new one
    monkeypatch.setenv(
        "AC_OPERATOR_PUBKEY",
        f"{pubkey_hex(old_key)},{pubkey_hex(new_key)}",
    )
    pid = make_proposal(client, keys[0])
    assert patch_state(client, new_key, pid, "discussing", "verify new").status_code == 200
    monkeypatch.setenv("AC_OPERATOR_PUBKEY", pubkey_hex(new_key))
    pid2 = make_proposal(client, keys[1])
    r = patch_state(client, old_key, pid2, "discussing", "old key after removal")
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == "operator only"
    pid3 = make_proposal(client, keys[0], title="third")
    r = patch_state(client, new_key, pid3, "discussing", "new key still works")
    assert r.status_code == 200, r.text


def test_whitespace_tolerated_in_list(sp, monkeypatch):
    client, keys, op_key, _, _ = sp
    monkeypatch.setenv("AC_OPERATOR_PUBKEY", f"  {pubkey_hex(op_key)} , ")
    pid = make_proposal(client, keys[0])
    r = patch_state(client, op_key, pid, "discussing", "whitespace ok")
    assert r.status_code == 200, r.text


def test_rotate_script_keeps_private_key_in_file_only(tmp_path):
    out = tmp_path / "operator.key.new"
    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "rotate_operator_key.py"), str(out)],
        capture_output=True,
        text=True,
        cwd=str(REPO),
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    stdout = proc.stdout

    # private key is in the file (mode 600) and 64 hex chars
    assert out.is_file()
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    private_hex = out.read_text().strip()
    assert len(private_hex) == 64 and all(
        c in "0123456789abcdef" for c in private_hex
    )

    # printed public key derives from the private file
    public_hex = SigningKey(bytes.fromhex(private_hex)).verify_key.encode().hex()
    assert public_hex in stdout
    # private key is NEVER printed/logged
    assert private_hex not in stdout
    assert private_hex not in proc.stderr
    # ceremony steps are printed
    for step in ("AC_OPERATOR_PUBKEY=<old-pubkey>,<new-pubkey>",
                 "AC_OPERATOR_PUBKEY=<new-pubkey>",
                 "ROTATION CEREMONY",
                 "expect 403"):
        assert step in stdout, step


def test_rotate_script_default_output_path(tmp_path, monkeypatch):
    monkeypatch.chdir(REPO)
    proc = subprocess.run(
        [sys.executable, "scripts/rotate_operator_key.py"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    default_out = REPO / "server" / "operator.key.new"
    try:
        assert default_out.is_file()
        assert stat.S_IMODE(default_out.stat().st_mode) == 0o600
        assert len(default_out.read_text().strip()) == 64
    finally:
        default_out.unlink(missing_ok=True)
