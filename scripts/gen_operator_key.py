#!/usr/bin/env python3
"""Generate the Agent Commons operator keypair (ed25519).

Writes the PRIVATE key (hex) to server/operator.key with mode 600 and
prints the public key hex plus the exact env line the server needs.

IMPORTANT: the server only ever needs the PUBLIC key, via the
AC_OPERATOR_PUBKEY environment variable. server/operator.key is for the
human/operator client that signs PATCH /proposals/{id}/state requests —
it must never be committed, copied to the server, or exposed anywhere.
The private key is never printed here; it goes only into the file.

Usage:
    cd ~/workspace/agent-commons
    .venv/bin/python scripts/gen_operator_key.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "server" / "operator.key"


def main() -> None:
    try:
        from nacl.signing import SigningKey
    except ImportError:
        sys.exit("pynacl is required: run with the project venv (.venv/bin/python)")

    key = SigningKey.generate()
    private_hex = key.encode().hex()
    public_hex = key.verify_key.encode().hex()

    # os.open with mode 0o600 creates the file with no group/other access
    # regardless of the process umask.
    fd = os.open(str(OUT), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(private_hex + "\n")
    except BaseException:
        os.close(fd)
        raise
    os.chmod(str(OUT), 0o600)

    print("Public key (hex):")
    print(public_hex)
    print()
    print("Set this in the server's environment:")
    print(f"export AC_OPERATOR_PUBKEY={public_hex}")
    print()
    print(
        f"Private key written to {OUT} (mode 600). "
        "Keep it secret — the server needs ONLY the public key via AC_OPERATOR_PUBKEY."
    )


if __name__ == "__main__":
    main()
