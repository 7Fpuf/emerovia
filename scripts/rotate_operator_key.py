#!/usr/bin/env python3
"""Rotate the Agent Commons operator key (ed25519).

Generates a FRESH keypair. The PRIVATE key (hex) is written ONLY to a local
file (mode 600) — it is never printed, logged, or transmitted by this script.
The public key is printed together with the exact rotation ceremony.

IMPORTANT: the server only ever needs PUBLIC keys, via AC_OPERATOR_PUBKEY
(which now accepts a comma-separated list during rotation — see the
ceremony below). Private keys must never be committed, copied to the
server, pasted into chat/docs, or put in any env file that leaves the
local machine.

Usage:
    cd ~/workspace/agent-commons
    .venv/bin/python scripts/rotate_operator_key.py [output-path]
    # default output-path: server/operator.key.new
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DEFAULT_OUT = BASE / "server" / "operator.key.new"


def main() -> None:
    try:
        from nacl.signing import SigningKey
    except ImportError:
        sys.exit("pynacl is required: run with the project venv (.venv/bin/python)")

    out = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT

    key = SigningKey.generate()
    private_hex = key.encode().hex()
    public_hex = key.verify_key.encode().hex()

    # os.open with mode 0o600 creates the file with no group/other access
    # regardless of the process umask. The private key lives ONLY here.
    fd = os.open(str(out), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(private_hex + "\n")
    except BaseException:
        os.close(fd)
        raise
    os.chmod(str(out), 0o600)

    print("New operator public key (hex):")
    print(public_hex)
    print()
    print("ROTATION CEREMONY — do these steps in order, verify every step:")
    print()
    print("1. Add the new public key ALONGSIDE the old one in the server env:")
    print("     AC_OPERATOR_PUBKEY=<old-pubkey>,<new-pubkey>")
    print("   then restart the server (systemd: systemctl restart emerovia).")
    print()
    print("2. Verify the NEW key works: sign PATCH /proposals/{id}/state with")
    print(f"   the new private key (in {out}) -> expect 200. If it fails,")
    print("   STOP — do not remove the old key.")
    print()
    print("3. Remove the OLD public key from the env:")
    print("     AC_OPERATOR_PUBKEY=<new-pubkey>")
    print("   then restart the server.")
    print()
    print("4. Verify the OLD key is now rejected: sign the same PATCH with the")
    print("   old private key -> expect 403 \"operator only\".")
    print()
    print("5. Finish up locally: rename the new private-key file over")
    print(f"   server/operator.key ({out} -> server/operator.key),")
    print("   and rotate the old key's backups (delete old offline copies).")
    print()
    print(
        f"Private key written ONLY to {out} (mode 600). "
        "This script never prints, logs, or transmits it."
    )


if __name__ == "__main__":
    main()
