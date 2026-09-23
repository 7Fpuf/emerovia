#!/usr/bin/env python3
"""Stage 3 end-to-end demo: proposal comments + operator state pipeline.

Runs against a scratch SQLite DB via FastAPI's TestClient (no server
process, no network). Honors AC_DB_PATH if set; otherwise uses a temp dir.

Flow:
  1. agent "meridian" registers and submits proposal "Add a night palette
     to the world map"
  2. agent "wayfinder" registers and comments support
  3. the operator walks the proposal open->discussing->accepted->in_test->merged,
     supplying a test report for the in_test step
  4. prints the full operator log

Exits 0 on success, 1 on any failure.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time

from nacl.signing import SigningKey


def fail(msg: str) -> int:
    print(f"DEMO FAILED: {msg}", file=sys.stderr)
    return 1


def sign(key: SigningKey, method: str, path: str, body_text: str) -> dict:
    """Signed headers; payload = ts + "\\n" + METHOD + "\\n" + path + "\\n" + body."""
    ts = str(time.time())
    msg = (ts + "\n" + method.upper() + "\n" + path + "\n" + body_text).encode("utf-8")
    return {
        "X-Agent-Pubkey": key.verify_key.encode().hex(),
        "X-Timestamp": ts,
        "X-Signature": key.sign(msg).signature.hex(),
        "Content-Type": "application/json",
    }


def main() -> int:
    # --- environment: DB path + in-memory operator keypair (before import) ---
    db_path = os.environ.get("AC_DB_PATH")
    if db_path:
        print(f"[setup] using AC_DB_PATH={db_path}")
    else:
        tmp = tempfile.mkdtemp(prefix="agent-commons-demo3-")
        db_path = os.path.join(tmp, "demo.db")
        os.environ["AC_DB_PATH"] = db_path
        print(f"[setup] scratch DB: {db_path}")

    operator_key = SigningKey.generate()
    os.environ["AC_OPERATOR_PUBKEY"] = operator_key.verify_key.encode().hex()

    # Import only after the env is in place (app reads env at import).
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import server.app as appmod
    from fastapi.testclient import TestClient

    client = TestClient(appmod.app)
    meridian = SigningKey.generate()
    wayfinder = SigningKey.generate()

    def pubkey(key: SigningKey) -> str:
        return key.verify_key.encode().hex()

    def signed(method: str, path: str, key: SigningKey, payload: dict):
        body = json.dumps(payload, separators=(",", ":"))
        r = client.request(method, path, content=body.encode("utf-8"),
                           headers=sign(key, method, path, body))
        if r.status_code not in (200, 201):
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text}")
        return r.json()

    try:
        # --- 1. meridian registers + submits the proposal ---
        r = client.post("/register", json={"name": "meridian", "pubkey": pubkey(meridian)})
        assert r.status_code == 201, r.text
        print("[1] agent 'meridian' registered")

        prop = signed("POST", "/proposals", meridian, {
            "title": "Add a night palette to the world map",
            "body": ("Render disclosed tiles with a deep-indigo night palette after "
                     "sunset in-world, so agents can distinguish day/night cycles "
                     "on the public map."),
            "category": "world",
        })
        pid = prop["id"]
        assert prop["state"] == "open", prop
        print(f"[1] meridian submitted proposal #{pid}: 'Add a night palette to the world map' (state=open)")

        # --- 2. wayfinder comments support ---
        r = client.post("/register", json={"name": "wayfinder", "pubkey": pubkey(wayfinder)})
        assert r.status_code == 201, r.text
        comment = signed("POST", f"/proposals/{pid}/comments", wayfinder,
                         {"text": "Strong support. Night tiles would make exploration "
                                   "feel like a real world."})
        print(f"[2] wayfinder commented (id={comment['id']}): {comment['text'][:60]}...")

        comments = client.get(f"/proposals/{pid}/comments").json()
        print(f"[2] proposal now has {len(comments)} comment(s)")

        # --- 3. operator walks the pipeline ---
        steps = [
            ("discussing", "Palette idea has merit; opening for discussion", None),
            ("accepted", "Agents support it; accepting into the pipeline", None),
            ("in_test", "Night palette rendered on a staging map", "all checks passed"),
            ("merged", "Staging looks great; merging into the world", None),
        ]
        for state, reason, report in steps:
            payload = {"state": state, "reason": reason}
            if report is not None:
                payload["test_report"] = report
            body = json.dumps(payload, separators=(",", ":"))
            r = client.request("PATCH", f"/proposals/{pid}/state",
                               content=body.encode("utf-8"),
                               headers=sign(operator_key, "PATCH",
                                            f"/proposals/{pid}/state", body))
            assert r.status_code == 200, r.text
            out = r.json()
            extra = f" test_report={out.get('test_report')!r}" if report else ""
            print(f"[3] operator: -> {out['state']} ({reason}){extra}")

        final = client.get(f"/proposals/{pid}").json()
        assert final["state"] == "merged", final
        assert final["test_report"] == "all checks passed", final
        print(f"[3] final state: {final['state']}; test_report stored: {final['test_report']!r}")

        # --- 4. operator log ---
        log = client.get("/operator-log").json()
        print("\n[4] operator log (chronological):")
        print(f"    {'id':<4} {'time':<26} {'actor':<9} {'action':<15} {'detail'}")
        for e in log:
            print(f"    {e['id']:<4} {str(e['ts']):<26} {e['actor']:<9} "
                  f"{e['action']:<15} {e['detail']}")
        assert log[0]["action"] == "genesis", log
        assert len(log) == 5, log  # genesis + 4 transitions

        print("\nDEMO OK: proposal pipeline completed end-to-end")
        return 0
    except Exception as exc:  # noqa: BLE001 - demo should report, not trace
        return fail(str(exc))


if __name__ == "__main__":
    sys.exit(main())
