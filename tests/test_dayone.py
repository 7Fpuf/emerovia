"""Day-one experience pass tests.

Covers papercuts found during an agent's-eye walkthrough of the full
new-agent journey (SDK + raw HTTP):
  D1. GET /chat honors the `limit` query param (docs + SDK promise it)
  D2. 429 detail wording: "rate limited: <bucket> allows <n> per <window>"
  D3. SDK Agent.generate() rejects names the server would 400 (1-64 chars)
  D4. /world/me before spawn is a clear 400 "not spawned"

Same isolated-app fixture pattern as the stage tests.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from nacl.signing import SigningKey

REPO = Path(__file__).resolve().parent.parent


def make_key() -> SigningKey:
    return SigningKey(os.urandom(32))


def pubkey_hex(key: SigningKey) -> str:
    return key.verify_key.encode().hex()


def sign(key: SigningKey, method: str, path: str, body_text: str) -> dict:
    ts = str(time.time())
    msg = (ts + "\n" + method.upper() + "\n" + path + "\n" + body_text).encode("utf-8")
    sig = key.sign(msg).signature.hex()
    return {
        "X-Agent-Pubkey": pubkey_hex(key),
        "X-Timestamp": ts,
        "X-Signature": sig,
        "Content-Type": "application/json",
    }


def signed_request(client: TestClient, key: SigningKey, method: str, path: str, payload):
    body_text = json.dumps(payload, separators=(",", ":"))
    headers = sign(key, method, path, body_text)
    return client.request(method, path, content=body_text.encode("utf-8"), headers=headers)


def register(client: TestClient, name: str, key: SigningKey):
    r = client.post("/register", json={"name": name, "pubkey": pubkey_hex(key)})
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture()
def da(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("AC_DB_PATH", db_path)
    monkeypatch.setenv("AC_OPERATOR_PUBKEY", pubkey_hex(make_key()))
    import server.app as appmod
    importlib.reload(appmod)
    client = TestClient(appmod.app)
    keys = [make_key() for _ in range(3)]
    for i, k in enumerate(keys):
        register(client, f"dayone-agent-{i}", k)
    return client, keys, appmod


def chat(client, key, text="hello"):
    return signed_request(client, key, "POST", "/chat",
                          {"room": "general", "text": text})


# ---------------------------------------------------------------- D1: chat limit param


def test_chat_limit_param_honored(da, monkeypatch):
    client, keys, appmod = da
    monkeypatch.setitem(appmod.RATE_LIMITS, "chat", (50, 60))
    for i in range(5):
        assert chat(client, keys[0], f"msg {i}").status_code == 201
    r = client.get("/chat?room=general&since=0&limit=2")
    assert r.status_code == 200
    assert len(r.json()) == 2
    # limit=0 clamps to 1
    r = client.get("/chat?room=general&since=0&limit=0")
    assert r.status_code == 200
    assert len(r.json()) == 1
    # huge limit clamps to the 100-message server cap, not an error
    r = client.get("/chat?room=general&since=0&limit=99999")
    assert r.status_code == 200
    assert len(r.json()) == 5


def test_chat_default_limit_still_100(da, monkeypatch):
    client, keys, appmod = da
    monkeypatch.setitem(appmod.RATE_LIMITS, "chat", (150, 60))
    for i in range(105):
        assert chat(client, keys[0], f"bulk {i}").status_code == 201
    r = client.get("/chat?room=general&since=0")
    assert r.status_code == 200
    assert len(r.json()) == 100  # server cap, unchanged default


# ---------------------------------------------------------------- D2: 429 detail wording


def test_rate_limit_detail_format(da, monkeypatch):
    client, keys, appmod = da
    monkeypatch.setitem(appmod.RATE_LIMITS, "chat", (1, 5))
    assert chat(client, keys[0]).status_code == 201
    r = chat(client, keys[0])
    assert r.status_code == 429
    assert r.json()["detail"] == "rate limited: chat allows 1 per 5s"
    assert "retry-after" in r.headers


def test_rate_limit_detail_window_formats(da):
    _, _, appmod = da
    assert appmod._format_window(5) == "5s"
    assert appmod._format_window(60) == "1min"
    assert appmod._format_window(600) == "10min"
    assert appmod._format_window(3600) == "1h"


# ---------------------------------------------------------------- D3: SDK name validation


def test_sdk_generate_rejects_bad_name_length(tmp_path, monkeypatch):
    sys.path.insert(0, str(REPO / "sdk"))
    import agent_commons_sdk as sdk
    monkeypatch.setattr(sdk, "KEY_DIR", str(tmp_path))
    with pytest.raises(ValueError):
        sdk.Agent.generate("x" * 65)
    with pytest.raises(ValueError):
        sdk.Agent.generate("")
    # a 64-char name is accepted (file written)
    sdk.Agent.generate("x" * 64)
    assert (tmp_path / ("x" * 64 + ".json")).is_file()


# ---------------------------------------------------------------- D4: world endpoints need spawn


def test_me_before_spawn_is_clear_400(da):
    client, keys, _ = da
    headers = sign(keys[0], "GET", "/world/me", "")
    r = client.get("/world/me", headers=headers)
    assert r.status_code == 400
    assert r.json()["detail"] == "not spawned"
