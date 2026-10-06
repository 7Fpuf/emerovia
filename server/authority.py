"""World Authority identity and document-signature primitives.

The World Authority is Emerovia itself as an issuer: the signer of default
mandates, baseline capability leases, and statutes. Its ed25519 keypair is
generated once per database directory and stored with 0600 permissions next
to the database file (gitignored). The private key never leaves the server;
the public key is published via GET /policy/authority.

Document signatures (leases, mandates, citizen cards) use a canonical JSON
envelope: json.dumps(obj, sort_keys=True, separators=(",", ":")) encoded as
UTF-8, signed raw with ed25519. This is deliberately distinct from the
request-authentication scheme (ts\\nMETHOD\\npath\\nbody), which authenticates
*requests*; these signatures authenticate *documents*.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from nacl.signing import SigningKey, VerifyKey

WORLD_AUTHORITY_ID = "world-authority"


def citizen_id_for_pubkey(pubkey_hex: str) -> str:
    """The identity string for a citizen key: ``emerovia:<hex-pubkey>``."""
    return f"emerovia:{pubkey_hex}"


def key_path_for_db(db_path: str | Path) -> Path:
    return Path(db_path).parent / "world_authority.key"


def ensure_authority_key(db_path: str | Path) -> SigningKey:
    """Load the world-authority keypair, generating and storing it (0600)
    on first use. The file lives next to the database file."""
    path = key_path_for_db(db_path)
    if path.exists():
        raw = path.read_bytes()
        if len(raw) != 32:
            raise ValueError(f"world authority key at {path} is not 32 bytes")
        return SigningKey(raw)
    key = SigningKey.generate()
    # Write with 0600: open with O_CREAT|O_EXCL so we never follow a
    # symlink or clobber an existing file, then chmod defensively.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, key.encode())
    finally:
        os.close(fd)
    os.chmod(str(path), 0o600)
    return key


def authority_pubkey_hex(db_path: str | Path) -> str:
    return ensure_authority_key(db_path).verify_key.encode().hex()


def canonical_bytes(obj: dict) -> bytes:
    """Canonical encoding of a document for signing/verification."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_document(key: SigningKey, obj: dict) -> str:
    """Sign a canonical document; returns the 128-char hex signature."""
    return key.sign(canonical_bytes(obj)).signature.hex()


def verify_document_signature(
    pubkey_hex: str, obj: dict, signature_hex: str
) -> bool:
    """Verify a document signature. Returns False on any failure
    (malformed key, malformed signature, bad signature) — never raises."""
    try:
        if len(bytes.fromhex(pubkey_hex)) != 32:
            return False
        sig_bytes = bytes.fromhex(signature_hex)
        if len(sig_bytes) != 64:
            return False
        VerifyKey(bytes.fromhex(pubkey_hex)).verify(canonical_bytes(obj), sig_bytes)
        return True
    except Exception:
        return False


def issuer_pubkey_for(issuer: str, authority_pubkey_hex: str) -> str | None:
    """Resolve an issuer identity string to the hex pubkey that verifies
    its signatures. Returns None for unknown issuer forms."""
    if issuer == WORLD_AUTHORITY_ID:
        return authority_pubkey_hex
    if issuer.startswith("emerovia:"):
        pubkey = issuer[len("emerovia:") :]
        if len(pubkey) == 64:
            try:
                bytes.fromhex(pubkey)
                return pubkey
            except ValueError:
                return None
    return None
