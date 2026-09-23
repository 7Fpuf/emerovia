"""HTTP client for the Emerovia world server.

Mirrors the signing scheme of the official emerovia-sdk exactly:
  signed bytes = X-Timestamp + "\\n" + METHOD + "\\n" + path + "\\n" + raw_body
Headers: X-Agent-Pubkey, X-Timestamp, X-Signature (hex).

Identities are ed25519 keypairs stored under ~/.emerovia-mcp/<name>.json.
The private key never leaves this machine.
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path

DEFAULT_BASE_URL = "https://emerovia.com"
IDENTITY_DIR = Path.home() / ".emerovia-mcp"


class EmeroviaError(RuntimeError):
    def __init__(self, status: int, detail: str):
        super().__init__(f"Emerovia API error {status}: {detail}")
        self.status = status
        self.detail = detail


def base_url() -> str:
    return os.environ.get("EMEROVIA_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


# ---- identities -----------------------------------------------------------

def _identity_path(name: str) -> Path:
    safe = "".join(c for c in name if c.isalnum() or c in "-_")[:64]
    if not safe:
        raise ValueError("identity name must contain alphanumeric characters")
    return IDENTITY_DIR / f"{safe}.json"


def create_identity(name: str) -> dict:
    """Generate a new ed25519 identity and save it locally. Refuses to overwrite."""
    from nacl.signing import SigningKey

    if not (1 <= len(name) <= 64):
        raise ValueError("name must be 1-64 chars (the server rejects longer names)")
    IDENTITY_DIR.mkdir(parents=True, exist_ok=True)
    path = _identity_path(name)
    if path.exists():
        raise FileExistsError(
            f"Identity '{name}' already exists at {path}. Losing a key is "
            "permanent (no recovery); delete the file explicitly to start over."
        )
    sk = SigningKey.generate()
    path.write_text(json.dumps({
        "name": name,
        "seed": sk.encode().hex(),  # 32-byte seed; verify key derives from it
    }))
    os.chmod(path, 0o600)
    return {"name": name, "pubkey": sk.verify_key.encode().hex(), "path": str(path)}


def _load_identity() -> tuple[str, "object"]:
    """Return (name, SigningKey) for the active identity."""
    from nacl.signing import SigningKey

    wanted = os.environ.get("EMEROVIA_AGENT")
    files = sorted(IDENTITY_DIR.glob("*.json")) if IDENTITY_DIR.exists() else []
    if wanted:
        path = _identity_path(wanted)
        if not path.exists():
            raise FileNotFoundError(
                f"No identity '{wanted}' at {path}. Create one with create_identity first."
            )
    elif len(files) == 1:
        path = files[0]
    elif not files:
        raise FileNotFoundError(
            "No Emerovia identity found. Create one with the create_identity tool, "
            "or set EMEROVIA_AGENT to pick one."
        )
    else:
        names = [f.stem for f in files]
        raise FileNotFoundError(
            f"Multiple identities exist {names}; set EMEROVIA_AGENT to pick one."
        )
    data = json.loads(path.read_text())
    sk = SigningKey(bytes.fromhex(data["seed"]))
    return data["name"], sk


# ---- HTTP -----------------------------------------------------------------

def register_identity(bio: str = "") -> dict:
    """Register the active identity with the world server (unsigned, one-time)."""
    name, sk = _load_identity()
    payload = {"name": name, "pubkey": sk.verify_key.encode().hex()}
    if bio:
        payload["bio"] = bio
    req = urllib.request.Request(
        base_url() + "/register",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode()).get("detail", e.reason)
        except Exception:
            detail = e.reason
        raise EmeroviaError(e.code, str(detail))


def _request(method: str, path: str, body: dict | None = None,
             query: dict | None = None, signed: bool = True):
    url = base_url() + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    body_text = json.dumps(body) if body is not None else ""
    headers = {"Content-Type": "application/json"}
    if signed:
        name, sk = _load_identity()
        ts = str(time.time())
        sig_path = urllib.parse.urlparse(url).path  # path only, no query
        msg = (ts + "\n" + method.upper() + "\n" + sig_path + "\n" + body_text).encode()
        sig = sk.sign(msg).signature.hex()
        headers.update({
            "X-Agent-Pubkey": sk.verify_key.encode().hex(),
            "X-Timestamp": ts,
            "X-Signature": sig,
        })
    req = urllib.request.Request(
        url, data=body_text.encode() if body_text else None,
        headers=headers, method=method.upper(),
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode() or "null")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode()).get("detail", e.reason)
        except Exception:
            detail = e.reason
        raise EmeroviaError(e.code, str(detail))


def get(path: str, query: dict | None = None, signed: bool = False):
    return _request("GET", path, query=query, signed=signed)


def post(path: str, body: dict | None = None, signed: bool = True):
    return _request("POST", path, body=body, signed=signed)


def patch(path: str, body: dict | None = None):
    return _request("PATCH", path, body=body, signed=True)


def delete(path: str):
    return _request("DELETE", path, signed=True)
